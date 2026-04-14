from flask import Flask, render_template, request, jsonify
import requests
import re
import time
import json
import os
import uuid
import threading
import random

app = Flask(__name__)

# --- 配置区域 ---
if os.path.exists("/data"):
    TOKEN_FILE = "/data/token_cache.json"
else:
    TOKEN_FILE = "token_cache.json"

# 代理 API 配置
PROXY_API_URL = "https://proxy.scdn.io/api/get_proxy.php"
PROXY_FETCH_COUNT = 10  # 每次从 API 获取多少个代理
PROXY_PROTOCOL = "http" # 推荐 http 或 https，兼容性好

MAX_USES_PER_PROXY = 3  # 每个代理最多使用3次就淘汰，防止被风控
HTTP_TIMEOUT = 15

# --- 全局变量 ---
APP_TOKEN_CHECK_TIMEOUT = 10
token_cache_lock = threading.Lock()
token_cache = {}

class ProxyManager:
    def __init__(self):
        self.proxies = []
        self.usage_count = {}
        self.lock = threading.Lock()
        self.last_fetch_time = 0
        self.fetch_interval = 60 # 至少间隔60秒才重新请求API，防止被封

    def get_proxy_dict(self, proxy_str):
        """将 'ip:port' 转换为 requests 需要的 {'http': '...', 'https': '...'} 格式"""
        if not proxy_str:
            return None
        # 确保格式为 http://ip:port
        if not proxy_str.startswith("http"):
            proxy_url = f"http://{proxy_str}"
        else:
            proxy_url = proxy_str
        return {"http": proxy_url, "https": proxy_url}

    def fetch_proxies_from_api(self):
        """从 API 获取新代理"""
        try:
            url = f"{PROXY_API_URL}?protocol={PROXY_PROTOCOL}&count={PROXY_FETCH_COUNT}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 200 and "proxies" in data["data"]:
                    new_proxies = data["data"]["proxies"]
                    with self.lock:
                        for p in new_proxies:
                            if p not in self.proxies:
                                self.proxies.append(p)
                                self.usage_count[p] = 0
                    print(f"Proxy Manager: Fetched {len(new_proxies)} new proxies. Total: {len(self.proxies)}")
                    self.last_fetch_time = time.time()
                    return True
        except Exception as e:
            print(f"Proxy Manager: Fetch failed - {e}")
        return False

    def get_next_proxy(self):
        """获取下一个可用代理，如果不够则自动补充"""
        with self.lock:
            # 1. 清理掉用废的代理
            active_proxies = [p for p in self.proxies if self.usage_count.get(p, 0) < MAX_USES_PER_PROXY]
            
            # 2. 如果活跃代理少于3个，且距离上次获取超过一定时间，则尝试补货
            if len(active_proxies) < 3 and (time.time() - self.last_fetch_time > self.fetch_interval):
                # 解锁去获取，避免死锁
                pass 
            elif not active_proxies:
                # 如果彻底没代理了，强制获取
                 if time.time() - self.last_fetch_time > 10: # 紧急情况下缩短间隔
                     self.fetch_proxies_from_api()
                     active_proxies = [p for p in self.proxies if self.usage_count.get(p, 0) < MAX_USES_PER_PROXY]

            if not active_proxies:
                # 如果还是没代理，返回 None 使用直连
                return None

            # 3. 随机选一个
            selected = random.choice(active_proxies)
            self.usage_count[selected] = self.usage_count.get(selected, 0) + 1
            return selected

    def mark_proxy_bad(self, proxy_str):
        """标记某个代理失效，立即增加其计数使其被淘汰"""
        with self.lock:
            if proxy_str in self.usage_count:
                self.usage_count[proxy_str] = MAX_USES_PER_PROXY + 1

# 初始化代理管理器
proxy_manager = ProxyManager()
# 启动时先尝试获取一次
proxy_manager.fetch_proxies_from_api()


def load_token_cache():
    global token_cache
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                token_cache = json.load(f)
        except Exception:
            token_cache = {}
    else:
        token_cache = {}


