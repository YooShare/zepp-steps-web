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

# ⚠️【已填充】静态代理列表
# 格式: "http://IP:端口"
PROXY_LIST = [
    "http://111.48.191.1:7890",
    "http://101.251.204.174:8080",
    "http://47.111.175.196:7897",
    "http://13.230.49.39:8080",
    "http://59.46.216.131:30001",
    "http://112.111.13.253:7890",
    "http://43.217.141.124:8008",
    "http://56.68.77.224:9067",
    "http://56.68.77.224:9772",
    "http://56.68.77.224:40974",
    "http://56.68.77.224:44799",
    "http://56.68.77.224:42939",
    "http://56.68.77.224:19328",
    "http://56.68.77.224:53261",
    "http://56.68.77.224:3125",
    "http://56.68.77.224:12994",
    "http://13.212.222.137:8787",
    "http://13.212.222.137:31281",
    "http://120.92.108.86:7890",
    "http://13.212.222.137:11571",
    "http://13.212.222.137:969",
    "http://13.212.222.137:28024",
    "http://13.212.222.137:476",
    "http://13.212.222.137:11169",
    "http://13.212.222.137:29503",
    "http://61.49.87.3:80",
    "http://56.68.77.224:29522",
    "http://13.212.14.16:9061",
    "http://13.212.14.16:1036",
    "http://56.68.77.224:12994",
    "http://13.212.222.137:44909",
    "http://13.212.222.137:57365",
    "http://13.233.195.7:7928",
    "http://13.233.195.7:562",
    "http://95.40.79.184:9940",
    "http://13.212.110.200:2450",
    "http://13.212.14.16:45684",
    "http://56.68.77.224:999",
    "http://43.208.16.199:4002",
    "http://13.212.14.16:16779",
    "http://13.212.222.137:28080",
    "http://56.68.77.224:20479",
    "http://13.212.222.137:4474",
    "http://116.171.106.15:3443",
    "http://39.98.86.246:8118",
]

HTTP_TIMEOUT = 10
APP_TOKEN_CHECK_TIMEOUT = 10

# --- 全局变量 ---
token_cache_lock = threading.Lock()
token_cache = {}

# 简单的代理轮询索引
proxy_index_lock = threading.Lock()
current_proxy_index = 0

def get_next_proxy():
    """
    轮询获取一个代理。
    如果列表为空，返回 None (直连)。
    """
    global current_proxy_index
    if not PROXY_LIST:
        return None
    
    with proxy_index_lock:
        proxy = PROXY_LIST[current_proxy_index % len(PROXY_LIST)]
        current_proxy_index += 1
        return proxy

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


def make_request(method, url, **kwargs):
    """
    统一的请求函数，支持代理自动重试和直连兜底
    """
    last_error = None
    
    # 1. 尝试使用代理列表中的代理 (最多尝试 3 个不同的代理，避免全部试完太慢)
    if PROXY_LIST:
        tried_count = 0
        max_tries = 3
        while tried_count < max_tries:
            proxy_str = get_next_proxy()
            proxies = {"http": proxy_str, "https": proxy_str} if proxy_str else None
            
            try:
                resp = requests.request(method, url, timeout=HTTP_TIMEOUT, proxies=proxies, **kwargs)
                return resp, None
            except Exception as e:
                last_error = f"Proxy {proxy_str} failed: {str(e)}"
                tried_count += 1
                continue # 换下一个代理

    # 2. 如果代理都失败了，或者没配代理，尝试直连
    try:
        resp = requests.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
        return resp, None
    except Exception as e:
        last_error = f"Direct connection failed: {str(e)}"
        return None, last_error


def login_access_token(account, password):
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

    res, err = make_request("POST", url, data=data, headers=headers)
    
    if err:
        return None, f"登录请求异常: {err}"

    if res.status_code == 200:
        try:
            data_json = res.json()
        except Exception:
            return None, "登录响应解析失败"
        if "access" in data_json:
            return data_json["access"], None
        return None, "用户名或密码不正确"
    elif res.status_code == 429:
        return None, "登录请求过于频繁(429)"
    else:
        return None, f"登录请求失败: {res.status_code}"


def grant_login_tokens(access_token, account):
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

    resp, err = make_request("POST", url, data=data, headers=headers)
    
    if err:
        return None, None, None, f"获取 login_token 异常: {err}"

    if resp.status_code == 429:
        return None, None, None, "获取 login_token 过于频繁(429)"

    try:
        resp_json = resp.json()
    except Exception:
        return None, None, None, "login_token 响应解析失败"

    try:
        token_info = resp_json["token_info"]
        login_token = token_info["login_token"]
        user_id = token_info["user_id"]
        app_token = token_info.get("app_token")
        return login_token, app_token, user_id, None
    except Exception:
        return None, None, None, f"提取 token_info 失败: {resp_json}"


def grant_app_token(login_token):
    url = (
        "https://account-cn.huami.com/v1/client/app_tokens"
        f"?app_name=com.xiaomi.hm.health"
        f"&dn=api-user.huami.com%2Capi-mifit.huami.com%2Capp-analytics.huami.com"
        f"&login_token={login_token}"
    )
    headers = {
        "User-Agent": "MiFit/5.3.0 (iPhone; iOS 14.7.1; Scale/3.00)"
    }

    resp, err = make_request("GET", url, headers=headers)
    
    if err:
        return None, f"获取 app_token 异常: {err}"

    if resp.status_code == 429:
        return None, "获取 app_token 过于频繁(429)"

    if resp.status_code != 200:
        return None, f"获取 app_token 失败: {resp.status_code}"

    try:
        data = resp.json()
    except Exception:
        return None, "app_token 响应解析失败"

    if "token_info" in data and "app_token" in data["token_info"]:
        return data["token_info"]["app_token"], None

    return None, f"无法解析 app_token: {data}"


def check_app_token(app_token):
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

    resp, err = make_request("GET", url, params=params, headers=headers)
    
    if err:
        return False, f"校验 app_token 异常: {err}"

    if resp.status_code != 200:
        return False, f"校验 app_token 失败: {resp.status_code}"

    try:
        data = resp.json()
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


def change_steps(user_id, app_token, steps):
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

    resp, err = make_request("POST", url, data=data, headers=headers)
    
    if err:
        return False, f"提交步数异常: {err}"

    try:
        res_json = resp.json()
    except Exception:
        return False, f"提交步数响应解析失败，状态码: {resp.status_code}"

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
    access_token, err = login_access_token(account, password)
    if not access_token:
        return None, f"获取 access_token 失败: {err}"

    login_token, app_token, user_id, err = grant_login_tokens(access_token, account)
    if not login_token:
        return None, f"获取 login_token 失败: {err}"

    if not app_token:
        app_token, err = grant_app_token(login_token)
        if not app_token:
            return None, f"获取 app_token 失败: {err}"

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
            new_app_token, err = grant_app_token(login_token)
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

    success, msg = change_steps(session_data["user_id"], session_data["app_token"], str(steps_int))

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

    success2, msg2 = change_steps(new_session["user_id"], new_session["app_token"], str(steps_int))
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