#!/bin/sh
set -e

if [ -n "$PROXY_VLESS_URL" ]; then
  echo "PROXY_VLESS_URL تنظیم شده؛ در حال ساخت کانفیگ xray..."
  python3 /app/generate_xray_config.py

  echo "در حال اجرای xray در پس‌زمینه..."
  /usr/local/bin/xray run -c /app/xray_config.json &
  XRAY_PID=$!

  sleep 2

  if ! kill -0 "$XRAY_PID" 2>/dev/null; then
    echo "⚠️  xray بالا نیومد! بدون پروکسی ادامه می‌دیم." >&2
  else
    export IG_PROXY="http://127.0.0.1:10809"
    echo "IG_PROXY روی $IG_PROXY تنظیم شد."
  fi
fi

exec python3 /app/bot.py
