import os
import asyncio
import time
import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.methods import DeleteWebhook
from dotenv import load_dotenv

load_dotenv()
bot = Bot(os.getenv("TELEGRAM_BOT_TOKEN"))
dp = Dispatcher()

WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")

def webapp_url():
    """Add cache-busting parameter so Telegram doesn't serve stale HTML."""
    return f"{WEBAPP_URL}?v={int(time.time())}"

async def track(user, text=""):
    if not WEBAPP_URL or not ADMIN_SECRET:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"{WEBAPP_URL}/api/track", json={
                "tg_id": user.id,
                "username": user.username or "",
                "first_name": user.first_name or "",
                "text": text,
                "secret": ADMIN_SECRET
            })
    except:
        pass

@dp.message(Command("start"))
async def старт(message: types.Message):
    await track(message.from_user)
    клавиатура = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🤖 Открыть AI Ассистент",
            web_app=WebAppInfo(url=webapp_url())
        )
    ]])
    await message.answer_photo(
        photo=f"{WEBAPP_URL}/logo.jpg",
        caption="👋 Привет!\n\n"
        "Я AI-ассистент с возможностями:\n\n"
        "💬 Умный чат — как ChatGPT\n"
        "🎨 Генерация изображений\n"
        "📐 Решение задач и код\n\n"
        "Нажми кнопку ниже 👇",
        reply_markup=клавиатура
    )

@dp.message(Command("admin"))
async def admin_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    клавиатура = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📊 Открыть админ-панель",
            web_app=WebAppInfo(url=webapp_url())
        )
    ]])
    await message.answer("🔐 Открой приложение и перейди во вкладку Админ", reply_markup=клавиатура)

@dp.message()
async def все_сообщения(message: types.Message):
    await track(message.from_user, message.text or "")

async def main():
    await bot(DeleteWebhook(drop_pending_updates=True))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
