import os
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
import yt_dlp

# Вставь свой токен вместо текста внутри кавычек
TOKEN = "9239415942:AAGVmZsTkZeNR8dZeqysBn3aB1r-OCoNrrw"
bot = Bot(token=TOKEN)
dp = Dispatcher()

def download_video(url: str) -> str:
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': 'downloaded_video.%(ext)s',
        'overwrites': True,
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer("Привет! Отправь мне ссылку на TikTok, Instagram Reels или YouTube Shorts, и я скачаю видео.")

@dp.message(F.text)
async def process_video_link(message: types.Message):
    url = message.text.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer("Пожалуйста, отправь корректную ссылку.")
        return

    msg = await message.answer("Скачиваю видео, подожди немного...")
    
    try:
        loop = asyncio.get_event_loop()
        file_path = await loop.run_in_executor(None, download_video, url)

        await msg.edit_text("Загружаю в Telegram...")
        video_file = types.FSInputFile(file_path)
        await message.answer_video(video=video_file)
        await msg.delete()

        if os.path.exists(file_path):
            os.remove(file_path)

    except Exception as e:
        await msg.edit_text("Не удалось скачать видео. Проверь ссылку или попробуй позже.")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
