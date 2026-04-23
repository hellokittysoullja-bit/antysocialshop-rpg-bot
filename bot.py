import asyncio
import logging
import os
import random
import time
from datetime import datetime, timedelta, date
from threading import Thread
from functools import wraps

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

import aiosqlite
from cachetools import TTLCache

# === ВЕБ-СЕРВЕР ДЛЯ RENDER ===
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Antysocialshop RPG Bot is alive!"

def run_web_server():
    port = int(os.getenv("PORT", 10000))
    web_app.run(host='0.0.0.0', port=port)

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ===
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
FARM_COOLDOWN_HOURS = 0.5
FARM_MIN = 5
FARM_MAX = 15
HAPPY_HOUR_MULTIPLIER = 2
HAPPY_HOUR_DURATION_MIN = 30

# === КЭШ ИГРОКОВ (быстрый доступ) ===
player_cache = TTLCache(maxsize=500, ttl=30)  # 30 секунд

def invalidate_cache(user_id):
    player_cache.pop(user_id, None)

# === АСИНХРОННАЯ ИНИЦИАЛИЗАЦИЯ БД ===
async def init_db():
    async with aiosqlite.connect('players.db') as db:
        await db.execute('PRAGMA journal_mode=WAL;')
        await db.execute('''CREATE TABLE IF NOT EXISTS players
                     (user_id INTEGER PRIMARY KEY,
                      username TEXT,
                      balance INTEGER DEFAULT 0,
                      blunts INTEGER DEFAULT 0,
                      guild TEXT DEFAULT NULL,
                      last_farm TIMESTAMP,
                      last_ritual TIMESTAMP,
                      last_daily TIMESTAMP,
                      titles TEXT DEFAULT '',
                      last_farm_date DATE,
                      passive_level INTEGER DEFAULT 0,
                      passive_collected TIMESTAMP,
                      karma INTEGER DEFAULT 0)''')
        # Миграции
        cur = await db.execute("PRAGMA table_info(players)")
        columns = [row[1] for row in await cur.fetchall()]
        if 'last_farm_date' not in columns:
            await db.execute('ALTER TABLE players ADD COLUMN last_farm_date DATE')
        if 'passive_level' not in columns:
            await db.execute('ALTER TABLE players ADD COLUMN passive_level INTEGER DEFAULT 0')
        if 'passive_collected' not in columns:
            await db.execute('ALTER TABLE players ADD COLUMN passive_collected TIMESTAMP')
        if 'karma' not in columns:
            await db.execute('ALTER TABLE players ADD COLUMN karma INTEGER DEFAULT 0')

        # Индексы
        await db.execute('CREATE INDEX IF NOT EXISTS idx_balance ON players(balance DESC)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_last_farm ON players(last_farm)')

        # Резервы
        await db.execute('''CREATE TABLE IF NOT EXISTS reservations
                     (art TEXT PRIMARY KEY,
                      user_id INTEGER,
                      username TEXT,
                      expires_at TIMESTAMP)''')
        await db.commit()

# === ФУНКЦИИ РАБОТЫ С БД (асинхронные) ===
async def get_player(user_id):
    async with aiosqlite.connect('players.db') as db:
        async with db.execute('''SELECT balance, blunts, guild, last_farm, last_ritual, last_daily,
                                        titles, last_farm_date, passive_level, passive_collected, karma
                                 FROM players WHERE user_id=?''', (user_id,)) as cursor:
            row = await cursor.fetchone()
    return row

async def get_player_cached(user_id):
    if user_id in player_cache:
        return player_cache[user_id]
    player = await get_player(user_id)
    if player:
        player_cache[user_id] = player
    return player

async def update_balance(user_id, username, amount):
    async with aiosqlite.connect('players.db') as db:
        await db.execute('INSERT OR IGNORE INTO players (user_id, username, balance, blunts) VALUES (?, ?, 0, 0)', (user_id, username))
        await db.execute('UPDATE players SET balance = balance + ?, username = ? WHERE user_id = ?', (amount, username, user_id))
        await db.commit()
    invalidate_cache(user_id)

async def update_blunts(user_id, username, amount):
    async with aiosqlite.connect('players.db') as db:
        await db.execute('INSERT OR IGNORE INTO players (user_id, username, balance, blunts) VALUES (?, ?, 0, 0)', (user_id, username))
        await db.execute('UPDATE players SET blunts = blunts + ?, username = ? WHERE user_id = ?', (amount, username, user_id))
        await db.commit()
    invalidate_cache(user_id)

async def update_last_farm(user_id):
    now = datetime.now()
    today = date.today()
    async with aiosqlite.connect('players.db') as db:
        await db.execute('UPDATE players SET last_farm = ?, last_farm_date = ? WHERE user_id = ?', (now, today, user_id))
        await db.commit()
    invalidate_cache(user_id)

async def update_last_ritual(user_id):
    async with aiosqlite.connect('players.db') as db:
        await db.execute('UPDATE players SET last_ritual = ? WHERE user_id = ?', (datetime.now(), user_id))
        await db.commit()
    invalidate_cache(user_id)

async def update_last_daily(user_id):
    async with aiosqlite.connect('players.db') as db:
        await db.execute('UPDATE players SET last_daily = ? WHERE user_id = ?', (datetime.now(), user_id))
        await db.commit()
    invalidate_cache(user_id)

