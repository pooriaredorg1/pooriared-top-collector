import requests
import base64
import json
import re
import os
import subprocess
import time
import random
import logging
from urllib.parse import urlparse, unquote, quote, parse_qs, urlunparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore

# --- تنظیمات اصلی (بهینه شده برای گرفتن تمام کانفیگ‌های سالم) ---
SUBSCRIPTION_URLS = [
    "https://raw.githubusercontent.com/pooriaredorg1/pooria/refs/heads/main/configs/proxy_configs.txt#POORIA-mixpro%20pooriaredorg1",
    "https://raw.githubusercontent.com/V2RAYCONFIGSPOOL/V2RAY_SUB/refs/heads/main/v2ray_configs.txt"
]
# ۱. استفاده از یک سرور تست جایگزین و قابل اعتماد
PING_TEST_URL = "http://cp.cloudflare.com/"
# ۲. افزایش چشمگیر زمان تایم‌اوت برای کانفیگ‌های کندتر
REQUEST_TIMEOUT = 120
# ۳. محدودیت کانفیگ نهایی (در صورت نیاز می‌توانید این را هم افزایش دهید)
MAX_FINAL_CONFIGS = 500
# ۴. کاهش تعداد تست‌های همزمان برای افزایش دقت
MAX_WORKERS = 75
# تگ‌های شما
TAG_PREFIX = "POORIARED"
SUBSCRIPTION_TAG = "POORIARED-topmix"
# تنظیمات فنی
BASE_SOCKS_PORT = 10800
XRAY_EXECUTABLE_PATH = './xray'
OUTPUT_FILE_NORMAL = "sub.txt"
OUTPUT_FILE_BASE64 = "sub_base64.txt"
OUTPUT_FINAL_LINK = "final_sub_link.txt"

# --- تنظیمات لاگ و متغیرهای گلوبال ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
ip_location_cache = {}
geoip_api_semaphore = Semaphore(5)

def get_geolocation(ip_address):
    """کشور مربوط به IP را با استفاده از API و کش دریافت می‌کند."""
    if ip_address in ip_location_cache:
        return ip_location_cache[ip_address]
    
    with geoip_api_semaphore:
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(f"http://ip-api.com/json/{ip_address}?fields=countryCode", timeout=10, headers=headers)
            if response.status_code == 200:
                country_code = response.json().get("countryCode", "N/A")
                ip_location_cache[ip_address] = country_code
                return country_code
        except requests.RequestException as e:
            logging.warning(f"Could not get geolocation for {ip_address}: {e}")
            return "N/A"
    return "N/A"

def decode_base64_content(content):
    """محتوای Base64 را با مدیریت خطا دیکود می‌کند."""
    try:
        padding = '=' * (4 - len(content) % 4)
        return base64.b64decode(content + padding).decode('utf-8')
    except Exception:
        return None

def fetch_subscription_content(url):
    """محتوای لینک اشتراک را دانلود و دیکود می‌کند."""
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        content = response.text
        decoded_content = decode_base64_content(content)
        return (decoded_content or content).splitlines()
    except requests.RequestException as e:
        logging.error(f"Failed to fetch {url.split('#')[0]}: {e}")
        return []

def generate_xray_config(proxy_config, local_port):
    """یک فایل کانفیگ موقت برای Xray بر اساس نوع پروتکل ایجاد می‌کند."""
    try:
        protocol = proxy_config.split("://")[0]
        xray_config = {
            "inbounds": [{"port": local_port, "listen": "127.0.0.1", "protocol": "socks", "settings": {"auth": "noauth", "udp": True}}],
            "outbounds": []
        }
        if protocol == "vmess":
            decoded_str = decode_base64_content(proxy_config.replace("vmess://", ""))
            if not decoded_str: return None
            vmess_data = json.loads(decoded_str)
            outbound = {
                "protocol": "vmess", "settings": {"vnext": [{"address": vmess_data.get("add"), "port": int(vmess_data.get("port", 443)), "users": [{"id": vmess_data.get("id"), "alterId": int(vmess_data.get("aid", 0))}]}]},
                "streamSettings": {"network": vmess_data.get("net", "tcp"), "security": vmess_data.get("tls", ""), "wsSettings": {"path": vmess_data.get("path")} if vmess_data.get("net") == "ws" else None, "tcpSettings": {"header": {"type": vmess_data.get("type")}} if vmess_data.get("net") == "tcp" else None}
            }
            xray_config["outbounds"].append(outbound)
        elif protocol in ["vless", "trojan"]:
            parsed_url = urlparse(proxy_config)
            params = parse_qs(parsed_url.query)
            outbound = {
                "protocol": protocol, "settings": {"vnext": [{"address": parsed_url.hostname, "port": parsed_url.port or 443, "users": [{"id": parsed_url.username}] if protocol == "vless" else [{"password": parsed_url.username}]}]},
                "streamSettings": {"network": params.get("type", ["tcp"])[0], "security": params.get("security", ["none"])[0], "tlsSettings": {"serverName": params.get("sni", [parsed_url.hostname])[0]} if params.get("security", ["none"])[0] == "tls" else None, "wsSettings": {"path": params.get("path", ["/"])[0]} if params.get("type", ["tcp"])[0] == "ws" else None}
            }
            if protocol == "vless":
                outbound["settings"]["vnext"][0]["users"][0]["flow"] = params.get("flow", [""])[0]
            xray_config["outbounds"].append(outbound)
        else: return None
        return xray_config
    except Exception as e:
        logging.debug(f"Could not generate config for {proxy_config[:30]}... : {e}")
        return None

