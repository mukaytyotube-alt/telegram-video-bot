import os
import sys
import shutil
import asyncio
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata

import yt_dlp

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramConflictError
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import LabeledPrice, PreCheckoutQuery

from supabase import create_client, Client


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def log_package_version(package_name):
    try:
        logger.info("%s version: %s", package_name, metadata.version(package_name))
    except metadata.PackageNotFoundError:
        logger.warning("%s is not installed", package_name)


logger.info("yt-dlp version: %s", yt_dlp.version.__version__)
log_package_version("curl-cffi")
log_package_version("bgutil-ytdlp-pot-provider")


# ------------------- Настройки -------------------
FREE_LIMIT = int(os.environ.get("FREE_LIMIT", "5"))
PREMIUM_LIMIT = int(os.environ.get("PREMIUM_LIMIT", "50"))

# Файл cookies в Netscape-формате (помогает против блокировок YouTube).
# На Render Secret Files лежат в /etc/secrets/ и доступны только для чтения,
# а yt-dlp пытается писать в cookie-файл, поэтому копируем его в /tmp.
_COOKIES_SOURCE = os.environ.get("COOKIES_FILE", "/etc/secrets/cookies.txt")
COOKIES_FILE = ""
if _COOKIES_SOURCE and os.path.exists(_COOKIES_SOURCE):
    try:
        shutil.copy(_COOKIES_SOURCE, "/tmp/cookies.txt")
        COOKIES_FILE = "/tmp/cookies.txt"
        logger.info("Cookies загружены из %s", _COOKIES_SOURCE)
    except Exception:
        logger.exception("Не удалось скопировать cookies")
else:
    logger.warning("Файл cookies не найден: %s", _COOKIES_SOURCE)


# JS-движок для YouTube (yt-dlp без него не может расшифровать ссылки).
# Пакет "deno" из requirements.txt кладёт бинарник рядом с python.
DENO_PATH = shutil.which("deno")
if not DENO_PATH:
    _candidate = os.path.join(os.path.dirname(sys.executable), "deno")
    if os.path.exists(_candidate):
        DENO_PATH = _candidate
if DENO_PATH:
    logger.info("JS runtime deno найден: %s", DENO_PATH)
else:
    logger.warning("JS runtime deno НЕ найден: YouTube может не работать")


bot_ready = threading.Event()

# Экономия памяти (бесплатный Render = 512 МБ):
# - одновременно качаем только одно видео;
# - ограничиваем память JS-движка Deno (0 = не ограничивать).
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(1)
DENO_MAX_HEAP_MB = int(os.environ.get("DENO_MAX_HEAP_MB", "256"))
if DENO_MAX_HEAP_MB > 0 and "DENO_V8_FLAGS" not in os.environ:
    os.environ["DENO_V8_FLAGS"] = f"--max-old-space-size={DENO_MAX_HEAP_MB}"


class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            if bot_ready.is_set():
                self.send_response(200)
                response = b"Bot is active"
            else:
                self.send_response(503)
                response = b"Bot is not ready"

            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), SimpleHandler)
    logger.info("HTTP health server started on port %s", port)
    server.serve_forever()


threading.Thread(target=run_server, daemon=True).start()


TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not TOKEN:
    raise ValueError("Не найден BOT_TOKEN!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

if SUPABASE_URL and SUPABASE_KEY:
    supabase: Client | None = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    supabase = None
    logger.warning("Supabase не настроен! Лимиты скачиваний ОТКЛЮЧЕНЫ.")


CHANNEL_1 = "@digital_author_ai"
CHANNEL_2 = "@Russia_RB"


def detect_platform(url: str) -> str:
    u = url.lower()
    if "tiktok.com" in u:
        return "TikTok 🎵"
    if "instagram.com" in u:
        return "Instagram Reels 📸"
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube ▶️"
    if "facebook.com" in u or "fb.watch" in u:
        return "Facebook"
    if "vk.com" in u or "vkvideo.ru" in u:
        return "VK"
    if "x.com" in u or "twitter.com" in u:
        return "X"
    return "сайта"


async def check_subscriptions(user_id: int) -> bool:
    for channel in (CHANNEL_1, CHANNEL_2):
        try:
            member = await bot.get_chat_member(
                chat_id=channel,
                user_id=user_id
            )
            if member.status not in ("member", "administrator", "creator"):
                return False
        except Exception:
            logger.exception("Ошибка проверки подписки: %s", channel)
            return False
    return True


def get_sub_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(
        text="📢 Подписаться на digital_author_ai",
        url="https://t.me/digital_author_ai"
    ))
    builder.row(types.InlineKeyboardButton(
        text="📢 Подписаться на Russia_RB",
        url="https://t.me/Russia_RB"
    ))
    builder.row(types.InlineKeyboardButton(
        text="✅ Я подписался(-ась)",
        callback_data="check_sub"
    ))
    return builder.as_markup()


