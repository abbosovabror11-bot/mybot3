import asyncio
import logging
import os
import sys
import time
import urllib.parse
from io import BytesIO
from typing import List, Tuple, Dict, Any, Callable, Awaitable

import aiohttp
import aiosqlite
from PIL import Image
from flask import Flask
from threading import Thread

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, StateFilter, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    Message,
    CallbackQuery,
    WebAppInfo
)
from aiogram.utils.chat_action import ChatActionSender

# ==============================================================================
# 1. FLASK WEB SERVER (Render Web Service talabi uchun)
# ==============================================================================

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running and Mini App is active!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# ==============================================================================
# 2. KONFIGURATSIYA
# ==============================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "7924336159:AAE0qyGKxp-CWLaNSjFvsgRO5VzIDDtrA6k")
ADMINS_RAW = os.getenv("ADMINS", "8694110588")
ADMINS = [int(admin_id) for admin_id in ADMINS_RAW.split(",") if admin_id.strip().isdigit()]

WEB_APP_URL = os.getenv("WEB_APP_URL", "https://eclectic-starlight-cfbad8.netlify.app") 
DATABASE_PATH = "bot_data.db"

ADMIN_CARD_NUMBER = "9860 6067 5617 3831"
ADMIN_CARD_NAME = "ABDOSOV ABRORBEK"

NSFW_WORDS = ["xxx", "erotic", "bikini", "jalap"]

USER_COOLDOWNS = {}
COOLDOWN_TIME = 120  # 2 daqiqa

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ProductionAIBot")


# ==============================================================================
# 3. MA'LUMOTLAR BAZASI
# ==============================================================================

