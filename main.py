
import os
import asyncio
import logging
import threading
import uuid

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
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

# Telegram
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("Не найден BOT_TOKEN в переменных окружения!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

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
        "Привет! Отправь мне ссылку на видео "
        "(YouTube, TikTok и др.), и я скачаю его для тебя."
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