def save_token_cache():
    with token_cache_lock:
        try:
            dir_name = os.path.dirname(TOKEN_FILE)
            if dir_name and not os.path.exists(dir_name):
                os.makedirs(dir_name)
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                json.dump(token_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Save token cache failed: {e}")


def is_phone(account):
    return re.match(r"^(1)\d{10}$", account) is not None


def now_ts():
    return int(time.time())


def mask_account(account):
    if len(account) <= 7:
        return account
    return account[:3] + "****" + account[-4:]


def get_account_key(account):
    if is_phone(account):
        return f"+86{account}"
    return account


def get_client_login_headers():
    return {
        "app_name": "com.xiaomi.hm.health",
        "x-request-id": str(uuid.uuid4()),
        "accept-language": "zh-CN",
        "appname": "com.xiaomi.hm.health",
        "cv": "50818_6.14.0",
        "v": "2.0",
        "appplatform": "android_phone",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    }


def login_access_token(account, password, proxy_str=None):
    if is_phone(account):
        login_account = f"+86{account}"
    else:
        login_account = account

    headers = {
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "user-agent": "MiFit/6.12.0 (MCE16; Android 16; Density/1.5)",
        "app_name": "com.xiaomi.hm.health",
    }

    url = f"https://api-user.huami.com/registrations/{login_account}/tokens"
    data = (
        f"client_id=HuaMi&country_code=CN&json_response=true&name={login_account}"
        f"&password={password}"
        f"&redirect_uri=https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html"
        f"&state=REDIRECTION&token=access"
    )

    proxies = proxy_manager.get_proxy_dict(proxy_str) if proxy_str else None

    try:
        res = requests.post(url, data=data, headers=headers, timeout=HTTP_TIMEOUT, proxies=proxies)
    except Exception as e:
        return None, f"登录请求异常: {str(e)}", proxy_str

    if res.status_code == 200:
        try:
            data_json = res.json()
        except Exception:
            return None, "登录响应解析失败", proxy_str
        if "access" in data_json:
            return data_json["access"], None, proxy_str
        return None, "用户名或密码不正确", proxy_str
    elif res.status_code == 429:
        return None, "登录请求过于频繁(429)", proxy_str
    else:
        return None, f"登录请求失败: {res.status_code}", proxy_str


def grant_login_tokens(access_token, account, proxy_str=None):
    url = "https://account.huami.com/v2/client/login"
    headers = get_client_login_headers()

    if is_phone(account):
        data = {
            "app_name": "com.xiaomi.hm.health",
            "app_version": "6.14.0",
            "code": access_token,
            "country_code": "CN",
            "device_id": "00:00:00:00:00:00",
            "device_model": "phone",
            "grant_type": "access_token",
            "third_name": "huami_phone",
        }
    else:
        data = {
            "allow_registration": "false",
            "app_name": "com.xiaomi.hm.health",
            "app_version": "6.14.0",
            "code": access_token,
            "country_code": "CN",
            "device_id": "00:00:00:00:00:00",
            "device_model": "android_phone",
            "grant_type": "access_token",
            "source": "com.xiaomi.hm.health",
            "third_name": "huami",
        }

    proxies = proxy_manager.get_proxy_dict(proxy_str) if proxy_str else None

    try:
        resp = requests.post(url, data=data, headers=headers, timeout=HTTP_TIMEOUT, proxies=proxies)
    except Exception as e:
        return None, None, None, f"获取 login_token 异常: {str(e)}", proxy_str

    if resp.status_code == 429:
        return None, None, None, "获取 login_token 过于频繁(429)", proxy_str

    try:
        resp_json = resp.json()
    except Exception:
        return None, None, None, "login_token 响应解析失败", proxy_str

    try:
        token_info = resp_json["token_info"]
        login_token = token_info["login_token"]
        user_id = token_info["user_id"]
        app_token = token_info.get("app_token")
        return login_token, app_token, user_id, None, proxy_str
    except Exception:
        return None, None, None, f"提取 token_info 失败: {resp_json}", proxy_str


def grant_app_token(login_token, proxy_str=None):
    url = (
        "https://account-cn.huami.com/v1/client/app_tokens"
        f"?app_name=com.xiaomi.hm.health"
        f"&dn=api-user.huami.com%2Capi-mifit.huami.com%2Capp-analytics.huami.com"
        f"&login_token={login_token}"
    )
    headers = {
        "User-Agent": "MiFit/5.3.0 (iPhone; iOS 14.7.1; Scale/3.00)"
    }

    proxies = proxy_manager.get_proxy_dict(proxy_str) if proxy_str else None

    try:
        resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT, proxies=proxies)
    except Exception as e:
        return None, f"获取 app_token 异常: {str(e)}", proxy_str

    if resp.status_code == 429:
        return None, "获取 app_token 过于频繁(429)", proxy_str

    if resp.status_code != 200:
        return None, f"获取 app_token 失败: {resp.status_code}", proxy_str

    try:
        data = resp.json()
    except Exception:
        return None, "app_token 响应解析失败", proxy_str

    if "token_info" in data and "app_token" in data["token_info"]:
        return data["token_info"]["app_token"], None, proxy_str

    return None, f"无法解析 app_token: {data}", proxy_str


