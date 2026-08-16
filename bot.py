"""
ربات تلگرامی که پست/ریل ارسال‌شده به دایرکت یک اکانت اینستاگرام را
دانلود کرده و برای کاربر مرتبط در تلگرام می‌فرستد.

امکانات:
  - منوی کاملاً دکمه‌ای برای کاربر عادی (بدون نیاز به تایپ کامند)
  - پنل مدیریت فول‌امکانات (فقط برای ادمین) با دکمه‌های شیشه‌ای:
    آمار، لیست کاربران، حذف اتصال، وضعیت اینستاگرام، ورود مجدد،
    پاک‌کردن سشن، توقف/شروع پول کردن، تنظیم فاصله چک، پیام همگانی
  - لاگین اینستاگرام کاملاً از داخل تلگرام: اگر کد دومرحله‌ای یا چالش
    امنیتی لازم باشد، ربات از ادمین در چت تلگرام می‌خواهد کد را بفرستد

اجرا:
  python bot.py
"""

import asyncio
import json
import logging
import os
import queue
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests
from dotenv import load_dotenv
from instagrapi import Client as IGClient
from instagrapi.exceptions import LoginRequired, TwoFactorRequired, ChallengeRequired
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("insta_tg_bot")

# ---------------------------------------------------------------------------
# تنظیمات و مسیرها
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION_FILE = DATA_DIR / "ig_session.json"
LINKS_FILE = DATA_DIR / "links.json"
SEEN_FILE = DATA_DIR / "seen_messages.json"
DOWNLOAD_DIR = DATA_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# فایلی که لینک VLESS تنظیم‌شده از پنل مدیریت توش ذخیره می‌شه (تا بعد از
# ری‌استارت هم بمونه، به شرطی که DATA_DIR روی یک Volume باشه)
VLESS_FILE = DATA_DIR / "proxy_vless_url.txt"
XRAY_CONFIG_PATH = DATA_DIR / "xray_runtime_config.json"
XRAY_BIN = "/usr/local/bin/xray"
LOCAL_PROXY_PORT = 10809

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
IG_USERNAME = os.getenv("INSTAGRAM_USERNAME")
IG_PASSWORD = os.getenv("INSTAGRAM_PASSWORD")
DEFAULT_POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "45"))
# اگه مستقیم یه پروکسی HTTP/SOCKS5 (نه VLESS) داری، می‌تونی همینجا از Railway ست کنی.
# این همیشه اولویت داره و جلوی مدیریت VLESS از پنل رو می‌گیره.
IG_PROXY = os.getenv("IG_PROXY", "").strip()
CODE_WAIT_TIMEOUT = int(os.getenv("CODE_WAIT_TIMEOUT_SECONDS", "600"))

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip().isdigit()
}

# حالت "منتظر ورودی متنی از کاربر" ->
# { chat_id: "link_username" | "broadcast" | "set_interval" | "remove_user" | "set_vless" }
pending_input: dict[int, str] = {}

_xray_process: subprocess.Popen | None = None


# ---------------------------------------------------------------------------
# ذخیره‌سازی ساده روی فایل JSON
# ---------------------------------------------------------------------------
def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_links() -> dict:
    return load_json(LINKS_FILE, {})


def set_link(ig_username: str, chat_id: int):
    links = get_links()
    links[ig_username.lower()] = chat_id
    save_json(LINKS_FILE, links)


def remove_link(ig_username: str):
    links = get_links()
    links.pop(ig_username.lower(), None)
    save_json(LINKS_FILE, links)


def get_seen() -> dict:
    return load_json(SEEN_FILE, {})


def set_seen(thread_id: str, item_id: str):
    seen = get_seen()
    seen[thread_id] = item_id
    save_json(SEEN_FILE, seen)


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


# ---------------------------------------------------------------------------
# مدیریت پروکسی VLESS از داخل خود ربات (بدون نیاز به تنظیم چیزی در Railway)
# ---------------------------------------------------------------------------
def get_saved_vless_url() -> str | None:
    if VLESS_FILE.exists():
        val = VLESS_FILE.read_text(encoding="utf-8").strip()
        return val or None
    return None


def save_vless_url(url: str):
    VLESS_FILE.write_text(url.strip(), encoding="utf-8")


def clear_vless_url():
    VLESS_FILE.unlink(missing_ok=True)


