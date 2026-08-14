"""
ربات تلگرامی که پست/ریل ارسال‌شده به دایرکت یک اکانت اینستاگرام را
دانلود کرده و برای کاربر مرتبط در تلگرام می‌فرستد.

امکانات:
  - پنل مدیریت (فقط برای ادمین) با دکمه‌های شیشه‌ای
  - لاگین اینستاگرام کاملاً از داخل تلگرام: اگر کد دومرحله‌ای یا چالش
    امنیتی لازم باشد، ربات از ادمین در چت تلگرام می‌خواهد که کد را
    بفرستد (نیازی به ترمینال نیست)

اجرا:
  python bot.py
"""

import asyncio
import json
import logging
import os
import queue
import sys
import threading
from pathlib import Path

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
# روی ریلیوی، DATA_DIR را روی مسیر Volume ست کن (مثلاً /data) تا سشن و
# لینک‌ها بعد از هر دیپلوی جدید پاک نشوند.
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION_FILE = DATA_DIR / "ig_session.json"
LINKS_FILE = DATA_DIR / "links.json"
SEEN_FILE = DATA_DIR / "seen_messages.json"
DOWNLOAD_DIR = DATA_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
IG_USERNAME = os.getenv("INSTAGRAM_USERNAME")
IG_PASSWORD = os.getenv("INSTAGRAM_PASSWORD")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "45"))
IG_PROXY = os.getenv("IG_PROXY", "").strip()
CODE_WAIT_TIMEOUT = int(os.getenv("CODE_WAIT_TIMEOUT_SECONDS", "600"))

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip().isdigit()
}


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


# ---------------------------------------------------------------------------
# مکانیزم دریافت کد ورود (2FA / چالش امنیتی) از ادمین، از طریق تلگرام
# ---------------------------------------------------------------------------
# چون لاگین اینستاگرام در یک ترد جداگانه (asyncio.to_thread) اجرا می‌شود، برای
# رد و بدل کردن کد بین آن ترد و لوپ اصلی تلگرام از یک صف Thread-safe استفاده
# می‌کنیم.
_code_queue: "queue.Queue[str]" = queue.Queue()
_awaiting_code = {"active": False}


def request_code_from_admin(application: Application, loop: asyncio.AbstractEventLoop, prompt: str) -> str:
    """
    این تابع داخل ترد جداگانه (نه ترد اصلی asyncio) صدا زده می‌شود.
    پیام را برای ادمین می‌فرستد و تا رسیدن پاسخ او (از طریق تلگرام) بلاک می‌ماند.
    """
    if not ADMIN_IDS:
        raise RuntimeError(
            "ADMIN_TELEGRAM_IDS تنظیم نشده؛ ربات نمی‌داند کد را از چه کسی در تلگرام بخواهد."
        )

    admin_id = next(iter(ADMIN_IDS))
    _awaiting_code["active"] = True

    # چون این کد در یک ترد غیرهمزمان اجرا می‌شود، ارسال پیام را با
    # run_coroutine_threadsafe به لوپ اصلی می‌سپاریم.
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


