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
# 1. KONFIGURATSIYA VA O'ZGARUVCHILAR
# ==============================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8856867256:AAGxdKm-7d6cjFet5hnk2OD5Lu5h6T_7Tvk")
ADMINS_RAW = os.getenv("ADMINS", "8694110588")
ADMINS = [int(admin_id) for admin_id in ADMINS_RAW.split(",") if admin_id.strip().isdigit()]

# Majburiy obuna kanallari ro'yxati
CHANNELS = [
    {
        "id": -1001234567890,           # Telegram kanal ID (-100 bilan boshlanadi)
        "url": "https://t.me/kanal1",  # Kanal havolasi
        "title": "📢 1-Asosiy Kanal"
    }
]

DATABASE_PATH = "bot_data.db"

# Kesh tizimi (Kanallarga obunani 2 daqiqa keshlaydi)
sub_cache = TTLCache(maxsize=20000, ttl=120)

# Anti-spam kesh (Foydalanuvchi har 5 sekundda 1 marta rasm so'ray oladi)
user_cooldown = TTLCache(maxsize=20000, ttl=5)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ProductionAIBot")


# ==============================================================================
# 2. MA'LUMOTLAR BAZASI BILAN ISHLASH (SQLITE ASYNC)
# ==============================================================================

class Database:
    @staticmethod
    async def init_db():
        """Baza jadvallarini yaratish va optimallashtirish"""
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
            await db.execute("CREATE INDEX IF NOT EXISTS idx_generations ON users(generations_count DESC);")
            await db.commit()

    @staticmethod
    async def add_user(user_id: int, full_name: str, username: str):
        """Yangi foydalanuvchi qo'shish yoki uning ma'lumotlarini yangilash"""
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
        """Generatsiyalar sonini oshirish"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "UPDATE users SET generations_count = generations_count + 1 WHERE user_id = ?",
                (user_id,)
            )
            await db.commit()

    @staticmethod
    async def set_user_active(user_id: int, is_active: bool):
        """Foydalanuvchi botni bloklaganini belgilash"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "UPDATE users SET is_active = ? WHERE user_id = ?",
                (1 if is_active else 0, user_id)
            )
            await db.commit()

    @staticmethod
    async def get_top_users(limit: int = 10) -> List[Tuple[str, int]]:
        """Eng faol foydalanuvchilar ro'yxati"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute(
                "SELECT full_name, generations_count FROM users WHERE is_active = 1 ORDER BY generations_count DESC LIMIT ?",
                (limit,)
            ) as cursor:
                return await cursor.fetchall()

    @staticmethod
    async def get_user_stats(user_id: int) -> int:
        """Foydalanuvchi statistikasini olish"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute("SELECT generations_count FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    @staticmethod
    async def get_all_user_ids() -> List[int]:
        """Barcha aktiv foydalanuvchilar ID ro'yxati"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute("SELECT user_id FROM users WHERE is_active = 1") as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows]

    @staticmethod
    async def get_system_stats() -> Tuple[int, int, int]:
        """Tizim umumiy statistikasi (Jami, Aktiv, Generatsiyalar)"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute("SELECT COUNT(*), SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), SUM(generations_count) FROM users") as cursor:
                row = await cursor.fetchone()
                total = row[0] or 0
                active = row[1] or 0
                generations = row[2] or 0
                return total, active, generations


# ==============================================================================
# 3. INTERFEYS VA TUGMALAR (KEYBOARDS)
# ==============================================================================

class Keyboards:
    @staticmethod
    def get_user_main() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🎨 Rasm Yaratish"), KeyboardButton(text="💡 Tasodifiy G'oya")],
                [KeyboardButton(text="🏆 TOP-10 Liderlar"), KeyboardButton(text="📊 Mening Statistikam")],
                [KeyboardButton(text="ℹ️ Bot Haqida")]
            ],
            resize_keyboard=True
        )

    @staticmethod
    def get_admin_main() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📈 Umumiy Statistika"), KeyboardButton(text="📢 Reklama Tarqatish")],
                [KeyboardButton(text="💾 Bazani Yuklab Olish"), KeyboardButton(text="⬅️ Foydalanuvchi Rejimiga O'tish")]
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
# 4. MIDDLEWARES (OBUNA FILTR VA ANTI-SPAM)
# ==============================================================================

class MultiChannelSubMiddleware(types.TelegramObject):
    """Kanallarga majburiy obuna filtratori"""
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

        if sub_cache.get(user.id) is True:
            return await handler(event, data)

        unsubscribed_channels = []

        for ch in CHANNELS:
            try:
                member = await bot.get_chat_member(chat_id=ch["id"], user_id=user.id)
                if member.status not in ["creator", "administrator", "member"]:
                    unsubscribed_channels.append(ch)
            except Exception as e:
                logger.warning(f"Kanal a'zoligini tekshirishda xatolik: {ch['id']} - {e}")

        if not unsubscribed_channels:
            sub_cache[user.id] = True
            return await handler(event, data)

        keyboard = []
        for ch in unsubscribed_channels:
            keyboard.append([InlineKeyboardButton(text=ch["title"], url=ch["url"])])
        
        keyboard.append([InlineKeyboardButton(text="✅ Obunani Tekshirish", callback_data="check_sub")])

        kb = InlineKeyboardMarkup(inline_keyboard=keyboard)
        text = "🎁 **Botdan BEPUL foydalanish uchun quyidagi kanallarga obuna bo'ling:**"

        if isinstance(event, Message):
            await event.answer(text, reply_markup=kb, parse_mode="Markdown")
        elif isinstance(event, CallbackQuery):
            if event.data == "check_sub":
                sub_cache.pop(user.id, None)
                await event.answer("🔄 Obuna qayta tekshirilmoqda...", show_alert=False)
                return await handler(event, data)
            await event.message.answer(text, reply_markup=kb, parse_mode="Markdown")
            await event.answer()
        return


