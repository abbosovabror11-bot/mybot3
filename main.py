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
from flask import Thread, Flask

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
    TelegramObject,
    WebAppInfo
)
from aiogram.utils.chat_action import ChatActionSender

# ==============================================================================
# 1. KONFIGURATSIYA (Flask Web Server for Render Web Service)
# ==============================================================================

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running and Mini App is active!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8856867256:AAENRvJL44yxjUSFhFDp5ygO9zFp-_yzMQc")
ADMINS_RAW = os.getenv("ADMINS", "8694110588")
ADMINS = [int(admin_id) for admin_id in ADMINS_RAW.split(",") if admin_id.strip().isdigit()]

WEB_APP_URL = os.getenv("WEB_APP_URL", "https://eclectic-starlight-cfbad8.netlify.app") 
DATABASE_PATH = "bot_data.db"

# 👉 KARTA MA'LUMOTLARINGIZ
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
# 2. MA'LUMOTLAR BAZASI
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
                    value TEXT NOT NULL
                )
            """)
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('star_price_50', '10000')")
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('star_price_100', '20000')")
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
    async def get_prices() -> Dict[str, str]:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute("SELECT key, value FROM settings WHERE key LIKE 'star_price_%'") as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}


# ==============================================================================
# 3. KEYBOARDS
# ==============================================================================

class Keyboards:
    @staticmethod
    def get_user_main() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🎨 AI Rasm Yaratish"), KeyboardButton(text="🎬 AI Video Yaratish")],
                [KeyboardButton(text="🚀 Mini App Ochish", web_app=WebAppInfo(url=WEB_APP_URL)), KeyboardButton(text="🛍 Shop")],
                [KeyboardButton(text="⭐ Stars Sotib Olish"), KeyboardButton(text="💡 Tasodifiy G'oya")],
                [KeyboardButton(text="🏆 TOP-10 Liderlar"), KeyboardButton(text="📊 Mening Statistikam")],
                [KeyboardButton(text="ℹ️ Bot Haqida")]
            ],
            resize_keyboard=True
        )

    @staticmethod
    def get_admin_main() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📢 Kanallarni Boshqarish"), KeyboardButton(text="📈 Umumiy Statistika")],
                [KeyboardButton(text="✉️ Reklama Tarqatish"), KeyboardButton(text="📂 Bazani Yuklab Olish")],
                [KeyboardButton(text="👤 Foydalanuvchi Rejimiga O'tish")]
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
    def get_shop_keyboard(prices: Dict[str, str]) -> InlineKeyboardMarkup:
        p50 = prices.get("star_price_50", "10000")
        p100 = prices.get("star_price_100", "20000")
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ 50 Stars — {p50} so'm", callback_data="buy_stars_50")],
            [InlineKeyboardButton(text=f"⭐ 100 Stars — {p100} so'm", callback_data="buy_stars_100")],
        ])


# ==============================================================================
# 4. MAJBURIY OBUNA TEKSHIRUVI
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
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        bot: Bot = data['bot']
        
        user: types.User = None
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user

        if not user or user.id in ADMINS:
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and (event.data == "check_sub" or event.data.startswith("buy_")):
            return await handler(event, data)

        unsubscribed = await check_user_subscriptions(bot, user.id)

        if unsubscribed:
            keyboard = []
            for title, link in unsubscribed:
                keyboard.append([InlineKeyboardButton(text=f"📢 {title}", url=link)])
            
            keyboard.append([InlineKeyboardButton(text="✅ Obunani Tekshirish", callback_data="check_sub")])
            kb = InlineKeyboardMarkup(inline_keyboard=keyboard)
            text = "⚠️ Botdan to'liq foydalanish uchun quyidagi kanallarga obuna bo'ling:"

            if isinstance(event, Message):
                await event.answer(text, reply_markup=kb)
            elif isinstance(event, CallbackQuery):
                await event.message.answer(text, reply_markup=kb)
                await event.answer()
            return

        return await handler(event, data)


# ==============================================================================
# 5. AI GENERATOR ENGINE
# ==============================================================================

class AIService:
    @staticmethod
    def is_nsfw(text: str) -> bool:
        text_lower = text.lower()
        return any(word in text_lower for word in NSFW_WORDS)

    @staticmethod
    async def translate_to_english(text: str) -> str:
        try:
            encoded_text = urllib.parse.quote(text)
            url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=en&dt=t&q={encoded_text}"
            headers = {'User-Agent': 'Mozilla/5.0'}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data[0][0][0]
        except Exception as e:
            logger.error(f"Tarjima xatosi: {e}")
        return text

    @staticmethod
    async def generate_image(prompt: str) -> Tuple[BytesIO | None, str]:
        encoded_prompt = urllib.parse.quote(prompt)
        headers = {'User-Agent': 'Mozilla/5.0'}

        url1 = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&seed={int(time.time())}&model=flux"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url1, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 5000:
                            return BytesIO(data), "Flux Ultra HD"
        except Exception as e:
            logger.warning(f"Rasm 1-Node xatosi: {e}")

        url2 = f"https://lexica.art/api/v1/search?q={encoded_prompt}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url2, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("images"):
                            img_src = data["images"][0]["src"]
                            async with session.get(img_src, headers=headers) as img_resp:
                                if img_resp.status == 200:
                                    return BytesIO(await img_resp.read()), "Lexica AI"
        except Exception as e:
            logger.warning(f"Rasm 2-Node xatosi: {e}")

        return None, "Xatolik"

    @staticmethod
    async def generate_video(prompt: str) -> Tuple[BytesIO | None, str]:
        encoded_prompt = urllib.parse.quote(prompt)
        headers = {'User-Agent': 'Mozilla/5.0'}

        video_urls = [
            f"https://image.pollinations.ai/prompt/cinematic%20video%20{encoded_prompt}?width=720&height=1280&nologo=true&model=flux-realism",
            f"https://pollinations.ai/p/{encoded_prompt}?width=720&height=1280&seed={int(time.time())}",
            f"https://image.pollinations.ai/prompt/animation%20{encoded_prompt}?width=720&height=1280&nologo=true"
        ]

        async with aiohttp.ClientSession() as session:
            for idx, url in enumerate(video_urls, 1):
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if len(data) > 15000:
                                return BytesIO(data), f"AI Video Engine v{idx}"
                except Exception as e:
                    logger.warning(f"Video {idx}-Node xatosi: {e}")

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
# 6. HANDLERLAR
# ==============================================================================

class AdminState(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_channel_data = State()

class PaymentState(StatesGroup):
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
    await Database.add_user(
        user_id=message.from_user.id,
        full_name=message.from_user.full_name,
        username=message.from_user.username
    )
    kb = Keyboards.get_admin_main() if message.from_user.id in ADMINS else Keyboards.get_user_main()
    await message.answer(
        "👋 AI Botiga Xush Kelibsiz!\n\nMenga xohlagan matningizni yuboring, rasm yoki tiniq video yaratib beraman 🎬",
        reply_markup=kb
    )

@dp.message(Command("admin"), F.from_user.id.in_(ADMINS))
async def admin_panel(message: types.Message):
    await message.answer("🛠 Admin Panel:", reply_markup=Keyboards.get_admin_main())

@dp.message(F.text == "👤 Foydalanuvchi Rejimiga O'tish")
async def back_to_user_mode(message: types.Message):
    await message.answer("✅ Foydalanuvchi rejimiga o'tdingiz.", reply_markup=Keyboards.get_user_main())

@dp.message(F.web_app_data)
async def web_app_data_handler(message: types.Message):
    prompt = message.web_app_data.data
    await process_image_generation(message, prompt)

@dp.message(F.text == "⭐ Stars Sotib Olish")
async def shop_handler(message: types.Message):
    prices = await Database.get_prices()
    await message.answer(
        "⭐ Stars Shop bo'limi:\n\nKerakli paketni tanlang:",
        reply_markup=Keyboards.get_shop_keyboard(prices)
    )

@dp.callback_query(F.data.in_(["buy_stars_50", "buy_stars_100"]))
async def buy_stars_card_flow(callback: types.CallbackQuery, state: FSMContext):
    prices = await Database.get_prices()
    if "50" in callback.data:
        amount = 50
        price = prices.get("star_price_50", "10000")
    else:
        amount = 100
        price = prices.get("star_price_100", "20000")

    await state.set_state(PaymentState.waiting_for_screenshot)
    await state.update_data(star_amount=amount, price_sum=price)

    text = (
        f"💳 **{amount} ta Stars sotib olish uchun to'lov:**\n\n"
        f"🔢 Karta raqami: `{ADMIN_CARD_NUMBER}`\n"
        f"👤 Karta egasi: {ADMIN_CARD_NAME}\n"
        f"💰 Summa: {price} so'm\n\n"
        f"📸 Pulni o'tkazgach, to'lov cheki (skrinshot) rasmini shu yerga yuboring:"
    )
    await callback.message.answer(text, reply_markup=Keyboards.get_cancel(), parse_mode="Markdown")
    await callback.answer()

@dp.message(StateFilter(PaymentState.waiting_for_screenshot), F.photo)
async def process_payment_screenshot(message: types.Message, state: FSMContext):
    data = await state.get_data()
    amount = data.get("star_amount", 50)
    price = data.get("price_sum", "10000")
    await state.clear()

    user = message.from_user
    photo = message.photo[-1].file_id

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"approve_{user.id}_{amount}"),
            InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject_{user.id}")
        ]
    ])

    for admin_id in ADMINS:
        try:
            await message.bot.send_photo(
                chat_id=admin_id,
                photo=photo,
                caption=(
                    f"🔔 **Yangi to'lov cheki!**\n\n"
                    f"👤 Foydalanuvchi: {user.full_name} (@{user.username or 'yoq'})\n"
                    f"🆔 ID: `{user.id}`\n"
                    f"⭐ Paket: {amount} Stars ({price} so'm)"
                ),
                reply_markup=kb,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Adminga yuborishda xato: {e}")

    await message.answer("✅ To'lov chekingiz adminga yuborildi! Tez orada tekshirib balansingizga qo'shib berishadi.", reply_markup=Keyboards.get_user_main())

@dp.message(StateFilter(PaymentState.waiting_for_screenshot))
async def cancel_payment_process(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor Qilish":
        await state.clear()
        await message.answer("❌ Bekor qilindi.", reply_markup=Keyboards.get_user_main())
        return
    await message.answer("⚠️ Iltimos, to'lov chekining skrinshot rasmini yuboring yoki Bekor Qilish tugmasini bosing.")

@dp.callback_query(F.data.startswith("approve_"))
async def approve_payment(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Siz admin emassiz!", show_alert=True)
        return

    parts = callback.data.split("_")
    target_user_id = int(parts[1])
    stars_amount = int(parts[2])

    await Database.add_balance(target_user_id, stars_amount)
    try:
        await callback.bot.send_message(
            target_user_id,
            f"🎉 To'lovingiz admin tomonidan tasdiqlandi! Balansingizga +{stars_amount} Stars qo'shildi ⭐"
        )
    except:
        pass

    await callback.message.edit_caption(
        caption=callback.message.caption + f"\n\n✅ **HOLAT:** TASDIQLANDI (+{stars_amount} Stars)",
        parse_mode="Markdown"
    )
    await callback.answer("Muvaffaqiyatli tasdiqlandi!")

@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("Siz admin emassiz!", show_alert=True)
        return

    target_user_id = int(callback.data.split("_")[1])
    try:
        await callback.bot.send_message(target_user_id, "❌ Kechirasiz, to'lov chekingiz admin tomonidan rad etildi.")
    except:
        pass

    await callback.message.edit_caption(
        caption=callback.message.caption + f"\n\n❌ **HOLAT:** RAD ETILDI",
        parse_mode="Markdown"
    )
    await callback.answer("Rad etildi.")

# --- AI VIDEO YARATISH BO'LIMI ---
@dp.message(F.text == "🎬 AI Video Yaratish")
async def start_video_creation(message: types.Message, state: FSMContext):
    await state.set_state(VideoState.waiting_for_prompt)
    await message.answer(
        "🎥 Qanday video yaratishni xohlaysiz? Mavzu yoki matnni yuboring (masalan: *Koinot bo'ylab uchayotgan kema*):",
        reply_markup=Keyboards.get_cancel(),
        parse_mode="Markdown"
    )

@dp.message(StateFilter(VideoState.waiting_for_prompt), F.text)
async def process_video_generation_step(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor Qilish":
        await state.clear()
        await message.answer("❌ Bekor qilindi.", reply_markup=Keyboards.get_user_main())
        return

    user_prompt = message.text.strip()
    await state.clear()

    user_id = message.from_user.id
    current_time = time.time()

    if user_id in USER_COOLDOWNS:
        elapsed = current_time - USER_COOLDOWNS[user_id]
        if elapsed < COOLDOWN_TIME:
            remaining = int(COOLDOWN_TIME - elapsed)
            minutes = remaining // 60
            seconds = remaining % 60
            await message.answer(f"⏳ Yangi kontent yaratish uchun yana {minutes} daqiqa {seconds} soniya kuting.", reply_markup=Keyboards.get_user_main())
            return

    if AIService.is_nsfw(user_prompt):
        await message.answer("🚫 Kechirasiz, taqiqlangan so'z aniqlandi.", reply_markup=Keyboards.get_user_main())
        return

    USER_COOLDOWNS[user_id] = current_time

    async with ChatActionSender.upload_video(bot=message.bot, chat_id=message.chat.id):
        status_msg = await message.answer("⏳ AI yuqori sifatli video tayyorlamoqda (biroz vaqt olishi mumkin)...")
        en_prompt = await AIService.translate_to_english(user_prompt)
        video_bytes, engine = await AIService.generate_video(en_prompt)

        if not video_bytes:
            await status_msg.edit_text("❌ Video yaratishda xatolik yuz berdi. Qaytadan urinib ko'ring.", reply_markup=Keyboards.get_user_main())
            return

        video_bytes.seek(0)
        await message.answer_video(
            BufferedInputFile(video_bytes.read(), filename="ai_video.mp4"),
            caption=f"🎬 **AI Video:** {user_prompt}\n⚙️ **Motor:** {engine}",
            reply_markup=Keyboards.get_user_main(),
            parse_mode="Markdown"
        )
        
        await Database.increment_generation(user_id)
        await status_msg.delete()

# --- ADMIN VA BOSHQA FUNKSIYALAR ---
@dp.message(F.text == "📢 Kanallarni Boshqarish", F.from_user.id.in_(ADMINS))
async def manage_channels(message: types.Message):
    channels = await Database.get_channels()
    text = "📢 Majburiy obuna kanallari:\n\n"
    keyboard = []
    if channels:
        for ch_id, title, link in channels:
            text += f"🔹 {title} (`{ch_id}`)\n"
            keyboard.append([InlineKeyboardButton(text=f"❌ O'chirish: {title}", callback_data=f"del_ch_{ch_id}")])
    else:
        text += "Hozircha kanal ulanmagan.\n\n"
    keyboard.append([InlineKeyboardButton(text="➕ Yangi Kanal Qo'shish", callback_data="add_channel")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@dp.callback_query(F.data == "add_channel", F.from_user.id.in_(ADMINS))
async def start_add_channel(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_channel_data)
    await callback.message.answer("📝 Kanal ma'lumotlarini yuboring:\n`Kanal_ID | Nomi | Havola`", reply_markup=Keyboards.get_cancel(), parse_mode="Markdown")
    await callback.answer()

@dp.message(StateFilter(AdminState.waiting_for_channel_data), F.from_user.id.in_(ADMINS))
async def process_add_channel(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor Qilish":
        await state.clear()
        await message.answer("✅ Bekor qilindi.", reply_markup=Keyboards.get_admin_main())
        return
    try:
        parts = [p.strip() for p in message.text.split("|")]
        await Database.add_channel(int(parts[0]), parts[1], parts[2])
        await state.clear()
        await message.answer("✅ Kanal muvaffaqiyatli qo'shildi!", reply_markup=Keyboards.get_admin_main())
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")

@dp.callback_query(F.data.startswith("del_ch_"), F.from_user.id.in_(ADMINS))
async def delete_channel_handler(callback: types.CallbackQuery):
    await Database.delete_channel(int(callback.data.split("_")[2]))
    await callback.answer("✅ O'chirildi.", show_alert=True)
    await callback.message.delete()

@dp.message(F.text == "📈 Umumiy Statistika", F.from_user.id.in_(ADMINS))
async def admin_stats(message: types.Message):
    total, active, gens = await Database.get_system_stats()
    await message.answer(f"📊 Statistika:\n\n👥 Jami foydalanuvchilar: {total}\n🟢 Aktiv: {active}\n🎨 Yaratilgan kontentlar: {gens}")

@dp.message(F.text == "✉️ Reklama Tarqatish", F.from_user.id.in_(ADMINS))
async def start_broadcast(message: types.Message, state: FSMContext):
    await state.set_state(AdminState.waiting_for_broadcast)
    await message.answer("📢 Reklama postini yuboring:", reply_markup=Keyboards.get_cancel())

@dp.message(StateFilter(AdminState.waiting_for_broadcast), F.from_user.id.in_(ADMINS))
async def process_broadcast(message: types.Message, state: FSMContext):
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
    await message.answer(f"✅ Reklama {success} ta foydalanuvchiga tarqatildi.", reply_markup=Keyboards.get_admin_main())

@dp.message(F.text == "📂 Bazani Yuklab Olish", F.from_user.id.in_(ADMINS))
async def download_db(message: types.Message):
    if os.path.exists(DATABASE_PATH):
        await message.answer_document(FSInputFile(DATABASE_PATH), caption="📂 Bot bazasi")

@dp.message(F.text == "🏆 TOP-10 Liderlar")
async def show_top(message: types.Message):
    top = await Database.get_top_users(10)
    text = "🏆 TOP-10 Liderlar:\n\n"
    for idx, (name, count) in enumerate(top, 1):
        text += f"{idx}. {name} — {count} ta kontent 🎨🎬\n"
    await message.answer(text)

@dp.message(F.text == "📊 Mening Statistikam")
async def user_stats(message: types.Message):
    count, balance = await Database.get_user_stats(message.from_user.id)
    await message.answer(f"📊 Sizning statistikangiz:\n\n🎨 Yaratgan ishlaringiz: {count} ta\n⭐ Balansingiz: {balance} Stars")

@dp.message(F.text == "💡 Tasodifiy G'oya")
async def random_idea(message: types.Message):
    import random
    ideas = ["Cyberpunk Toshkent 🏙", "Koinotdagi sehrli saroy 🏰", "Futuristik sport mashinasi 🏎", "Kosmonavt mushuk 🐱‍👤"]
    await message.answer(f"💡 Tavsiya qilinadigan g'oya:\n\n{random.choice(ideas)}")

@dp.message(F.text == "🎨 AI Rasm Yaratish")
async def info_handler(message: types.Message):
    await message.answer("✍️ Xohlagan matningizni yuboring yoki Mini App orqali foydalaning 🚀")

@dp.message(F.text == "ℹ️ Bot Haqida")
async def about_handler(message: types.Message):
    await message.answer("🤖 Ushbu bot sun'iy intellekt yordamida yuqori sifatli rasmlar, stikerlar va videolar yaratib beradi.")

@dp.callback_query(F.data == "check_sub")
async def check_sub_handler(callback: types.CallbackQuery, bot: Bot):
    unsubscribed = await check_user_subscriptions(bot, callback.from_user.id)
    if not unsubscribed:
        try: await callback.message.delete() except: pass
        await callback.message.answer("✅ Obuna tasdiqlandi!", reply_markup=Keyboards.get_user_main())
    else:
        await callback.answer("❌ Hamma kanalga obuna bo'lmadingiz!", show_alert=True)

async def process_image_generation(message: types.Message, user_prompt: str):
    user_id = message.from_user.id
    current_time = time.time()
    
    if user_id in USER_COOLDOWNS:
        elapsed = current_time - USER_COOLDOWNS[user_id]
        if elapsed < COOLDOWN_TIME:
            remaining = int(COOLDOWN_TIME - elapsed)
            minutes = remaining // 60
            seconds = remaining % 60
            await message.answer(f"⏳ Yangi kontent yaratish uchun yana {minutes} daqiqa {seconds} soniya kuting.")
            return

    if AIService.is_nsfw(user_prompt):
        await message.answer("🚫 Kechirasiz, taqiqlangan so'z aniqlandi.")
        return

    USER_COOLDOWNS[user_id] = current_time

    async with ChatActionSender.upload_photo(bot=message.bot, chat_id=message.chat.id):
        status_msg = await message.answer("⏳ AI rasm chizmoqda, biroz kuting...")
        en_prompt = await AIService.translate_to_english(user_prompt)
        img_bytes, engine = await AIService.generate_image(en_prompt)

        if not img_bytes:
            await status_msg.edit_text("❌ Rasm yaratishda xatolik yuz berdi. Qaytadan urinib ko'ring.")
            return

        sticker_bytes = AIService.create_sticker(img_bytes)
        img_bytes.seek(0)

        await message.answer_photo(BufferedInputFile(img_bytes.read(), filename="img.png"), caption=f"✨ So'rov: {user_prompt}\n⚙️ Motor: {engine}")
        await message.answer_sticker(BufferedInputFile(sticker_bytes.read(), filename="st.webp"))
        
        await Database.increment_generation(user_id)
        await status_msg.delete()

@dp.message(F.text & ~F.text.startswith("/"))
async def text_handler(message: types.Message):
    if message.text in ["🎨 AI Rasm Yaratish", "🎬 AI Video Yaratish", "🚀 Mini App Ochish", "🛍 Shop", "⭐ Stars Sotib Olish", "💡 Tasodifiy G'oya", "🏆 TOP-10 Liderlar", "📊 Mening Statistikam", "ℹ️ Bot Haqida"]:
        return
    await process_image_generation(message, message.text.strip())


# ==============================================================================
# 7. ISHGA TUSHIRISH (Flask + Telegram Bot birga)
# ==============================================================================

async def main():
    # Flask serverini fon rejimida (Thread) ishga tushiramiz
    from threading import Thread
    server_thread = Thread(target=run_web_server)
    server_thread.daemon = True
    server_thread.start()

    await Database.init_db()
    bot = Bot(token=BOT_TOKEN)
    logger.info("Bot va Web Server birga ishga tushmoqda...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
