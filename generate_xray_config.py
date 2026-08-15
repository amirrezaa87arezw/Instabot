"""
این اسکریپت لینک VLESS را (از متغیر محیطی PROXY_VLESS_URL) می‌خواند و یک
فایل کانفیگ برای Xray-core می‌سازد که یک پروکسی HTTP روی 127.0.0.1:10809
باز می‌کند. ربات بعداً از همین پروکسی محلی برای اتصال به اینستاگرام استفاده
می‌کند (بدون نیاز به تغییر در کد اصلی bot.py).
"""

import json
import os
import sys
from urllib.parse import parse_qs, unquote, urlparse

CONFIG_PATH = "/app/xray_config.json"
LOCAL_PROXY_PORT = 10809


def main():
    vless_url = os.environ.get("PROXY_VLESS_URL", "").strip()
    if not vless_url:
        print("PROXY_VLESS_URL تنظیم نشده؛ از xray صرف‌نظر می‌شود.")
        sys.exit(0)

    if not vless_url.startswith("vless://"):
        print("PROXY_VLESS_URL باید با vless:// شروع بشه.", file=sys.stderr)
        sys.exit(1)

    p = urlparse(vless_url)
    uuid = p.username
    host = p.hostname
    port = p.port or 443

    if not uuid or not host:
        print("لینک VLESS نامعتبره (uuid یا host پیدا نشد).", file=sys.stderr)
        sys.exit(1)

    q = parse_qs(p.query)

    def qget(key, default=""):
        vals = q.get(key, [default])
        return vals[0] if vals and vals[0] != "" else default

    security = qget("security", "none") or "none"
    encryption = qget("encryption", "none") or "none"
    network = qget("type", "tcp") or "tcp"
    ws_path = unquote(qget("path", "/"))
    ws_host = qget("host", host)
    sni = qget("sni", ws_host)

    stream_settings = {"network": network}

    if network == "ws":
        stream_settings["wsSettings"] = {
            "path": ws_path,
            "headers": {"Host": ws_host},
        }
    elif network == "grpc":
        stream_settings["grpcSettings"] = {"serviceName": ws_path.lstrip("/")}

    if security == "tls":
        stream_settings["security"] = "tls"
        stream_settings["tlsSettings"] = {"serverName": sni, "allowInsecure": False}

    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": LOCAL_PROXY_PORT,
                "protocol": "http",
                "settings": {},
            }
        ],
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": host,
                            "port": port,
                            "users": [{"id": uuid, "encryption": encryption}],
                        }
                    ]
                },
                "streamSettings": stream_settings,
            }
        ],
    }

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    print(f"کانفیگ xray ساخته شد: {CONFIG_PATH}")
    print(f"پروکسی محلی روی http://127.0.0.1:{LOCAL_PROXY_PORT} در دسترس خواهد بود.")


if __name__ == "__main__":
    main()