# ------------------- Работа с Supabase -------------------

def _parse_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def db_get_user(user_id: int):
    resp = supabase.table("users").select("*").eq("user_id", user_id).execute()
    return resp.data[0] if resp.data else None


def db_create_user(user_id: int):
    now = datetime.now(timezone.utc)
    supabase.table("users").insert({
        "user_id": user_id,
        "downloads_count": 0,
        "last_reset": now.isoformat(),
        "is_premium": False,
    }).execute()


def db_reset_counter(user_id: int, now: datetime):
    supabase.table("users").update({
        "downloads_count": 0,
        "last_reset": now.isoformat(),
    }).eq("user_id", user_id).execute()


def db_deactivate_premium(user_id: int):
    supabase.table("users").update({
        "is_premium": False,
        "premium_until": None,
    }).eq("user_id", user_id).execute()


def db_increment(user_id: int):
    user = db_get_user(user_id)
    if user is None:
        return
    downloads = int(user.get("downloads_count") or 0)
    supabase.table("users").update({
        "downloads_count": downloads + 1
    }).eq("user_id", user_id).execute()


def db_activate_premium(user_id: int, until: datetime):
    supabase.table("users").upsert({
        "user_id": user_id,
        "is_premium": True,
        "premium_until": until.isoformat(),
    }, on_conflict="user_id").execute()


async def check_limit(user_id: int):
    """Возвращает (allowed, message, remaining). Ничего НЕ инкрементирует."""
    if supabase is None:
        return True, "", None

    try:
        user = await asyncio.to_thread(db_get_user, user_id)
        if user is None:
            await asyncio.to_thread(db_create_user, user_id)
            user = {
                "downloads_count": 0,
                "is_premium": False,
                "last_reset": None,
                "premium_until": None,
            }

        now = datetime.now(timezone.utc)

        is_premium = bool(user.get("is_premium"))
        premium_until = _parse_dt(user.get("premium_until"))
        if is_premium and premium_until and now > premium_until:
            await asyncio.to_thread(db_deactivate_premium, user_id)
            is_premium = False

        last_reset = _parse_dt(user.get("last_reset")) or now
        downloads = int(user.get("downloads_count") or 0)

        if now - last_reset >= timedelta(hours=24):
            downloads = 0
            await asyncio.to_thread(db_reset_counter, user_id, now)

        limit = PREMIUM_LIMIT if is_premium else FREE_LIMIT

        if downloads >= limit:
            tarif_name = "🌟 Premium" if is_premium else "Free"
            return False, (
                f"❌ Лимит исчерпан!\n\n"
                f"Тариф: {tarif_name}\n"
                f"Использовано: {downloads}/{limit}\n\n"
                f"Лимит обновится через 24 часа.\n"
                f"Оформите Premium через /buy за 100 Stars!"
            ), 0

        return True, "", limit - downloads

    except Exception:
        logger.exception("Ошибка Supabase при проверке лимита")
        # fail-closed: при ошибке БД НЕ даём безлимит, а сообщаем пользователю
        return False, (
            "⚠️ Не удалось проверить лимит скачиваний (ошибка базы данных).\n"
            "Попробуйте позже или обратитесь в поддержку."
        ), None


# ------------------- Команды -------------------

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id

    if not await check_subscriptions(user_id):
        await message.answer(
            "👋 Привет!\n\n"
            "Чтобы пользоваться ботом, подпишитесь на наши каналы:",
            reply_markup=get_sub_keyboard()
        )
        return

    await message.answer(
        "👋 Привет! Отправь мне ссылку на видео "
        "(TikTok, Instagram Reels, YouTube Shorts), "
        "и я скачаю его для тебя.\n\n"
        f"📦 **Тарифы:**\n"
        f"• **Free:** {FREE_LIMIT} скачиваний в сутки\n"
        f"• **Premium:** {PREMIUM_LIMIT} скачиваний в сутки\n"
        "• **Стоимость Premium:** 100 Stars / 30 дней\n\n"
        "💳 Используй команду /buy, чтобы купить Premium.",
        parse_mode="Markdown"
    )


@dp.callback_query(lambda c: c.data == "check_sub")
async def process_check_sub(callback: types.CallbackQuery):
    if await check_subscriptions(callback.from_user.id):
        await callback.message.edit_text(
            "✅ Спасибо за подписку!\n\n"
            "Теперь отправьте ссылку на видео."
        )
        await callback.answer()
    else:
        await callback.answer(
            "❌ Вы подписались ещё не на все каналы!",
            show_alert=True
        )


@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
    await message.answer_invoice(
        title="🌟 Premium подписка (30 дней)",
        description=(
            "Увеличение лимита скачиваний "
            f"до {PREMIUM_LIMIT} видео в сутки на 30 дней."
        ),
        payload="premium_30_days",
        currency="XTR",
        prices=[LabeledPrice(label="30 дней Premium", amount=100)]
    )