def check_app_token(app_token, proxy_str=None):
    url = "https://api-mifit-cn3.zepp.com/huami.health.getUserInfo.json"
    params = {
        "r": str(uuid.uuid4()),
        "userid": "1188760659",
        "appid": "428135909242707968",
        "channel": "Normal",
        "country": "CN",
        "cv": "50818_6.14.0",
        "device": "android_31",
        "device_type": "android_phone",
        "lang": "zh_CN",
        "timezone": "Asia/Shanghai",
        "v": "2.0"
    }
    headers = {
        "User-Agent": "MiFit6.14.0 (M2007J1SC; Android 12; Density/2.75)",
        "Accept-Encoding": "gzip",
        "hm-privacy-diagnostics": "false",
        "country": "CN",
        "appplatform": "android_phone",
        "hm-privacy-ceip": "true",
        "x-request-id": str(uuid.uuid4()),
        "timezone": "Asia/Shanghai",
        "channel": "Normal",
        "cv": "50818_6.14.0",
        "appname": "com.xiaomi.hm.health",
        "v": "2.0",
        "apptoken": app_token,
        "lang": "zh_CN",
        "clientid": "428135909242707968"
    }

    proxies = proxy_manager.get_proxy_dict(proxy_str) if proxy_str else None

    try:
        response = requests.get(url, params=params, headers=headers, timeout=APP_TOKEN_CHECK_TIMEOUT, proxies=proxies)
    except Exception as e:
        return False, f"校验 app_token 异常: {str(e)}"

    if response.status_code != 200:
        return False, f"校验 app_token 失败: {response.status_code}"

    try:
        data = response.json()
    except Exception:
        return False, "校验 app_token 响应解析失败"

    if data.get("message") == "success":
        return True, None

    return False, data.get("message", "app_token 无效")


def build_data_json(date_today, device_id, steps):
    return (
        "%5b%7b%22data_hr%22%3a%22" + "%5c%2fv7%2b" * 480 +
        f"%22%2c%22date%22%3a%22{date_today}%22%2c%22data%22%3a%5b%7b%22start%22%3a0%2c%22stop%22%3a1439%2c%22value%22%3a%22" +
        "A" * 5760 +
        f"%22%2c%22tz%22%3a32%2c%22did%22%3a%22{device_id}%22%2c%22src%22%3a24%7d%5d%2c%22summary%22%3a%22%7b%5c%22v%5c%22%3a6%2c%5c%22slp%5c%22%3a%7b%5c%22st%5c%22%3a0%2c%5c%22ed%5c%22%3a0%2c%5c%22dp%5c%22%3a0%2c%5c%22lt%5c%22%3a0%2c%5c%22wk%5c%22%3a0%2c%5c%22usrSt%5c%22%3a-1440%2c%5c%22usrEd%5c%22%3a-1440%2c%5c%22wc%5c%22%3a0%2c%5c%22is%5c%22%3a0%2c%5c%22lb%5c%22%3a0%2c%5c%22to%5c%22%3a0%2c%5c%22dt%5c%22%3a0%2c%5c%22rhr%5c%22%3a0%2c%5c%22ss%5c%22%3a0%7d%2c%5c%22stp%5c%22%3a%7b%5c%22ttl%5c%22%3a{steps}%2c%5c%22dis%5c%22%3a0%2c%5c%22cal%5c%22%3a0%2c%5c%22wk%5c%22%3a0%2c%5c%22rn%5c%22%3a0%2c%5c%22runDist%5c%22%3a0%2c%5c%22runCal%5c%22%3a0%2c%5c%22stage%5c%22%3a%5b%5d%7d%2c%5c%22goal%5c%22%3a0%2c%5c%22tz%5c%22%3a%5c%2228800%5c%22%7d%22%2c%22source%22%3a24%2c%22type%22%3a0%7d%5d"
    )