async def add_title(user_id, title_emoji):
    async with aiosqlite.connect('players.db') as db:
        cursor = await db.execute('SELECT titles FROM players WHERE user_id=?', (user_id,))
        row = await cursor.fetchone()
        titles = row[0] if row and row[0] else ''
        if title_emoji not in titles:
            titles = (titles + ' ' + title_emoji).strip()
            await db.execute('UPDATE players SET titles=? WHERE user_id=?', (titles, user_id))
            await db.commit()
    invalidate_cache(user_id)

async def get_top(limit=10):
    async with aiosqlite.connect('players.db') as db:
        async with db.execute('SELECT username, balance, guild FROM players ORDER BY balance DESC LIMIT ?', (limit,)) as cursor:
            rows = await cursor.fetchall()
    return rows

async def get_guild(user_id):
    player = await get_player_cached(user_id)
    return player[2] if player else None

async def set_guild(user_id, guild_name):
    async with aiosqlite.connect('players.db') as db:
        await db.execute('UPDATE players SET guild=? WHERE user_id=?', (guild_name, user_id))
        await db.commit()
    invalidate_cache(user_id)

async def count_guilds():
    async with aiosqlite.connect('players.db') as db:
        async with db.execute('SELECT guild, COUNT(*) FROM players WHERE guild IS NOT NULL GROUP BY guild') as cursor:
            rows = await cursor.fetchall()
    counts = {'BLACK': 0, 'WHITE': 0}
    for guild, cnt in rows:
        if guild in counts:
            counts[guild] = cnt
    return counts

# === РЕЗЕРВЫ ===
async def add_reservation(art, user_id, username):
    expires = datetime.now() + timedelta(hours=24)
    async with aiosqlite.connect('players.db') as db:
        await db.execute('INSERT OR REPLACE INTO reservations (art, user_id, username, expires_at) VALUES (?, ?, ?, ?)',
                         (art, user_id, username, expires))
        await db.commit()

async def get_reservation(art):
    async with aiosqlite.connect('players.db') as db:
        async with db.execute('SELECT user_id, username, expires_at FROM reservations WHERE art=?', (art,)) as cursor:
            return await cursor.fetchone()

async def remove_expired_reservations():
    async with aiosqlite.connect('players.db') as db:
        await db.execute('DELETE FROM reservations WHERE expires_at < ?', (datetime.now(),))
        await db.commit()

# === КЛАВИАТУРЫ ===
async def get_main_menu_keyboard(user_id):
    keyboard = [
        [InlineKeyboardButton("🍬 Фармить", callback_data='farm')],
        [InlineKeyboardButton("💰 Баланс", callback_data='balance'), InlineKeyboardButton("🌿 Крафт", callback_data='craft')],
        [InlineKeyboardButton("💨 Дунуть", callback_data='smoke')]
    ]
    player = await get_player_cached(user_id)
    if player:
        guild = await get_guild(user_id)
        if guild == 'BLACK':
            keyboard.append([InlineKeyboardButton("🕯️ Ритуал", callback_data='ritual')])
        passive_collected = player[9]
        if passive_collected:
            if isinstance(passive_collected, str):
                last = datetime.fromisoformat(passive_collected)
            else:
                last = passive_collected
            hours = (datetime.now() - last).total_seconds() / 3600
            if hours >= 1:
                keyboard.append([InlineKeyboardButton("🪴 Собрать", callback_data='collect')])
    keyboard.extend([
        [InlineKeyboardButton("📊 Статус", callback_data='status'), InlineKeyboardButton("🏆 Топ", callback_data='top')],
        [InlineKeyboardButton("🕋 Гильдии", callback_data='guild_info'), InlineKeyboardButton("📜 Законы", callback_data='rules')],
        [InlineKeyboardButton("🪪 Скидка", callback_data='privilege'), InlineKeyboardButton("📦 Каталог", callback_data='catalog')],
        [InlineKeyboardButton("🎡 Колесо", callback_data='daily'), InlineKeyboardButton("⚡ Ускорение", callback_data='rush_help')]
    ])
    return InlineKeyboardMarkup(keyboard)

def get_back_to_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 Меню", callback_data='menu')]])

