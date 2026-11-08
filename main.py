import os
import asyncio
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardBuilder, LabeledPrice, PreCheckoutQuery
from supabase import create_client, Client
import yt_dlp

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# HTTP-сервер для Render
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is active")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), SimpleHandler)
    logging.info(f"HTTP server started on port {port}")
    server.serve_forever()


threading.Thread(target=run_server, daemon=True).start()

# Переменные окружения
TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not TOKEN:
    raise ValueError("Не найден BOT_TOKEN в переменных окружения!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Инициализация Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# Каналы для обязательной подписки
CHANNEL_1 = "@digital_author_ai"
CHANNEL_2 = "@Russia_RB"


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
            logging.exception(f"Ошибка проверки подписки: {channel}")
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


# Функция проверки и обновления лимитов в Supabase
async def check_and_update_limit(user_id: int) -> tuple[bool, str]:
    if not supabase:
        return True, ""

    now = datetime.now(timezone.utc)

    try:
        response = supabase.table("users").select("*").eq("user_id", user_id).execute()
        user_data = response.data

        if not user_data:
            # Создаем нового пользователя (Free)
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

            last_reset = datetime.fromisoformat(last_reset_str.replace("Z", "+00:00")) if last_reset_str else now

            # Проверяем, не истек ли премиум
            if is_premium and premium_until_str:
                premium_until = datetime.fromisoformat(premium_until_str.replace("Z", "+00:00"))
                if now > premium_until:
                    is_premium = False
                    supabase.table("users").update({
                        "is_premium": False,
                        "premium_until": None
                    }).eq("user_id", user_id).execute()

            # Сброс лимита каждые 24 часа
            if now - last_reset >= timedelta(hours=24):
                downloads = 0
                last_reset = now
                supabase.table("users").update({
                    "downloads_count": 0,
                    "last_reset": now.isoformat()
                }).eq("user_id", user_id).execute()

        # Лимиты: Free — 5, Premium — 50
        limit = 50 if is_premium else 5

        if downloads >= limit:
            tarif_name = "🌟 Premium" if is_premium else "Free"
            return False, (
                f"❌ Лимит скачиваний исчерпан!\n"
                f"Ваш тариф: {tarif_name} ({downloads}/{limit}).\n"
                f"Лимит обновится через 24 часа.\n\n"
                f"💡 Хотите увеличить до 50 скачиваний? Оформите **Premium** всего за 100 Stars с помощью команды /buy!"
            )

        # Увеличиваем счетчик
        new_downloads = downloads + 1
        supabase.table("users").update({
            "downloads_count": new_downloads
        }).eq("user_id", user_id).execute()

        return True, ""

    except Exception:
        logging.exception("Ошибка при проверке лимитов в Supabase")
        return True, ""


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id

    if not await check_subscriptions(user_id):
        await message.answer(
            "👋 Привет! Чтобы пользоваться ботом, "
            "подпишитесь на наши каналы:",
            reply_markup=get_sub_keyboard()
        )
        return

    await message.answer(
        "👋 Привет! Отправь мне ссылку на видео "
        "(YouTube, TikTok и др.), и я скачаю его для тебя.\n\n"
        "📦 **Тарифы:**\n"
        "• **Free:** 5 скачиваний в сутки\n"
        "• **Premium:** 50 скачиваний в сутки (100 Stars / 30 дней)\n\n"
        "💳 Используй команду /buy, чтобы купить Premium."
    )


@dp.callback_query(lambda c: c.data == "check_sub")
async def process_check_sub(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    if await check_subscriptions(user_id):
        await callback.message.edit_text(
            "✅ Спасибо за подписку! Теперь отправьте ссылку на видео."
        )
        await callback.answer()
    else:
        await callback.answer(
            "❌ Вы подписались еще не на все каналы!",
            show_alert=True
        )


# Команда покупки Premium подписки (100 Stars за 30 дней)
@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
    await message.answer_invoice(
        title="🌟 Premium подписка (30 дней)",
        description="Увеличение лимита скачиваний до 50 в сутки на 30 дней.",
        payload="premium_30_days",
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(label="30 дней Premium", amount=100)],
    )


# Предпроверка перед оплатой
@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await query.answer(ok=True)


# Успешная оплата (начисление Premium)
@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    user_id = message.from_user.id
    now = datetime.now(timezone.utc)
    premium_until = now + timedelta(days=30)

    if supabase:
        try:
            supabase.table("users").update({
                "is_premium": True,
                "premium_until": premium_until.isoformat()
            }).eq("user_id", user_id).execute()
        except Exception:
            logging.exception("Ошибка обновления Premium статуса в Supabase")

    await message.answer(
        "🎉 **Оплата прошла успешно!**\n\n"
        "Вам активирован тариф **🌟 Premium** на 30 дней.\n"
        "Ваш лимит увеличен до **50 скачиваний в сутки**. Приятного использования!"
    )


# Скачивание видео
def download_video(url: str) -> str:
    unique_name = uuid.uuid4().hex

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": f"video_{unique_name}.%(ext)s",
        "overwrites": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            )
        },
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
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

    # Проверка и списание лимита перед скачиванием
    allowed, limit_msg = await check_and_update_limit(user_id)
    if not allowed:
        await message.answer(limit_msg)
        return

    msg = await message.answer("⏳ Скачиваю видео...")
    file_path = None

    try:
        file_path = await asyncio.to_thread(download_video, url)

        await msg.edit_text("📤 Загружаю видео...")

        video_file = types.FSInputFile(file_path)
        await message.answer_video(video=video_file)

        await msg.delete()

    except Exception as e:
        logging.exception("Ошибка скачивания видео")

        try:
            await msg.edit_text(
                f"❌ Не удалось скачать видео: {str(e)[:3000]}"
            )
        except Exception:
            pass

    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                logging.exception("Не удалось удалить видеофайл")


# Запуск с восстановлением после неожиданных ошибок
async def main():
    while True:
        try:
            logging.info("Запуск Telegram polling...")

            await dp.start_polling(
                bot,
                close_bot_session=False
            )

            logging.warning("Polling завершился.")
            break

        except asyncio.CancelledError:
            raise

        except Exception:
            logging.exception(
                "Бот неожиданно остановился. "
                "Повторный запуск через 5 секунд..."
            )
            await asyncio.sleep(5)

    await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен.")