def resolve_vless_url() -> tuple[str | None, str | None]:
    """اولویت با متغیر محیطی Railway است؛ در غیر این صورت مقدار ذخیره‌شده
    از پنل مدیریت استفاده می‌شود."""
    env_val = os.getenv("PROXY_VLESS_URL", "").strip()
    if env_val:
        return env_val, "متغیر محیطی Railway"
    saved = get_saved_vless_url()
    if saved:
        return saved, "پنل مدیریت"
    return None, None


def build_xray_config(vless_url: str, port: int = LOCAL_PROXY_PORT) -> dict:
    if not vless_url or not vless_url.startswith("vless://"):
        raise ValueError("لینک باید با vless:// شروع بشه.")

    p = urlparse(vless_url)
    uuid = p.username
    host = p.hostname
    ig_port = p.port or 443
    if not uuid or not host:
        raise ValueError("لینک VLESS نامعتبره (uuid یا host توش پیدا نشد).")

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
        stream_settings["wsSettings"] = {"path": ws_path, "headers": {"Host": ws_host}}
    elif network == "grpc":
        stream_settings["grpcSettings"] = {"serviceName": ws_path.lstrip("/")}
    if security == "tls":
        stream_settings["security"] = "tls"
        stream_settings["tlsSettings"] = {"serverName": sni, "allowInsecure": False}

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"listen": "127.0.0.1", "port": port, "protocol": "http", "settings": {}}
        ],
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": host,
                            "port": ig_port,
                            "users": [{"id": uuid, "encryption": encryption}],
                        }
                    ]
                },
                "streamSettings": stream_settings,
            }
        ],
    }


def stop_xray():
    global _xray_process, IG_PROXY
    if _xray_process and _xray_process.poll() is None:
        _xray_process.terminate()
        try:
            _xray_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _xray_process.kill()
    _xray_process = None
    # اگه IG_PROXY مستقیم از env ست نشده، پاکش کن (چون دیگه xray فعال نیست)
    if not os.getenv("IG_PROXY", "").strip():
        IG_PROXY = ""


