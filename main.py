import asyncio
import logging
import os
import sys
import time
import urllib.parse
from io import BytesIO
from typing import Callable, Dict, Any, Awaitable, List, Tuple

import aiohttp
import aiosqlite
from PIL import Image
from aiohttp import web

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
    LabeledPrice,
    WebAppInfo
)
from aiogram.utils.chat_action import ChatActionSender

# ==============================================================================
# 1. KONFIGURATSIYA
# ==============================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8856867256:AAGxdKm-7d6cjFet5hnk2OD5Lu5h6T_7Tvk")
ADMINS_RAW = os.getenv("ADMINS", "8694110588")
ADMINS = [int(admin_id) for admin_id in ADMINS_RAW.split(",") if admin_id.strip().isdigit()]

PORT = int(os.getenv("PORT", 8080))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")
# Mini App saytingiz havolasi Netlify'dan olganingizga o'zgartirildi:
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://eclectic-starlight-cfbad8.netlify.app") 
DATABASE_PATH = "bot_data.db"

NSFW_WORDS = ["nude", "naked", "sex", "porn", "hentai", "xxx", "erotic", "bikini", "jalap", "yalang'och", "jinsiy"]

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
            await db.execute("""
                INSERT OR IGNORE INTO settings (key, value) VALUES ('star_price', '50')
            """)
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
    async def set_user_active(user_id: int, is_active: bool):
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "UPDATE users SET is_active = ? WHERE user_id = ?",
                (1 if is_active else 0, user_id)
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
    async def get_user_stats(user_id: int) -> int:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute("SELECT generations_count FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

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
    async def get_star_price() -> int:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute("SELECT value FROM settings WHERE key = 'star_price'") as cursor:
                row = await cursor.fetchone()
                return int(row[0]) if row else 50

    @staticmethod
    async def set_star_price(new_price: int):
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute("UPDATE settings SET value = ? WHERE key = 'star_price'", (str(new_price),))
            await db.commit()


# ==============================================================================
# 3. KEYBOARDS
# ==============================================================================

class Keyboards:
    @staticmethod
    def get_user_main() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🎨 AI Rasm Yaratish 🚀"), KeyboardButton(text="🌐 Mini App Ochish 📱", web_app=WebAppInfo(url=WEB_APP_URL))],
                [KeyboardButton(text="💡 Tasodifiy G'oya ✨"), KeyboardButton(text="⭐ Stars Sotib Olish 💰")],
                [KeyboardButton(text="🏆 TOP-10 Liderlar ⭐️"), KeyboardButton(text="📊 Mening Statistikam 📈")],
                [KeyboardButton(text="ℹ️ Bot Haqida 🤖")]
            ],
            resize_keyboard=True
        )

    @staticmethod
    def get_admin_main() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📢 Kanallarni Boshqarish ⚙️"), KeyboardButton(text="⭐ Stars Narxini O'zgartirish 💰")],
                [KeyboardButton(text="📊 Umumiy Statistika 📈"), KeyboardButton(text="📣 Reklama Tarqatish 🚀")],
                [KeyboardButton(text="💾 Bazani Yuklab Olish 📥"), KeyboardButton(text="👤 Foydalanuvchi Rejimiga O'tish ⬅️")]
            ],
            resize_keyboard=True
        )

    @staticmethod
    def get_cancel() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="❌ Bekor Qilish")]],
            resize_keyboard=True
        )


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

class SubscriptionMiddleware(types.TelegramObject):
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

        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)

        unsubscribed = await check_user_subscriptions(bot, user.id)

        if unsubscribed:
            keyboard = []
            for title, link in unsubscribed:
                keyboard.append([InlineKeyboardButton(text=f"📢 {title}", url=link)])
            
            keyboard.append([InlineKeyboardButton(text="✅ Obunani Tekshirish", callback_data="check_sub")])
            kb = InlineKeyboardMarkup(inline_keyboard=keyboard)
            text = "⚠️ **Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:**"

            if isinstance(event, Message):
                await event.answer(text, reply_markup=kb, parse_mode="Markdown")
            elif isinstance(event, CallbackQuery):
                await event.message.answer(text, reply_markup=kb, parse_mode="Markdown")
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
            logger.warning(f"1-Node xatosi: {e}")

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
            logger.warning(f"2-Node xatosi: {e}")

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
# 6. WEB SERVER (Ping & Mini App Receiver)
# ==============================================================================

