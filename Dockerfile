FROM python:3.11-slim

# نصب xray-core برای تبدیل کانفیگ VLESS به یک پروکسی HTTP محلی.
# bot.py خودش (از طریق subprocess) این باینری رو موقع نیاز اجرا/متوقف می‌کنه.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl unzip ca-certificates && \
    curl -L -o /tmp/xray.zip https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip && \
    mkdir -p /usr/local/bin/xray-core && \
    unzip -o /tmp/xray.zip -d /usr/local/bin/xray-core && \
    ln -s /usr/local/bin/xray-core/xray /usr/local/bin/xray && \
    rm /tmp/xray.zip && \
    apt-get purge -y unzip curl && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