def start_xray(vless_url: str) -> tuple[bool, str]:
    """کانفیگ رو می‌سازه، xray رو اجرا می‌کنه و IG_PROXY رو ست می‌کنه.
    خروجی: (موفق بود یا نه, پیام/آدرس پروکسی)"""
    global _xray_process, IG_PROXY

    stop_xray()

    try:
        config = build_xray_config(vless_url)
    except Exception as e:
        return False, str(e)

    try:
        XRAY_CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
    except Exception as e:
        return False, f"نوشتن کانفیگ شکست خورد: {e}"

    try:
        _xray_process = subprocess.Popen(
            [XRAY_BIN, "run", "-c", str(XRAY_CONFIG_PATH)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False, "باینری xray پیدا نشد. مطمئن شو از Dockerfile دیپلوی شده."
    except Exception as e:
        return False, f"اجرای xray شکست خورد: {e}"

    time.sleep(2)
    if _xray_process.poll() is not None:
        _xray_process = None
        return False, "پردازش xray بلافاصله بعد از اجرا متوقف شد (لینک ممکنه نامعتبر باشه)."

    IG_PROXY = f"http://127.0.0.1:{LOCAL_PROXY_PORT}"
    return True, IG_PROXY


def test_proxy_connection() -> tuple[bool, str]:
    if not IG_PROXY:
        return False, "هیچ پروکسی‌ای فعال نیست."
    try:
        resp = requests.get(
            "https://www.instagram.com/",
            proxies={"http": IG_PROXY, "https": IG_PROXY},
            timeout=15,
        )
        return True, f"HTTP {resp.status_code} (زمان پاسخ: {resp.elapsed.total_seconds():.1f} ثانیه)"
    except Exception as e:
        return False, str(e)


def proxy_status_text() -> str:
    if not IG_PROXY:
        return "غیرفعال ❌"
    if os.getenv("IG_PROXY", "").strip():
        return "فعال ✅ (پروکسی مستقیم از Railway)"
    _, source = resolve_vless_url()
    return f"فعال ✅ (VLESS، منبع: {source})"


# ---------------------------------------------------------------------------
# مکانیزم دریافت کد ورود (2FA / چالش امنیتی) از ادمین، از طریق تلگرام
# ---------------------------------------------------------------------------
_code_queue: "queue.Queue[str]" = queue.Queue()
_awaiting_code = {"active": False}


def request_code_from_admin(application: Application, loop: asyncio.AbstractEventLoop, prompt: str) -> str:
    if not ADMIN_IDS:
        raise RuntimeError(
            "ADMIN_TELEGRAM_IDS تنظیم نشده؛ ربات نمی‌داند کد را از چه کسی در تلگرام بخواهد."
        )

    admin_id = next(iter(ADMIN_IDS))
    _awaiting_code["active"] = True

    fut = asyncio.run_coroutine_threadsafe(
        application.bot.send_message(chat_id=admin_id, text=prompt),
        loop,
    )
    fut.result(timeout=30)

    log.info("منتظر دریافت کد از ادمین در تلگرام هستیم...")
    try:
        code = _code_queue.get(timeout=CODE_WAIT_TIMEOUT)
    except queue.Empty:
        _awaiting_code["active"] = False
        raise TimeoutError("ادمین در بازه‌ی زمانی مشخص‌شده کدی ارسال نکرد.")

    return code.strip()


# ---------------------------------------------------------------------------
# اتصال به اینستاگرام
# ---------------------------------------------------------------------------
def ig_login(application: Application, loop: asyncio.AbstractEventLoop) -> IGClient:
    cl = IGClient()
    if IG_PROXY:
        cl.set_proxy(IG_PROXY)

    def challenge_code_handler(username, choice):
        choice_name = getattr(choice, "name", str(choice))
        prompt = (
            f"🔐 اینستاگرام برای ورود به @{username} یک کد چالش امنیتی "
            f"(روش: {choice_name}) فرستاده.\n"
            "لطفاً همون کد رو همینجا برای من بفرست:"
        )
        return request_code_from_admin(application, loop, prompt)

    cl.challenge_code_handler = challenge_code_handler

    if SESSION_FILE.exists():
        try:
            cl.load_settings(SESSION_FILE)
            cl.login(IG_USERNAME, IG_PASSWORD)
            cl.get_timeline_feed()
            log.info("با سشن ذخیره‌شده وارد اینستاگرام شدیم.")
            return cl
        except Exception as e:
            log.warning("سشن قدیمی معتبر نبود (%s)، دوباره لاگین می‌کنیم...", e)

    try:
        cl.login(IG_USERNAME, IG_PASSWORD)
    except TwoFactorRequired:
        prompt = (
            "🔑 اینستاگرام کد تایید دومرحله‌ای (اپ Authenticator یا پیامک) می‌خواد.\n"
            "لطفاً کد رو همینجا برای من بفرست:"
        )
        code = request_code_from_admin(application, loop, prompt)
        cl.login(IG_USERNAME, IG_PASSWORD, verification_code=code)
    except ChallengeRequired:
        cl.challenge_resolve(cl.last_json)

    cl.dump_settings(SESSION_FILE)
    log.info("لاگین موفق به اینستاگرام و ذخیره سشن.")
    return cl


PROXY_HINT = (
    "این خطا معمولاً یعنی IP سرور (مثلاً ریلیوی) توسط اینستاگرام مشکوک/مسدود "
    "شناخته شده، چون یک IP دیتاسنتریه نه موبایل/خانگی.\n\n"
    "راه‌حل‌های پیشنهادی:\n"
    "۱) یک پروکسی مسکونی (Residential Proxy) تهیه کن و مقدارش رو در متغیر محیطی "
    "IG_PROXY بذار (فرمت: http://user:pass@host:port) و از پنل، «ورود مجدد» رو بزن.\n"
    "۲) چند ساعت صبر کن، ممکنه محدودیت موقت باشه.\n"
    "۳) مطمئن شو یوزرنیم/پسورد درسته و با اپ موبایل چک کن اکانت قفل/چالش‌خورده نباشه."
)


# ---------------------------------------------------------------------------
# دانلود مدیای اشتراک‌گذاری‌شده در دایرکت
# ---------------------------------------------------------------------------
def download_shared_media(cl: IGClient, item) -> list[Path]:
    paths: list[Path] = []

    media = getattr(item, "media_share", None) or getattr(item, "clip", None)
    if media is None:
        return paths

    media_pk = media.pk
    try:
        info = cl.media_info(media_pk)
    except Exception as e:
        log.error("گرفتن اطلاعات مدیا شکست خورد: %s", e)
        return paths

    try:
        if info.media_type == 1:
            path = cl.photo_download(info.pk, folder=DOWNLOAD_DIR)
            paths.append(Path(path))
        elif info.media_type == 2:
            path = cl.video_download(info.pk, folder=DOWNLOAD_DIR)
            paths.append(Path(path))
        elif info.media_type == 8:
            downloaded = cl.album_download(info.pk, folder=DOWNLOAD_DIR)
            paths.extend(Path(p) for p in downloaded)
    except Exception as e:
        log.error("دانلود مدیا شکست خورد: %s", e)

    return paths


# ---------------------------------------------------------------------------
# حلقه‌ی پول کردن دایرکت اینستاگرام
# ---------------------------------------------------------------------------
async def poll_instagram(context: ContextTypes.DEFAULT_TYPE):
    cl: IGClient | None = context.bot_data.get("ig_client")
    if cl is None:
        return

    links = get_links()
    if not links:
        return

    try:
        threads = await asyncio.to_thread(cl.direct_threads, 20)
    except LoginRequired:
        log.warning("سشن اینستاگرام منقضی شده، لاگین مجدد...")
        loop = asyncio.get_running_loop()
        try:
            context.bot_data["ig_client"] = await asyncio.to_thread(ig_login, context.application, loop)
        except Exception as e:
            log.error("لاگین مجدد شکست خورد: %s", e)
        return
    except Exception as e:
        log.error("خطا در گرفتن دایرکت‌ها: %s", e)
        return

    seen = get_seen()

    for thread in threads:
        try:
            other_users = [u.username.lower() for u in thread.users]
        except Exception:
            other_users = []

        matched_user = next((u for u in other_users if u in links), None)
        if not matched_user:
            continue

        chat_id = links[matched_user]
        thread_id = str(thread.id)
        last_seen_item_id = seen.get(thread_id)

        new_items = []
        for msg_item in thread.messages:
            if msg_item.id == last_seen_item_id:
                break
            new_items.append(msg_item)

        if not new_items:
            continue

        set_seen(thread_id, thread.messages[0].id)

        for msg_item in reversed(new_items):
            paths = await asyncio.to_thread(download_shared_media, cl, msg_item)
            if not paths:
                continue

            for p in paths:
                try:
                    if p.suffix.lower() in (".mp4", ".mov"):
                        await context.bot.send_video(
                            chat_id=chat_id, video=p.open("rb"), caption="از اینستاگرام دانلود شد ✅"
                        )
                    else:
                        await context.bot.send_photo(
                            chat_id=chat_id, photo=p.open("rb"), caption="از اینستاگرام دانلود شد ✅"
                        )
                except Exception as e:
                    log.error("ارسال فایل به تلگرام شکست خورد: %s", e)
                finally:
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# کنترل جاب پول کردن (شروع/توقف/تغییر فاصله)
# ---------------------------------------------------------------------------
def start_polling_job(application: Application, interval: int):
    old_job = application.bot_data.get("poll_job")
    if old_job:
        old_job.schedule_removal()
    job = application.job_queue.run_repeating(
        poll_instagram, interval=interval, first=5, name="poll_instagram"
    )
    application.bot_data["poll_job"] = job
    application.bot_data["poll_interval"] = interval
    log.info("پول کردن دایرکت اینستاگرام هر %s ثانیه فعال شد.", interval)


def stop_polling_job(application: Application):
    job = application.bot_data.get("poll_job")
    if job:
        job.schedule_removal()
    application.bot_data["poll_job"] = None


# ---------------------------------------------------------------------------
# منوی کاربر عادی (کاملاً دکمه‌ای)
# ---------------------------------------------------------------------------
def user_menu_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    links = get_links()
    is_linked = chat_id in links.values()
    rows = []
    if is_linked:
        rows.append([InlineKeyboardButton("🔄 تغییر اکانت متصل", callback_data="user:link")])
        rows.append([InlineKeyboardButton("❌ قطع اتصال", callback_data="user:unlink")])
    else:
        rows.append([InlineKeyboardButton("🔗 اتصال اکانت اینستاگرام", callback_data="user:link")])
    rows.append([InlineKeyboardButton("ℹ️ راهنما", callback_data="user:help")])
    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "سلام! 👋\n"
        "با این ربات می‌تونی پست/ریل‌هایی که تو اینستاگرام برای اکانت مخصوص "
        "این ربات دایرکت می‌کنی رو خودکار همینجا دریافت کنی.\n\n"
        "از دکمه‌های زیر استفاده کن:",
        reply_markup=user_menu_keyboard(chat_id),
    )


async def user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat.id
    action = query.data.split(":", 1)[1]
    await query.answer()

    if action == "link":
        pending_input[chat_id] = "link_username"
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="user:cancel")]])
        await query.edit_message_text(
            "لطفاً یوزرنیم اینستاگرامت رو (بدون @) به‌صورت یک پیام معمولی بفرست:",
            reply_markup=cancel_kb,
        )

    elif action == "unlink":
        links = get_links()
        removed = [u for u, c in links.items() if c == chat_id]
        for u in removed:
            remove_link(u)
        text = "اتصال با موفقیت قطع شد ✅" if removed else "اکانتی برای این چت متصل نبود."
        await query.edit_message_text(text, reply_markup=user_menu_keyboard(chat_id))

    elif action == "help":
        await query.edit_message_text(
            "ℹ️ راهنمای استفاده:\n\n"
            "۱) دکمه‌ی «اتصال اکانت اینستاگرام» رو بزن و یوزرنیمت رو بفرست.\n"
            "۲) از همون اکانت، پست یا ریل موردنظر رو با دکمه‌ی Share برای اکانت "
            "اینستاگرام ربات بفرست.\n"
            "۳) ظرف چند ثانیه فایل خودکار همینجا برات میاد.",
            reply_markup=user_menu_keyboard(chat_id),
        )

    elif action == "cancel":
        pending_input.pop(chat_id, None)
        await query.edit_message_text("لغو شد.", reply_markup=user_menu_keyboard(chat_id))

    elif action == "menu":
        await query.edit_message_text("منو:", reply_markup=user_menu_keyboard(chat_id))