class Database:
    @staticmethod
    async def init_db():
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    username TEXT,
                    generations_count INTEGER DEFAULT 0,
                    balance INTEGER DEFAULT 0,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER DEFAULT 1
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    invite_link TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS star_packages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stars_amount INTEGER NOT NULL,
                    price_sum INTEGER NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS gift_packages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gift_name TEXT NOT NULL,
                    price_sum INTEGER NOT NULL
                )
            """)
            # Boshlang'ich paketlar
            await db.execute("INSERT OR IGNORE INTO star_packages (id, stars_amount, price_sum) VALUES (1, 50, 10000)")
            await db.execute("INSERT OR IGNORE INTO star_packages (id, stars_amount, price_sum) VALUES (2, 100, 20000)")
            await db.execute("INSERT OR IGNORE INTO star_packages (id, stars_amount, price_sum) VALUES (3, 150, 30000)")
            await db.execute("INSERT OR IGNORE INTO star_packages (id, stars_amount, price_sum) VALUES (4, 200, 40000)")
            
            await db.execute("INSERT OR IGNORE INTO gift_packages (id, gift_name, price_sum) VALUES (1, '🎁 Rose Gift', 15000)")
            await db.execute("INSERT OR IGNORE INTO gift_packages (id, gift_name, price_sum) VALUES (2, '💎 Diamond Gift', 45000)")
            await db.commit()

    @staticmethod
    async def get_setting(key: str) -> str | None:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    @staticmethod
    async def set_setting(key: str, value: str):
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute("""
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;
            """, (key, value))
            await db.commit()

    @staticmethod
    async def add_user(user_id: int, full_name: str, username: str):
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute("""
                INSERT INTO users (user_id, full_name, username, is_active) 
                VALUES (?, ?, ?, 1)
                ON CONFLICT(user_id) DO UPDATE SET 
                    full_name = excluded.full_name,
                    username = excluded.username,
                    is_active = 1;
            """, (user_id, full_name, username or "Mavjud emas"))
            await db.commit()

    @staticmethod
    async def increment_generation(user_id: int):
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "UPDATE users SET generations_count = generations_count + 1 WHERE user_id = ?",
                (user_id,)
            )
            await db.commit()

    @staticmethod
    async def get_top_users(limit: int = 10) -> List[Tuple[str, int]]:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute(
                "SELECT full_name, generations_count FROM users WHERE is_active = 1 ORDER BY generations_count DESC LIMIT ?",
                (limit,)
            ) as cursor:
                return await cursor.fetchall()

    @staticmethod
    async def get_user_stats(user_id: int) -> Tuple[int, int]:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute("SELECT generations_count, balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return (row[0], row[1]) if row else (0, 0)

    @staticmethod
    async def add_balance(user_id: int, amount: int):
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
            await db.commit()

    @staticmethod
    async def get_all_user_ids() -> List[int]:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute("SELECT user_id FROM users WHERE is_active = 1") as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows]

    @staticmethod
    async def get_system_stats() -> Tuple[int, int, int]:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute("SELECT COUNT(*), SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), SUM(generations_count) FROM users") as cursor:
                row = await cursor.fetchone()
                return row[0] or 0, row[1] or 0, row[2] or 0

    @staticmethod
    async def add_channel(channel_id: int, title: str, invite_link: str):
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute("""
                INSERT INTO channels (channel_id, title, invite_link)
                VALUES (?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    title = excluded.title,
                    invite_link = excluded.invite_link;
            """, (channel_id, title, invite_link))
            await db.commit()

    @staticmethod
    async def delete_channel(channel_id: int):
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
            await db.commit()

    @staticmethod
    async def get_channels() -> List[Tuple[int, str, str]]:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute("SELECT channel_id, title, invite_link FROM channels") as cursor:
                return await cursor.fetchall()

    @staticmethod
    async def get_star_packages() -> List[Tuple[int, int, int]]:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute("SELECT id, stars_amount, price_sum FROM star_packages ORDER BY stars_amount ASC") as cursor:
                return await cursor.fetchall()

    @staticmethod
    async def add_or_update_package(stars: int, price: int):
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute("""
                INSERT INTO star_packages (stars_amount, price_sum) VALUES (?, ?)
            """, (stars, price))
            await db.commit()

    @staticmethod
    async def delete_package(pkg_id: int):
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute("DELETE FROM star_packages WHERE id = ?", (pkg_id,))
            await db.commit()

    @staticmethod
    async def get_gift_packages() -> List[Tuple[int, str, int]]:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute("SELECT id, gift_name, price_sum FROM gift_packages ORDER BY id ASC") as cursor:
                return await cursor.fetchall()

    @staticmethod
    async def add_or_update_gift(name: str, price: int):
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute("""
                INSERT INTO gift_packages (gift_name, price_sum) VALUES (?, ?)
            """, (name, price))
            await db.commit()

    @staticmethod
    async def delete_gift(gift_id: int):
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute("DELETE FROM gift_packages WHERE id = ?", (gift_id,))
            await db.commit()


# ==============================================================================
# 4. KEYBOARDS
# ==============================================================================

class Keyboards:
    @staticmethod
    def get_user_main() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🎨 AI Rasm Yaratish"), KeyboardButton(text="🎬 AI Video Yaratish")],
                [KeyboardButton(text="🚀 Mini App Ochish", web_app=WebAppInfo(url=WEB_APP_URL)), KeyboardButton(text="⭐ Stars va Giftlar")],
                [KeyboardButton(text="💡 Tasodifiy G'oya"), KeyboardButton(text="🏆 TOP-10 Liderlar")],
                [KeyboardButton(text="📊 Mening Statistikam"), KeyboardButton(text="ℹ️ Bot Haqida")]
            ],
            resize_keyboard=True
        )

    @staticmethod
    def get_admin_main() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📢 Kanallarni Boshqarish"), KeyboardButton(text="⭐ Stars Paketlarini Boshqarish")],
                [KeyboardButton(text="🎁 Gift Paketlarini Boshqarish"), KeyboardButton(text="📢 To'lovlar Kanalini Sozlash")],
                [KeyboardButton(text="📈 Umumiy Statistika"), KeyboardButton(text="✉️ Reklama Tarqatish")],
                [KeyboardButton(text="📂 Bazani Yuklab Olish"), KeyboardButton(text="👤 Foydalanuvchi Rejimiga O'tish")]
            ],
            resize_keyboard=True
        )

    @staticmethod
    def get_cancel() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="❌ Bekor Qilish")]],
            resize_keyboard=True
        )

    @staticmethod
    def get_shop_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Stars Sotib Olish", callback_data="shop_stars")],
            [InlineKeyboardButton(text="🎁 Telegram Gift Sotib Olish", callback_data="shop_gifts")]
        ])


# ==============================================================================
# 5. MAJBURIY OBUNA TEKSHIRUVI
# ==============================================================================

async def check_user_subscriptions(bot: Bot, user_id: int) -> List[Tuple[str, str]]:
    channels = await Database.get_channels()
    unsubscribed = []
    for ch_id, title, link in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status in ["left", "kicked"]:
                unsubscribed.append((title, link))
        except Exception as e:
            logger.warning(f"Kanalni tekshirishda xatolik: {e}")
    return unsubscribed

class SubscriptionMiddleware:
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        bot: Bot = data['bot']
        user = event.from_user if isinstance(event, (Message, CallbackQuery)) else None

        if not user or user.id in ADMINS:
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and (event.data == "check_sub" or event.data.startswith(("shop_", "buy_", "gift_", "pkg_"))):
            return await handler(event, data)

        unsubscribed = await check_user_subscriptions(bot, user.id)
        if unsubscribed:
            keyboard = [[InlineKeyboardButton(text=f"📢 {t}", url=l)] for t, l in unsubscribed]
            keyboard.append([InlineKeyboardButton(text="✅ Obunani Tekshirish", callback_data="check_sub")])
            text = "⚠️ Botdan to'liq foydalanish uchun quyidagi kanallarga obuna bo'ling:"

            if isinstance(event, Message):
                await event.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
            elif isinstance(event, CallbackQuery):
                await event.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
                await event.answer()
            return

        return await handler(event, data)


# ==============================================================================
# 6. AI ENGINE
# ==============================================================================

class AIService:
    @staticmethod
    def is_nsfw(text: str) -> bool:
        return any(word in text.lower() for word in NSFW_WORDS)

    @staticmethod
    async def translate_to_english(text: str) -> str:
        try:
            encoded_text = urllib.parse.quote(text)
            url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=en&dt=t&q={encoded_text}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data[0][0][0]
        except Exception:
            pass
        return text

    @staticmethod
    async def generate_image(prompt: str) -> Tuple[BytesIO | None, str]:
        encoded_prompt = urllib.parse.quote(prompt)
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&seed={int(time.time())}&model=flux"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 5000:
                            return BytesIO(data), "Flux Ultra HD"
        except Exception:
            pass
        return None, "Xatolik"

    @staticmethod
    async def generate_video(prompt: str) -> Tuple[BytesIO | None, str]:
        encoded_prompt = urllib.parse.quote(prompt)
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = f"https://image.pollinations.ai/prompt/cinematic%20video%20{encoded_prompt}?width=720&height=1280&nologo=true&model=flux-realism"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 15000:
                            return BytesIO(data), "AI Video Engine"
        except Exception:
            pass
        return None, "Xatolik"

    @staticmethod
    def create_sticker(image_bytes: BytesIO) -> BytesIO:
        image_bytes.seek(0)
        img = Image.open(image_bytes).convert("RGBA")
        img.thumbnail((512, 512))
        output = BytesIO()
        img.save(output, format="WEBP", quality=95)
        output.seek(0)
        return output


# ==============================================================================
# 7. HANDLERLAR
# ==============================================================================

class AdminState(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_channel_data = State()
    waiting_for_package_data = State()
    waiting_for_gift_data = State()
    waiting_for_payment_channel = State()

class PaymentState(StatesGroup):
    waiting_for_username = State()
    waiting_for_screenshot = State()

class VideoState(StatesGroup):
    waiting_for_prompt = State()

dp = Dispatcher(storage=MemoryStorage())
sub_mw = SubscriptionMiddleware()
dp.message.middleware(sub_mw)
dp.callback_query.middleware(sub_mw)

@dp.message(CommandStart())
async def start_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await Database.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    kb = Keyboards.get_admin_main() if message.from_user.id in ADMINS else Keyboards.get_user_main()
    await message.answer("👋 AI Botiga Xush Kelibsiz!\n\nMenga xohlagan matningizni yuboring, rasm yoki tiniq video yaratib beraman 🎬", reply_markup=kb)

@dp.message(Command("admin"), F.from_user.id.in_(ADMINS))
async def admin_panel(message: types.Message):
    await message.answer("🛠 Admin Panel:", reply_markup=Keyboards.get_admin_main())

@dp.message(F.text == "👤 Foydalanuvchi Rejimiga O'tish")
async def back_to_user_mode(message: types.Message):
    await message.answer("✅ Foydalanuvchi rejimiga o'tdingiz.", reply_markup=Keyboards.get_user_main())

@dp.message(F.web_app_data)
async def web_app_data_handler(message: types.Message):
    await process_image_generation(message, message.web_app_data.data)

@dp.message(F.text == "⭐ Stars va Giftlar")
async def shop_menu_handler(message: types.Message):
    await message.answer("🛍 Kerakli bo'limni tanlang:", reply_markup=Keyboards.get_shop_menu())

@dp.callback_query(F.data == "shop_stars")
async def shop_stars_list(callback: types.CallbackQuery):
    packages = await Database.get_star_packages()
    keyboard = []
    for pkg_id, stars, price in packages:
        keyboard.append([InlineKeyboardButton(text=f"⭐ {stars} Stars — {price} so'm", callback_data=f"buy_pkg_{pkg_id}")])
    await callback.message.edit_text("⭐ Mavjud Stars Paketlari:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@dp.callback_query(F.data == "shop_gifts")
async def shop_gifts_list(callback: types.CallbackQuery):
    gifts = await Database.get_gift_packages()
    keyboard = []
    for g_id, g_name, price in gifts:
        keyboard.append([InlineKeyboardButton(text=f"{g_name} — {price} so'm", callback_data=f"buy_gift_{g_id}")])
    await callback.message.edit_text("🎁 Mavjud Telegram Giftlari:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@dp.callback_query(F.data.startswith(("buy_pkg_", "buy_gift_")))
async def select_item_flow(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    item_type = parts[1] # pkg yoki gift
    item_id = int(parts[2])

    if item_type == "pkg":
        packages = await Database.get_star_packages()
        item = next((p for p in packages if p[0] == item_id), None)
        if not item: return
        name, price = f"{item[1]} ta Stars", item[2]
        await state.update_data(item_title=name, item_price=price, is_gift=False)
    else:
        gifts = await Database.get_gift_packages()
        item = next((g for g in gifts if g[0] == item_id), None)
        if not item: return
        name, price = item[1], item[2]
        await state.update_data(item_title=name, item_price=price, is_gift=True)

    await state.set_state(PaymentState.waiting_for_username)
    await callback.message.answer(
        f"✅ Siz **{name}** ({price} so'm) tanladingiz.\n\n"
        f"👤 Iltimos, qabul qilib oluvchi **Telegram username'ingizni** kiriting (masalan: `@username`):",
        reply_markup=Keyboards.get_cancel(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(StateFilter(PaymentState.waiting_for_username), F.text)
async def process_user_username(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor Qilish":
        await state.clear()
        await message.answer("❌ Bekor qilindi.", reply_markup=Keyboards.get_user_main())
        return

    target_username = message.text.strip()
    await state.update_data(target_username=target_username)
    
    data = await state.get_data()
    title = data.get("item_title")
    price = data.get("item_price")

    _, balance = await Database.get_user_stats(message.from_user.id)

    if balance >= price:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Balansdan to'lash ({balance} so'mdan)", callback_data="pay_from_balance")],
            [InlineKeyboardButton(text="💳 Karta orqali to'lash", callback_data="pay_from_card")]
        ])
        await message.answer(f"💰 Sizning balansingizda **{balance} so'm** bor.\nTanlangan mahsulot: **{title}** ({price} so'm).\n\nTo'lov usulini tanlang:", reply_markup=kb, parse_mode="Markdown")
    else:
        await proceed_to_card_payment(message, state)

@dp.callback_query(F.data == "pay_from_balance")
async def pay_balance_handler(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    price = data.get("item_price")
    title = data.get("item_title")
    target_username = data.get("target_username")
    user_id = callback.from_user.id

    _, balance = await Database.get_user_stats(user_id)
    if balance < price:
        await callback.answer("Balansingiz yetarli emas!", show_alert=True)
        return

    await Database.add_balance(user_id, -price)
    await state.clear()

    await callback.message.edit_text(f"✅ Balansingizdan {price} so'm yechildi va xaridingiz qabul qilindi! Admin tez orada {title}ni taqdim etadi.")
    
    for admin_id in ADMINS:
        try:
            await callback.bot.send_message(
                admin_id,
                f"🔔 **Yangi xarid (Balansdan)!**\n\n"
                f"👤 Foydalanuvchi: {callback.from_user.full_name} (@{callback.from_user.username or 'yoq'})\n"
                f"🎯 Username: `{target_username}`\n"
                f"🛍 Mahsulot: {title} ({price} so'm)\n"
                f"💳 To'lov turi: Bot balansi orqali",
                parse_mode="Markdown"
            )
        except:
            pass

@dp.callback_query(F.data == "pay_from_card")
async def pay_card_cb(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await send_card_details(callback.message, state)

async def proceed_to_card_payment(message: types.Message, state: FSMContext):
    await send_card_details(message, state)

async def send_card_details(message: types.Message, state: FSMContext):
    data = await state.get_data()
    title = data.get("item_title")
    price = data.get("item_price")
    target_username = data.get("target_username")
    
    await state.set_state(PaymentState.waiting_for_screenshot)
    text = (
        f"💳 **{title} uchun to'lov ma'lumotlari:**\n\n"
        f"🔢 Karta raqami: `{ADMIN_CARD_NUMBER}`\n"
        f"👤 Karta egasi: {ADMIN_CARD_NAME}\n"
        f"💰 Summa: {price} so'm\n"
        f"🎯 Qabul qiluvchi: {target_username}\n\n"
        f"📸 Pulni o'tkazgach, to'lov cheki (skrinshot) rasmini shu yerga yuboring:"
    )
    await message.answer(text, reply_markup=Keyboards.get_cancel(), parse_mode="Markdown")

@dp.message(StateFilter(PaymentState.waiting_for_screenshot), F.photo)
async def process_payment_screenshot(message: types.Message, state: FSMContext):
    data = await state.get_data()
    title = data.get("item_title")
    price = data.get("item_price")
    target_username = data.get("target_username")
    await state.clear()

    user = message.from_user
    photo = message.photo[-1].file_id

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"approve_{user.id}"),
            InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject_{user.id}")
        ]
    ])

    for admin_id in ADMINS:
        try:
            await message.bot.send_photo(
                chat_id=admin_id,
                photo=photo,
                caption=(
                    f"🔔 **Yangi to'lov cheki (Karta)!**\n\n"
                    f"👤 Foydalanuvchi: {user.full_name} (@{user.username or 'yoq'})\n"
                    f"🎯 Profil: `{target_username}`\n"
                    f"🆔 User ID: `{user.id}`\n"
                    f"🛍 Mahsulot: {title} ({price} so'm)"
                ),
                reply_markup=kb,
                parse_mode="Markdown"
            )
        except:
            pass

    await message.answer("✅ To'lov chekingiz adminga yuborildi! Tez orada tekshirib bajarib berishadi.", reply_markup=Keyboards.get_user_main())

@dp.callback_query(F.data.startswith("approve_"))
async def approve_payment(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS: return
    target_user_id = int(callback.data.split("_")[1])

    try:
        await callback.bot.send_message(target_user_id, "🎉 To'lovingiz tasdiqlandi! Buyurtmangiz bajarildi 🎁⭐")
    except:
        pass

    # To'lovlar kanaliga xabar yuborish
    pay_channel = await Database.get_setting("payment_channel")
    if pay_channel:
        try:
            caption = callback.message.caption or ""
            # Admin xabaridan ma'lumotlarni yig'amiz yoki chiroyli qilib yasaymiz
            # Misol uchun rasm bilan birga kanalga tashlash
            bot_info = await callback.bot.get_me()
            bot_username = bot_info.username
            
            # Matnni rasmda ko'rsatilgandek shakllantirish
            # User ID ni yashiramiz (masalan: 583******)
            masked_user = str(target_user_id)[:3] + "******"
            
            # Target usernameni yashirish (masalan: @Th**********)
            lines = caption.split("\n")
            target_u = "Noma'lum"
            item_desc = "Mahsulot"
            for line in lines:
                if "Profil:" in line or "🎯 Username:" in line:
                    target_u = line.split(":")[-1].strip()
                if "🛍 Mahsulot:" in line:
                    item_desc = line.split(":")[-1].strip()
            
            if len(target_u) > 3:
                masked_target = target_u[:3] + "**********"
            else:
                masked_target = "@********"

            is_gift = "Gift" in item_desc
            header_type = "GIFT SOTIB OLINDI" if is_gift else "STARS SOTIB OLINDI"
            
            # Narx va miqdorni aniqlash
            price_val = "0"
            for word in item_desc.split():
                if word.isdigit() or word.replace(",", "").isdigit():
                    price_val = word
            
            # Narxni va miqdorni alohida ajratib olish
            import random
            rand_num = random.randint(2000, 3000)
            
            channel_text = (
                f"📦 **{header_type} - #{rand_num}**\n\n"
                f"👤 User: {masked_user}\n"
                f"🎯 Qabul qiluvchi: {masked_target}\n"
            )
            if not is_gift:
                stars_cnt = "100"
                for w in item_desc.split():
                    if "ta" in w.lower() or w.isdigit():
                        stars_cnt = w.replace("ta", "").strip()
                channel_text += f"⭐ Miqdor: {stars_cnt} ta\n"
            else:
                channel_text += f"🎁 Gift: {item_desc}\n"
                
            # Narxni to'g'ri topish
            exact_price = "10000"
            for line in lines:
                if "so'm" in line:
                    exact_price = line.split("(")[-1].replace("so'm", "").replace(")", "").strip()

            channel_text += f"\n💰 Narxi: {exact_price} so'm"

            ch_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⭐ Stars olish", url=f"https://t.me/{bot_username}")]
            ])

            if callback.message.photo:
                await callback.bot.send_photo(
                    chat_id=int(pay_channel),
                    photo=callback.message.photo[-1].file_id,
                    caption=channel_text,
                    reply_markup=ch_kb,
                    parse_mode="Markdown"
                )
            else:
                await callback.bot.send_message(
                    chat_id=int(pay_channel),
                    text=channel_text,
                    reply_markup=ch_kb,
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"To'lovlar kanaliga yuborishda xatolik: {e}")

    await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ **HOLAT:** TASDIQLANDI", parse_mode="Markdown")
    await callback.answer("Tasdiqlandi va kanalga yuborildi!")

@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS: return
    target_user_id = int(callback.data.split("_")[1])
    try:
        await callback.bot.send_message(target_user_id, "❌ To'lov chekingiz rad etildi.")
    except:
        pass
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ **HOLAT:** RAD ETILDI", parse_mode="Markdown")
    await callback.answer("Rad etildi.")

# ==============================================================================
# 8. ADMIN: TO'LOVLAR KANALINI SOZLASH VA PAKETLAR
# ==============================================================================

@dp.message(F.text == "📢 To'lovlar Kanalini Sozlash", F.from_user.id.in_(ADMINS))
async def setup_payment_channel(message: types.Message, state: FSMContext):
    current = await Database.get_setting("payment_channel")
    await state.set_state(AdminState.waiting_for_payment_channel)
    await message.answer(
        f"📢 Hozirgi to'lovlar kanali IDsi: `{current}`\n\n"
        f"Yangi to'lovlar kanali ID raqamini yuboring (masalan: `-100123456789`):\n"
        f"*(Bot o'sha kanalda admin bo'lishi shart!)*",
        reply_markup=Keyboards.get_cancel(),
        parse_mode="Markdown"
    )

@dp.message(StateFilter(AdminState.waiting_for_payment_channel), F.from_user.id.in_(ADMINS))
async def process_payment_channel_setting(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor Qilish":
        await state.clear()
        await message.answer("✅ Bekor qilindi.", reply_markup=Keyboards.get_admin_main())
        return
    try:
        ch_id = message.text.strip()
        await Database.set_setting("payment_channel", ch_id)
        await state.clear()
        await message.answer(f"✅ To'lovlar kanali muvaffaqiyatli saqlandi: `{ch_id}`", reply_markup=Keyboards.get_admin_main(), parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")

@dp.message(F.text == "⭐ Stars Paketlarini Boshqarish", F.from_user.id.in_(ADMINS))
async def manage_packages(message: types.Message):
    packages = await Database.get_star_packages()
    text = "⭐ Stars Paketlari:\n\n"
    keyboard = []
    for pkg_id, stars, price in packages:
        text += f"🔹 {stars} Stars — {price} so'm\n"
        keyboard.append([InlineKeyboardButton(text=f"❌ O'chirish: {stars} Stars", callback_data=f"del_pkg_{pkg_id}")])
    keyboard.append([InlineKeyboardButton(text="➕ Paket Qo'shish", callback_data="add_package")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data == "add_package", F.from_user.id.in_(ADMINS))
async def start_add_package(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_package_data)
    await callback.message.answer("📝 Format: `Stars_miqdori | Narxi(so'm)`\nMasalan: `250 | 50000`", reply_markup=Keyboards.get_cancel(), parse_mode="Markdown")
    await callback.answer()

@dp.message(StateFilter(AdminState.waiting_for_package_data), F.from_user.id.in_(ADMINS))
async def process_add_package(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor Qilish":
        await state.clear()
        await message.answer("✅ Bekor qilindi.", reply_markup=Keyboards.get_admin_main())
        return
    try:
        parts = [p.strip() for p in message.text.split("|")]
        await Database.add_or_update_package(int(parts[0]), int(parts[1]))
        await state.clear()
        await message.answer("✅ Stars paketi qo'shildi!", reply_markup=Keyboards.get_admin_main())
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}. Formatni to'g'ri kiriting.")

@dp.callback_query(F.data.startswith("del_pkg_"), F.from_user.id.in_(ADMINS))
async def delete_package_handler(callback: types.CallbackQuery):
    await Database.delete_package(int(callback.data.split("_")[2]))
    await callback.answer("O'chirildi.", show_alert=True)
    await callback.message.delete()

@dp.message(F.text == "🎁 Gift Paketlarini Boshqarish", F.from_user.id.in_(ADMINS))
async def manage_gifts(message: types.Message):
    gifts = await Database.get_gift_packages()
    text = "🎁 Telegram Gift Paketlari:\n\n"
    keyboard = []
    for g_id, g_name, price in gifts:
        text += f"🔹 {g_name} — {price} so'm\n"
        keyboard.append([InlineKeyboardButton(text=f"❌ O'chirish: {g_name}", callback_data=f"del_gift_{g_id}")])
    keyboard.append([InlineKeyboardButton(text="➕ Gift Qo'shish", callback_data="add_gift")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data == "add_gift", F.from_user.id.in_(ADMINS))
async def start_add_gift(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_gift_data)
    await callback.message.answer("📝 Format: `Gift_nomi | Narxi(so'm)`\nMasalan: `🎁 Golden Crown | 75000`", reply_markup=Keyboards.get_cancel(), parse_mode="Markdown")
    await callback.answer()

@dp.message(StateFilter(AdminState.waiting_for_gift_data), F.from_user.id.in_(ADMINS))
async def process_add_gift(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor Qilish":
        await state.clear()
        await message.answer("✅ Bekor qilindi.", reply_markup=Keyboards.get_admin_main())
        return
    try:
        parts = [p.strip() for p in message.text.split("|")]
        await Database.add_or_update_gift(parts[0], int(parts[1]))
        await state.clear()
        await message.answer("✅ Gift muvaffaqiyatli qo'shildi!", reply_markup=Keyboards.get_admin_main())
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}. Formatni to'g'ri kiriting.")

@dp.callback_query(F.data.startswith("del_gift_"), F.from_user.id.in_(ADMINS))
async def delete_gift_handler(callback: types.CallbackQuery):
    await Database.delete_gift(int(callback.data.split("_")[2]))
    await callback.answer("O'chirildi.", show_alert=True)
    await callback.message.delete()

# ==============================================================================
# 9. QOLGAN FUNKSIYALAR (Video, Rasm, Statistika, Broadcast)
# ==============================================================================

@dp.message(F.text == "🎬 AI Video Yaratish")
async def start_video(message: types.Message, state: FSMContext):
    await state.set_state(VideoState.waiting_for_prompt)
    await message.answer("🎥 Video uchun matn yoki mavzu kiriting:", reply_markup=Keyboards.get_cancel())

@dp.message(StateFilter(VideoState.waiting_for_prompt), F.text)
async def process_video(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor Qilish":
        await state.clear()
        await message.answer("Bekor qilindi.", reply_markup=Keyboards.get_user_main())
        return
    prompt = message.text.strip()
    await state.clear()
    
    user_id = message.from_user.id
    if user_id in USER_COOLDOWNS and time.time() - USER_COOLDOWNS[user_id] < COOLDOWN_TIME:
        await message.answer("⏳ Biroz kuting.")
        return
    USER_COOLDOWNS[user_id] = time.time()

    async with ChatActionSender.upload_video(bot=message.bot, chat_id=message.chat.id):
        status = await message.answer("⏳ Video tayyorlanmoqda...")
        en_prompt = await AIService.translate_to_english(prompt)
        v_bytes, engine = await AIService.generate_video(en_prompt)
        if not v_bytes:
            await status.edit_text("❌ Xatolik yuz berdi.")
            return
        v_bytes.seek(0)
        await message.answer_video(BufferedInputFile(v_bytes.read(), filename="video.mp4"), caption=f"🎬 {prompt}", reply_markup=Keyboards.get_user_main())
        await Database.increment_generation(user_id)
        await status.delete()

@dp.message(F.text == "📢 Kanallarni Boshqarish", F.from_user.id.in_(ADMINS))
async def manage_channels(message: types.Message):
    channels = await Database.get_channels()
    keyboard = [[InlineKeyboardButton(text=f"❌ {t}", callback_data=f"del_ch_{cid}")] for cid, t, _ in channels]
    keyboard.append([InlineKeyboardButton(text="➕ Kanal Qo'shish", callback_data="add_channel")])
    await message.answer("📢 Kanallar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data == "add_channel", F.from_user.id.in_(ADMINS))
async def add_ch_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_channel_data)
    await callback.message.answer("📝 Format: `Kanal_ID | Nomi | Havola`", parse_mode="Markdown")
    await callback.answer()

@dp.message(StateFilter(AdminState.waiting_for_channel_data), F.from_user.id.in_(ADMINS))
async def proc_ch(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor Qilish":
        await state.clear()
        return
    parts = [p.strip() for p in message.text.split("|")]
    await Database.add_channel(int(parts[0]), parts[1], parts[2])
    await state.clear()
    await message.answer("✅ Kanal qo'shildi!", reply_markup=Keyboards.get_admin_main())

@dp.callback_query(F.data.startswith("del_ch_"), F.from_user.id.in_(ADMINS))
async def del_ch(callback: types.CallbackQuery):
    await Database.delete_channel(int(callback.data.split("_")[2]))
    await callback.answer("O'chirildi.", show_alert=True)
    await callback.message.delete()

@dp.message(F.text == "📈 Umumiy Statistika", F.from_user.id.in_(ADMINS))
async def stats(message: types.Message):
    total, active, gens = await Database.get_system_stats()
    await message.answer(f"📊 Jami: {total}\n🟢 Aktiv: {active}\n🎨 Generatsiyalar: {gens}")

@dp.message(F.text == "✉️ Reklama Tarqatish", F.from_user.id.in_(ADMINS))
async def broadcast(message: types.Message, state: FSMContext):
    await state.set_state(AdminState.waiting_for_broadcast)
    await message.answer("📢 Reklama postini yuboring:")

@dp.message(StateFilter(AdminState.waiting_for_broadcast), F.from_user.id.in_(ADMINS))
async def proc_bc(message: types.Message, state: FSMContext):
    await state.clear()
    users = await Database.get_all_user_ids()
    success = 0
    for uid in users:
        try:
            await message.copy_to(chat_id=uid)
            success += 1
            await asyncio.sleep(0.03)
        except:
            pass
    await message.answer(f"✅ {success} ta foydalanuvchiga tarqatildi.", reply_markup=Keyboards.get_admin_main())

@dp.message(F.text == "📂 Bazani Yuklab Olish", F.from_user.id.in_(ADMINS))
async def dl_db(message: types.Message):
    if os.path.exists(DATABASE_PATH):
        await message.answer_document(FSInputFile(DATABASE_PATH))

@dp.message(F.text == "🏆 TOP-10 Liderlar")
async def top10(message: types.Message):
    top = await Database.get_top_users(10)
    text = "🏆 TOP-10 Liderlar:\n\n" + "\n".join([f"{i}. {n} — {c} ta" for i, (n, c) in enumerate(top, 1)])
    await message.answer(text)

@dp.message(F.text == "📊 Mening Statistikam")
async def u_stats(message: types.Message):
    count, balance = await Database.get_user_stats(message.from_user.id)
    await message.answer(f"📊 Sizning statistikangiz:\n\n🎨 Ishlaringiz: {count} ta\n⭐ Balansingiz: {balance} so'm")

@dp.message(F.text == "💡 Tasodifiy G'oya")
async def r_idea(message: types.Message):
    import random
    ideas = ["Cyberpunk Toshkent 🏙", "Sehrli saroy 🏰", "Futuristik mashina 🏎"]
    await message.answer(f"💡 G'oya: {random.choice(ideas)}")

@dp.message(F.text == "ℹ️ Bot Haqida")
async def about(message: types.Message):
    await message.answer("🤖 AI Bot yordamida rasm va videolar yaratishingiz mumkin.")

@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: types.CallbackQuery, bot: Bot):
    if not await check_user_subscriptions(bot, callback.from_user.id):
        try: await callback.message.delete()
        except: pass
        await callback.message.answer("✅ Obuna tasdiqlandi!", reply_markup=Keyboards.get_user_main())
    else:
        await callback.answer("❌ Hali hamma kanalga obuna bo'lmadingiz!", show_alert=True)

async def process_image_generation(message: types.Message, prompt: str):
    user_id = message.from_user.id
    if user_id in USER_COOLDOWNS and time.time() - USER_COOLDOWNS[user_id] < COOLDOWN_TIME:
        await message.answer("⏳ Biroz kuting.")
        return
    USER_COOLDOWNS[user_id] = time.time()

    async with ChatActionSender.upload_photo(bot=message.bot, chat_id=message.chat.id):
        status = await message.answer("⏳ Rasm chizilmoqda...")
        en_prompt = await AIService.translate_to_english(prompt)
        img_bytes, engine = await AIService.generate_image(en_prompt)
        if not img_bytes:
            await status.edit_text("❌ Xatolik yuz berdi.")
            return
        sticker = AIService.create_sticker(img_bytes)
        img_bytes.seek(0)
        await message.answer_photo(BufferedInputFile(img_bytes.read(), filename="img.png"), caption=f"✨ {prompt}")
        await message.answer_sticker(BufferedInputFile(sticker.read(), filename="st.webp"))
        await Database.increment_generation(user_id)
        await status.delete()

@dp.message(F.text & ~F.text.startswith("/"))
async def text_handler(message: types.Message):
    if message.text in ["🎨 AI Rasm Yaratish", "🎬 AI Video Yaratish", "🚀 Mini App Ochish", "⭐ Stars va Giftlar", "💡 Tasodifiy G'oya", "🏆 TOP-10 Liderlar", "📊 Mening Statistikam", "ℹ️ Bot Haqida"]:
        return
    await process_image_generation(message, message.text.strip())


# ==============================================================================
# 10. ISHga TUSHIRISH
# ==============================================================================

async def main():
    server_thread = Thread(target=run_web_server)
    server_thread.daemon = True
    server_thread.start()

    await Database.init_db()
    bot = Bot(token=BOT_TOKEN)
    logger.info("Bot ishga tushdi...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
