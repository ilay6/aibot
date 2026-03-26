import os
import asyncio
import time
import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, LabeledPrice
)
from aiogram.methods import DeleteWebhook
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "AIgptchatII_bot")
PREMIUM_STARS = int(os.getenv("PREMIUM_STARS", "100"))

bot = Bot(BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


def webapp_url():
    return f"{WEBAPP_URL}?v={int(time.time())}"


async def _post(path: str, data: dict) -> dict:
    if not WEBAPP_URL or not ADMIN_SECRET:
        return {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(f"{WEBAPP_URL}{path}", json=data)
            return r.json()
    except Exception:
        return {}


async def _get(path: str) -> dict:
    if not WEBAPP_URL or not ADMIN_SECRET:
        return {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{WEBAPP_URL}{path}", headers={"x-admin-secret": ADMIN_SECRET})
            return r.json()
    except Exception:
        return {}


async def track(user: types.User, text: str = "", role: str = "user"):
    await _post("/api/track", {
        "tg_id": user.id,
        "username": user.username or "",
        "first_name": user.first_name or "",
        "text": text,
        "secret": ADMIN_SECRET,
        "role": role
    })


@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    # Handle referral parameter
    args = message.text.split(maxsplit=1)
    ref_arg = args[1] if len(args) > 1 else ""
    if ref_arg.startswith("ref_"):
        try:
            referrer_id = int(ref_arg[4:])
            if referrer_id != message.from_user.id:
                await _post("/api/referral/add", {
                    "referrer_tg_id": referrer_id,
                    "referred_tg_id": message.from_user.id,
                    "secret": ADMIN_SECRET
                })
        except Exception:
            pass

    await track(message.from_user)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤖 Открыть AI Ассистент", web_app=WebAppInfo(url=webapp_url()))
    ]])
    try:
        await message.answer_photo(
            photo=f"{WEBAPP_URL}/preview.png",
            caption=(
                "👋 Привет!\n\n"
                "Я AI-ассистент с возможностями:\n\n"
                "💬 Умный чат — как ChatGPT\n"
                "🎨 Генерация изображений\n"
                "🔍 Веб-поиск\n"
                "📐 Решение задач и код\n\n"
                "Нажми кнопку ниже 👇"
            ),
            reply_markup=keyboard
        )
    except Exception:
        await message.answer("👋 Привет! Открой ассистента:", reply_markup=keyboard)


@dp.message(Command("ref"))
async def ref_cmd(message: types.Message):
    user = message.from_user
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user.id}"
    data = await _get(f"/api/referral/count/{user.id}")
    count = data.get("count", 0)
    bonus = count * 5
    await message.answer(
        f"👥 <b>Реферальная программа</b>\n\n"
        f"Твоя ссылка:\n<code>{ref_link}</code>\n\n"
        f"Нажми чтобы скопировать, отправь друзьям!\n\n"
        f"📊 Приглашено: <b>{count}</b> чел.\n"
        f"🎁 Бонус: <b>+{bonus}</b> сообщений/час\n\n"
        f"За каждого друга +5 сообщений в час сверх лимита!",
        parse_mode="HTML"
    )


@dp.message(Command("premium"))
async def premium_cmd(message: types.Message):
    if not bot:
        return
    # Check current status
    data = await _get(f"/api/premium/status/{message.from_user.id}")
    if data.get("is_premium"):
        until = data.get("premium_until", "")[:10]
        await message.answer(f"⭐ У тебя уже активен Premium до {until}!")
        return
    await message.answer_invoice(
        title="⭐ Premium подписка",
        description="Безлимитный AI-чат на 30 дней. Никаких ограничений на количество сообщений.",
        payload=f"premium_{message.from_user.id}",
        provider_token="",  # Empty string = Telegram Stars
        currency="XTR",
        prices=[LabeledPrice(label="Premium 30 дней", amount=PREMIUM_STARS)]
    )


@dp.pre_checkout_query()
async def pre_checkout(query: types.PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def payment_success(message: types.Message):
    user = message.from_user
    await _post("/api/premium/grant", {
        "tg_id": user.id,
        "first_name": user.first_name or "",
        "username": user.username or "",
        "text": "",
        "secret": ADMIN_SECRET
    })
    await message.answer(
        "🌟 <b>Premium активирован!</b>\n\n"
        "✅ 30 дней безлимитного чата\n"
        "✅ Все модели без ограничений\n"
        "✅ Приоритетная обработка\n\n"
        "Спасибо за поддержку! 🙏",
        parse_mode="HTML"
    )


@dp.message(Command("mystats"))
async def mystats_cmd(message: types.Message):
    data = await _get(f"/api/stats/{message.from_user.id}")
    if "error" in data:
        await message.answer("Статистика временно недоступна")
        return
    msgs = data.get("messages", 0)
    refs = data.get("referrals", 0)
    is_prem = data.get("is_premium", False)
    limit = data.get("hourly_limit", 30)
    prem_str = "✅ Активен (безлимит)" if is_prem else f"❌ Нет (лимит {limit}/час)"
    await message.answer(
        f"📊 <b>Твоя статистика</b>\n\n"
        f"💬 Сообщений: <b>{msgs}</b>\n"
        f"👥 Приглашено: <b>{refs}</b>\n"
        f"⭐ Premium: {prem_str}\n\n"
        f"Купить Premium: /premium\n"
        f"Пригласить друзей: /ref",
        parse_mode="HTML"
    )


@dp.message(Command("admin"))
async def admin_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📊 Открыть админ-панель", web_app=WebAppInfo(url=webapp_url()))
    ]])
    await message.answer("🔐 Открой приложение и перейди во вкладку Админ", reply_markup=keyboard)


@dp.message()
async def all_messages(message: types.Message):
    await track(message.from_user, message.text or "")


# For standalone polling (fallback / dev mode)
async def run_polling():
    if not bot:
        print("No BOT_TOKEN, skipping polling")
        return
    await bot(DeleteWebhook(drop_pending_updates=True))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run_polling())