# ---------------------------------------------------------------------------
# پنل مدیریت فول‌امکانات (فقط ادمین)
# ---------------------------------------------------------------------------
def admin_panel_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    polling_on = context.bot_data.get("poll_job") is not None
    interval = context.bot_data.get("poll_interval", DEFAULT_POLL_INTERVAL)
    toggle_label = "⏸ توقف پول کردن دایرکت" if polling_on else "▶️ شروع پول کردن دایرکت"

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 آمار ربات", callback_data="admin:stats")],
            [InlineKeyboardButton("👥 لیست اکانت‌های متصل", callback_data="admin:users")],
            [InlineKeyboardButton("🗑 حذف یک اتصال", callback_data="admin:remove_user")],
            [InlineKeyboardButton("🔌 وضعیت اتصال اینستاگرام", callback_data="admin:ig_status")],
            [InlineKeyboardButton("🔁 ورود مجدد به اینستاگرام", callback_data="admin:ig_relogin")],
            [InlineKeyboardButton("🧹 پاک‌کردن سشن اینستاگرام", callback_data="admin:clear_session")],
            [InlineKeyboardButton(toggle_label, callback_data="admin:toggle_polling")],
            [InlineKeyboardButton(f"⏱ فاصله چک دایرکت (الان {interval}s)", callback_data="admin:set_interval")],
            [InlineKeyboardButton("📢 ارسال پیام همگانی", callback_data="admin:broadcast")],
            [InlineKeyboardButton(f"🌐 پروکسی: {proxy_status_text()}", callback_data="admin:proxy_info")],
            [InlineKeyboardButton("🔧 تنظیم پروکسی VLESS", callback_data="admin:set_vless")],
            [InlineKeyboardButton("🧪 تست پروکسی", callback_data="admin:test_proxy")],
            [InlineKeyboardButton("🗑 حذف پروکسی VLESS", callback_data="admin:remove_vless")],
        ]
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.message.reply_text("⛔️ این دستور فقط برای ادمین رباته.")
        return

    await update.message.reply_text(
        "🛠 پنل مدیریت ربات\n\nیکی از گزینه‌ها رو انتخاب کن:",
        reply_markup=admin_panel_keyboard(context),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat.id

    if not is_admin(user.id if user else None):
        await query.answer("⛔️ دسترسی نداری.", show_alert=True)
        return

    await query.answer()
    action = query.data.split(":", 1)[1]
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="admin:cancel")]])

    if action == "stats":
        links = get_links()
        ig_connected = context.bot_data.get("ig_client") is not None
        polling_on = context.bot_data.get("poll_job") is not None
        interval = context.bot_data.get("poll_interval", DEFAULT_POLL_INTERVAL)
        text = (
            "📊 آمار ربات\n\n"
            f"تعداد اکانت‌های متصل: {len(links)}\n"
            f"وضعیت اینستاگرام: {'متصل ✅' if ig_connected else 'قطع ❌'}\n"
            f"پول کردن دایرکت: {'فعال ✅' if polling_on else 'متوقف ⏸'}\n"
            f"فاصله‌ی چک: هر {interval} ثانیه"
        )
        await query.edit_message_text(text, reply_markup=admin_panel_keyboard(context))

    elif action == "users":
        links = get_links()
        if not links:
            text = "👥 هیچ اکانتی هنوز متصل نشده."
        else:
            lines = [f"• @{u} → chat_id: {c}" for u, c in links.items()]
            text = "👥 اکانت‌های متصل:\n\n" + "\n".join(lines)
        await query.edit_message_text(text, reply_markup=admin_panel_keyboard(context))

    elif action == "remove_user":
        pending_input[chat_id] = "remove_user"
        await query.edit_message_text(
            "یوزرنیم اینستاگرامی که می‌خوای اتصالش حذف بشه رو (بدون @) بفرست:",
            reply_markup=cancel_kb,
        )

    elif action == "ig_status":
        cl = context.bot_data.get("ig_client")
        if cl is None:
            text = "🔌 وضعیت اینستاگرام: متصل نیست ❌"
        else:
            try:
                username = await asyncio.to_thread(lambda: cl.username)
                text = f"🔌 وضعیت اینستاگرام: متصل ✅\nاکانت: @{username}"
            except Exception:
                text = "🔌 وضعیت اینستاگرام: متصل ✅ (جزئیات در دسترس نیست)"
        await query.edit_message_text(text, reply_markup=admin_panel_keyboard(context))

    elif action == "ig_relogin":
        await query.edit_message_text("⏳ در حال تلاش برای ورود مجدد به اینستاگرام...")
        loop = asyncio.get_running_loop()
        try:
            new_cl = await asyncio.to_thread(ig_login, context.application, loop)
            context.bot_data["ig_client"] = new_cl
            await context.bot.send_message(chat_id, "✅ ورود مجدد به اینستاگرام موفق بود.")
        except Exception as e:
            log.error("ورود مجدد شکست خورد: %s", e)
            await context.bot.send_message(chat_id, f"❌ ورود ناموفق بود:\n{e}\n\n{PROXY_HINT}")
        await context.bot.send_message(chat_id, "منو:", reply_markup=admin_panel_keyboard(context))

    elif action == "clear_session":
        try:
            SESSION_FILE.unlink(missing_ok=True)
            context.bot_data["ig_client"] = None
            text = "🧹 سشن پاک شد ✅ برای لاگین مجدد از «ورود مجدد به اینستاگرام» استفاده کن."
        except Exception as e:
            text = f"خطا در پاک‌کردن سشن: {e}"
        await query.edit_message_text(text, reply_markup=admin_panel_keyboard(context))

    elif action == "toggle_polling":
        if context.bot_data.get("poll_job") is not None:
            stop_polling_job(context.application)
            text = "⏸ پول کردن دایرکت متوقف شد."
        else:
            start_polling_job(context.application, context.bot_data.get("poll_interval", DEFAULT_POLL_INTERVAL))
            text = "▶️ پول کردن دایرکت دوباره شروع شد."
        await query.edit_message_text(text, reply_markup=admin_panel_keyboard(context))

    elif action == "set_interval":
        pending_input[chat_id] = "set_interval"
        await query.edit_message_text(
            "فاصله‌ی جدید چک دایرکت رو به ثانیه بفرست (حداقل ۱۰):",
            reply_markup=cancel_kb,
        )

    elif action == "broadcast":
        pending_input[chat_id] = "broadcast"
        await query.edit_message_text(
            "متنی که می‌خوای برای همه‌ی کاربرای متصل ارسال بشه رو بنویس:",
            reply_markup=cancel_kb,
        )

    elif action == "proxy_info":
        env_vless = os.getenv("PROXY_VLESS_URL", "").strip()
        env_proxy = os.getenv("IG_PROXY", "").strip()
        saved = get_saved_vless_url()
        lines = [f"🌐 وضعیت پروکسی: {proxy_status_text()}"]
        if env_proxy:
            lines.append("منبع: پروکسی مستقیم از متغیر IG_PROXY در Railway (اولویت اول).")
        elif env_vless:
            lines.append("منبع: PROXY_VLESS_URL در Railway (اولویت با اینه، نه پنل).")
        elif saved:
            lines.append("منبع: لینک VLESS ذخیره‌شده از همین پنل مدیریت.")
        else:
            lines.append("هیچ پروکسی‌ای تنظیم نشده.")
        await query.edit_message_text("\n".join(lines), reply_markup=admin_panel_keyboard(context))

    elif action == "set_vless":
        if os.getenv("IG_PROXY", "").strip():
            await query.edit_message_text(
                "⚠️ متغیر IG_PROXY در تنظیمات Railway ست شده و همیشه اولویت اول رو داره.\n"
                "برای مدیریت پروکسی از همینجا (پنل)، اول اون متغیر رو از Variables توی "
                "Railway پاک کن، بعد دوباره امتحان کن.",
                reply_markup=admin_panel_keyboard(context),
            )
            return
        if os.getenv("PROXY_VLESS_URL", "").strip():
            await query.edit_message_text(
                "⚠️ متغیر PROXY_VLESS_URL در تنظیمات Railway ست شده و اولویت رو ازش می‌گیره.\n"
                "برای مدیریت پروکسی از همینجا (پنل)، اول اون متغیر رو از Variables توی "
                "Railway پاک کن، بعد دوباره امتحان کن.",
                reply_markup=admin_panel_keyboard(context),
            )
            return
        pending_input[chat_id] = "set_vless"
        await query.edit_message_text(
            "کل لینک vless://... رو بدون هیچ تغییری، به‌صورت یک پیام همینجا بفرست:",
            reply_markup=cancel_kb,
        )

    elif action == "test_proxy":
        await query.edit_message_text("🧪 در حال تست اتصال پروکسی به اینستاگرام...")
        ok, msg = await asyncio.to_thread(test_proxy_connection)
        text = f"✅ پروکسی سالمه:\n{msg}" if ok else f"❌ تست پروکسی شکست خورد:\n{msg}"
        await context.bot.send_message(chat_id, text, reply_markup=admin_panel_keyboard(context))

    elif action == "remove_vless":
        await asyncio.to_thread(stop_xray)
        clear_vless_url()
        await query.edit_message_text(
            "🗑 پروکسی VLESS متوقف و حذف شد. از این به بعد اینستاگرام بدون پروکسی تلاش می‌کنه.",
            reply_markup=admin_panel_keyboard(context),
        )

    elif action == "cancel":
        pending_input.pop(chat_id, None)
        await query.edit_message_text("لغو شد.", reply_markup=admin_panel_keyboard(context))


