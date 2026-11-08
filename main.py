import os
import asyncio
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import LabeledPrice, PreCheckoutQuery
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
                