# === ДЕКОРАТОРЫ ===
def rate_limit(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id
        now = time.time()
        last = context.user_data.get('last_callback_time', 0)
        if now - last < 0.5:
            await update.callback_query.answer("Слишком быстро! Подожди немного.")
            return
        context.user_data['last_callback_time'] = now
        return await func(update, context, *args, **kwargs)
    return wrapper

def timing(f):
    @wraps(f)
    async def wrapper(*args, **kwargs):
        start = time.monotonic()
        result = await f(*args, **kwargs)
        logger.info(f"{f.__name__} took {time.monotonic() - start:.3f}s")
        return result
    return wrapper

# === ХЕНДЛЕРЫ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = await get_player_cached(user_id)

    if context.args and context.args[0] == 'activate':
        if not player:
            await update_balance(user_id, username, 0)
            await update_blunts(user_id, username, 0)
            await update_balance(user_id, username, 100)
            bonus_msg = "🎁 Смотритель дарует тебе 100 🍬.\n\n"
        else:
            bonus_msg = ""
        welcome_text = ("🎉 *Добро пожаловать в Гильдию antysocialshop!*\n\n"
                        "▸ _Смотритель приветствует тебя._\n"
                        "▸ _Здесь добываются редкие экземпляры, зарабатывают Очки Антисошл (🍬), курят бланты и вступают в гильдии._\n\n"
                        "🕯️ *ЧЁРНАЯ ГИЛЬДИЯ* — стабильность, ритуалы, власть.\n"
                        "⚜️ *БЕЛАЯ ГИЛЬДИЯ* — азарт, удача, танец на лезвии.\n\n"
                        "▸ _Выбери свой путь:_")
        full_text = bonus_msg + welcome_text
        guild = await get_guild(user_id)
        if not guild:
            guild_text = "\n🕋 Прежде чем начать, выбери свою Гильдию:"
            guild_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🕯️ Чёрная", callback_data='guild_join_BLACK'),
                 InlineKeyboardButton("⚜️ Белая", callback_data='guild_join_WHITE')]
            ])
            await update.message.reply_text(full_text + guild_text, reply_markup=guild_kb, parse_mode='Markdown')
        else:
            await update.message.reply_text(full_text, reply_markup=await get_main_menu_keyboard(user_id), parse_mode='Markdown')
        return

    if not player:
        await update_balance(user_id, username, 0)
        await update_blunts(user_id, username, 0)
        activation_text = ("👁‍🗨 *Смотритель заметил тебя.*\n"
                          "🪄 *Ткань реальности ждёт твоего шага.*\n"
                          "🎁 Нажми, чтобы получить 100 🍬 и войти в 🔒 закрытый сектор.")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ АКТИВИРОВАТЬ ТЕРМИНАЛ", callback_data='activate_menu')]])
        await update.message.reply_text(activation_text, reply_markup=keyboard, parse_mode='Markdown')
        return

    guild = await get_guild(user_id)
    welcome_back = "⚔️ *С возвращением в Гильдию!*\n\n"
    if guild == 'BLACK':
        welcome_back += "🕯️ Ты состоишь в *Чёрной Гильдии*.\n"
    elif guild == 'WHITE':
        welcome_back += "⚜️ Ты состоишь в *Белой Гильдии*.\n"
    else:
        welcome_back += "Ты пока не в Гильдии. Вступи, чтобы получить бонусы.\n"
    welcome_back += "\n🎮 *Твой терминал:*"
    await update.message.reply_text(welcome_back, reply_markup=await get_main_menu_keyboard(user_id), parse_mode='Markdown')

@timing
@rate_limit
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    try:
        if data == 'menu':
            await query.message.edit_text("🎮 *ГЛАВНОЕ МЕНЮ*", reply_markup=await get_main_menu_keyboard(user_id), parse_mode='Markdown')
        elif data == 'farm':
            await farm_callback(update, context)
        elif data == 'balance':
            await balance_callback(update, context)
        elif data == 'craft':
            await craft_callback(update, context)
        elif data == 'smoke':
            await smoke_callback(update, context)
        elif data == 'ritual':
            await ritual_callback(update, context)
        elif data == 'collect':
            await collect_callback(update, context)
        elif data == 'status':
            await status_callback(update, context)
        elif data == 'top':
            await top_callback(update, context)
        elif data == 'guild_info':
            await guild_info_callback(update, context)
        elif data == 'rules':
            await rules_callback(update, context)
        elif data == 'privilege':
            await privilege_callback(update, context)
        elif data == 'catalog':
            await catalog_callback(update, context)
        elif data == 'daily':
            await daily_callback(update, context)
        elif data == 'activate_menu':
            # обработка активации из кнопки
            user = query.from_user
            username = user.username or user.first_name
            player = await get_player_cached(user_id)
            if not player:
                await update_balance(user_id, username, 0)
                await update_blunts(user_id, username, 0)
                await update_balance(user_id, username, 100)
                bonus_msg = "🎁 Смотритель дарует тебе 100 🍬.\n\n"
            else:
                bonus_msg = ""
            welcome_text = ("🎉 *Добро пожаловать в Гильдию antysocialshop!*\n\n"
                            "▸ _Смотритель приветствует тебя._\n"
                            "▸ _Здесь добываются редкие экземпляры, зарабатывают Очки Антисошл (🍬), курят бланты и вступают в гильдии._\n\n"
                            "🕯️ *ЧЁРНАЯ ГИЛЬДИЯ* — стабильность, ритуалы, власть.\n"
                            "⚜️ *БЕЛАЯ ГИЛЬДИЯ* — азарт, удача, танец на лезвии.\n\n"
                            "▸ _Выбери свой путь:_")
            full_text = bonus_msg + welcome_text
            guild = await get_guild(user_id)
            if not guild:
                guild_text = "\n🕋 Прежде чем начать, выбери свою Гильдию:"
                guild_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🕯️ Чёрная", callback_data='guild_join_BLACK'),
                     InlineKeyboardButton("⚜️ Белая", callback_data='guild_join_WHITE')]
                ])
                await query.message.edit_text(full_text + guild_text, reply_markup=guild_kb, parse_mode='Markdown')
            else:
                await query.message.edit_text(full_text, reply_markup=await get_main_menu_keyboard(user_id), parse_mode='Markdown')
        elif data == 'guild_join_BLACK':
            await set_guild(user_id, 'BLACK')
            await query.message.edit_text("✅ Ты вступил в Гильдию 🕯️ *Чёрная*", parse_mode='Markdown')
        elif data == 'guild_join_WHITE':
            await set_guild(user_id, 'WHITE')
            await query.message.edit_text("✅ Ты вступил в Гильдию ⚜️ *Белая*", parse_mode='Markdown')
        elif data == 'rush_help':
            await query.message.reply_text("Используй /rush для сброса кулдауна фарма (тратит 1 🌿).")
        else:
            await query.message.edit_text("❓ Неизвестная команда.")
    except Exception as e:
        logger.error(f"Error in button_handler: {e}", exc_info=True)
        await query.message.edit_text("⚠️ Произошла ошибка. Попробуй позже.")

