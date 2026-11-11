import os
import asyncio
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yt_dlp

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramConflictError
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import LabeledPrice, PreCheckoutQuery

from supabase import create_client, Client


# ==========================================
# 1. ЛОГИРОВАНИЕ
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ==========================================
# 2. HTTP-СЕРВЕР ДЛЯ RENDER И UPTIMEROBOT
# ==========================================

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
    logger.info("HTTP server started on port %s", port)
    server.serve_forever()


threading.Thread(target=run_server, daemon=True).start()


# ==========================================
# 3. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ==========================================

TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not TOKEN:
    raise ValueError("Не найден BOT_TOKEN!")

if not SUPABASE_URL or not SUPABASE_KEY:
    logger.warning(
        "Supabase не настроен. Лимиты и Premium работать корректно не будут."
    )


# ==========================================
# 4. ИНИЦИАЛИЗАЦИЯ БОТА И БАЗЫ ДАННЫХ
# ==========================================

bot = Bot(token=TOKEN)
dp = Dispatcher()

supabase: Client = (
    create_client(SUPABASE_URL, SUPABASE_KEY)
    if SUPABASE_URL and SUPABASE_KEY
    else None
)


# ==========================================
# 5. НАСТРОЙКИ КАНАЛОВ
# ==========================================

CHANNEL_1 = "@digital_author_ai"
CHANNEL_2 = "@Russia_RB"


# ==========================================
# 6. ПРОВЕРКА ПОДПИСКИ
# ==========================================

async def check_subscriptions(user_id: int) -> bool:
    for channel in [CHANNEL_1, CHANNEL_2]:
        try:
            member = await bot.get_chat_member(
                chat_id=channel,
                user_id=user_id
            )
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except Exception:
            logger.exception("Ошибка проверки подписки: %s", channel)
            return False

    return True


def get_sub_keyboard():
    builder = InlineKeyboardBuilder()

    builder.row(
        types.InlineKeyboardButton(
            text="📢 Подписаться на digital_author_ai",
            url="https://t.me/digital_author_ai"
        )
    )
    builder.row(
        types.InlineKeyboardButton(
            text="📢 Подписаться на Russia_RB",
            url="https://t.me/Russia_RB"
        )
    )
    builder.row(
        types.InlineKeyboardButton(
            text="✅ Я подписался(-ась)",
            callback_data="check_sub"
        )
    )

    return builder.as_markup()


# ==========================================
# 7. ЛИМИТЫ И SUPABASE
# ==========================================

async def check_and_update_limit(user_id: int) -> tuple[bool, str]:
    if not supabase:
        return True, ""

    now = datetime.now(timezone.utc)

    try:
        response = (
            supabase.table("users")
            .select("*")
            .eq("user_id", user_id)
            .execute()
        )
        user_data = response.data

        if not user_data:
            supabase.table("users").insert({
                "user_id": user_id,
                "downloads_count": 0,
                "last_reset": now.isoformat(),
                "is_premium": False
            }).execute()

            is_premium = False
            downloads = 0
            last_reset = now
        else:
            user = user_data[0]
            is_premium = user.get("is_premium", False)
            downloads = user.get("downloads_count", 0)

            last_reset_str = user.get("last_reset")
            premium_until_str = user.get("premium_until")

            last_reset = (
                datetime.fromisoformat(last_reset_str.replace("Z", "+00:00"))
                if last_reset_str
                else now
            )

            if is_premium and premium_until_str:
                premium_until = datetime.fromisoformat(
                    premium_until_str.replace("Z", "+00:00")
                )
                if now > premium_until:
                    is_premium = False
                    supabase.table("users").update({
                        "is_premium": False,
                        "premium_until": None
                    }).eq("user_id", user_id).execute()

            if now - last_reset >= timedelta(hours=24):
                downloads = 0
                last_reset = now
                supabase.table("users").update({
                    "downloads_count": 0,
                    "last_reset": now.isoformat()
                }).eq("user_id", user_id).execute()

        limit = 50 if is_premium else 5

        if downloads >= limit:
            tarif_name = "🌟 Premium" if is_premium else "Free"
            return False, (
                f"❌ Лимит исчерпан!\n\n"
                f"Тариф: {tarif_name}\n"
                f"Использовано: {downloads}/{limit}\n\n"
                f"Обновление через 24 часа.\n"
                f"Оформите Premium через /buy за 100 Stars!"
            )

        new_downloads = downloads + 1
        supabase.table("users").update({
            "downloads_count": new_downloads
        }).eq("user_id", user_id).execute()

        return True, ""

    except Exception:
        logger.exception("Ошибка при проверке лимитов Supabase")
        # Сохраняем прежнее поведение: не блокируем скачивание при сбое БД.
        return True, ""


