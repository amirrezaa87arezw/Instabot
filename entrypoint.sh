#!/bin/sh
set -e

# مدیریت پروکسی VLESS (اجرا/توقف/تست xray) حالا کاملاً داخل خود bot.py
# انجام می‌شه (از طریق پنل مدیریت تلگرام یا متغیر PROXY_VLESS_URL در Railway).
exec python3 /app/bot.py