class AntiSpamMiddleware(types.TelegramObject):
    """Foydalanuvchilarning ketma-ket spamingining oldini olish"""
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
                await event.answer("⏳ **Iltimos, biroz kutib turing!** So'rovingiz qayta ishlanmoqda (5s cooldown).", parse_mode="Markdown")
                return
            user_cooldown[user.id] = True

        return await handler(event, data)


# ================== 5. XIZMATLAR (AI, TRANSLATE & STICKER PROCESSOR) ==================

class AIService:
    @staticmethod
    async def translate_to_english(text: str) -> str:
        """O'zbek matnini avtomatik ingliz tiliga o'girish"""
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
        """3 Bosqichli zaxira tizimi orqali AI rasm generatsiyasi"""
        encoded_prompt = urllib.parse.quote(prompt)
        
        # 1-Bosqich: Pollinations AI (HD Prompt Engine)
        url1 = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&seed={int(time.time())}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url1, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 5000:
                            return BytesIO(data), "Pollinations AI v3"
        except Exception as e:
            logger.warning(f"1-API ishlamadi: {e}")

        # 2-Bosqich: Lexica API (Zaxira)
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
                                    return BytesIO(await img_resp.read()), "Lexica AI Engine"
        except Exception as e:
            logger.warning(f"2-API ishlamadi: {e}")

        # 3-Bosqich: Unsplash Direct Engine (Eng oxirgi zaxira)
        url3 = f"https://source.unsplash.com/1024x1024/?{encoded_prompt}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url3, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return BytesIO(await resp.read()), "Unsplash Engine"
        except Exception as e:
            logger.error(f"3-API ham ishlamadi: {e}")

        return None, "Xatolik"

    @staticmethod
    def create_sticker(image_bytes: BytesIO) -> BytesIO:
        """Rasmni stiker formatiga (WEBP 512x512) convert qilish"""
        image_bytes.seek(0)
        img = Image.open(image_bytes).convert("RGBA")
        img.thumbnail((512, 512))
        
        output = BytesIO()
        img.save(output, format="WEBP", quality=90)
        output.seek(0)
        return output


# ==============================================================================
# 6. HANDLERLAR VA FSM
# ==============================================================================

class AdminState(StatesGroup):
    waiting_for_broadcast = State()

dp = Dispatcher(storage=MemoryStorage())

# Middlewares bog'lash
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
        "✨ **Mutlaqo Bepul Professional AI Botiga Xush Kelibsiz!**\n\n"
        "Manga istalgan matningizni yuboring (Masalan: *Dengiz bo'yidagi koinot kemasi*), men unga mos **HD Rasm** hamda **Stiker** tayyorlab beraman!",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.message(Command("admin"), F.from_user.id.in_(ADMINS))
async def admin_panel(message: types.Message):
    await message.answer("👑 **Admin Panelga Xush Kelibsiz!**", reply_markup=Keyboards.get_admin_main())

@dp.message(F.text == "⬅️ Foydalanuvchi Rejimiga O'tish")
async def back_to_user_mode(message: types.Message):
    await message.answer("👤 Foydalanuvchi rejimiga o'tdingiz.", reply_markup=Keyboards.get_user_main())

@dp.message(F.text == "📈 Umumiy Statistika", F.from_user.id.in_(ADMINS))
async def admin_stats(message: types.Message):
    total, active, generations = await Database.get_system_stats()
    text = (
        f"📊 **BOTNING UMUMIY STATISTIKASI:**\n\n"
        f"👥 Jami Obunachilar: `{total}` ta\n"
        f"🟢 Aktiv Foydalanuvchilar: `{active}` ta\n"
        f"🎨 Yaratilgan Rasmlar: `{generations}` ta"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "📢 Reklama Tarqatish", F.from_user.id.in_(ADMINS))
async def start_broadcast(message: types.Message, state: FSMContext):
    await state.set_state(AdminState.waiting_for_broadcast)
    await message.answer("📢 **Reklama postini yuboring (Rasm, Matn, Video va b.):**", reply_markup=Keyboards.get_cancel())

@dp.message(F.text == "❌ Bekor Qilish", StateFilter(AdminState.waiting_for_broadcast))
async def cancel_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Reklama bekor qilindi.", reply_markup=Keyboards.get_admin_main())

