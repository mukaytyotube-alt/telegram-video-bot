import os
import shutil
import asyncio
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadError

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


# ------------------- Настройки -------------------
FREE_LIMIT = int(os.environ.get("FREE_LIMIT", "5"))
PREMIUM_LIMIT = int(os.environ.get("PREMIUM_LIMIT", "50"))

# Бот Telegram не принимает файлы больше 50 МБ.
# Формат выбираем с запасом (45 МБ), а готовый файл проверяем по жёсткому лимиту.
MAX_VIDEO_BYTES = 45 * 1024 * 1024
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024

# YouTube Shorts скачиваются в отдельном боте
SHORTS_BOT_URL = "https://t.me/ShortYTDownloadBot"

# Файл cookies в Netscape-формате (нужен только если сайт просит вход).
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
    logger.info("Файл cookies не найден (%s) — работаем без него", _COOKIES_SOURCE)


bot_ready = threading.Event()


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
    if "facebook.com" in u or "fb.watch" in u:
        return "Facebook"
    if "vk.com" in u or "vkvideo.ru" in u:
        return "VK"
    if "x.com" in u or "twitter.com" in u:
        return "X"
    return "сайта"


def is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in url.lower()


def is_youtube_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return (
        host == "youtu.be"
        or host == "youtube.com"
        or host.endswith(".youtube.com")
    )


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


def get_shorts_keyboard():
    """Кнопка, которая открывает отдельного бота для YouTube Shorts."""
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(
        text="▶️ Скачать Shorts",
        url=SHORTS_BOT_URL
    ))
    return builder.as_markup()


# ------------------- Работа с Supabase -------------------

def _parse_dt(value):
    if not value:
        return None
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


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
    """Включает Premium. Если пользователя нет в users — создаёт с нормальным last_reset."""
    now = datetime.now(timezone.utc)
    user = db_get_user(user_id)
    data = {
        "is_premium": True,
        "premium_until": until.isoformat(),
    }
    if user is None:
        data.update({
            "user_id": user_id,
            "downloads_count": 0,
            "last_reset": now.isoformat(),
        })
        supabase.table("users").insert(data).execute()
    else:
        if not user.get("last_reset"):
            data["last_reset"] = now.isoformat()
        supabase.table("users").update(data).eq("user_id", user_id).execute()


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
        "(TikTok, Instagram Reels), "
        "и я скачаю его для тебя.\n\n"
        "▶️ YouTube Shorts скачиваются в отдельном боте — "
        "нажми кнопку ниже.\n\n"
        f"📦 **Тарифы:**\n"
        f"• **Free:** {FREE_LIMIT} скачиваний в сутки\n"
        f"• **Premium:** {PREMIUM_LIMIT} скачиваний в сутки\n"
        "• **Стоимость Premium:** 100 Stars / 30 дней\n\n"
        "💳 Используй команду /buy, чтобы купить Premium.",
        parse_mode="Markdown",
        reply_markup=get_shorts_keyboard()
    )


