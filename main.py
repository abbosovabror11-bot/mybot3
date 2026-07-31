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
from cachetools import TTLCache
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
    TelegramObject
)
from aiogram.utils.chat_action import ChatActionSender

# ==============================================================================
# 1. KONFIGURATSIYA
# ==============================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8856867256:AAGxdKm-7d6cjFet5hnk2OD5Lu5h6T_7Tvk")
ADMINS_RAW = os.getenv("ADMINS", "8694110588")
ADMINS = [int(admin_id) for admin_id in ADMINS_RAW.split(",") if admin_id.strip().isdigit()]

# RENDERDA O'CHIB KETMAYDIGAN KANAL MA'LUMOTLARI (SHU YERGA YOZING YOKI ENV ULANING):
# Format: "KANAL_ID|KANAL_NOMI|LINK"
# Agar 2 ta bo'lsa vergul bilan: "-100123|Kanal1|link1, -100456|Kanal2|link2"
CHANNELS_ENV = os.getenv("CHANNELS", "")

PORT = int(os.getenv("PORT", 8080))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")
DATABASE_PATH = "bot_data.db"

sub_cache = TTLCache(maxsize=20000, ttl=60)
user_cooldown = TTLCache(maxsize=20000, ttl=4)

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
                total = row[0] or 0
                active = row[1] or 0
                generations = row[2] or 0
                return total, active, generations

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
        db_channels = []
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute("SELECT channel_id, title, invite_link FROM channels") as cursor:
                db_channels = await cursor.fetchall()
        
        # Agar ENV faylda kanallar kiritilgan bo'lsa ularni qo'shish
        env_channels = []
        if CHANNELS_ENV:
            try:
                for item in CHANNELS_ENV.split(","):
                    parts = item.strip().split("|")
                    if len(parts) == 3:
                        env_channels.append((int(parts[0]), parts[1], parts[2]))
            except Exception as e:
                logger.error(f"CHANNELS ENV xatosi: {e}")

        # Dublikatlarni tozalash
        all_channels = {ch[0]: ch for ch in (db_channels + env_channels)}
        return list(all_channels.values())


# ==============================================================================
# 3. KEYBOARDS
# ==============================================================================

class Keyboards:
    @staticmethod
    def get_user_main() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🎨 AI Rasm Yaratish 🚀"), KeyboardButton(text="💡 Tasodifiy G'oya ✨")],
                [KeyboardButton(text="🏆 TOP-10 Liderlar ⭐️"), KeyboardButton(text="📊 Mening Statistikam 📈")],
                [KeyboardButton(text="ℹ️ Bot Haqida 🤖")]
            ],
            resize_keyboard=True
        )

    @staticmethod
    def get_admin_main() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📢 Kanallarni Boshqarish ⚙️"), KeyboardButton(text="📊 Umumiy Statistika 📈")],
                [KeyboardButton(text="📣 Reklama Tarqatish 🚀"), KeyboardButton(text="💾 Bazani Yuklab Olish 📥")],
                [KeyboardButton(text="👤 Foydalanuvchi Rejimiga O'tish ⬅️")]
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
# 4. MIDDLEWARES (QAT'IY OBUNA TEKSHIRUVI)
# ==============================================================================

class MultiChannelSubMiddleware(types.TelegramObject):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        bot: Bot = data['bot']
        user: types.User = data.get('event_from_user')

        if not user or user.id in ADMINS:
            return await handler(event, data)

        # "Obunani tekshirish" tugmasiga to'g'ridan-to'g'ri o'tkazish
        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)

        # Keshni tekshirish
        if sub_cache.get(user.id) is True:
            return await handler(event, data)

        channels = await Database.get_channels()
        if not channels:
            return await handler(event, data)

        unsubscribed_channels = []
        for ch_id, title, link in channels:
            try:
                member = await bot.get_chat_member(chat_id=ch_id, user_id=user.id)
                if member.status in ["left", "kicked"]:
                    unsubscribed_channels.append((title, link))
            except Exception as e:
                logger.warning(f"Kanal tekshirishda xatolik: ID: {ch_id} - Xatolik: {e}")

        if not unsubscribed_channels:
            sub_cache[user.id] = True
            return await handler(event, data)

        # Obunasi bo'lmasa xabar va tugmalarni chiqarish
        keyboard = []
        for title, link in unsubscribed_channels:
            keyboard.append([InlineKeyboardButton(text=f"📢 {title}", url=link)])
        
        keyboard.append([InlineKeyboardButton(text="✅ Obunani Tekshirish", callback_data="check_sub")])
        kb = InlineKeyboardMarkup(inline_keyboard=keyboard)
        text = "🎁 **Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:**"

        if isinstance(event, Message):
            await event.answer(text, reply_markup=kb, parse_mode="Markdown")
        elif isinstance(event, CallbackQuery):
            await event.message.answer(text, reply_markup=kb, parse_mode="Markdown")
            await event.answer()
        return