@dp.message(StateFilter(AdminState.waiting_for_broadcast), F.from_user.id.in_(ADMINS))
async def process_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    users = await Database.get_all_user_ids()
    await message.answer(f"⏳ `{len(users)}` ta foydalanuvchiga reklama yuborish boshlandi...", parse_mode="Markdown")

    success, failed = 0, 0
    for uid in users:
        try:
            await message.copy_to(chat_id=uid)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
            await Database.set_user_active(uid, False)

    await message.answer(
        f"✅ **Reklama yakunlandi!**\n\n🟢 Muvaffaqiyatli: `{success}`\n🔴 Yetib bormadi (Bloklangan): `{failed}`",
        reply_markup=Keyboards.get_admin_main(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "💾 Bazani Yuklab Olish", F.from_user.id.in_(ADMINS))
async def download_db(message: types.Message):
    if os.path.exists(DATABASE_PATH):
        db_file = FSInputFile(DATABASE_PATH)
        await message.answer_document(document=db_file, caption="💾 **Ma'lumotlar bazasining zaxira fayli.**")
    else:
        await message.answer("❌ Baza fayli topilmadi.")

@dp.message(F.text == "🏆 TOP-10 Liderlar")
async def show_top(message: types.Message):
    top_users = await Database.get_top_users(10)
    text = "🏆 **Eng Faol TOP-10 Foydalanuvchilar:**\n\n"
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    for idx, (name, count) in enumerate(top_users):
        medal = medals[idx] if idx < len(medals) else "👤"
        safe_name = name.replace("<", "").replace(">", "")
        text += f"{medal} **{safe_name}** — `{count}` ta rasm\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "📊 Mening Statistikam")
async def user_stats(message: types.Message):
    count = await Database.get_user_stats(message.from_user.id)
    await message.answer(f"👤 **Siz yaratgan AI rasmlar soni:** `{count}` ta", parse_mode="Markdown")

@dp.message(F.text == "💡 Tasodifiy G'oya")
async def random_idea(message: types.Message):
    ideas = [
        "Cyberpunk uslubidagi neon chiroqlar ostidagi toshkent shahri",
        "Koinotda suzib yurgan sehrli shisha saroy, HD 8k",
        "O'zbekiston tog'larida joylashgan kelajak texnologiyalar shahri",
        "Qadimgi samarqand poytaxti, fantaziya va sehrli uslubda"
    ]
    import random
    idea = random.choice(ideas)
    await message.answer(f"💡 **G'oya:** `{idea}`\n\nUshbu matnni nusxalab botga yuboring!", parse_mode="Markdown")

@dp.message(F.text == "🎨 Rasm Yaratish")
@dp.message(F.text == "ℹ️ Bot Haqida")
async def info_handler(message: types.Message):
    await message.answer(
        "🎨 **AI Rasm va Stiker olish usuli:**\n\n"
        "Shunchaki xohlagan tasviringizni matn ko'rinishida yuboring va bot sizga mos HD Rasm va Stiker yaratib beradi!",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "check_sub")
async def check_sub_handler(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.message.answer("🎉 **Rahmat! Obuna tasdiqlandi.** Botdan bemalol foydalanishingiz mumkin.", reply_markup=Keyboards.get_user_main())

@dp.message(F.text & ~F.text.startswith("/"))
async def generate_ai_handler(message: types.Message):
    user_prompt = message.text.strip()
    
    async with ChatActionSender.upload_photo(bot=data_bot, chat_id=message.chat.id):
        status_msg = await message.answer("🎨 *AI rasmingizni chizmoqda... Biroz kutib turing.*", parse_mode="Markdown")
        
        # O'zbek tilidan Ingliz tiliga o'girish
        en_prompt = await AIService.translate_to_english(user_prompt)
        
        # AI Orqali rasm yaratish
        img_bytes, engine_name = await AIService.generate_image(en_prompt)

        if not img_bytes:
            await status_msg.edit_text("❌ Serverlar hozirda juda band. Iltimos, 1 minutdan so'ng qayta urinib ko'ring.")
            return

        # Stiker yaratish
        sticker_bytes = AIService.create_sticker(img_bytes)
        img_bytes.seek(0)
        
        bot_info = await data_bot.get_me()
        
        await message.answer_photo(
            photo=BufferedInputFile(img_bytes.read(), filename="ai_image.png"),
            caption=f"🖼 **So'rovingiz:** `{user_prompt}`\n⚙️ **Engine:** `{engine_name}`\n\n✨ Botimiz: @{bot_info.username}",
            parse_mode="Markdown"
        )
        await message.answer_sticker(sticker=BufferedInputFile(sticker_bytes.read(), filename="ai_sticker.webp"))
        
        await Database.increment_generation(message.from_user.id)
        await status_msg.delete()


# ==============================================================================
# 7. BOTNI ISHGA TUSHIRISH (MAIN ENTRYPOINT)
# ==============================================================================

async def main():
    global data_bot
    data_bot = Bot(token=BOT_TOKEN)
    
    await Database.init_db()
    logger.info("🚀 Production Bot Muvaffaqiyatli Ishga Tushdi!")
    
    try:
        await dp.start_polling(data_bot)
    finally:
        await data_bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi!")