@dp.callback_query(lambda c: c.data == "check_sub")
async def process_check_sub(callback: types.CallbackQuery):
    if await check_subscriptions(callback.from_user.id):
        await callback.message.edit_text(
            "✅ Спасибо за подписку!\n\n"
            "Теперь отправьте ссылку на видео из TikTok или Instagram Reels.\n"
            "YouTube Shorts скачиваются в отдельном боте — кнопка ниже.",
            reply_markup=get_shorts_keyboard()
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
    p = message.successful_payment
    user_id = message.from_user.id

    # charge_id нужен для возврата Stars — всегда пишем в лог
    logger.info(
        "PAYMENT user=%s charge=%s stars=%s",
        user_id, p.telegram_payment_charge_id, p.total_amount
    )

    if p.invoice_payload != "premium_30_days" or p.currency != "XTR":
        logger.error("Некорректный платёж: %s", p)
        return

    if supabase:
        try:
            now = datetime.now(timezone.utc)
            existing = await asyncio.to_thread(db_get_user, user_id)
            old_until = _parse_dt(existing.get("premium_until")) if existing else None
            # продлеваем от текущего конца, если подписка ещё активна
            base = old_until if old_until and old_until > now else now
            await asyncio.to_thread(
                db_activate_premium, user_id, base + timedelta(days=30)
            )
        except Exception:
            logger.exception(
                "Ошибка активации Premium, charge=%s",
                p.telegram_payment_charge_id
            )
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

# Кодеки, которые Telegram (особенно веб-версия) проигрывает хуже, чем H.264
_EXOTIC_CODECS = (
    "h265", "hevc", "hvc1", "hev1", "bytevc1",
    "av01", "av1", "vp9", "vp09", "vp8",
)


def _is_watermarked(f: dict) -> bool:
    """Формат TikTok с водяным знаком.

    yt-dlp помечает его как "Download video, watermarked" (format_id
    download / download_addr). Именно его раньше и подхватывал запасной
    вариант "worst" — отсюда водяные знаки "не всегда".
    """
    note = str(f.get("format_note") or "").lower()
    format_id = str(f.get("format_id") or "").lower()
    url = str(f.get("url") or "").lower()
    return (
        "watermark" in note
        or format_id in ("download", "download_addr")
        or "watermark=1" in url
    )


def _is_exotic_codec(f: dict) -> bool:
    vcodec = str(f.get("vcodec") or "").lower()
    return any(codec in vcodec for codec in _EXOTIC_CODECS)


def _format_size(f: dict):
    return f.get("filesize") or f.get("filesize_approx")


def _pick_format(candidates: list):
    """Из списка (лучшие первыми) выбирает формат для Telegram."""
    if not candidates:
        return None

    # mp4 и обычный H.264 — первыми; порядок по качеству от yt-dlp сохраняется
    ranked = sorted(
        candidates,
        key=lambda f: (f.get("ext") != "mp4", _is_exotic_codec(f))
    )

    # лучший по качеству, который влезает в лимит (неизвестный размер — ок)
    for f in ranked:
        if (_format_size(f) or 0) <= MAX_VIDEO_BYTES:
            return f

    # всё тяжелее лимита — берём самый лёгкий
    return min(ranked, key=lambda f: _format_size(f) or 0)


def make_format_selector(avoid_watermark: bool, state: dict):
    """Свой выбор формата для yt-dlp.

    Для TikTok версия с водяным знаком берётся ТОЛЬКО если других нет —
    тогда state["watermarked"] = True.
    """

    def selector(ctx):
        # yt-dlp отдаёт форматы от худшего к лучшему — разворачиваем
        formats = list(ctx.get("formats") or [])[::-1]

        # как у "best": только готовые файлы сразу с видео и звуком
        formats = [
            f for f in formats
            if f.get("vcodec") != "none" and f.get("acodec") != "none"
        ]

        if avoid_watermark:
            groups = [
                ([f for f in formats if not _is_watermarked(f)], False),
                ([f for f in formats if _is_watermarked(f)], True),
            ]
        else:
            groups = [(formats, False)]

        for group, watermarked in groups:
            chosen = _pick_format(group)
            if chosen is not None:
                state["watermarked"] = watermarked
                yield chosen
                return

    return selector


def _download(url: str, fmt) -> str:
    unique_name = uuid.uuid4().hex

    ydl_opts = {
        "format": fmt,
        "outtmpl": f"video_{unique_name}.%(ext)s",
        "overwrites": True,
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
        # если по ссылке несколько видео (карусель, профиль) — берём только первое
        "playlist_items": "1",
        "merge_output_format": "mp4",
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "cookiefile": COOKIES_FILE or None,
    }

    logger.info("Начинаем скачивание ссылки: %s", url)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise RuntimeError("yt-dlp не вернул информацию о видео")

        if info.get("entries"):
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise RuntimeError("yt-dlp не нашёл видео по этой ссылке")
            info = entries[0]

        for item in info.get("requested_downloads") or []:
            path = item.get("filepath")
            if path and os.path.exists(path):
                return path

        return ydl.prepare_filename(info)


def download_video(url: str):
    """Скачивает видео. Возвращает (путь к файлу, остался ли водяной знак)."""
    state = {"watermarked": False}
    selector = make_format_selector(is_tiktok_url(url), state)

    try:
        return _download(url, selector), state["watermarked"]
    except DownloadError as e:
        if "Requested format is not available" not in str(e):
            raise
        logger.warning("Готового файла со звуком нет — пробуем собрать видео и звук")

    return _download(url, "bv*+ba/b"), False


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

    # YouTube здесь больше не скачиваем — отправляем в отдельного бота.
    # Лимит при этом не тратится.
    if is_youtube_url(url):
        await message.answer(
            "▶️ YouTube Shorts скачиваются в отдельном боте.\n\n"
            "Нажмите кнопку ниже и отправьте ссылку туда.",
            reply_markup=get_shorts_keyboard()
        )
        return

    allowed, limit_msg, remaining = await check_limit(user_id)
    if not allowed:
        await message.answer(limit_msg)
        return

    platform = detect_platform(url)
    status_msg = await message.answer(f"⏳ Скачиваю видео с {platform}...")
    file_path = None

    try:
        file_path, watermarked = await asyncio.to_thread(download_video, url)

        if not os.path.exists(file_path):
            raise FileNotFoundError("Скачанный видеофайл не найден.")

        if os.path.getsize(file_path) > TELEGRAM_UPLOAD_LIMIT:
            raise RuntimeError("File is too large")

        await status_msg.edit_text("📤 Загружаю видео в Telegram...")
        await message.answer_video(video=types.FSInputFile(file_path))
        await status_msg.delete()

        # Считаем скачивание ТОЛЬКО после успешной отправки
        if supabase is not None:
            try:
                await asyncio.to_thread(db_increment, user_id)
            except Exception:
                logger.exception("Не удалось увеличить счётчик скачиваний")

        if watermarked:
            await message.answer(
                "⚠️ TikTok отдал только версию с водяным знаком — "
                "убрать его в этот раз не получилось."
            )

        if remaining is not None:
            await message.answer(
                f"✅ Готово! Осталось скачиваний сегодня: {remaining - 1}"
            )

    except Exception as e:
        logger.exception("Ошибка скачивания или отправки видео")
        error_text = str(e)

        if "File is too large" in error_text or "Request Entity Too Large" in error_text:
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