async def handle_possible_code_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پیام‌های متنی ادمین را می‌گیرد؛ اگر منتظر کد ورود بودیم، آن را به صف می‌فرستد."""
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        return
    if not _awaiting_code["active"]:
        return

    code = (update.message.text or "").strip()
    if not code:
        return

    _awaiting_code["active"] = False
    _code_queue.put(code)
    await update.message.reply_text("کد دریافت شد ✅ در حال تلاش برای ورود به اینستاگرام...")


# ---------------------------------------------------------------------------
# اتصال به اینستاگرام
# ---------------------------------------------------------------------------
def ig_login(application: Application, loop: asyncio.AbstractEventLoop) -> IGClient:
    cl = IGClient()
    if IG_PROXY:
        cl.set_proxy(IG_PROXY)

    # هندلر سفارشی: هر وقت اینستاگرام برای چالش امنیتی کد بخواهد (پیامک/ایمیل)،
    # این تابع صدا زده می‌شود و باید کد را برگرداند.
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
            cl.get_timeline_feed()  # تست معتبر بودن سشن
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
        # تلاش برای حل خودکار چالش با استفاده از challenge_code_handler که بالاتر ست شد
        cl.challenge_resolve(cl.last_json)

    cl.dump_settings(SESSION_FILE)
    log.info("لاگین موفق به اینستاگرام و ذخیره سشن.")
    return cl


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
        if info.media_type == 1:  # عکس
            path = cl.photo_download(info.pk, folder=DOWNLOAD_DIR)
            paths.append(Path(path))
        elif info.media_type == 2:  # ویدیو / ریل
            path = cl.video_download(info.pk, folder=DOWNLOAD_DIR)
            paths.append(Path(path))
        elif info.media_type == 8:  # کاروسل
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
# دستورات عمومی تلگرام
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 👋\n"
        "برای دریافت خودکار پست/ریل‌هایی که به اکانت اینستاگرام مخصوص این ربات "
        "توی دایرکت می‌فرستی، اول باید اکانتت رو معرفی کنی:\n\n"
        "/link یوزرنیم_اینستاگرامت\n\n"
        "بعدش هر پست یا ریلی که از طریق دکمه Share برای اکانت اینستاگرام ربات "
        "بفرستی، خودکار برات اینجا میاد."
    )


async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استفاده درست: /link یوزرنیم_اینستاگرامت (بدون @)")
        return

    ig_username = context.args[0].lstrip("@").strip()
    set_link(ig_username, update.effective_chat.id)
    await update.message.reply_text(
        f"ثبت شد ✅\nاکانت اینستاگرام @{ig_username} به این چت وصل شد.\n"
        "حالا هر چی از دایرکت اون اکانت به اکانت ربات بفرستی، اینجا برات میاد."
    )


async def cmd_unlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    links = get_links()
    chat_id = update.effective_chat.id
    removed = [u for u, c in list(links.items()) if c == chat_id]
    for u in removed:
        del links[u]
    save_json(LINKS_FILE, links)
    if removed:
        await update.message.reply_text("اتصال حذف شد.")
    else:
        await update.message.reply_text("اکانتی برای این چت ثبت نشده بود.")


# ---------------------------------------------------------------------------
# پنل مدیریت (فقط ادمین)
# ---------------------------------------------------------------------------
def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 آمار ربات", callback_data="admin:stats")],
            [InlineKeyboardButton("👥 اکانت‌های متصل", callback_data="admin:links")],
            [InlineKeyboardButton("🔌 وضعیت اتصال اینستاگرام", callback_data="admin:ig_status")],
            [InlineKeyboardButton("🔁 ورود مجدد به اینستاگرام", callback_data="admin:ig_relogin")],
        ]
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.message.reply_text("⛔️ این دستور فقط برای ادمین رباته.")
        return

    await update.message.reply_text(
        "🛠 پنل مدیریت ربات\n\nیکی از گزینه‌ها رو انتخاب کن:",
        reply_markup=admin_panel_keyboard(),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    if not is_admin(user.id if user else None):
        await query.answer("⛔️ دسترسی نداری.", show_alert=True)
        return

    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "stats":
        links = get_links()
        ig_connected = context.bot_data.get("ig_client") is not None
        text = (
            "📊 آمار ربات\n\n"
            f"تعداد اکانت‌های متصل: {len(links)}\n"
            f"وضعیت اینستاگرام: {'متصل ✅' if ig_connected else 'قطع ❌'}\n"
            f"فاصله‌ی چک دایرکت: هر {POLL_INTERVAL_SECONDS} ثانیه"
        )
        await query.edit_message_text(text, reply_markup=admin_panel_keyboard())

    elif action == "links":
        links = get_links()
        if not links:
            text = "👥 هیچ اکانتی هنوز متصل نشده."
        else:
            lines = [f"• @{u} → chat_id: {c}" for u, c in links.items()]
            text = "👥 اکانت‌های متصل:\n\n" + "\n".join(lines)
        await query.edit_message_text(text, reply_markup=admin_panel_keyboard())

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
        await query.edit_message_text(text, reply_markup=admin_panel_keyboard())

    elif action == "ig_relogin":
        await query.edit_message_text("⏳ در حال تلاش برای ورود مجدد به اینستاگرام...")
        loop = asyncio.get_running_loop()
        try:
            new_cl = await asyncio.to_thread(ig_login, context.application, loop)
            context.bot_data["ig_client"] = new_cl
            await context.bot.send_message(user.id, "✅ ورود مجدد به اینستاگرام موفق بود.")
        except Exception as e:
            log.error("ورود مجدد شکست خورد: %s", e)
            await context.bot.send_message(user.id, f"❌ ورود ناموفق بود:\n{e}")


# ---------------------------------------------------------------------------
# راه‌اندازی
# ---------------------------------------------------------------------------
async def post_init(application: Application):
    loop = asyncio.get_running_loop()
    try:
        application.bot_data["ig_client"] = await asyncio.to_thread(ig_login, application, loop)
    except Exception as e:
        log.error("لاگین اولیه‌ی اینستاگرام شکست خورد: %s", e)
        if ADMIN_IDS:
            admin_id = next(iter(ADMIN_IDS))
            await application.bot.send_message(
                admin_id,
                f"⚠️ لاگین اولیه به اینستاگرام شکست خورد:\n{e}\n\n"
                "از پنل مدیریت (/admin) گزینه‌ی «ورود مجدد» رو بزن.",
            )

    application.job_queue.run_repeating(poll_instagram, interval=POLL_INTERVAL_SECONDS, first=5)
    log.info("پول کردن دایرکت اینستاگرام هر %s ثانیه فعال شد.", POLL_INTERVAL_SECONDS)


def main():
    if not TELEGRAM_TOKEN or not IG_USERNAME or not IG_PASSWORD:
        raise SystemExit(
            "لطفاً فایل .env را بر اساس .env.example پر کنید "
            "(TELEGRAM_BOT_TOKEN, INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)."
        )
    if not ADMIN_IDS:
        log.warning(
            "ADMIN_TELEGRAM_IDS تنظیم نشده! پنل مدیریت غیرفعال می‌مونه و اگه اینستاگرام "
            "کد ۲مرحله‌ای بخواد، ربات نمی‌تونه از کسی درخواستش کنه."
        )

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("unlink", cmd_unlink))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:"))
    # این هندلر باید آخر از همه ثبت بشه: فقط وقتی فعاله که منتظر کد ورود هستیم
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_possible_code_reply))

    log.info("ربات تلگرام استارت شد.")
    app.run_polling()


if __name__ == "__main__":
    main()