# ==========================================
# 8. КОМАНДА START
# ==========================================

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
        "(YouTube, TikTok и другие площадки), "
        "и я скачаю его для тебя.\n\n"
        "📦 **Тарифы:**\n"
        "• **Free:** 5 скачиваний в сутки\n"
        "• **Premium:** 50 скачиваний в сутки\n"
        "• **Стоимость Premium:** 100 Stars / 30 дней\n\n"
        "💳 Используй команду /buy, чтобы купить Premium.",
        parse_mode="Markdown"
    )


# ==========================================
# 9. ПРОВЕРКА ПОДПИСКИ ПО КНОПКЕ
# ==========================================

@dp.callback_query(lambda c: c.data == "check_sub")
async def process_check_sub(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    if await check_subscriptions(user_id):
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


# ==========================================
# 10. ПОКУПКА PREMIUM
# ==========================================

@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
    await message.answer_invoice(
        title="🌟 Premium подписка (30 дней)",
        description=(
            "Увеличение лимита скачиваний "
            "до 50 видео в сутки на 30 дней."
        ),
        payload="premium_30_days",
        currency="XTR",
        prices=[
            LabeledPrice(label="30 дней Premium", amount=100)
        ]
    )


# ==========================================
# 11. ПРЕДПРОВЕРКА ОПЛАТЫ
# ==========================================

@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await query.answer(ok=True)


# ==========================================
# 12. УСПЕШНАЯ ОПЛАТА
# ==========================================

@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    user_id = message.from_user.id
    now = datetime.now(timezone.utc)
    premium_until = now + timedelta(days=30)

    if supabase:
        try:
            supabase.table("users").upsert(
                {
                    "user_id": user_id,
                    "is_premium": True,
                    "premium_until": premium_until.isoformat()
                },
                on_conflict="user_id"
            ).execute()
        except Exception:
            logger.exception("Ошибка обновления Premium в Supabase")
            await message.answer(
                "⚠️ Оплата получена, но возникла ошибка "
                "активации Premium. Обратитесь в поддержку."
            )
            return

    await message.answer(
        "🎉 **Оплата прошла успешно!**\n\n"
        "Вам активирован тариф 🌟 **Premium** на 30 дней.\n\n"
        "Ваш лимит увеличен до **50 скачиваний в сутки**.\n"
        "Приятного использования!",
        parse_mode="Markdown"
    )


# ==========================================
# 13. СКАЧИВАНИЕ ВИДЕО
# ==========================================

def download_video(url: str) -> str:
    """
    Скачивает видео через yt-dlp.
    Обновлённые параметры улучшают повторные попытки и выбор формата.
    Если YouTube требует дополнительную проверку, она может потребовать
    отдельной настройки PO Token Provider или авторизации.
    """

    unique_name = uuid.uuid4().hex

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": f"video_{unique_name}.%(ext)s",
        "overwrites": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 30,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


# ==========================================
# 14. ОБРАБОТКА СООБЩЕНИЙ
# ==========================================

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

    allowed, limit_msg = await check_and_update_limit(user_id)
    if not allowed:
        await message.answer(limit_msg)
        return

    msg = await message.answer("⏳ Скачиваю видео...")
    file_path = None

    try:
        file_path = await asyncio.to_thread(download_video, url)

        if not os.path.exists(file_path):
            raise FileNotFoundError("Скачанный видеофайл не найден.")

        await msg.edit_text("📤 Загружаю видео в Telegram...")
        video_file = types.FSInputFile(file_path)
        await message.answer_video(video=video_file)
        await msg.delete()

    except Exception as e:
        logger.exception("Ошибка скачивания или отправки видео")
        error_text = str(e)

        if "Sign in to confirm" in error_text or "not a bot" in error_text:
            user_error = (
                "YouTube временно блокирует автоматическую загрузку "
                "и требует дополнительную проверку. Попробуйте позже "
                "или отправьте ссылку на другое видео."
            )
        else:
            user_error = error_text[:900]

        try:
            await msg.edit_text(
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


# ==========================================
# 15. ЗАПУСК И ВОССТАНОВЛЕНИЕ БОТА
# ==========================================

async def main():
    try:
        while True:
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

                logger.warning("Telegram polling завершился.")
                break

            except TelegramConflictError:
                bot_ready.clear()
                logger.critical(
                    "ОШИБКА 409 CONFLICT: другой экземпляр бота "
                    "уже получает обновления. Проверьте все места запуска."
                )
                raise

            except asyncio.CancelledError:
                raise

            except Exception:
                bot_ready.clear()
                logger.exception(
                    "Ошибка подключения к Telegram. Повтор через 15 секунд."
                )
                await asyncio.sleep(15)

    finally:
        bot_ready.clear()
        await bot.session.close()
        logger.info("Сессия Telegram закрыта.")


# ==========================================
# 16. ТОЧКА ВХОДА
# ==========================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную.")