# ---------------------------------------------------------------------------
# مدیریت پیام‌های متنی (کد ورود اینستاگرام + حالت‌های pending_input)
# ---------------------------------------------------------------------------
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # اولویت ۱: کد ورود اینستاگرام (فقط از ادمین اول)
    if user and is_admin(user.id) and _awaiting_code["active"]:
        _awaiting_code["active"] = False
        _code_queue.put(text)
        await update.message.reply_text("کد دریافت شد ✅ در حال تلاش برای ورود به اینستاگرام...")
        return

    pending = pending_input.pop(chat_id, None)

    if pending == "link_username":
        ig_username = text.lstrip("@").strip()
        if not ig_username:
            pending_input[chat_id] = "link_username"
            await update.message.reply_text("یوزرنیم نامعتبره. دوباره امتحان کن (بدون @):")
            return
        set_link(ig_username, chat_id)
        await update.message.reply_text(
            f"اکانت @{ig_username} با موفقیت وصل شد ✅\n"
            "حالا هر پست/ریلی که از دایرکت اون اکانت به اکانت ربات بفرستی، اینجا برات میاد.",
            reply_markup=user_menu_keyboard(chat_id),
        )
        return

    if pending == "broadcast" and is_admin(user.id if user else None):
        links = get_links()
        sent = 0
        for _, cid in links.items():
            try:
                await context.bot.send_message(cid, f"📢 پیام از ادمین:\n\n{text}")
                sent += 1
            except Exception:
                pass
        await update.message.reply_text(
            f"پیام برای {sent} کاربر ارسال شد.", reply_markup=admin_panel_keyboard(context)
        )
        return

    if pending == "set_interval" and is_admin(user.id if user else None):
        if not text.isdigit() or int(text) < 10:
            pending_input[chat_id] = "set_interval"
            await update.message.reply_text("عدد معتبر (حداقل ۱۰ ثانیه) بفرست:")
            return
        new_interval = int(text)
        start_polling_job(context.application, new_interval)
        await update.message.reply_text(
            f"فاصله‌ی چک دایرکت روی {new_interval} ثانیه تنظیم شد ✅",
            reply_markup=admin_panel_keyboard(context),
        )
        return

    if pending == "remove_user" and is_admin(user.id if user else None):
        uname = text.lstrip("@").strip().lower()
        links = get_links()
        if uname in links:
            remove_link(uname)
            await update.message.reply_text(
                f"اتصال @{uname} حذف شد ✅", reply_markup=admin_panel_keyboard(context)
            )
        else:
            await update.message.reply_text(
                "همچین اکانتی توی لیست پیدا نشد.", reply_markup=admin_panel_keyboard(context)
            )
        return

    if pending == "set_vless" and is_admin(user.id if user else None):
        url = text.strip()
        if not url.startswith("vless://"):
            pending_input[chat_id] = "set_vless"
            await update.message.reply_text(
                "این یه لینک VLESS معتبر نیست؛ باید با vless:// شروع بشه. دوباره بفرست:"
            )
            return

        await update.message.reply_text("⏳ در حال ساخت کانفیگ و راه‌اندازی پروکسی...")
        save_vless_url(url)
        ok, msg = await asyncio.to_thread(start_xray, url)
        if ok:
            await update.message.reply_text(
                f"✅ پروکسی راه‌اندازی شد.\nپروکسی محلی: {msg}\n\n"
                "پیشنهاد می‌کنم الان «🧪 تست پروکسی» رو بزنی، اگه سالم بود بعدش "
                "«🔁 ورود مجدد به اینستاگرام» رو بزن.",
                reply_markup=admin_panel_keyboard(context),
            )
        else:
            await update.message.reply_text(
                f"❌ راه‌اندازی پروکسی شکست خورد:\n{msg}", reply_markup=admin_panel_keyboard(context)
            )
        return

    # هیچ حالت خاصی فعال نبود -> منو رو نشون بده
    await update.message.reply_text("از دکمه‌های زیر استفاده کن:", reply_markup=user_menu_keyboard(chat_id))