def change_steps(user_id, app_token, steps, proxy_str=None):
    sec_timestamp = int(time.time())
    date_today = time.strftime("%F")
    device_id = "0000000000000000"
    data_json = build_data_json(date_today, device_id, steps)

    headers = {
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "user-agent": "MiFit/6.12.0 (MCE16; Android 16; Density/1.5)",
        "app_name": "com.xiaomi.hm.health",
        "apptoken": app_token,
    }

    url = f"https://api-mifit-cn.huami.com/v1/data/band_data.json?&t={sec_timestamp}"
    data = (
        f"userid={user_id}&last_sync_data_time={sec_timestamp}"
        f"&device_type=0&last_deviceid={device_id}&data_json={data_json}"
    )

    proxies = proxy_manager.get_proxy_dict(proxy_str) if proxy_str else None

    try:
        res = requests.post(url, data=data, headers=headers, timeout=HTTP_TIMEOUT, proxies=proxies)
    except Exception as e:
        return False, f"提交步数异常: {str(e)}"

    try:
        res_json = res.json()
    except Exception:
        return False, f"提交步数响应解析失败，状态码: {res.status_code}"

    if res_json.get("message") == "success":
        return True, "success"

    return False, f"{res_json}"


def get_cached_account(account):
    key = get_account_key(account)
    return token_cache.get(key)


def set_cached_account(account, cache_data):
    key = get_account_key(account)
    with token_cache_lock:
        token_cache[key] = cache_data
    save_token_cache()


def delete_cached_account(account):
    key = get_account_key(account)
    with token_cache_lock:
        if key in token_cache:
            del token_cache[key]
    save_token_cache()


def refresh_all_tokens(account, password):
    max_retries = 3
    last_err = ""
    
    for i in range(max_retries):
        proxy_str = proxy_manager.get_next_proxy()
        
        access_token, err, used_proxy = login_access_token(account, password, proxy_str)
        if not access_token:
            if "429" in err and used_proxy:
                proxy_manager.mark_proxy_bad(used_proxy)
                continue
            last_err = err
            continue

        login_token, app_token, user_id, err, used_proxy = grant_login_tokens(access_token, account, used_proxy)
        if not login_token:
            if "429" in err and used_proxy:
                proxy_manager.mark_proxy_bad(used_proxy)
                continue
            last_err = err
            continue

        if not app_token:
            app_token, err, used_proxy = grant_app_token(login_token, used_proxy)
            if not app_token:
                if "429" in err and used_proxy:
                    proxy_manager.mark_proxy_bad(used_proxy)
                    continue
                last_err = err
                continue

        cache_data = {
            "account": get_account_key(account),
            "access_token": access_token,
            "login_token": login_token,
            "app_token": app_token,
            "user_id": user_id,
            "device_id": "00:00:00:00:00:00",
            "updated_at": now_ts()
        }
        set_cached_account(account, cache_data)
        return cache_data, None

    return None, f"多次尝试后仍失败: {last_err}"