# === КОЛБЭКИ ===
async def farm_callback(update, context):
    user = update.callback_query.from_user
    msg = update.callback_query.message
    user_id = user.id
    username = user.username or user.first_name
    player = await get_player_cached(user_id)

    if player:
        balance, blunts, guild, last_farm_str, _, _, _, _, _, _, _ = player
        if last_farm_str:
            last_farm = datetime.fromisoformat(last_farm_str)
            if datetime.now() - last_farm < timedelta(hours=FARM_COOLDOWN_HOURS):
                remaining = timedelta(hours=FARM_COOLDOWN_HOURS) - (datetime.now() - last_farm)
                text = f"⏳ Жди {remaining.seconds//60} мин."
                await msg.reply_text(text, reply_markup=get_back_to_menu_keyboard())
                return

    earned = random.randint(FARM_MIN, FARM_MAX)
    if 'happy_hour_active' in context.bot_data and context.bot_data['happy_hour_active']:
        if context.bot_data['happy_hour_end_time'] and datetime.now() < context.bot_data['happy_hour_end_time']:
            earned *= HAPPY_HOUR_MULTIPLIER
    if random.randint(1, 100) == 1:
        earned *= 5
        await context.bot.send_message(chat_id="@guild_antysocial", text=f"🌟 @{username} наткнулся на *Золотую жилу*! +{earned} 🍬", parse_mode='Markdown')
    first_bonus = 0
    if player and player[7] != date.today():
        first_bonus = 10
        earned += first_bonus

    old_balance = player[0] if player else 0
    await update_balance(user_id, username, earned)
    await update_last_farm(user_id)
    new_player = await get_player_cached(user_id)  # кэш обновится
    new_balance = new_player[0]

    if new_balance < 500: progress = f"📈 до ⚔️ {500 - new_balance} 🍬"
    elif new_balance < 2000: progress = f"📈 до 👻 {2000 - new_balance} 🍬"
    else: progress = "👑 Максимальный ранг"

    bonus_str = f" (+{first_bonus}🎁)" if first_bonus else ""
    text = f"🍬 +{earned}{bonus_str} → 💰 {new_balance}\n{progress}"
    await msg.reply_text(text, reply_markup=get_back_to_menu_keyboard())
    await check_rank_up(context, user_id, username, old_balance, new_balance)

async def balance_callback(update, context):
    user = update.callback_query.from_user
    msg = update.callback_query.message
    user_id = user.id
    player = await get_player_cached(user_id)
    if not player:
        await update_balance(user_id, user.username or user.first_name, 0)
        balance_val, blunts = 0, 0
    else:
        balance_val, blunts = player[0], player[1]
    if balance_val < 500: progress = f"📈 до ⚔️ {500 - balance_val} 🍬"
    elif balance_val < 2000: progress = f"📈 до 👻 {2000 - balance_val} 🍬"
    else: progress = "👑 Максимальный ранг"
    text = f"💰 *БАЛАНС*\n`{balance_val}` 🍬\n🌿 `{blunts}` Бланта\n{progress}"
    await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def craft_callback(update, context):
    user = update.callback_query.from_user
    msg = update.callback_query.message
    user_id = user.id
    username = user.username or user.first_name
    player = await get_player_cached(user_id)
    if not player:
        await update_balance(user_id, username, 0)
        balance_val = 0
    else:
        balance_val = player[0]
    if balance_val < 5:
        await msg.reply_text("🕳️ Пусто. Нужно 5 🍬.", reply_markup=get_back_to_menu_keyboard())
        return
    await update_balance(user_id, username, -5)
    await update_blunts(user_id, username, 1)
    new_player = await get_player_cached(user_id)
    new_balance, new_blunts = new_player[0], new_player[1]
    text = f"🌿 Ты свернул Блант. Пальцы пахнут уважением. → 💰 {new_balance} | 🌿 {new_blunts}"
    await msg.reply_text(text, reply_markup=get_back_to_menu_keyboard())