class AntiSpamMiddleware(types.TelegramObject):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user: types.User = data.get('event_from_user')
        if not user or user.id in ADMINS:
            return await handler(event, data)

        if isinstance(event, Message) and event.text and not event.text.startswith("/"):
            if user_cooldown.get(user.id):
                await event.answer("⏳ **Iltimos, biroz kutib turing!** Ketma-ket so'rov yubormang.", parse_mode="Markdown")
                return
            user_cooldown[user.id] = True

        return await handler(event, data)


# ==============================================================================
# 5. AI XIZMATLARI
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
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data[0][0][0]
        except Exception as e:
            logger.error(f"Tarjima xatosi: {e}")
        return text

    @staticmethod
    async def generate_image(prompt: str) -> Tuple[BytesIO | None, str]:
        encoded_prompt = urllib.parse.quote(prompt)
        url1 = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&seed={int(time.time())}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url1, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 5000:
                            return BytesIO(data), "Pollinations AI v3"
        except Exception as e:
            logger.warning(f"1-API xatosi: {e}")

        url2 = f"https://lexica.art/api/v1/search?q={encoded_prompt}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url2, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("images"):
                            img_src = data["images"][0]["src"]
                            async with session.get(img_src) as img_resp:
                                if img_resp.status == 200:
                                    return BytesIO(await img_resp.read()), "Lexica Engine"
        except Exception as e:
            logger.warning(f"2-API xatosi: {e}")

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
# 6. WEB SERVER
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

dp = Dispatcher(storage=MemoryStorage())

sub_mw = MultiChannelSubMiddleware()
anti_spam_mw = AntiSpamMiddleware()

dp.message.outer_middleware(sub_mw)
dp.callback_query.outer_middleware(sub_mw)
dp.message.middleware(anti_spam_mw)

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
        "Manga xohlagan so'zingizni yuboring (Masalan: *Dengiz bo'yidagi kelajak shahri*), men unga mos **HD Rasm** hamda **Stiker** yaratib beraman!",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.message(Command("admin"), F.from_user.id.in_(ADMINS))
async def admin_panel(message: types.Message):
    await message.answer("👑 **Admin Panelga Xush Kelibsiz!**\nQuyidagi tugmalar orqali botni boshqarasiz:", reply_markup=Keyboards.get_admin_main())

@dp.message(F.text == "👤 Foydalanuvchi Rejimiga O'tish ⬅️")
async def back_to_user_mode(message: types.Message):
    await message.answer("👤 Foydalanuvchi rejimiga o'tdingiz.", reply_markup=Keyboards.get_user_main())

# ----------------- ADMIN -----------------

@dp.message(F.text == "📢 Kanallarni Boshqarish ⚙️", F.from_user.id.in_(ADMINS))
async def manage_channels(message: types.Message):
    channels = await Database.get_channels()
    text = "⚙️ **MAJBURIY OBUNA KANALLARI:**\n\n"
    keyboard = []
    
    if channels:
        for ch_id, title, link in channels:
            text += f"🔹 **{title}**\nID: `{ch_id}`\nHavola: {link}\n\n"
            keyboard.append([InlineKeyboardButton(text=f"❌ O'chirish: {title}", callback_data=f"del_ch_{ch_id}")])
    else:
        text += "⚠️ *Hozircha hech qanday kanal ulanmagan.*\n\n"
        
    keyboard.append([InlineKeyboardButton(text="➕ Yangi Kanal Qo'shish", callback_data="add_channel")])
    kb = InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "add_channel", F.from_user.id.in_(ADMINS))
async def start_add_channel(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_channel_data)
    await callback.message.answer(
        "➕ **Yangi kanal qo'shish yo'riqnomasi:**\n\n"
        "1. Botni kanalingizga **ADMIN** qiling.\n"
        "2. Kanal ma'lumotlarini quyidagi formatda yuboring:\n\n"
        "`Kanal_ID | Kanal_Nomi | Taklif_Havolasi`\n\n"
        "📌 **Misol:**\n"
        "`-1001234567890 | Asosiy Kanal | https://t.me/kanal_nomi`",
        parse_mode="Markdown",
        reply_markup=Keyboards.get_cancel()
    )
    await callback.answer()