async def handle_ping(request):
    return web.Response(text="Bot is live 24/7!", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Server {PORT}-portda ishlamoqda!")

async def self_ping_loop():
    await asyncio.sleep(10)
    if not RENDER_EXTERNAL_URL:
        return
    ping_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/ping"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(ping_url, timeout=10) as resp:
                    pass
        except Exception as e:
            logger.warning(f"Ping xatosi: {e}")
        await asyncio.sleep(240)


# ==============================================================================
# 7. HANDLERLAR
# ==============================================================================

class AdminState(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_channel_data = State()
    waiting_for_new_price = State()

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
        "✨ **Professional AI Botiga Xush Kelibsiz!**\n\n"
        "Manga xohlagan matningizni yuboring yoki **Mini App** orqali qulay tarzda rasm yarating!",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.message(Command("admin"), F.from_user.id.in_(ADMINS))
async def admin_panel(message: types.Message):
    await message.answer("👑 **Admin Panel:**", reply_markup=Keyboards.get_admin_main())

@dp.message(F.text == "👤 Foydalanuvchi Rejimiga O'tish ⬅️")
async def back_to_user_mode(message: types.Message):
    await message.answer("👤 Foydalanuvchi rejimiga o'tdingiz.", reply_markup=Keyboards.get_user_main())

# Mini App orqali yuborilgan ma'lumotni qabul qilish
@dp.message(F.web_app_data)
async def web_app_data_handler(message: types.Message):
    prompt = message.web_app_data.data
    await process_image_generation(message, prompt)

# Kanallarni boshqarish
@dp.message(F.text == "📢 Kanallarni Boshqarish ⚙️", F.from_user.id.in_(ADMINS))
async def manage_channels(message: types.Message):
    channels = await Database.get_channels()
    text = "⚙️ **MAJBURIY OBUNA KANALLARI:**\n\n"
    keyboard = []
    if channels:
        for ch_id, title, link in channels:
            text += f"🔹 **{title}** (`{ch_id}`)\n"
            keyboard.append([InlineKeyboardButton(text=f"❌ O'chirish: {title}", callback_data=f"del_ch_{ch_id}")])
    else:
        text += "⚠️ *Hozircha kanal ulanmagan.*\n\n"
    keyboard.append([InlineKeyboardButton(text="➕ Yangi Kanal Qo'shish", callback_data="add_channel")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@dp.callback_query(F.data == "add_channel", F.from_user.id.in_(ADMINS))
async def start_add_channel(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_channel_data)
    await callback.message.answer("Kanal ma'lumotlarini yuboring:\n`Kanal_ID | Nomi | Havola`", parse_mode="Markdown", reply_markup=Keyboards.get_cancel())
    await callback.answer()

@dp.message(StateFilter(AdminState.waiting_for_channel_data), F.from_user.id.in_(ADMINS))
async def process_add_channel(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor Qilish":
        await state.clear()
        await message.answer("Bekor qilindi.", reply_markup=Keyboards.get_admin_main())
        return
    try:
        parts = [p.strip() for p in message.text.split("|")]
        await Database.add_channel(int(parts[0]), parts[1], parts[2])
        await state.clear()
        await message.answer("✅ Kanal qo'shildi!", reply_markup=Keyboards.get_admin_main())
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")

@dp.callback_query(F.data.startswith("del_ch_"), F.from_user.id.in_(ADMINS))
async def delete_channel_handler(callback: types.CallbackQuery):
    await Database.delete_channel(int(callback.data.split("_")[2]))
    await callback.answer("✅ O'chirildi!", show_alert=True)
    await callback.message.delete()

# Stars narxini o'zgartirish
@dp.message(F.text == "⭐ Stars Narxini O'zgartirish 💰", F.from_user.id.in_(ADMINS))
async def change_price_start(message: types.Message, state: FSMContext):
    current = await Database.get_star_price()
    await state.set_state(AdminState.waiting_for_new_price)
    await message.answer(f"💰 Joriy narx: `{current}` Stars\Yangi narelni yuboring:", reply_markup=Keyboards.get_cancel(), parse_mode="Markdown")

@dp.message(StateFilter(AdminState.waiting_for_new_price), F.from_user.id.in_(ADMINS))
async def process_new_price(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor Qilish":
        await state.clear()
        await message.answer("Bekor qilindi.", reply_markup=Keyboards.get_admin_main())
        return
    if message.text.isdigit():
        await Database.set_star_price(int(message.text))
        await state.clear()
        await message.answer(f"✅ Yangi narx: {message.text} Stars", reply_markup=Keyboards.get_admin_main())

@dp.message(F.text == "📊 Umumiy Statistika 📈", F.from_user.id.in_(ADMINS))
async def admin_stats(message: types.Message):
    total, active, gens = await Database.get_system_stats()
    await message.answer(f"📈 Jami: {total}\n🟢 Aktiv: {active}\n🎨 Rasmlar: {gens}")

@dp.message(F.text == "📣 Reklama Tarqatish 🚀", F.from_user.id.in_(ADMINS))
async def start_broadcast(message: types.Message, state: FSMContext):
    await state.set_state(AdminState.waiting_for_broadcast)
    await message.answer("📢 Reklama postini yuboring:", reply_markup=Keyboards.get_cancel())

@dp.message(StateFilter(AdminState.waiting_for_broadcast), F.from_user.id.in_(ADMINS))
async def process_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    users = await Database.get_all_user_ids()
    for uid in users:
        try:
            await message.copy_to(chat_id=uid)
            await asyncio.sleep(0.03)
        except:
            pass
    await message.answer("✅ Reklama tarqatildi!", reply_markup=Keyboards.get_admin_main())

@dp.message(F.text == "💾 Bazani Yuklab Olish 📥", F.from_user.id.in_(ADMINS))
async def download_db(message: types.Message):
    if os.path.exists(DATABASE_PATH):
        await message.answer_document(FSInputFile(DATABASE_PATH))

# Foydalanuvchi qismi
@dp.message(F.text == "⭐ Stars Sotib Olish 💰")
async def send_invoice_handler(message: types.Message):
    price = await Database.get_star_price()
    await message.bot.send_invoice(
        chat_id=message.chat.id, title="VIP Obuna", description="AI imkoniyatlari",
        payload="vip", provider_token="", currency="XTR", prices=[LabeledPrice(label="Paket", amount=price)]
    )

@dp.pre_checkout_query()
async def pre_checkout_handler(query: types.PreCheckoutQuery):
    await query.bot.answer_pre_checkout_query(query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    await message.answer("🎉 To'lov muvaffaqiyatli amalga oshirildi!")

@dp.message(F.text == "🏆 TOP-10 Liderlar ⭐️")
async def show_top(message: types.Message):
    top = await Database.get_top_users(10)
    text = "⭐️ **TOP-10 Liderlar:**\n\n"
    for idx, (name, count) in enumerate(top, 1):
        text += f"{idx}. {name} — {count} ta\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "📊 Mening Statistikam 📈")
async def user_stats(message: types.Message):
    count = await Database.get_user_stats(message.from_user.id)
    await message.answer(f"📊 Siz yaratgan rasmlar: `{count}` ta", parse_mode="Markdown")

@dp.message(F.text == "💡 Tasodifiy G'oya ✨")
async def random_idea(message: types.Message):
    import random
    ideas = ["Cyberpunk Toshkent", "Koinotdagi saroy", "Sehrli o'rmon"]
    await message.answer(f"✨ G'oya: `{random.choice(ideas)}`", parse_mode="Markdown")

@dp.message(F.text == "🎨 AI Rasm Yaratish 🚀")
async def info_handler(message: types.Message):
    await message.answer("🎨 Xohlagan matningizni yuboring yoki Mini App'dan foydalaning!")

@dp.callback_query(F.data == "check_sub")
async def check_sub_handler(callback: types.CallbackQuery, bot: Bot):
    unsubscribed = await check_user_subscriptions(bot, callback.from_user.id)
    if not unsubscribed:
        try: await callback.message.delete() except: pass
        await callback.message.answer("🎉 Tasdiqlandi!", reply_markup=Keyboards.get_user_main())
    else:
        await callback.answer("❌ Hali hamma kanalga obuna bo'lmadingiz!", show_alert=True)

# Asosiy AI Generator funksiyasi
async def process_image_generation(message: types.Message, user_prompt: str):
    if AIService.is_nsfw(user_prompt):
        await message.answer("⚠️ Taqiqlangan so'z aniqlandi!")
        return

    async with ChatActionSender.upload_photo(bot=data_bot, chat_id=message.chat.id):
        status_msg = await message.answer("🎨 *AI rasm chizmoqda...*", parse_mode="Markdown")
        en_prompt = await AIService.translate_to_english(user_prompt)
        img_bytes, engine = await AIService.generate_image(en_prompt)

        if not img_bytes:
            await status_msg.edit_text("❌ Xatolik yuz berdi.")
            return

        sticker_bytes = AIService.create_sticker(img_bytes)
        img_bytes.seek(0)

        await message.answer_photo(BufferedInputFile(img_bytes.read(), filename="img.png"), caption=f"🖼 `{user_prompt}`", parse_mode="Markdown")
        await message.answer_sticker(BufferedInputFile(sticker_bytes.read(), filename="st.webp"))
        
        await Database.increment_generation(message.from_user.id)
        await status_msg.delete()

@dp.message(F.text & ~F.text.startswith("/"))
async def text_handler(message: types.Message):
    await process_image_generation(message, message.text.strip())


# ==============================================================================
# 8. ISHGA TUSHIRISH
# ==============================================================================

async def main():
    global data_bot
    data_bot = Bot(token=BOT_TOKEN)
    await Database.init_db()
    asyncio.create_task(start_web_server())
    asyncio.create_task(self_ping_loop())
    logger.info("🚀 Bot ishga tushdi!")
    try:
        await dp.start_polling(data_bot)
    finally:
        await data_bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
