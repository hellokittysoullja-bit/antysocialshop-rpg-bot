# bot.py
import os
import random
import sqlite3
import time
from datetime import datetime, timedelta, date
from threading import Thread
from functools import wraps
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# === ВЕБ-СЕРВЕР ДЛЯ RENDER ===
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Antysocialshop RPG Bot is alive!"

def run_web_server():
    port = int(os.getenv("PORT", 10000))
    web_app.run(host='0.0.0.0', port=port)

# === ДЕКОРАТОР ПОВТОРНЫХ ПОПЫТОК ДЛЯ БД ===
def retry_on_lock(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(3):
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt < 2:
                    time.sleep(0.1)
                else:
                    raise
    return wrapper

# === НАСТРОЙКИ ===
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
FARM_COOLDOWN_HOURS = 0.5
FARM_MIN = 5
FARM_MAX = 15
HAPPY_HOUR_MULTIPLIER = 2
HAPPY_HOUR_DURATION_MIN = 30

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ===
happy_hour_active = False
happy_hour_end_time = None
last_bot_messages = {}
reservations = {}

# === ИНИЦИАЛИЗАЦИЯ БД ===
@retry_on_lock
def init_db():
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('PRAGMA journal_mode=WAL;')
    c.execute('''CREATE TABLE IF NOT EXISTS players
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
    try: c.execute('ALTER TABLE players ADD COLUMN last_farm_date DATE')
    except: pass
    try: c.execute('ALTER TABLE players ADD COLUMN passive_level INTEGER DEFAULT 0')
    except: pass
    try: c.execute('ALTER TABLE players ADD COLUMN passive_collected TIMESTAMP')
    except: pass
    try: c.execute('ALTER TABLE players ADD COLUMN karma INTEGER DEFAULT 0')
    except: pass

    conn.commit()
    conn.close()

def get_player(user_id):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('''SELECT balance, blunts, guild, last_farm, last_ritual, last_daily,
                        titles, last_farm_date, passive_level, passive_collected, karma
                 FROM players WHERE user_id=?''', (user_id,))
    row = c.fetchone()
    conn.close()
    return row

@retry_on_lock
def update_balance(user_id, username, amount):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO players (user_id, username, balance, blunts) VALUES (?, ?, 0, 0)', (user_id, username))
    c.execute('UPDATE players SET balance = balance + ?, username = ? WHERE user_id = ?', (amount, username, user_id))
    conn.commit()
    conn.close()

@retry_on_lock
def update_blunts(user_id, username, amount):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO players (user_id, username, balance, blunts) VALUES (?, ?, 0, 0)', (user_id, username))
    c.execute('UPDATE players SET blunts = blunts + ?, username = ? WHERE user_id = ?', (amount, username, user_id))
    conn.commit()
    conn.close()

@retry_on_lock
def update_last_farm(user_id):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('UPDATE players SET last_farm = ?, last_farm_date = ? WHERE user_id = ?', (datetime.now(), date.today(), user_id))
    conn.commit()
    conn.close()

@retry_on_lock
def update_last_ritual(user_id):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('UPDATE players SET last_ritual = ? WHERE user_id = ?', (datetime.now(), user_id))
    conn.commit()
    conn.close()

@retry_on_lock
def update_last_daily(user_id):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('UPDATE players SET last_daily = ? WHERE user_id = ?', (datetime.now(), user_id))
    conn.commit()
    conn.close()

@retry_on_lock
def add_title(user_id, title_emoji):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT titles FROM players WHERE user_id=?', (user_id,))
    row = c.fetchone()
    titles = row[0] if row and row[0] else ''
    if title_emoji not in titles:
        titles = titles + (' ' + title_emoji).strip()
        c.execute('UPDATE players SET titles=? WHERE user_id=?', (titles, user_id))
    conn.commit()
    conn.close()

def get_top(limit=10):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT username, balance, guild FROM players ORDER BY balance DESC LIMIT ?', (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_guild(user_id):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT guild FROM players WHERE user_id=?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

@retry_on_lock
def set_guild(user_id, guild_name):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('UPDATE players SET guild=? WHERE user_id=?', (guild_name, user_id))
    conn.commit()
    conn.close()

def count_guilds():
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT guild, COUNT(*) FROM players WHERE guild IS NOT NULL GROUP BY guild')
    rows = c.fetchall()
    conn.close()
    counts = {'BLACK': 0, 'WHITE': 0}
    for guild, cnt in rows:
        if guild in counts:
            counts[guild] = cnt
    return counts

def get_guild_bonus(user_id):
    guild = get_guild(user_id)
    if guild == 'BLACK':
        return {'smoke_save_chance': 0, 'ritual_available': True, 'stable_discount': True}
    elif guild == 'WHITE':
        return {'smoke_save_chance': 20, 'ritual_available': False, 'stable_discount': False}
    else:
        return {'smoke_save_chance': 0, 'ritual_available': False, 'stable_discount': True}

def get_main_menu_keyboard(user_id=None):
    keyboard = [
        [InlineKeyboardButton("🍬 Фармить", callback_data='farm')],
        [InlineKeyboardButton("💰 Баланс", callback_data='balance'), InlineKeyboardButton("🌿 Крафт", callback_data='craft')],
        [InlineKeyboardButton("💨 Дунуть", callback_data='smoke')]
    ]
    if user_id:
        player = get_player(user_id)
        if player:
            if get_guild(user_id) == 'BLACK':
                keyboard.append([InlineKeyboardButton("🕯️ Ритуал", callback_data='ritual')])
            passive_collected = player[9]
            if passive_collected:
                last = datetime.fromisoformat(passive_collected) if isinstance(passive_collected, str) else passive_collected
                hours = (datetime.now() - last).total_seconds() / 3600
                if hours >= 1:
                    keyboard.append([InlineKeyboardButton("🪴 Собрать", callback_data='collect')])
    keyboard.extend([
        [InlineKeyboardButton("📊 Статус", callback_data='status'), InlineKeyboardButton("🏆 Топ", callback_data='top')],
        [InlineKeyboardButton("🕋 Гильдии", callback_data='guild_info'), InlineKeyboardButton("📜 Законы", callback_data='rules')],
        [InlineKeyboardButton("🪪 Скидка", callback_data='privilege'), InlineKeyboardButton("📦 Каталог", callback_data='catalog')],
        [InlineKeyboardButton("🎡 Колесо", callback_data='daily'), InlineKeyboardButton("🎲 Ткань Судьбы", callback_data='play')]
    ])
    return InlineKeyboardMarkup(keyboard)

def get_back_to_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 Меню", callback_data='menu')]])

async def send_selfdestruct_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None, parse_mode='Markdown'):
    chat_id = update.effective_chat.id
    if chat_id in last_bot_messages:
        try: await context.bot.delete_message(chat_id, last_bot_messages[chat_id])
        except: pass
    msg = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    last_bot_messages[chat_id] = msg.message_id
    context.job_queue.run_once(delete_message, 15, data={'chat_id': chat_id, 'message_id': msg.message_id})
    return msg

async def delete_message(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    try:
        await context.bot.delete_message(data['chat_id'], data['message_id'])
        if data['chat_id'] in last_bot_messages and last_bot_messages[data['chat_id']] == data['message_id']:
            del last_bot_messages[data['chat_id']]
    except: pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = get_player(user_id)

    if context.args and context.args[0] == 'activate':
        if not player:
            update_balance(user_id, username, 0)
            update_blunts(user_id, username, 0)
            update_balance(user_id, username, 100)
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
        await update.message.reply_text(full_text, reply_markup=get_main_menu_keyboard(user_id), parse_mode='Markdown')
        return

    if not player:
        update_balance(user_id, username, 0)
        update_blunts(user_id, username, 0)
        activation_text = ("👁‍🗨 *Смотритель заметил тебя.*\n"
                          "🪄 *Ткань реальности ждёт твоего шага.*\n"
                          "🎁 Нажми, чтобы получить 100 🍬 и войти в 🔒 закрытый сектор.")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ АКТИВИРОВАТЬ ТЕРМИНАЛ", callback_data='activate_menu')]])
        await update.message.reply_text(activation_text, reply_markup=keyboard, parse_mode='Markdown')
        return

    guild = player[2]
    welcome_back = "⚔️ *С возвращением в Гильдию!*\n\n"
    if guild == 'BLACK': welcome_back += "🕯️ Ты состоишь в *Чёрной Гильдии*.\n"
    elif guild == 'WHITE': welcome_back += "⚜️ Ты состоишь в *Белой Гильдии*.\n"
    else: welcome_back += "Ты пока не в Гильдии. Вступи, чтобы получить бонусы.\n"
    welcome_back += "\n🎮 *Твой терминал:*"
    await update.message.reply_text(welcome_back, reply_markup=get_main_menu_keyboard(user_id), parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try: await query.answer()
    except: pass
    data = query.data
    user_id = query.from_user.id

    try:
        if data == 'menu': await menu(update, context)
        elif data == 'farm': await farm(update, context)
        elif data == 'balance': await balance(update, context)
        elif data == 'craft': await craft(update, context)
        elif data == 'smoke': await smoke(update, context)
        elif data == 'ritual': await ritual(update, context)
        elif data == 'collect': await collect(update, context)
        elif data == 'status': await status(update, context)
        elif data == 'top': await top(update, context)
        elif data == 'guild_info': await guild_info(update, context)
        elif data == 'rules': await rules(update, context)
        elif data == 'privilege': await privilege(update, context)
        elif data == 'catalog': await catalog(update, context)
        elif data == 'daily': await daily(update, context)
        elif data == 'activate_menu':
            player = get_player(user_id)
            username = query.from_user.username or query.from_user.first_name
            if not player:
                update_balance(user_id, username, 0)
                update_blunts(user_id, username, 0)
                update_balance(user_id, username, 100)
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
            await query.message.edit_text(full_text, reply_markup=get_main_menu_keyboard(user_id), parse_mode='Markdown')
        elif data == 'play':
            await play(update, context)
        elif data == 'guild_join_BLACK':
            set_guild(user_id, 'BLACK')
            await query.message.edit_text("✅ Ты вступил в Гильдию 🕯️ *Чёрная*", parse_mode='Markdown')
        elif data == 'guild_join_WHITE':
            set_guild(user_id, 'WHITE')
            await query.message.edit_text("✅ Ты вступил в Гильдию ⚜️ *Белая*", parse_mode='Markdown')
        else:
            await query.message.edit_text("❓ Неизвестная команда.")
    except Exception as e:
        error_text = f"⚠️ Ошибка: {str(e)[:100]}"
        try: await query.message.edit_text(error_text)
        except: await query.message.reply_text(error_text)

async def play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = get_player(user_id)
    if not player:
        await (update.effective_message or update.message).reply_text("🕳️ Ты ещё не активирован. /start")
        return
    riddles = [
        ("Смотритель предлагает выбор: Левая дверь — стабильность, Правая — риск.", "🚪 Левая", "🚪 Правая", 10, 5, 20),
        ("Что выберешь: тёмный угол (стабильно) или свет (азарт)?", "🌑 Угол", "💡 Свет", 8, 4, 18),
        ("Ткань шепчет: 'Безопасность или удача?'", "🔒 Безопасность", "🎲 Удача", 12, 6, 25)
    ]
    riddle, left_text, right_text, safe_reward, risk_min, risk_max = random.choice(riddles)
    context.user_data['play_reward'] = (safe_reward, risk_min, risk_max)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(left_text, callback_data='play_safe'), InlineKeyboardButton(right_text, callback_data='play_risk')]])
    await (update.effective_message or update.message).reply_text(f"🎲 *ТКАНЬ СУДЬБЫ*\n\n{riddle}", reply_markup=keyboard, parse_mode='Markdown')

async def play_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    username = query.from_user.username or query.from_user.first_name
    if 'play_reward' not in context.user_data:
        await query.message.edit_text("🕳️ Судьба ускользнула. Попробуй снова.")
        return
    safe, rmin, rmax = context.user_data.pop('play_reward')
    if data == 'play_safe':
        earned = safe
        update_balance(user_id, username, earned)
        text = f"🔒 Ты выбрал стабильность. +{earned} 🍬"
    else:
        earned = random.randint(rmin, rmax)
        update_balance(user_id, username, earned)
        text = f"🎲 Ты рискнул! +{earned} 🍬"
    new_balance = get_player(user_id)[0]
    await query.message.edit_text(f"{text}\n💰 Баланс: `{new_balance}` 🍬", parse_mode='Markdown')

async def farm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        user = update.callback_query.from_user
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        user = update.effective_user
        msg = update.message
    user_id = user.id
    username = user.username or user.first_name
    if 'player' not in context.user_data: context.user_data['player'] = get_player(user_id)
    player = context.user_data['player']

    if player:
        balance, blunts, guild, last_farm_str, _, _, _, _, _, _, _, _ = player
        if last_farm_str:
            last_farm = datetime.fromisoformat(last_farm_str)
            if datetime.now() - last_farm < timedelta(hours=FARM_COOLDOWN_HOURS):
                remaining = timedelta(hours=FARM_COOLDOWN_HOURS) - (datetime.now() - last_farm)
                text = f"⏳ Жди {remaining.seconds//60} мин."
                if update.effective_chat.type == "private":
                    await msg.reply_text(text, reply_markup=get_back_to_menu_keyboard())
                else:
                    await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())
                return

    earned = random.randint(FARM_MIN, FARM_MAX)
    if happy_hour_active and datetime.now() < happy_hour_end_time:
        earned *= HAPPY_HOUR_MULTIPLIER
    if random.randint(1, 100) == 1:
        earned *= 5
        await context.bot.send_message(chat_id="@guild_antysocial", text=f"🌟 @{username} наткнулся на *Золотую жилу*! +{earned} 🍬", parse_mode='Markdown')
    first_bonus = 0
    if player and player[7] != date.today():
        first_bonus = 10
        earned += first_bonus

    old_balance = player[0] if player else 0
    update_balance(user_id, username, earned)
    update_last_farm(user_id)
    new_player = get_player(user_id)
    context.user_data['player'] = new_player
    new_balance = new_player[0]

    if new_balance < 500: progress = f"📈 до ⚔️ {500 - new_balance} 🍬"
    elif new_balance < 2000: progress = f"📈 до 👻 {2000 - new_balance} 🍬"
    else: progress = "👑 Максимальный ранг"

    bonus_str = f" (+{first_bonus}🎁)" if first_bonus else ""
    text = f"🍬 +{earned}{bonus_str} → 💰 {new_balance}\n{progress}"
    if update.effective_chat.type == "private":
        await msg.reply_text(text, reply_markup=get_back_to_menu_keyboard())
    else:
        await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())
    await check_rank_up(context, user_id, username, old_balance, new_balance)

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message or update.message
    user_id = user.id
    username = user.username or user.first_name
    player = get_player(user_id)
    if not player:
        update_balance(user_id, username, 0)
        balance_val = 0
        blunts = 0
    else:
        balance_val, blunts, _, _, _, _, _, _, _, _, _, _ = player

    if balance_val < 500: progress = f"📈 до ⚔️ {500 - balance_val} 🍬"
    elif balance_val < 2000: progress = f"📈 до 👻 {2000 - balance_val} 🍬"
    else: progress = "👑 Максимальный ранг"

    text = f"💰 *БАЛАНС*\n`{balance_val}` 🍬\n🌿 `{blunts}` Бланта\n{progress}"
    if update.effective_chat.type == "private":
        await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    else:
        await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())

async def craft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message or update.message
    user_id = user.id
    username = user.username or user.first_name
    player = get_player(user_id)
    if not player:
        update_balance(user_id, username, 0)
        balance_val = 0
        guild = None
    else:
        balance_val, _, guild, _, _, _, _, _, _, _, _, _ = player

    if balance_val < 5:
        text = "🕳️ Пусто. Нужно 5 🍬."
        if update.effective_chat.type == "private": await msg.reply_text(text, reply_markup=get_back_to_menu_keyboard())
        else: await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())
        return

    update_balance(user_id, username, -5)
    update_blunts(user_id, username, 1)
    new_player = get_player(user_id)
    new_balance, new_blunts = new_player[0], new_player[1]
    text = f"🌿 Ты свернул Блант. Пальцы пахнут уважением. → 💰 {new_balance} | 🌿 {new_blunts}"
    if update.effective_chat.type == "private": await msg.reply_text(text, reply_markup=get_back_to_menu_keyboard())
    else: await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())

async def smoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message or update.message
    user_id = user.id
    username = user.username or user.first_name
    player = get_player(user_id)
    if not player:
        update_blunts(user_id, username, 0)
        blunts = 0
        guild = None
    else:
        _, blunts, guild, _, _, _, _, _, _, _, _, _ = player

    if blunts < 1:
        text = "🌿 У тебя нет Блантов. Используй /craft"
        if update.effective_chat.type == "private": await msg.reply_text(text, reply_markup=get_back_to_menu_keyboard())
        else: await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())
        return

    bonus_info = get_guild_bonus(user_id)
    save_blunt = bonus_info['smoke_save_chance'] > 0 and random.randint(1, 100) <= bonus_info['smoke_save_chance']
    if not save_blunt: update_blunts(user_id, username, -1)

    r = random.randint(1, 100)
    if r <= 50:
        effect = "💨 *Лёгкий приход*\n[Гул Фабрики №9] «Станки работают в ритме твоего сердца...»\n🍬 +10"
        update_balance(user_id, username, 10)
    elif r <= 75:
        effect = "💨 *Паранойя...*\n[Зловещий шёпот] «Смотритель наблюдает...»\n✨ Никакого видимого эффекта."
    else:
        effect = "💨 *Плацебо*\n[Тишина] «Дым рассеялся, ничего не изменилось...»"
    new_balance = get_player(user_id)[0]
    text = effect + (f"\n💰 Баланс: `{new_balance}` 🍬" if r <= 50 else "")
    if save_blunt: text += "\n⚜️ *Белая Гильдия сохранила твой Блант!*"
    if update.effective_chat.type == "private": await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    else: await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())

async def ritual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message or update.message
    user_id = user.id
    username = user.username or user.first_name
    player = get_player(user_id)
    if not player:
        await msg.reply_text("🕳️ Ты ещё не активирован. /start")
        return
    balance, blunts, guild, _, last_ritual_str, _, _, _, _, _, _, _ = player
    if guild != 'BLACK':
        await msg.reply_text("❌ Только Чёрная Гильдия.")
        return
    if last_ritual_str and datetime.now() - datetime.fromisoformat(last_ritual_str) < timedelta(hours=24):
        remaining = timedelta(hours=24) - (datetime.now() - datetime.fromisoformat(last_ritual_str))
        await msg.reply_text(f"⏳ Жди {remaining.seconds//3600} ч.")
        return
    old_balance = balance
    update_balance(user_id, username, 15)
    update_last_ritual(user_id)
    new_balance = get_player(user_id)[0]
    text = f"🕯️ *РИТУАЛ ЗАВЕРШЁН*\n«Тьма одарила тебя стабильностью.»\n🍬 `+15` → 💰 {new_balance}"
    await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    await check_rank_up(context, user_id, username, old_balance, new_balance)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message or update.message
    user_id = user.id
    username = user.username or user.first_name
    player = get_player(user_id)
    if not player:
        update_balance(user_id, username, 0)
        balance_val = 0
        guild = None
        titles = ''
    else:
        balance_val, blunts, guild, _, _, _, titles, _, _, _, karma = player
    rank = "👻 Призрак" if balance_val >= 2000 else "⚔️ Ветеран" if balance_val >= 500 else "💉 Рекрут"
    guild_emoji = " 🕯️" if guild == 'BLACK' else " ⚜️" if guild == 'WHITE' else ""
    text = (f"👤 *{username}*{guild_emoji}\n"
            f"👻 *Ранг:* {rank}\n"
            f"💰 *ОАС:* `{balance_val}` 🍬\n"
            f"🌿 *Бланты:* `{blunts}`\n"
            f"🛡️ *Карма:* `{karma}`\n"
            f"🪴 *Урожай:* `+{player[8]*5 if player else 0}` 🍬 / час\n"
            f"🧬 *Титулы:* {titles if titles else '—'}")
    if update.effective_chat.type == "private": await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    else: await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message or update.message
    user_id = update.effective_user.id
    top_players = get_top(10)
    if not top_players:
        await msg.reply_text("🏆 Топ пока пуст.")
        return
    text = "🏆 *ТОП-10 ИГРОКОВ*\n\n"
    for i, (name, bal, guild) in enumerate(top_players, 1):
        medal = "🥇" if i==1 else "🥈" if i==2 else "🥉" if i==3 else f"{i}."
        g_emoji = "🕯️" if guild == 'BLACK' else "⚜️" if guild == 'WHITE' else ""
        text += f"{medal} {name} {g_emoji} — `{bal}` 🍬\n"
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM players WHERE balance > (SELECT balance FROM players WHERE user_id=?)', (user_id,))
    pos = c.fetchone()[0] + 1
    conn.close()
    text += f"\n📊 *Твоя позиция:* `{pos}`"
    if update.effective_chat.type == "private": await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    else: await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ("📜 **ЗАКОНЫ ГИЛЬДИИ**\n\n"
            "🍬 Фарми.  💨 Дуй.  🪴 Расти.\n\n"
            "▸ /farm — добыча 🍬 (раз в час)\n"
            "▸ /craft — 5 🍬 = 1 🌿 Блант\n"
            "▸ /smoke   — активация 🌿\n"
            "▸ /daily  — 🎡 Колесо\n"
            "▸ /privilege — твоя скидка\n\n"
            "▸ _Ранг даёт власть._\n"
            "▸ _Гильдия даёт путь._")
    await (update.effective_message or update.message).reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def guild_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = get_player(user_id)
    if not player:
        await update.message.reply_text("🕳️ Ты ещё не активирован. /start")
        return
    if get_guild(user_id):
        await update.message.reply_text("❌ Ты уже в Гильдии.")
        return
    if not context.args:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🕯️ Чёрная", callback_data='guild_join_BLACK'),
             InlineKeyboardButton("⚜️ Белая", callback_data='guild_join_WHITE')]
        ])
        await update.message.reply_text("🕋 Выбери свою Гильдию, Странник:", reply_markup=keyboard)
        return
    guild_name = context.args[0].upper()
    if guild_name in ['BLACK', 'WHITE']:
        set_guild(user_id, guild_name)
        emoji = "🕯️" if guild_name == 'BLACK' else "⚜️"
        await update.message.reply_text(f"✅ Ты вступил в Гильдию {emoji} *{guild_name}*", parse_mode='Markdown')
    else:
        await update.message.reply_text("❌ Доступно: BLACK или WHITE")

async def guild_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    counts = count_guilds()
    text = (f"🕋 *ГИЛЬДИИ*\n\n"
            f"🕯️ Чёрная: `{counts['BLACK']}` странников\n"
            f"⚜️ Белая: `{counts['WHITE']}` странников\n\n"
            f"🕯️ Ритуал: `+15` 🍬 раз в `24` ч.\n"
            f"⚜️ Удача: `20%` сохранить Блант при 💨.")
    await (update.effective_message or update.message).reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def privilege(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    player = get_player(user_id)
    if not player:
        await update.message.reply_text("🕳️ Ты ещё не активирован. /start")
        return
    balance = player[0]
    guild = player[2]
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
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = get_player(user_id)
    if not player:
        await update.message.reply_text("🕳️ Ты ещё не активирован. /start")
        return
    passive_level = player[8] or 0
    if passive_level == 0:
        await update.message.reply_text("❌ Нет авто‑сборщика. /upgrades")
        return
    passive_collected = player[9]
    if passive_collected:
        last = datetime.fromisoformat(passive_collected) if isinstance(passive_collected, str) else passive_collected
        hours = (datetime.now() - last).total_seconds() / 3600
        earned = int(hours * 5 * passive_level)
        if earned >= 1:
            update_balance(user_id, username, earned)
            conn = sqlite3.connect('players.db')
            c = conn.cursor()
            c.execute('UPDATE players SET passive_collected=? WHERE user_id=?', (datetime.now(), user_id))
            conn.commit()
            conn.close()
            new_balance = get_player(user_id)[0]
            text = f"🪴 *УРОЖАЙ СОБРАН*\nТвой куст принёс `{earned}` 🍬.\n💰 *Баланс:* `{new_balance}` 🍬"
            await update.message.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
        else:
            await update.message.reply_text("⏳ Пока нечего собирать.")
    else:
        conn = sqlite3.connect('players.db')
        c = conn.cursor()
        c.execute('UPDATE players SET passive_collected=? WHERE user_id=?', (datetime.now(), user_id))
        conn.commit()
        conn.close()
        await update.message.reply_text("⏳ Авто‑сборщик активирован. Заходи через час.")

async def rush(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = get_player(user_id)
    if not player:
        await update.message.reply_text("🕳️ Ты ещё не активирован. /start")
        return
    if player[1] < 1:
        await update.message.reply_text("🌿 У тебя нет Блантов. /craft")
        return
    update_blunts(user_id, username, -1)
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('UPDATE players SET last_farm=NULL WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("⚡ *УСКОРЕНИЕ*\nКулдаун /farm сброшен.\n-1 🌿 Блант", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = get_player(user_id)
    if not player:
        await update.message.reply_text("🕳️ Ты ещё не активирован. /start")
        return
    last_daily = player[5]
    if last_daily and datetime.now() - datetime.fromisoformat(last_daily) < timedelta(hours=24):
        remaining = timedelta(hours=24) - (datetime.now() - datetime.fromisoformat(last_daily))
        await update.message.reply_text(f"⏳ Колесо спит. Жди {remaining.seconds//3600} ч.")
        return
    r = random.randint(1, 100)
    if r <= 40: prize, prize_text = 5, "+5 🍬"; update_balance(user_id, username, prize)
    elif r <= 65: prize, prize_text = 10, "+10 🍬"; update_balance(user_id, username, prize)
    elif r <= 80: prize, prize_text = 1, "+1 🌿 Блант"; update_blunts(user_id, username, prize)
    elif r <= 90: prize, prize_text = 20, "+20 🍬"; update_balance(user_id, username, prize)
    elif r <= 97: prize, prize_text = 2, "+2 🌿 Бланта"; update_blunts(user_id, username, prize)
    else:
        prize, prize_text = 50, "🌟 *ДЖЕКПОТ!* +50 🍬"
        update_balance(user_id, username, prize)
        await context.bot.send_message(chat_id="@guild_antysocial", text=f"🌟 @{username} сорвал *Джекпот* (+50 🍬) на 🎡 Колесе! Смотритель доволен.", parse_mode='Markdown')
    update_last_daily(user_id)
    new_balance = get_player(user_id)[0]
    await update.message.reply_text(f"🎡 *КОЛЕСО СМОТРИТЕЛЯ*\n{prize_text} → 💰 {new_balance} 🍬", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Перейти", url="https://t.me/antysocialshop")]])
    await (update.effective_message or update.message).reply_text("🕯️ *ANTYSOCIALSHOP · КАТАЛОГ*", parse_mode='Markdown', reply_markup=keyboard)

async def claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = get_player(user_id)
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
    if art in reservations:
        await update.message.reply_text("❌ Уже застолбили.")
        return
    update_balance(user_id, username, -cost)
    reservations[art] = {"user_id": user_id, "username": username, "expires": datetime.now() + timedelta(hours=24)}
    text = (f"🔒 *РЕЗЕРВ* `{art}`\n\n"
            f"⏳ `24` ч на активацию в ЛС.\n"
            f"💸 Списано: `{cost}` 🍬 (вернутся `+10%` 🎁 при активации).")
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    await context.bot.send_message(chat_id="@guild_antysocial", text=f"🔒 *[РЕЗЕРВ]* @{username} застолбил {art} на 24 часа.", parse_mode='Markdown')

async def proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*), SUM(balance) FROM players')
    total_players, total_oas = c.fetchone()
    conn.close()
    text = f"📊 *СТАТИСТИКА*\n\n👥 Игроков: `{total_players or 0}`\n💰 ОАС в системе: `{total_oas or 0}` 🍬"
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def pin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    player = get_player(user_id)
    if not player or player[0] < 200:
        await update.message.reply_text("🕳️ Пусто. Нужно 200 🍬.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение для закрепа.")
        return
    update_balance(user_id, update.effective_user.username, -200)
    await update.message.reply_to_message.pin()
    await update.message.reply_text("📌 *ЗАКРЕПЛЕНО*\nСообщение закреплено на `1` час.\n💸 Списано: `200` 🍬", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot: continue
        await update.message.reply_text(f"🕯️ @{member.username or member.first_name}, добро пожаловать в Гильдию. Твой первый /farm уже ждёт.")

async def update_pulse(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*), SUM(balance) FROM players')
    total_players, total_oas = c.fetchone()
    total_players = total_players or 0
    total_oas = total_oas or 0
    c.execute('SELECT COUNT(*) FROM players WHERE guild="BLACK"')
    black = c.fetchone()[0] or 0
    c.execute('SELECT COUNT(*) FROM players WHERE guild="WHITE"')
    white = c.fetchone()[0] or 0
    c.execute("SELECT COUNT(DISTINCT user_id) FROM players WHERE last_farm > ?", (datetime.now() - timedelta(hours=1),))
    online = c.fetchone()[0] or 0
    conn.close()

    total_for_percent = black + white
    if total_for_percent > 0:
        black_percent = int(black / total_for_percent * 100)
        bar = "🕯️" + "▰" * (black_percent//10) + "▱" * (10 - black_percent//10) + f" ⚜️ {black_percent}%"
    else:
        bar = "🕯️▱▱▱▱▱▱▱▱▱▱ ⚜️ 50%"

    chat_desc = f"{bar} | 👥 {online}"
    try: await context.bot.set_chat_description(chat_id="@guild_antysocial", description=chat_desc)
    except: pass

async def refresh_pulse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update_pulse(context)
    await update.message.reply_text("✅ Пульс обновлён.")

async def happy_hour_trigger(context: ContextTypes.DEFAULT_TYPE):
    global happy_hour_active, happy_hour_end_time
    happy_hour_active = True
    happy_hour_end_time = datetime.now() + timedelta(minutes=HAPPY_HOUR_DURATION_MIN)
    await context.bot.send_message(chat_id="@guild_antysocial", text="🌟 *ЧАС УДАЧИ!* Все действия приносят x2 🍬 в течение 30 минут!", parse_mode='Markdown')
    context.job_queue.run_once(reset_happy_hour, HAPPY_HOUR_DURATION_MIN * 60)

async def reset_happy_hour(context: ContextTypes.DEFAULT_TYPE):
    global happy_hour_active
    happy_hour_active = False
    await context.bot.send_message(chat_id="@guild_antysocial", text="⏳ Час Удачи завершён.")

async def handle_chat_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    mapping = {'фарм': farm, 'farm': farm, 'дунуть': smoke, 'smoke': smoke, 'крафт': craft, 'craft': craft,
               'баланс': balance, 'balance': balance, 'колесо': daily, 'daily': daily, 'топ': top, 'top': top,
               'статус': status, 'status': status}
    if text in mapping: await mapping[text](update, context)

async def check_rank_up(context: ContextTypes.DEFAULT_TYPE, user_id: int, username: str, old_balance: int, new_balance: int):
    if old_balance < 500 <= new_balance:
        await context.bot.send_message(chat_id="@guild_antysocial", text=f"🎉 @{username} достиг ранга ⚔️ *Ветеран*! Гильдия рукоплещет.", parse_mode='Markdown')
    if old_balance < 2000 <= new_balance:
        await context.bot.send_message(chat_id="@guild_antysocial", text=f"👻 @{username} стал *Призраком*! Ткань реальности дрожит.", parse_mode='Markdown')

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        msg = update.message
    await msg.reply_text("🎮 *ГЛАВНОЕ МЕНЮ*", reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode='Markdown')

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return await update.message.reply_text("⛔ Только Смотритель.")
    try:
        if update.message.reply_to_message:
            target = update.message.reply_to_message.from_user
            update_balance(target.id, target.username or target.first_name, int(context.args[0]))
            await update.message.reply_text(f"✅ @{target.username or target.first_name} получил {context.args[0]} 🍬.")
        else:
            await update.message.reply_text("Ответьте на сообщение пользователя.")
    except: await update.message.reply_text("/add <сумма> ответом на сообщение.")

# Русские команды (через CommandHandler)
async def farm_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await farm(update, context)
async def smoke_ru_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await smoke(update, context)
async def craft_ru_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await craft(update, context)
async def balance_ru_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await balance(update, context)
async def daily_ru_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await daily(update, context)
async def top_ru_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await top(update, context)
async def status_ru_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await status(update, context)

def main():
    print("=== [DEBUG] main() started ===")
    try:
        init_db()
        print("=== [DEBUG] init_db() completed ===")
    except Exception as e:
        print(f"=== [DEBUG] init_db() FAILED: {e} ===")
        raise
    web_thread = Thread(target=run_web_server); web_thread.daemon = True; web_thread.start()
    app = Application.builder().token(TOKEN).build()

    # Английские команды
    app.add_handler(CommandHandler("start", start)); app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("farm", farm)); app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("craft", craft)); app.add_handler(CommandHandler("smoke", smoke))
    app.add_handler(CommandHandler("ritual", ritual)); app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("top", top)); app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("guild", guild_join)); app.add_handler(CommandHandler("privilege", privilege))
    app.add_handler(CommandHandler("claim", claim)); app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("proof", proof)); app.add_handler(CommandHandler("pin", pin_message))
    app.add_handler(CommandHandler("catalog", catalog)); app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("rush", rush)); app.add_handler(CommandHandler("collect", collect))
    app.add_handler(CommandHandler("pulse", refresh_pulse)); app.add_handler(CommandHandler("play", play))

    # Русские команды (теперь CommandHandler)
    app.add_handler(CommandHandler("фарм", farm_ru)); app.add_handler(CommandHandler("баланс", balance_ru_cmd))
    app.add_handler(CommandHandler("крафт", craft_ru_cmd)); app.add_handler(CommandHandler("дунуть", smoke_ru_cmd))
    app.add_handler(CommandHandler("ритуал", ritual_ru)); app.add_handler(CommandHandler("статус", status_ru_cmd))
    app.add_handler(CommandHandler("топ", top_ru_cmd)); app.add_handler(CommandHandler("колесо", daily_ru_cmd))
    app.add_handler(CommandHandler("привилегия", privilege_ru)); app.add_handler(CommandHandler("забрать", claim_ru))
    app.add_handler(CommandHandler("дейли", daily_ru)); app.add_handler(CommandHandler("каталог", catalog_ru))
    app.add_handler(CommandHandler("ускорение", rush_ru)); app.add_handler(CommandHandler("вступить", guild_join_ru))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r'^(фарм|farm|дунуть|smoke|крафт|craft|баланс|balance|колесо|daily|топ|top|статус|status)$'), handle_chat_shortcut))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(CallbackQueryHandler(play_callback, pattern='^(play_safe|play_risk)$'))
    app.add_handler(CallbackQueryHandler(button_handler))

    job_queue = app.job_queue
    job_queue.run_repeating(update_pulse, interval=300, first=10)
    job_queue.run_once(lambda c: c.job_queue.run_repeating(happy_hour_trigger, interval=random.randint(14400, 28800), first=random.randint(3600, 10800)), when=1)

    print("=== [DEBUG] Handlers registered. Starting polling... ===")
    app.run_polling()

if __name__ == '__main__':
    main()