@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    user_id = message.from_user.id
    now = datetime.now(timezone.utc)
    premium_until = now + timedelta(days=30)

    if supabase:
        try:
            await asyncio.to_thread(db_activate_premium, user_id, premium_until)
        except Exception:
            logger.exception("Ошибка активации Premium")
            await message.answer(
                "⚠️ Оплата получена, но возникла ошибка "
                "активации Premium. Обратитесь в поддержку."
            )
            return

    await message.answer(
        "🎉 **Оплата прошла успешно!**\n\n"
        "Вам активирован тариф 🌟 **Premium** на 30 дней.\n\n"
        f"Ваш лимит увеличен до **{PREMIUM_LIMIT} скачиваний в сутки**.\n"
        "Приятного использования!",
        parse_mode="Markdown"
    )


# ------------------- Скачивание -------------------

def download_video(url: str) -> str:
    unique_name = uuid.uuid4().hex

    ydl_opts = {
        # лимит бота Telegram — 50 МБ, поэтому ищем файл < 45 МБ
        "format": (
            "best[ext=mp4][height<=480?][filesize_approx<45M]/"
            "best[ext=mp4][height<=480?]/"
            "best[height<=480?][filesize_approx<45M]/"
            "worst[ext=mp4]/worst"
        ),
        "outtmpl": f"video_{unique_name}.%(ext)s",
        "overwrites": True,
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "cookiefile": COOKIES_FILE or None,
    }
    if DENO_PATH:
        ydl_opts["js_runtimes"] = {"deno": {"path": DENO_PATH}}

    logger.info("Начинаем скачивание ссылки: %s", url)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise RuntimeError("yt-dlp не вернул информацию о видео")
        return ydl.prepare_filename(info)


@dp.message()
async def handle_message(message: types.Message):
    if not message.text:
        await message.answer("Пожалуйста, отправьте ссылку на видео.")
        return

    user_id = message.from_user.id

    if not await check_subscriptions(user_id):
        await message.answer(
            "⚠️ Для использования бота необходимо подписаться на каналы:",
            reply_markup=get_sub_keyboard()
        )
        return

    url = message.text.strip()

    if not url.startswith(("http://", "https://")):
        await message.answer("Пожалуйста, отправьте корректную ссылку.")
        return

    allowed, limit_msg, remaining = await check_limit(user_id)
    if not allowed:
        await message.answer(limit_msg)
        return

    platform = detect_platform(url)
    status_msg = await message.answer(f"⏳ Скачиваю видео с {platform}...")
    file_path = None

    try:
        async with DOWNLOAD_SEMAPHORE:
            file_path = await asyncio.to_thread(download_video, url)

        if not os.path.exists(file_path):
            raise FileNotFoundError("Скачанный видеофайл не найден.")

        await status_msg.edit_text("📤 Загружаю видео в Telegram...")
        await message.answer_video(video=types.FSInputFile(file_path))
        await status_msg.delete()

        # Считаем скачивание ТОЛЬКО после успешной отправки
        if supabase is not None:
            try:
                await asyncio.to_thread(db_increment, user_id)
            except Exception:
                logger.exception("Не удалось увеличить счётчик скачиваний")

        if remaining is not None:
            await message.answer(
                f"✅ Готово! Осталось скачиваний сегодня: {remaining - 1}"
            )

    except Exception as e:
        logger.exception("Ошибка скачивания или отправки видео")
        error_text = str(e)

        if "Sign in to confirm" in error_text or "not a bot" in error_text:
            user_error = (
                "YouTube заблокировал скачивание с IP сервера. "
                "Попробуйте позже или используйте TikTok/Reels."
            )
        elif "File is too large" in error_text or "Request Entity Too Large" in error_text:
            user_error = "Видео слишком большое для отправки в Telegram (лимит 50 МБ)."
        elif "Unexpected response from webpage" in error_text:
            user_error = (
                "Сайт вернул страницу проверки вместо данных видео. "
                "Возможно, ограничение IP сервера или антибот-проверка."
            )
        else:
            user_error = error_text[:900]

        try:
            await status_msg.edit_text(
                "❌ Не удалось скачать или отправить видео.\n\n"
                f"Причина: {user_error}"
            )
        except Exception:
            pass

    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                logger.exception("Не удалось удалить видеофайл")


async def main():
    try:
        logger.info("Проверка подключения к Telegram...")
        me = await bot.get_me()
        logger.info("Бот подключён: @%s", me.username)

        bot_ready.set()
        logger.info("Запуск Telegram polling...")

        await dp.start_polling(
            bot,
            close_bot_session=False
        )

    except TelegramConflictError:
        bot_ready.clear()
        logger.critical(
            "Telegram 409 CONFLICT: другой экземпляр этого же бота "
            "уже получает updates. Оставьте polling только в одном сервисе Render."
        )
        raise

    finally:
        bot_ready.clear()
        await bot.session.close()
        logger.info("Сессия Telegram закрыта.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную.")