@dp.message(StateFilter(AdminState.waiting_for_channel_data), F.from_user.id.in_(ADMINS))
async def process_add_channel(message: types.Message, state: FSMContext):
    if message.text == "❌ Bekor Qilish":
        await state.clear()
        await message.answer("❌ Bekor qilindi.", reply_markup=Keyboards.get_admin_main())
        return

    try:
        parts = [p.strip() for p in message.text.split("|")]
        if len(parts) != 3:
            await message.answer("⚠️ Format noto'g'ri. Iltimos: `Kanal_ID | Kanal_Nomi | Taklif_Havolasi` shaklida yuboring.")
            return

        ch_id = int(parts[0])
        title = parts[1]
        link = parts[2]

        await Database.add_channel(ch_id, title, link)
        await state.clear()
        await message.answer(f"✅ **{title}** muvaffaqiyatli saqlandi!", reply_markup=Keyboards.get_admin_main())
    except ValueError:
        await message.answer("⚠️ Kanal ID raqam bo'lishi kerak (Masalan: -1001234567890).")
    except Exception as e:
        await message.answer(f"❌ Xatolik yuz berdi: {e}")

@dp.callback_query(F.data.startswith("del_ch_"), F.from_user.id.in_(ADMINS))
async def delete_channel_handler(callback: types.CallbackQuery):
    ch_id = int(callback.data.split("_")[2])
    await Database.delete_channel(ch_id)
    await callback.answer("✅ Kanal o'chirildi!", show_alert=True)
    await callback.message.delete()

@dp.message(F.text == "📊 Umumiy Statistika 📈", F.from_user.id.in_(ADMINS))
async def admin_stats(message: types.Message):
    total, active, generations = await Database.get_system_stats()
    text = (
        f"📈 **BOTNING UMUMIY STATISTIKASI:**\n\n"
        f"👥 Jami Obunachilar: `{total}` ta\n"
        f"🟢 Aktiv Foydalanuvchilar: `{active}` ta\n"
        f"🎨 Yaratilgan AI Rasmlar: `{generations}` ta"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "📣 Reklama Tarqatish 🚀", F.from_user.id.in_(ADMINS))
async def start_broadcast(message: types.Message, state: FSMContext):
    await state.set_state(AdminState.waiting_for_broadcast)
    await message.answer("📢 **Reklama postini yuboring:**", reply_markup=Keyboards.get_cancel())

@dp.message(F.text == "❌ Bekor Qilish", StateFilter(AdminState.waiting_for_broadcast))
async def cancel_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Reklama bekor qilindi.", reply_markup=Keyboards.get_admin_main())

@dp.message(StateFilter(AdminState.waiting_for_broadcast), F.from_user.id.in_(ADMINS))
async def process_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    users = await Database.get_all_user_ids()
    await message.answer(f"⏳ `{len(users)}` ta foydalanuvchiga reklama yuborilmoqda...", parse_mode="Markdown")

    success, failed = 0, 0
    for uid in users:
        try:
            await message.copy_to(chat_id=uid)
            success += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1
            await Database.set_user_active(uid, False)

    await message.answer(
        f"🚀 **Reklama tarqatildi!**\n\n🟢 Yuborildi: `{success}`\n🔴 Yetib bormadi: `{failed}`",
        reply_markup=Keyboards.get_admin_main(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "💾 Bazani Yuklab Olish 📥", F.from_user.id.in_(ADMINS))
async def download_db(message: types.Message):
    if os.path.exists(DATABASE_PATH):
        db_file = FSInputFile(DATABASE_PATH)
        await message.answer_document(document=db_file, caption="💾 **Ma'lumotlar bazasi zaxira nusxasi.**")
    else:
        await message.answer("❌ Baza topilmadi.")

# ----------------- FOYDALANUVCHI -----------------

@dp.message(F.text == "🏆 TOP-10 Liderlar ⭐️")
async def show_top(message: types.Message):
    top_users = await Database.get_top_users(10)
    text = "⭐️ **Eng Faol TOP-10 AI Ijodkorlar:**\n\n"
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    for idx, (name, count) in enumerate(top_users):
        medal = medals[idx] if idx < len(medals) else "👤"
        safe_name = name.replace("<", "").replace(">", "")
        text += f"{medal} **{safe_name}** — `{count}` ta rasm\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "📊 Mening Statistikam 📈")
async def user_stats(message: types.Message):
    count = await Database.get_user_stats(message.from_user.id)
    await message.answer(f"📊 **Siz yaratgan AI rasmlar soni:** `{count}` ta", parse_mode="Markdown")

@dp.message(F.text == "💡 Tasodifiy G'oya ✨")
async def random_idea(message: types.Message):
    ideas = [
        "Cyberpunk uslubidagi neon chiroqlar ostidagi Toshkent shahri",
        "Koinotda suzib yurgan sehrli shisha saroy, HD 8k render",
        "O'zbekiston tog'larida joylashgan kelajak texnologiyalar shahri",
        "Toshkent metrosida kofe ichib o'tirgan multfilm qahramoni",
        "Sehrli o'rmon ichida yonayotgan kristal shar"
    ]
    import random
    idea = random.choice(ideas)
    await message.answer(f"✨ **G'oya:** `{idea}`\n\nUshbu matnni nusxalab botga yuboring!", parse_mode="Markdown")

@dp.message(F.text == "🎨 AI Rasm Yaratish 🚀")
@dp.message(F.text == "ℹ️ Bot Haqida 🤖")
async def info_handler(message: types.Message):
    await message.answer(
        "🎨 **Rasm va Stiker yaratish yo'riqnomasi:**\n\n"
        "Shunchaki xohlagan tasviringizni matn ko'rinishida yuboring (Masalan: *Balandlikda o'tirgan chiroyli mushuk*) va bot sizga mos **HD Rasm** va **Stiker** yaratib beradi!",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "check_sub")
async def check_sub_handler(callback: types.CallbackQuery, bot: Bot):
    channels = await Database.get_channels()
    unsubscribed_channels = []
    
    for ch_id, title, link in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=callback.from_user.id)
            if member.status in ["left", "kicked"]:
                unsubscribed_channels.append((title, link))
        except Exception:
            pass

    if not unsubscribed_channels:
        sub_cache[callback.from_user.id] = True
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer("🎉 **Obunangiz tasdiqlandi!** Endi istalgan matningizni yuborib HD rasm olishingiz mumkin.", reply_markup=Keyboards.get_user_main())
    else:
        await callback.answer("❌ Siz hali barcha kanallarga obuna bo'lmadingiz!", show_alert=True)