def get_valid_app_session(account, password):
    cache_data = get_cached_account(account)

    if cache_data:
        app_token = cache_data.get("app_token")
        login_token = cache_data.get("login_token")
        user_id = cache_data.get("user_id")

        if app_token and user_id:
            ok, _ = check_app_token(app_token)
            if ok:
                return cache_data, None

        if login_token:
            proxy_str = proxy_manager.get_next_proxy()
            new_app_token, err, used_proxy = grant_app_token(login_token, proxy_str)
            if not new_app_token and used_proxy:
                 # 尝试直连兜底
                 new_app_token, err, _ = grant_app_token(login_token, None)
            
            if new_app_token:
                cache_data["app_token"] = new_app_token
                cache_data["updated_at"] = now_ts()
                set_cached_account(account, cache_data)
                return cache_data, None

    new_cache, err = refresh_all_tokens(account, password)
    if new_cache:
        return new_cache, None

    return None, err


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/update_steps", methods=["POST"])
def update_steps_api():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"status": "error", "message": "请求数据不是有效 JSON"})

    account = str(data.get("account", "")).strip()
    password = str(data.get("password", "")).strip()
    steps = str(data.get("steps", "30000")).strip()

    if not account or not password:
        return jsonify({"status": "error", "message": "账号和密码不能为空"})

    if not steps.isdigit():
        return jsonify({"status": "error", "message": "步数必须是数字"})

    steps_int = int(steps)
    if steps_int < 1000 or steps_int > 98000:
        return jsonify({"status": "error", "message": "步数建议在 1000-98000 之间"})

    session_data, err = get_valid_app_session(account, password)
    if not session_data:
        return jsonify({
            "status": "error",
            "message": f"获取有效会话失败: {err}"
        })

    # 提交步数时也尝试使用代理
    proxy_str = proxy_manager.get_next_proxy()
    success, msg = change_steps(session_data["user_id"], session_data["app_token"], str(steps_int), proxy_str)
    
    # 如果提交失败且用了代理，尝试直连重试
    if not success and proxy_str:
        success, msg = change_steps(session_data["user_id"], session_data["app_token"], str(steps_int), None)

    if success:
        session_data["updated_at"] = now_ts()
        set_cached_account(account, session_data)
        return jsonify({
            "status": "success",
            "message": f"成功，账号 {mask_account(account)} 步数已更新为 {steps_int}"
        })

    new_session, err2 = refresh_all_tokens(account, password)
    if not new_session:
        return jsonify({
            "status": "error",
            "message": f"提交失败，且刷新 token 失败: {msg} / {err2}"
        })

    proxy_str2 = proxy_manager.get_next_proxy()
    success2, msg2 = change_steps(new_session["user_id"], new_session["app_token"], str(steps_int), proxy_str2)
    if not success2 and proxy_str2:
        success2, msg2 = change_steps(new_session["user_id"], new_session["app_token"], str(steps_int), None)

    if success2:
        new_session["updated_at"] = now_ts()
        set_cached_account(account, new_session)
        return jsonify({
            "status": "success",
            "message": f"成功，账号 {mask_account(account)} 步数已更新为 {steps_int}（已自动刷新 token）"
        })

    return jsonify({
        "status": "error",
        "message": f"提交失败: {msg2}"
    })


@app.route("/api/cache_status", methods=["GET"])
def cache_status():
    result = []
    for k, v in token_cache.items():
        result.append({
            "account": mask_account(k),
            "has_access_token": bool(v.get("access_token")),
            "has_login_token": bool(v.get("login_token")),
            "has_app_token": bool(v.get("app_token")),
            "user_id": v.get("user_id"),
            "updated_at": v.get("updated_at")
        })
    return jsonify({"status": "success", "data": result})


@app.route("/api/clear_cache", methods=["POST"])
def clear_cache():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"status": "error", "message": "请求数据不是有效 JSON"})

    account = str(data.get("account", "")).strip()
    if not account:
        return jsonify({"status": "error", "message": "账号不能为空"})

    delete_cached_account(account)
    return jsonify({"status": "success", "message": f"已清除 {mask_account(account)} 的缓存"})


if __name__ == "__main__":
    load_token_cache()
    PORT = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=PORT, debug=False)