def test_config_with_xray(config, worker_id):
    """یک کانفیگ را با استفاده از یک نمونه Xray واقعی تست می‌کند."""
    local_port = BASE_SOCKS_PORT + worker_id
    config_file_path = f"temp_config_{worker_id}.json"
    xray_proc = None
    try:
        xray_json_config = generate_xray_config(config, local_port)
        if not xray_json_config:
            return None
        with open(config_file_path, 'w') as f:
            json.dump(xray_json_config, f)
            
        xray_proc = subprocess.Popen([XRAY_EXECUTABLE_PATH, '-c', config_file_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        
        proxies = {'http': f'socks5://127.0.0.1:{local_port}', 'https': f'socks5://127.0.0.1:{local_port}'}
        start_time = time.time()
        response = requests.get(PING_TEST_URL, proxies=proxies, timeout=REQUEST_TIMEOUT)
        
        if response.status_code in [200, 204]:
            latency = int((time.time() - start_time) * 1000)
            server_ip = urlparse(config).hostname
            location = get_geolocation(server_ip)
            logging.info(f"✅ SUCCESS: {config[:40]}... | Ping: {latency}ms | Location: {location}")
            return {"config": config, "latency": latency, "location": location}
    except Exception as e:
        # لاگ کردن خطاها به جای دیباگ برای اینکه در خروجی اکشن دیده شوند
        logging.info(f"❌ FAILED: {config[:40]}... | Error: {type(e).__name__}")
        return None
    finally:
        if xray_proc:
            xray_proc.kill()
        if os.path.exists(config_file_path):
            os.remove(config_file_path)

def rename_config(config, new_name):
    """نام (fragment/#) کانفیگ را با نام جدید جایگزین می‌کند."""
    try:
        if config.startswith("vmess://"):
            decoded_str = decode_base64_content(config.replace("vmess://", ""))
            if not decoded_str: return config
            vmess_data = json.loads(decoded_str)
            vmess_data['ps'] = new_name
            encoded_str = base64.b64encode(json.dumps(vmess_data, separators=(',', ':')).encode()).decode()
            return f"vmess://{encoded_str}"
        else:
            base_url = config.split("#")[0]
            return f"{base_url}#{quote(new_name)}"
    except Exception:
        return f"{config.split('#')[0]}#{quote(new_name)}"

def main():
    logging.info("🚀 Starting collector...")
    all_configs = set()
    for url in SUBSCRIPTION_URLS:
        configs = fetch_subscription_content(url)
        logging.info(f"📥 Found {len(configs)} configs from {url.split('#')[0]}")
        for config in configs:
            if config.strip() and any(proto in config for proto in ["vless://", "vmess://", "trojan://"]):
                all_configs.add(config.strip())

    logging.info(f"\n🔬 Found {len(all_configs)} unique configs. Starting real test with Xray...")
    working_configs = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_config = {executor.submit(test_config_with_xray, config, i): config for i, config in enumerate(list(all_configs))}
        for future in as_completed(future_to_config):
            result = future.result()
            if result:
                working_configs.append(result)

    logging.info(f"\n🏁 Test finished. Found {len(working_configs)} working configs.")
    if not working_configs:
        logging.warning("No working configs found. Exiting without creating files.")
        return

    working_configs.sort(key=lambda x: x['latency'])
    
    if len(working_configs) > MAX_FINAL_CONFIGS:
        logging.info(f"Limiting final configs to the top {MAX_FINAL_CONFIGS}.")
        working_configs = working_configs[:MAX_FINAL_CONFIGS]

    final_configs_list = []
    for i, item in enumerate(working_configs):
        new_name = f"{TAG_PREFIX}-{i+1:03} | {item['location']} | {item['latency']}ms"
        renamed_config = rename_config(item['config'], new_name)
        final_configs_list.append(renamed_config)

    final_sub_content = "\n".join(final_configs_list)
    final_sub_base64 = base64.b64encode(final_sub_content.encode()).decode()

    with open(OUTPUT_FILE_NORMAL, "w") as f:
        f.write(final_sub_content)
    with open(OUTPUT_FILE_BASE64, "w") as f:
        f.write(final_sub_base64)
        
    logging.info("\n" + "="*40)
    logging.info("✅ Base subscription files created successfully.")
    logging.info(f"📄 Saved {len(final_configs_list)} configs to {OUTPUT_FILE_NORMAL} and {OUTPUT_FILE_BASE64}")
    
    repo_name = os.getenv("GITHUB_REPOSITORY")
    if repo_name:
        final_link = f"https://raw.githubusercontent.com/{repo_name}/main/{OUTPUT_FILE_BASE64}#{SUBSCRIPTION_TAG}"
        with open(OUTPUT_FINAL_LINK, "w") as f:
            f.write(final_link)
        logging.info(f"🔗 Final subscription link saved to {OUTPUT_FINAL_LINK}")
    else:
        logging.warning("Could not determine repository name. Skipping final link generation.")
    
    logging.info("="*40)

if __name__ == "__main__":
    if not os.path.exists(XRAY_EXECUTABLE_PATH) and not os.path.exists(XRAY_EXECUTABLE_PATH + '.exe'):
        logging.error("❌ Xray executable not found! This should be handled by the GitHub Action.")
    else:
        main()