# ----------------- AI GENERATSIYA -----------------

@dp.message(F.text & ~F.text.startswith("/"))
async def generate_ai_handler(message: types.Message):
    user_prompt = message.text.strip()
    
    if user_prompt in [
        "🎨 AI Rasm Yaratish 🚀", "💡 Tasodifiy G'oya ✨", "🏆 TOP-10 Liderlar ⭐️",
        "📊 Mening Statistikam 📈", "ℹ️ Bot Haqida 🤖", "📢 Kanallarni Boshqarish ⚙️",
        "📊 Umumiy Statistika 📈", "📣 Reklama Tarqatish 🚀", "💾 Bazani Yuklab Olish 📥",
        "👤 Foydalanuvchi Rejimiga O'tish ⬅️", "❌ Bekor Qilish"
    ]:
        return

    if AIService.is_nsfw(user_prompt):
        await message.answer("⚠️ **Kechirasiz! Botda nojo'ya va taqiqlangan mazmundagi rasmlarni yaratish cheklangan.**")
        return

    async with ChatActionSender.upload_photo(bot=data_bot, chat_id=message.chat.id):
        status_msg = await message.answer("🎨 *AI rasmingiz va stikeringizni chizmoqda... Biroz kutib turing.*", parse_mode="Markdown")
        
        en_prompt = await AIService.translate_to_english(user_prompt)
        img_bytes, engine_name = await AIService.generate_image(en_prompt)

        if not img_bytes:
            await status_msg.edit_text("❌ Serverlar hozirda band. Iltimos, 1 minutdan so'ng qayta urinib ko'ring.")
            return

        sticker_bytes = AIService.create_sticker(img_bytes)
        img_bytes.seek(0)
        
        bot_info = await data_bot.get_me()
        
        await message.answer_photo(
            photo=BufferedInputFile(img_bytes.read(), filename="ai_image.png"),
            caption=f"🖼 **So'rovingiz:** `{user_prompt}`\n⚙️ **Engine:** `{engine_name}`\n\n✨ Botimiz: @{bot_info.username}",
            parse_mode="Markdown"
        )
        
        await message.answer_sticker(
            sticker=BufferedInputFile(sticker_bytes.read(), filename="sticker.webp")
        )
        
        await Database.increment_generation(message.from_user.id)
        await status_msg.delete()


# ==============================================================================
# 8. ISHGA TUSHIRISH
# ==============================================================================

async def main():
    global data_bot
    data_bot = Bot(token=BOT_TOKEN)
    
    await Database.init_db()
    
    asyncio.create_task(start_web_server())
    asyncio.create_task(self_ping_loop())
    
    logger.info("🚀 Bot va Web Server ishga tushdi!")
    
    try:
        await dp.start_polling(data_bot)
    finally:
        await data_bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi!")