# ---------------------------------------------------------------------------
# راه‌اندازی
# ---------------------------------------------------------------------------
async def post_init(application: Application):
    loop = asyncio.get_running_loop()

    # اگه پروکسی مستقیم (IG_PROXY) از Railway ست نشده، ببین آیا یک لینک VLESS
    # (از env یا از پنل مدیریت که قبلاً ذخیره شده) داریم که باید xray رو باهاش راه بندازیم
    if not IG_PROXY:
        vless_url, source = resolve_vless_url()
        if vless_url:
            ok, msg = await asyncio.to_thread(start_xray, vless_url)
            if ok:
                log.info("پروکسی VLESS (منبع: %s) با موفقیت فعال شد: %s", source, msg)
            else:
                log.error("راه‌اندازی پروکسی VLESS (منبع: %s) شکست خورد: %s", source, msg)
                if ADMIN_IDS:
                    admin_id = next(iter(ADMIN_IDS))
                    await application.bot.send_message(
                        admin_id,
                        f"⚠️ راه‌اندازی پروکسی VLESS (منبع: {source}) شکست خورد:\n{msg}",
                    )

    try:
        application.bot_data["ig_client"] = await asyncio.to_thread(ig_login, application, loop)
    except Exception as e:
        log.error("لاگین اولیه‌ی اینستاگرام شکست خورد: %s", e)
        application.bot_data["ig_client"] = None
        if ADMIN_IDS:
            admin_id = next(iter(ADMIN_IDS))
            await application.bot.send_message(
                admin_id,
                f"⚠️ لاگین اولیه به اینستاگرام شکست خورد.\n\nپیام خطا:\n{e}\n\n{PROXY_HINT}",
            )

    start_polling_job(application, DEFAULT_POLL_INTERVAL)


def main():
    if not TELEGRAM_TOKEN or not IG_USERNAME or not IG_PASSWORD:
        raise SystemExit(
            "متغیرهای محیطی پر نشدن. توی تنظیمات Railway (تب Variables) این‌ها رو اضافه کن: "
            "TELEGRAM_BOT_TOKEN, INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD."
        )
    if not ADMIN_IDS:
        log.warning(
            "ADMIN_TELEGRAM_IDS تنظیم نشده! پنل مدیریت غیرفعال می‌مونه و اگه اینستاگرام "
            "کد ۲مرحله‌ای بخواد، ربات نمی‌تونه از کسی درخواستش کنه."
        )

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(user_callback, pattern=r"^user:"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:"))
    # این هندلر باید آخر از همه ثبت بشه: هم کد ورود رو می‌گیره، هم حالت‌های pending_input رو
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    log.info("ربات تلگرام استارت شد.")
    app.run_polling()


if __name__ == "__main__":
    main()
