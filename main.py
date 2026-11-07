import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
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

# Инициализация Telegram-бота из переменных окружения Render
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Не найден BOT_TOKEN в переменных окружения Render!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привет! Отправь мне ссылку на видео, и я скачаю его для тебя.")

def download_video(url: str) -> str:
    ydl_opts = {
        'format': 'best',
        'outtmpl': 'downloaded_video.%(ext)s',
        'overwrites': True,
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return filename

@dp.message()
async def handle_message(message: types.Message):
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
