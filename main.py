import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
import yt_dlp

# Веб-сервер-заглушка для Render (чтобы проходила проверка портов)
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active")

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    server.serve_forever()

# Запускаем веб-сервер в фоновом потоке
threading.Thread(target=run_server, daemon=True).start()

# Инициализация Telegram-бота
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Не найден BOT_TOKEN в переменных окружения Render!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Ваши каналы для обязательной подписки
CHANNEL_1 = "@digital_author_ai"
CHANNEL_2 = "@Russia_RB"

async def check_subscriptions(user_id: int) -> bool:
    channels = [CHANNEL_1, CHANNEL_2]
    for channel in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except Exception:
            return False
    return True

def get_sub_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📢 Подписаться на digital_author_ai", url=f"https://t.me/{CHANNEL_1.replace('@', '')}"))
    builder.row(types.InlineKeyboardButton(text="📢 Подписаться на Russia_RB", url=f"https://t.me/{CHANNEL_2.replace('@', '')}"))
    builder.row(types.InlineKeyboardButton(text="✅ Я подписался(-ась)", callback_data="check_sub"))
    return builder.as_markup()

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    is_subbed = await check_subscriptions(user_id)
    
    if not is_subbed:
        await message.answer(
            "👋 Привет! Чтобы пользоваться ботом, пожалуйста, подпишитесь на наши каналы:",
            reply_markup=get_sub_keyboard()
        )
        return

    await message.answer("Привет! Отправь мне ссылку на видео (YouTube, TikTok и др.), и я скачаю его для тебя.")

@dp.callback_query(lambda c: c.data == "check_sub")
async def process_check_sub(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    is_subbed = await check_subscriptions(user_id)
    
    if is_subbed:
        await callback.message.edit_text("✅ Спасибо за подписку! Теперь отправьте мне ссылку на видео.")
    else:
        await callback.answer("❌ Вы подписались еще не на все каналы!", show_alert=True)

def download_video(url: str) -> str:
    ydl_opts = {
        'format': 'best',
        'outtmpl': 'downloaded_video.%(ext)s',
        'overwrites': True,
        'quiet': True,
        'no_warnings': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return filename

@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    is_subbed = await check_subscriptions(user_id)
    
    if not is_subbed:
        await message.answer(
            "⚠️ Для использования бота необходимо подписаться на каналы:",
            reply_markup=get_sub_keyboard()
        )
        return

    url = message.text.strip()
    if not url.startswith("http"):
        await message.answer("Пожалуйста, отправьте корректную ссылку на видео.")
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
        await msg.edit_text(f"Не удалось скачать видео. Ошибка: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