async def smoke_callback(update, context):
    user = update.callback_query.from_user
    msg = update.callback_query.message
    user_id = user.id
    username = user.username or user.first_name
    player = await get_player_cached(user_id)
    if not player:
        await update_blunts(user_id, username, 0)
        blunts, guild = 0, None
    else:
        blunts, guild = player[1], player[2]
    if blunts < 1:
        await msg.reply_text("🌿 У тебя нет Блантов. Используй /craft", reply_markup=get_back_to_menu_keyboard())
        return
    # бонус гильдии
    if guild == 'WHITE' and random.randint(1, 100) <= 20:
        save_blunt = True
    else:
        save_blunt = False
        await update_blunts(user_id, username, -1)

    r = random.randint(1, 100)
    if r <= 50:
        effect = "💨 *Лёгкий приход*\n[Гул Фабрики №9] «Станки работают в ритме твоего сердца...»\n🍬 +10"
        await update_balance(user_id, username, 10)
    elif r <= 75:
        effect = "💨 *Паранойя...*\n[Зловещий шёпот] «Смотритель наблюдает...»\n✨ Никакого видимого эффекта."
    else:
        effect = "💨 *Плацебо*\n[Тишина] «Дым рассеялся, ничего не изменилось...»"
    new_balance = (await get_player_cached(user_id))[0]
    text = effect + (f"\n💰 Баланс: `{new_balance}` 🍬" if r <= 50 else "")
    if save_blunt:
        text += "\n⚜️ *Белая Гильдия сохранила твой Блант!*"
    await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def ritual_callback(update, context):
    user = update.callback_query.from_user
    msg = update.callback_query.message
    user_id = user.id
    username = user.username or user.first_name
    player = await get_player_cached(user_id)
    if not player:
        await msg.reply_text("🕳️ Ты ещё не активирован. /start")
        return
    balance, blunts, guild, _, last_ritual_str, _, _, _, _, _, _ = player
    if guild != 'BLACK':
        await msg.reply_text("❌ Только Чёрная Гильдия.")
        return
    if last_ritual_str:
        last_ritual = datetime.fromisoformat(last_ritual_str)
        if datetime.now() - last_ritual < timedelta(hours=24):
            remaining = timedelta(hours=24) - (datetime.now() - last_ritual)
            await msg.reply_text(f"⏳ Жди {remaining.seconds//3600} ч.")
            return
    old_balance = balance
    await update_balance(user_id, username, 15)
    await update_last_ritual(user_id)
    new_balance = (await get_player_cached(user_id))[0]
    text = f"🕯️ *РИТУАЛ ЗАВЕРШЁН*\n«Тьма одарила тебя стабильностью.»\n🍬 `+15` → 💰 {new_balance}"
    await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    await check_rank_up(context, user_id, username, old_balance, new_balance)

async def collect_callback(update, context):
    user = update.callback_query.from_user
    msg = update.callback_query.message
    user_id = user.id
    username = user.username or user.first_name
    player = await get_player_cached(user_id)
    if not player:
        await msg.reply_text("🕳️ Ты ещё не активирован. /start")
        return
    passive_level = player[8] or 0
    if passive_level == 0:
        await msg.reply_text("❌ Нет авто‑сборщика.")
        return
    passive_collected = player[9]
    if passive_collected:
        if isinstance(passive_collected, str):
            last = datetime.fromisoformat(passive_collected)
        else:
            last = passive_collected
        hours = (datetime.now() - last).total_seconds() / 3600
        earned = int(hours * 5 * passive_level)
        if earned >= 1:
            await update_balance(user_id, username, earned)
            async with aiosqlite.connect('players.db') as db:
                await db.execute('UPDATE players SET passive_collected=? WHERE user_id=?', (datetime.now(), user_id))
                await db.commit()
            invalidate_cache(user_id)
            new_balance = (await get_player_cached(user_id))[0]
            text = f"🪴 *УРОЖАЙ СОБРАН*\nТвой куст принёс `{earned}` 🍬.\n💰 *Баланс:* `{new_balance}` 🍬"
            await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
        else:
            await msg.reply_text("⏳ Пока нечего собирать.")
    else:
        async with aiosqlite.connect('players.db') as db:
            await db.execute('UPDATE players SET passive_collected=? WHERE user_id=?', (datetime.now(), user_id))
            await db.commit()
        invalidate_cache(user_id)
        await msg.reply_text("⏳ Авто‑сборщик активирован. Заходи через час.")

async def status_callback(update, context):
    user = update.callback_query.from_user
    msg = update.callback_query.message
    user_id = user.id
    username = user.username or user.first_name
    player = await get_player_cached(user_id)
    if not player:
        await update_balance(user_id, username, 0)
        balance_val, blunts, guild, titles, passive_level, karma = 0, 0, None, '', 0, 0
    else:
        balance_val, blunts, guild, _, _, _, titles, _, passive_level, _, karma = player
    rank = "👻 Призрак" if balance_val >= 2000 else "⚔️ Ветеран" if balance_val >= 500 else "💉 Рекрут"
    guild_emoji = " 🕯️" if guild == 'BLACK' else " ⚜️" if guild == 'WHITE' else ""
    text = (f"👤 *{username}*{guild_emoji}\n"
            f"👻 *Ранг:* {rank}\n"
            f"💰 *ОАС:* `{balance_val}` 🍬\n"
            f"🌿 *Бланты:* `{blunts}`\n"
            f"🛡️ *Карма:* `{karma}`\n"
            f"🪴 *Урожай:* `+{passive_level*5 if passive_level else 0}` 🍬 / час\n"
            f"🧬 *Титулы:* {titles if titles else '—'}")
    await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def top_callback(update, context):
    msg = update.callback_query.message
    user_id = update.callback_query.from_user.id
    top_players = await get_top(10)
    if not top_players:
        await msg.reply_text("🏆 Топ пока пуст.")
        return
    text = "🏆 *ТОП-10 ИГРОКОВ*\n\n"
    for i, (name, bal, guild) in enumerate(top_players, 1):
        medal = "🥇" if i==1 else "🥈" if i==2 else "🥉" if i==3 else f"{i}."
        g_emoji = "🕯️" if guild == 'BLACK' else "⚜️" if guild == 'WHITE' else ""
        text += f"{medal} {name} {g_emoji} — `{bal}` 🍬\n"
    async with aiosqlite.connect('players.db') as db:
        async with db.execute('SELECT COUNT(*) FROM players WHERE balance > (SELECT balance FROM players WHERE user_id=?)', (user_id,)) as cursor:
            pos = (await cursor.fetchone())[0] + 1
    text += f"\n📊 *Твоя позиция:* `{pos}`"
    await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def guild_info_callback(update, context):
    counts = await count_guilds()
    text = (f"🕋 *ГИЛЬДИИ*\n\n"
            f"🕯️ Чёрная: `{counts['BLACK']}` странников\n"
            f"⚜️ Белая: `{counts['WHITE']}` странников\n\n"
            f"🕯️ Ритуал: `+15` 🍬 раз в `24` ч.\n"
            f"⚜️ Удача: `20%` сохранить Блант при 💨.")
    await update.callback_query.message.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def rules_callback(update, context):
    text = ("📜 **ЗАКОНЫ ГИЛЬДИИ**\n\n"
            "🍬 Фарми.  💨 Дуй.  🪴 Расти.\n\n"
            "▸ /farm — добыча 🍬 (раз в час)\n"
            "▸ /craft — 5 🍬 = 1 🌿 Блант\n"
            "▸ /smoke   — активация 🌿\n"
            "▸ /daily  — 🎡 Колесо\n"
            "▸ /privilege — твоя скидка\n\n"
            "▸ _Ранг даёт власть._\n"
            "▸ _Гильдия даёт путь._")
    await update.callback_query.message.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def privilege_callback(update, context):
    user_id = update.callback_query.from_user.id
    player = await get_player_cached(user_id)
    if not player:
        await update.callback_query.message.reply_text("🕳️ Ты ещё не активирован. /start")
        return
    balance, guild = player[0], player[2]
    if balance >= 2000:
        rank, divisor, max_percent, target = "👻 Призрак", 10, 0.20, None
    elif balance >= 500:
        rank, divisor, max_percent, target = "⚔️ Ветеран", 15, 0.15, 2000
    else:
        rank, divisor, max_percent, target = "💉 Рекрут", 20, 0.10, 500
    if guild == 'WHITE':
        max_percent = 0.15
        guild_note = "🎲 Шанс 20% не потратить ОАС"
    else:
        guild_note = "🔒 Стабильно"
    text = (f"🪪 *ТВОЯ СКИДКА*\n\n"
            f"{rank} {guild or 'Нет'}\n💰 `{balance}` 🍬\n\n"
            f"💸 Каждые `{divisor}` 🍬 = `1` ₽ скидки\n"
            f"📉 Максимум: `{int(max_percent*100)}%` от цены\n"
            f"{guild_note}\n")
    if target:
        percent = min(100, int(balance / target * 100))
        bar = "🟩" * (percent//10) + "⬛" * (10 - percent//10)
        text += f"\n⚔️ *Прогресс:* {bar} `{percent}%`\n"
        phrase = ("«Ты слышишь шёпот Фабрики...»" if percent<30 else
                  "«Ткань реальности отзывается...»" if percent<70 else
                  "«Смотритель чувствует твоё приближение...»")
        text += f"👁‍🗨 _{phrase}_"
    await update.callback_query.message.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def catalog_callback(update, context):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Перейти", url="https://t.me/antysocialshop")]])
    await update.callback_query.message.reply_text("🕯️ *ANTYSOCIALSHOP · КАТАЛОГ*", parse_mode='Markdown', reply_markup=keyboard)

async def daily_callback(update, context):
    user = update.callback_query.from_user
    msg = update.callback_query.message
    user_id = user.id
    username = user.username or user.first_name
    player = await get_player_cached(user_id)
    if not player:
        await msg.reply_text("🕳️ Ты ещё не активирован. /start")
        return
    last_daily = player[5]
    if last_daily:
        last_daily_dt = datetime.fromisoformat(last_daily)
        if datetime.now() - last_daily_dt < timedelta(hours=24):
            remaining = timedelta(hours=24) - (datetime.now() - last_daily_dt)
            await msg.reply_text(f"⏳ Колесо спит. Жди {remaining.seconds//3600} ч.")
            return
    r = random.randint(1, 100)
    if r <= 40: prize, prize_text = 5, "+5 🍬"; await update_balance(user_id, username, prize)
    elif r <= 65: prize, prize_text = 10, "+10 🍬"; await update_balance(user_id, username, prize)
    elif r <= 80: prize, prize_text = 1, "+1 🌿 Блант"; await update_blunts(user_id, username, prize)
    elif r <= 90: prize, prize_text = 20, "+20 🍬"; await update_balance(user_id, username, prize)
    elif r <= 97: prize, prize_text = 2, "+2 🌿 Бланта"; await update_blunts(user_id, username, prize)
    else:
        prize, prize_text = 50, "🌟 *ДЖЕКПОТ!* +50 🍬"
        await update_balance(user_id, username, prize)
        asyncio.create_task(context.bot.send_message(chat_id="@guild_antysocial", text=f"🌟 @{username} сорвал *Джекпот* (+50 🍬) на 🎡 Колесе! Смотритель доволен.", parse_mode='Markdown'))
    await update_last_daily(user_id)
    new_balance = (await get_player_cached(user_id))[0]
    await msg.reply_text(f"🎡 *КОЛЕСО СМОТРИТЕЛЯ*\n{prize_text} → 💰 {new_balance} 🍬", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

# === ДРУГИЕ КОМАНДЫ ===
async def claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = await get_player_cached(user_id)
    if not player:
        await update.message.reply_text("🕳️ Ты ещё не активирован. /start")
        return
    balance = player[0]
    cost = 100 if balance >= 2000 else 150 if balance >= 500 else 200
    if balance < cost:
        await update.message.reply_text(f"🕳️ Пусто. Нужно {cost} 🍬.")
        return
    try:
        art = context.args[0]
        if not art.startswith('#'): art = '#' + art
    except IndexError:
        await update.message.reply_text("❌ Укажи код: /claim #BAL001")
        return
    existing = await get_reservation(art)
    if existing and existing[2] > datetime.now():
        await update.message.reply_text("❌ Уже застолбили.")
        return
    await update_balance(user_id, username, -cost)
    await add_reservation(art, user_id, username)
    text = (f"🔒 *РЕЗЕРВ* `{art}`\n\n"
            f"⏳ `24` ч на активацию в ЛС.\n"
            f"💸 Списано: `{cost}` 🍬 (вернутся `+10%` 🎁 при активации).")
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    asyncio.create_task(context.bot.send_message(chat_id="@guild_antysocial", text=f"🔒 *[РЕЗЕРВ]* @{username} застолбил {art} на 24 часа.", parse_mode='Markdown'))

async def rush(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = await get_player_cached(user_id)
    if not player:
        await update.message.reply_text("🕳️ Ты ещё не активирован. /start")
        return
    if player[1] < 1:
        await update.message.reply_text("🌿 У тебя нет Блантов. /craft")
        return
    await update_blunts(user_id, username, -1)
    async with aiosqlite.connect('players.db') as db:
        await db.execute('UPDATE players SET last_farm=NULL WHERE user_id=?', (user_id,))
        await db.commit()
    invalidate_cache(user_id)
    await update.message.reply_text("⚡ *УСКОРЕНИЕ*\nКулдаун /farm сброшен.\n-1 🌿 Блант", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect('players.db') as db:
        async with db.execute('SELECT COUNT(*), SUM(balance) FROM players') as cursor:
            total_players, total_oas = await cursor.fetchone()
    text = f"📊 *СТАТИСТИКА*\n\n👥 Игроков: `{total_players or 0}`\n💰 ОАС в системе: `{total_oas or 0}` 🍬"
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def pin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    player = await get_player_cached(user_id)
    if not player or player[0] < 200:
        await update.message.reply_text("🕳️ Пусто. Нужно 200 🍬.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение для закрепа.")
        return
    await update_balance(user_id, update.effective_user.username, -200)
    await update.message.reply_to_message.pin()
    await update.message.reply_text("📌 *ЗАКРЕПЛЕНО*\nСообщение закреплено на `1` час.\n💸 Списано: `200` 🍬", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

# === ВСПОМОГАТЕЛЬНЫЕ ===
async def check_rank_up(context, user_id, username, old_balance, new_balance):
    if old_balance < 500 <= new_balance:
        await context.bot.send_message(chat_id="@guild_antysocial", text=f"🎉 @{username} достиг ранга ⚔️ *Ветеран*! Гильдия рукоплещет.", parse_mode='Markdown')
    if old_balance < 2000 <= new_balance:
        await context.bot.send_message(chat_id="@guild_antysocial", text=f"👻 @{username} стал *Призраком*! Ткань реальности дрожит.", parse_mode='Markdown')

# === ФОНОВЫЕ ЗАДАЧИ ===
async def update_pulse(context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect('players.db') as db:
        async with db.execute('SELECT COUNT(*), SUM(balance) FROM players') as cursor:
            total_players, total_oas = await cursor.fetchone()
        total_players = total_players or 0
        total_oas = total_oas or 0
        async with db.execute('SELECT COUNT(*) FROM players WHERE guild="BLACK"') as cursor:
            black = (await cursor.fetchone())[0] or 0
        async with db.execute('SELECT COUNT(*) FROM players WHERE guild="WHITE"') as cursor:
            white = (await cursor.fetchone())[0] or 0
        async with db.execute("SELECT COUNT(DISTINCT user_id) FROM players WHERE last_farm > ?", (datetime.now() - timedelta(hours=1),)) as cursor:
            online = (await cursor.fetchone())[0] or 0
    total_for_percent = black + white
    if total_for_percent > 0:
        black_percent = int(black / total_for_percent * 100)
        bar = "🕯️" + "▰" * (black_percent//10) + "▱" * (10 - black_percent//10) + f" ⚜️ {black_percent}%"
    else:
        bar = "🕯️▱▱▱▱▱▱▱▱▱▱ ⚜️ 50%"
    chat_desc = f"{bar} | 👥 {online}"
    try:
        await context.bot.set_chat_description(chat_id="@guild_antysocial", description=chat_desc)
    except Exception as e:
        logger.error(f"update_pulse error: {e}")

async def happy_hour_trigger(context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['happy_hour_active'] = True
    context.bot_data['happy_hour_end_time'] = datetime.now() + timedelta(minutes=HAPPY_HOUR_DURATION_MIN)
    await context.bot.send_message(chat_id="@guild_antysocial", text="🌟 *ЧАС УДАЧИ!* Все действия приносят x2 🍬 в течение 30 минут!", parse_mode='Markdown')
    context.job_queue.run_once(reset_happy_hour, HAPPY_HOUR_DURATION_MIN * 60)

async def reset_happy_hour(context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['happy_hour_active'] = False
    await context.bot.send_message(chat_id="@guild_antysocial", text="⏳ Час Удачи завершён.")

# === ЗАПУСК ===
async def main():
    await init_db()
    await remove_expired_reservations()

    web_thread = Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()

    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", lambda u,c: start(u,c)))  # вызов меню
    app.add_handler(CommandHandler("farm", farm_callback))
    app.add_handler(CommandHandler("balance", balance_callback))
    app.add_handler(CommandHandler("craft", craft_callback))
    app.add_handler(CommandHandler("smoke", smoke_callback))
    app.add_handler(CommandHandler("ritual", ritual_callback))
    app.add_handler(CommandHandler("collect", collect_callback))
    app.add_handler(CommandHandler("status", status_callback))
    app.add_handler(CommandHandler("top", top_callback))
    app.add_handler(CommandHandler("rules", rules_callback))
    app.add_handler(CommandHandler("privilege", privilege_callback))
    app.add_handler(CommandHandler("claim", claim))
    app.add_handler(CommandHandler("daily", daily_callback))
    app.add_handler(CommandHandler("proof", proof))
    app.add_handler(CommandHandler("pin", pin_message))
    app.add_handler(CommandHandler("catalog", catalog_callback))
    app.add_handler(CommandHandler("rush", rush))
    app.add_handler(CommandHandler("add", lambda u,c: update.message.reply_text("Команда add временно отключена")))  # админка по желанию

    # Русские команды через MessageHandler (полностью сохранил старую схему)
    app.add_handler(MessageHandler(filters.Regex(r'^/фарм$'), farm_callback))
    app.add_handler(MessageHandler(filters.Regex(r'^/баланс$'), balance_callback))
    app.add_handler(MessageHandler(filters.Regex(r'^/крафт$'), craft_callback))
    app.add_handler(MessageHandler(filters.Regex(r'^/дунуть$'), smoke_callback))
    app.add_handler(MessageHandler(filters.Regex(r'^/ритуал$'), ritual_callback))
    app.add_handler(MessageHandler(filters.Regex(r'^/статус$'), status_callback))
    app.add_handler(MessageHandler(filters.Regex(r'^/топ$'), top_callback))
    app.add_handler(MessageHandler(filters.Regex(r'^/колесо$'), daily_callback))
    app.add_handler(MessageHandler(filters.Regex(r'^/привилегия$'), privilege_callback))
    app.add_handler(MessageHandler(filters.Regex(r'^/забрать(?:\s+(.+))?$'), claim))
    app.add_handler(MessageHandler(filters.Regex(r'^/каталог$'), catalog_callback))
    app.add_handler(MessageHandler(filters.Regex(r'^/ускорение$'), rush))
    app.add_handler(MessageHandler(filters.Regex(r'^/вступить(?:\s+(.+))?$'), lambda u,c: set_guild(u.effective_user.id, c.args[0].upper()) if c.args else None))

    # Короткие текстовые команды
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r'^(фарм|farm|дунуть|smoke|крафт|craft|баланс|balance|колесо|daily|топ|top|статус|status)$'),
                                   lambda u,c: button_handler(u,c)))

    # Приветствие новых
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS,
                                   lambda u,c: u.message.reply_text(f"🕯️ @{(u.message.new_chat_members[0].username or u.message.new_chat_members[0].first_name)}, добро пожаловать в Гильдию. Твой первый /farm уже ждёт.")))

    app.add_handler(CallbackQueryHandler(button_handler))

    # Фон
    app.job_queue.run_repeating(update_pulse, interval=300, first=10)
    app.job_queue.run_repeating(remove_expired_reservations, interval=600, first=30)
    # Час удачи запускаем через случайный интервал (как в оригинале)
    app.job_queue.run_once(
        lambda c: c.job_queue.run_repeating(happy_hour_trigger, interval=random.randint(14400, 28800), first=random.randint(3600, 10800)),
        when=1
    )

    logger.info("Bot polling started")
    await app.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
