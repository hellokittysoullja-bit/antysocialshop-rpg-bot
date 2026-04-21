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
FARM_COOLDOWN_HOURS = 1
FARM_MIN = 10
FARM_MAX = 25

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
                  passive_collected TIMESTAMP)''')
    # Миграция для старых БД
    try:
        c.execute('ALTER TABLE players ADD COLUMN last_farm_date DATE')
    except:
        pass
    try:
        c.execute('ALTER TABLE players ADD COLUMN passive_level INTEGER DEFAULT 0')
    except:
        pass
    try:
        c.execute('ALTER TABLE players ADD COLUMN passive_collected TIMESTAMP')
    except:
        pass
    conn.commit()
    conn.close()
    print("База данных инициализирована.")

def get_player(user_id):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT balance, blunts, guild, last_farm, last_ritual, last_daily, titles, last_farm_date, passive_level, passive_collected FROM players WHERE user_id=?', (user_id,))
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

# === ФУНКЦИИ ГИЛЬДИЙ ===
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

# === ФОРМАТИРОВАНИЕ СООБЩЕНИЙ ===
def format_message(text):
    lines = text.split('\n')
    max_len = max(len(line) for line in lines) if lines else 0
    top_bottom = '─' * (max_len + 2)
    formatted = f"┌{top_bottom}┐\n"
    for line in lines:
        formatted += f"│ {line.ljust(max_len)} │\n"
    formatted += f"└{top_bottom}┘"
    return formatted

# === ГЛАВНОЕ МЕНЮ (ДИНАМИЧЕСКОЕ) ===
def get_main_menu_keyboard(user_id=None):
    keyboard = [
        [InlineKeyboardButton("🍬 Фармить ОАС", callback_data='farm')],
        [InlineKeyboardButton("💰 Баланс", callback_data='balance'),
         InlineKeyboardButton("🌿 Крафт Бланта", callback_data='craft')],
        [InlineKeyboardButton("💨 Дунуть", callback_data='smoke')]
    ]
    if user_id:
        player = get_player(user_id)
        if player:
            if get_guild(user_id) == 'BLACK':
                keyboard.append([InlineKeyboardButton("🕯️ Ритуал", callback_data='ritual')])
            # Кнопка сбора пассивного дохода
            passive_collected = player[9]
            if passive_collected:
                last = datetime.fromisoformat(passive_collected) if isinstance(passive_collected, str) else passive_collected
                hours = (datetime.now() - last).total_seconds() / 3600
                if hours >= 1:
                    keyboard.append([InlineKeyboardButton("📦 Собрать доход", callback_data='collect')])
    keyboard.extend([
        [InlineKeyboardButton("📊 Статус", callback_data='status'),
         InlineKeyboardButton("🏆 Топ", callback_data='top')],
        [InlineKeyboardButton("🕋 Гильдии", callback_data='guild_info'),
         InlineKeyboardButton("📜 Законы", callback_data='rules')],
        [InlineKeyboardButton("🪪 Привилегия", callback_data='privilege'),
         InlineKeyboardButton("🔒 Забрать", callback_data='claim_help')],
        [InlineKeyboardButton("🎡 Колесо", callback_data='daily'),
         InlineKeyboardButton("⚡ Ускорение", callback_data='rush_help')]
    ])
    return InlineKeyboardMarkup(keyboard)

def get_back_to_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 Меню", callback_data='menu')]])

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ САМОУДАЛЯЮЩИХСЯ СООБЩЕНИЙ ===
last_bot_messages = {}

async def send_selfdestruct_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None, parse_mode='Markdown'):
    chat_id = update.effective_chat.id
    if chat_id in last_bot_messages:
        try:
            await context.bot.delete_message(chat_id, last_bot_messages[chat_id])
        except:
            pass
    msg = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    last_bot_messages[chat_id] = msg.message_id
    context.job_queue.run_once(delete_message, 5, data={'chat_id': chat_id, 'message_id': msg.message_id})
    return msg

async def delete_message(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    try:
        await context.bot.delete_message(data['chat_id'], data['message_id'])
        if data['chat_id'] in last_bot_messages and last_bot_messages[data['chat_id']] == data['message_id']:
            del last_bot_messages[data['chat_id']]
    except:
        pass

# === ОБРАБОТЧИК НАЖАТИЙ ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'menu':
        await menu(update, context)
    elif data == 'farm':
        await farm(update, context)
    elif data == 'balance':
        await balance(update, context)
    elif data == 'craft':
        await craft(update, context)
    elif data == 'smoke':
        await smoke(update, context)
    elif data == 'ritual':
        await ritual(update, context)
    elif data == 'status':
        await status(update, context)
    elif data == 'top':
        await top(update, context)
    elif data == 'guild_info':
        await guild_info(update, context)
    elif data == 'rules':
        await rules(update, context)
    elif data == 'privilege':
        await privilege(update, context)
    elif data == 'claim_help':
        await query.message.reply_text("Используй `/claim #КОД` или `/забрать #КОД`, чтобы застолбить экземпляр на 24 часа.", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    elif data == 'daily':
        await daily(update, context)
    elif data == 'collect':
        await collect(update, context)
    elif data == 'rush_help':
        await query.message.reply_text("Используй `/rush` — потрать 1 Блант и мгновенно сбрось кулдаун `/farm`.", reply_markup=get_back_to_menu_keyboard())

# === ОСНОВНЫЕ КОМАНДЫ ===
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

    # Кэширование игрока
    if 'player' not in context.user_data:
        context.user_data['player'] = get_player(user_id)
    player = context.user_data['player']

    if player:
        balance, blunts, guild, last_farm_str, _, _, _, _, _, _ = player
        if last_farm_str:
            last_farm = datetime.fromisoformat(last_farm_str)
            if datetime.now() - last_farm < timedelta(hours=FARM_COOLDOWN_HOURS):
                remaining = timedelta(hours=FARM_COOLDOWN_HOURS) - (datetime.now() - last_farm)
                if update.effective_chat.type == "private":
                    await msg.reply_text(f"⏳ Жди {remaining.seconds//60} мин", reply_markup=get_back_to_menu_keyboard())
                else:
                    await send_selfdestruct_message(update, context, f"⏳ Жди {remaining.seconds//60} мин", reply_markup=get_back_to_menu_keyboard())
                return

    earned = random.randint(FARM_MIN, FARM_MAX)
    # Золотая жила (1% шанс x5)
    if random.randint(1, 100) == 1:
        earned *= 5
        await context.bot.send_message(chat_id="@guild_antysocial", text=f"🌟 @{username} наткнулся на золотую жилу! +{earned} ОАС!")

    # Бонус первого входа в день
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

    # Прогресс до ранга
    if new_balance < 500:
        need = 500 - new_balance
        progress = f"📈 До Ветерана: {need} ОАС"
    elif new_balance < 2000:
        need = 2000 - new_balance
        progress = f"📈 До Призрака: {need} ОАС"
    else:
        progress = "👑 Максимальный ранг"

    bonus_str = f" (+{first_bonus}🎁)" if first_bonus else ""
    text = format_message(f"🍬 +{earned} ОАС{bonus_str}\n💰 {new_balance}\n{progress}")

    if update.effective_chat.type == "private":
        await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    else:
        await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())

    await check_rank_up(context, user_id, username, old_balance, new_balance)
    await check_secret_titles(user_id, username, context)

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        user = update.callback_query.from_user
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        user = update.effective_user
        msg = update.message

    user_id = user.id
    username = user.username or user.first_name
    player = get_player(user_id)
    if not player:
        update_balance(user_id, username, 0)
        balance_val = 0
        blunts = 0
    else:
        balance_val, blunts, _, _, _, _, _, _, _, _ = player

    if balance_val < 500:
        need = 500 - balance_val
        progress = f"📈 До Ветерана: {need} ОАС"
    elif balance_val < 2000:
        need = 2000 - balance_val
        progress = f"📈 До Призрака: {need} ОАС"
    else:
        progress = "👑 Максимальный ранг"

    text = format_message(f"💰 ОАС: {balance_val}\n🌿 Бланты: {blunts}\n{progress}")
    if update.effective_chat.type == "private":
        await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    else:
        await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())

async def craft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        user = update.callback_query.from_user
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        user = update.effective_user
        msg = update.message

    user_id = user.id
    username = user.username or user.first_name
    player = get_player(user_id)
    if not player:
        update_balance(user_id, username, 0)
        balance_val = 0
        guild = None
    else:
        balance_val, _, guild, _, _, _, _, _, _, _ = player

    if balance_val < 5:
        text = format_message("❌ Недостаточно ОАС. Нужно 5 ОАС.")
        if update.effective_chat.type == "private":
            await msg.reply_text(text, reply_markup=get_back_to_menu_keyboard())
        else:
            await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())
        return

    update_balance(user_id, username, -5)
    update_blunts(user_id, username, 1)
    new_player = get_player(user_id)
    new_balance, new_blunts = new_player[0], new_player[1]

    if guild == 'BLACK':
        craft_msg = "🌿 Ты сплетаешь тьму в Блант..."
    elif guild == 'WHITE':
        craft_msg = "🌿 Ты очищаешь волокна в Блант..."
    else:
        craft_msg = "🌿 Ты закрафтил 1 Блант!"

    text = format_message(f"{craft_msg}\n💰 ОАС: {new_balance}\n🌿 Бланты: {new_blunts}")
    if update.effective_chat.type == "private":
        await msg.reply_text(text, reply_markup=get_back_to_menu_keyboard())
    else:
        await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())

async def smoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        user = update.callback_query.from_user
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        user = update.effective_user
        msg = update.message

    user_id = user.id
    username = user.username or user.first_name
    player = get_player(user_id)
    if not player:
        update_blunts(user_id, username, 0)
        blunts = 0
        guild = None
    else:
        _, blunts, guild, _, _, _, _, _, _, _ = player

    if blunts < 1:
        text = format_message("❌ У тебя нет Блантов.")
        if update.effective_chat.type == "private":
            await msg.reply_text(text, reply_markup=get_back_to_menu_keyboard())
        else:
            await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())
        return

    bonus_info = get_guild_bonus(user_id)
    save_blunt = False
    if bonus_info['smoke_save_chance'] > 0:
        if random.randint(1, 100) <= bonus_info['smoke_save_chance']:
            save_blunt = True

    if not save_blunt:
        update_blunts(user_id, username, -1)
        spend_msg = ""
    else:
        spend_msg = "\n⚜️ Белая Гильдия сохранила твой Блант!"

    r = random.randint(1, 100)
    effect_name = ""
    flavor_text = ""
    oas_gain = 0
    almost_msg = ""

    if r <= 50:
        effect_name = "Лёгкий приход 💨"
        flavor_text = "[Гул Фабрики №9]\n«Станки работают в ритме твоего сердца...»"
        oas_gain = 10
        update_balance(user_id, username, oas_gain)
    elif r <= 75:
        effect_name = "Паранойя..."
        flavor_text = "[Зловещий шёпот]\n«Смотритель наблюдает...»"
    else:
        effect_name = "Плацебо"
        flavor_text = "[Тишина]\n«Дым рассеялся, ничего не изменилось...»"
        if random.randint(1, 100) <= 30:
            almost_msg = "\n\n👁‍🗨 Кажется, ещё одна затяжка — и ткань бы отозвалась..."

    message = f"💨 Ты скурил блант...\n\n{flavor_text}\n\n👁‍🗨 Эффект: {effect_name}\n"
    if oas_gain > 0:
        new_player = get_player(user_id)
        message += f"🍬 +{oas_gain} ОАС\n💰 Баланс: {new_player[0]} ОАС"
    else:
        message += "✨ Никакого видимого эффекта."

    message += spend_msg + almost_msg
    text = format_message(message)

    if update.effective_chat.type == "private":
        await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    else:
        await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())

    await check_secret_titles(user_id, username, context)

async def ritual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        user = update.callback_query.from_user
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        user = update.effective_user
        msg = update.message

    user_id = user.id
    username = user.username or user.first_name
    player = get_player(user_id)
    if not player:
        await msg.reply_text("❌ Сначала /start")
        return

    balance, blunts, guild, _, last_ritual_str, _, _, _, _, _ = player
    if guild != 'BLACK':
        await msg.reply_text("❌ Только Чёрная Гильдия.")
        return

    if last_ritual_str:
        last_ritual = datetime.fromisoformat(last_ritual_str)
        if datetime.now() - last_ritual < timedelta(hours=24):
            remaining = timedelta(hours=24) - (datetime.now() - last_ritual)
            await msg.reply_text(f"⏳ Жди {remaining.seconds//3600} ч")
            return

    old_balance = balance
    update_balance(user_id, username, 15)
    update_last_ritual(user_id)
    new_balance = get_player(user_id)[0]
    text = format_message(f"🕯️ Ритуал завершён\n🍬 +15 ОАС\n💰 {new_balance}")
    await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    await check_rank_up(context, user_id, username, old_balance, new_balance)
    await check_secret_titles(user_id, username, context)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        user = update.callback_query.from_user
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        user = update.effective_user
        msg = update.message

    user_id = user.id
    username = user.username or user.first_name
    player = get_player(user_id)
    if not player:
        update_balance(user_id, username, 0)
        balance_val = 0
        guild = None
        titles = ''
    else:
        balance_val, blunts, guild, _, _, _, titles, _, _, _ = player

    if balance_val >= 2000:
        rank = "👻 Призрак"
    elif balance_val >= 500:
        rank = "⚔️ Ветеран"
    else:
        rank = "💉 Рекрут"

    guild_emoji = ""
    if guild == 'BLACK':
        guild_emoji = " 🕯️"
        guild_name = "Чёрная"
    elif guild == 'WHITE':
        guild_emoji = " ⚜️"
        guild_name = "Белая"
    else:
        guild_name = "Нет"

    text = f"{username}{guild_emoji}\nРанг: {rank}\n💰 ОАС: {balance_val}\n🌿 Бланты: {blunts}\nГильдия: {guild_name}"
    if titles:
        text += f"\n🛡️ Титулы: {titles}"
    formatted = format_message(text)
    if update.effective_chat.type == "private":
        await msg.reply_text(formatted, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    else:
        await send_selfdestruct_message(update, context, formatted, reply_markup=get_back_to_menu_keyboard())

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        msg = update.message

    user_id = update.effective_user.id
    top_players = get_top(10)
    if not top_players:
        await msg.reply_text("Топ пока пуст.")
        return
    text = "🏆 ТОП-10\n\n"
    for i, (name, bal, guild) in enumerate(top_players, 1):
        medal = "🥇" if i==1 else "🥈" if i==2 else "🥉" if i==3 else f"{i}."
        g_emoji = "🕯️" if guild == 'BLACK' else "⚜️" if guild == 'WHITE' else ""
        text += f"{medal} {name} {g_emoji}: {bal} ОАС\n"

    # Позиция игрока
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM players WHERE balance > (SELECT balance FROM players WHERE user_id=?)', (user_id,))
    pos = c.fetchone()[0] + 1
    conn.close()
    text += f"\n📊 Твоя позиция: {pos}"

    formatted = format_message(text)
    if update.effective_chat.type == "private":
        await msg.reply_text(formatted, reply_markup=get_back_to_menu_keyboard())
    else:
        await send_selfdestruct_message(update, context, formatted, reply_markup=get_back_to_menu_keyboard())

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        msg = update.message
    text = (
        "📜 ЗАКОНЫ ГИЛЬДИИ\n\n"
        f"• /farm — раз в час: {FARM_MIN}-{FARM_MAX} ОАС\n"
        "• /craft — 5 ОАС = 1 Блант\n"
        "• /smoke — активировать Блант\n"
        "• /daily — Колесо Смотрителя\n"
        "• /privilege — персональная скидка\n"
        "• /claim #КОД — застолбить экземпляр\n\n"
        "🕯️ ЧЁРНАЯ — Ритуал (+15 ОАС/24ч)\n"
        "⚜️ БЕЛАЯ — 20% сохранить Блант\n\n"
        "Ранги: 💉0-499 | ⚔️500-1999 | 👻2000+"
    )
    await msg.reply_text(format_message(text), parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def guild_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        user = update.callback_query.from_user
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        user = update.effective_user
        msg = update.message

    user_id = user.id
    username = user.username or user.first_name
    player = get_player(user_id)
    if not player:
        update_balance(user_id, username, 0)

    current_guild = get_guild(user_id)
    if current_guild:
        emoji = "🕯️" if current_guild == 'BLACK' else "⚜️"
        await msg.reply_text(f"❌ Ты уже в Гильдии {emoji} {current_guild}")
        return

    try:
        guild_name = context.args[0].upper()
        if guild_name not in ['BLACK', 'WHITE']:
            await msg.reply_text("❌ Доступно: BLACK или WHITE")
            return
        set_guild(user_id, guild_name)
        emoji = "🕯️" if guild_name == 'BLACK' else "⚜️"
        await msg.reply_text(f"✅ Ты вступил в Гильдию {emoji} {guild_name}")
    except IndexError:
        await msg.reply_text("❌ /guild join BLACK или WHITE")

async def guild_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        msg = update.message

    counts = count_guilds()
    text = f"🕋 ГИЛЬДИИ\n\n🕯️ Чёрная: {counts['BLACK']}\n⚜️ Белая: {counts['WHITE']}"
    await msg.reply_text(format_message(text), reply_markup=get_back_to_menu_keyboard())

async def privilege(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    player = get_player(user_id)
    if not player:
        await update.message.reply_text("❌ Сначала /start")
        return
    balance = player[0]
    guild = player[2]
    bonus_info = get_guild_bonus(user_id)

    if balance >= 2000:
        rank = "👻 Призрак"
        divisor = 10
        max_percent = 0.20
        target = None
    elif balance >= 500:
        rank = "⚔️ Ветеран"
        divisor = 15
        max_percent = 0.15
        target = 2000
    else:
        rank = "💉 Рекрут"
        divisor = 20
        max_percent = 0.10
        target = 500

    if guild == 'WHITE':
        max_percent = 0.15
        guild_note = "🎲 Шанс 20% не потратить ОАС"
    else:
        guild_note = "🔒 Стабильно"

    text = f"🪪 ПРИВИЛЕГИЯ\n\nРанг: {rank} ({guild or 'Нет'})\nБаланс: {balance} ОАС\n\n🔹 Скидка: 1₽ за {divisor} ОАС\n🔹 Лимит: {int(max_percent*100)}%\n🔹 {guild_note}\n"
    if target:
        percent = int(balance / target * 100)
        bar = "🟩" * (percent//10) + "⬛" * (10 - percent//10)
        text += f"\n⚔️ Путь к {'Ветерану' if target==500 else 'Призраку'}\n{bar} {percent}%\n"
        if percent < 30:
            phrase = "«Ты слышишь шёпот Фабрики...»"
        elif percent < 70:
            phrase = "«Ткань реальности отзывается...»"
        else:
            phrase = "«Смотритель чувствует твоё приближение...»"
        text += f"\n👁‍🗨 {phrase}"

    await update.message.reply_text(format_message(text), parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

# === ПАССИВНЫЙ ДОХОД ===
async def collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        user = update.callback_query.from_user
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        user = update.effective_user
        msg = update.message

    user_id = user.id
    username = user.username or user.first_name
    player = get_player(user_id)
    if not player:
        await msg.reply_text("❌ Сначала /start")
        return

    passive_level = player[8] or 0
    if passive_level == 0:
        await msg.reply_text("❌ У тебя нет авто‑сборщика. Купи в /upgrades")
        return

    passive_collected = player[9]
    if passive_collected:
        last = datetime.fromisoformat(passive_collected) if isinstance(passive_collected, str) else passive_collected
        hours = (datetime.now() - last).total_seconds() / 3600
        earned = int(hours * 15 * passive_level)
        if earned >= 1:
            update_balance(user_id, username, earned)
            conn = sqlite3.connect('players.db')
            c = conn.cursor()
            c.execute('UPDATE players SET passive_collected=? WHERE user_id=?', (datetime.now(), user_id))
            conn.commit()
            conn.close()
            new_balance = get_player(user_id)[0]
            text = format_message(f"📦 Собрано {earned} ОАС\n💰 Баланс: {new_balance}")
            await msg.reply_text(text, reply_markup=get_back_to_menu_keyboard())
        else:
            await msg.reply_text("⏳ Пока нечего собирать")
    else:
        conn = sqlite3.connect('players.db')
        c = conn.cursor()
        c.execute('UPDATE players SET passive_collected=? WHERE user_id=?', (datetime.now(), user_id))
        conn.commit()
        conn.close()
        await msg.reply_text("⏳ Авто‑сборщик активирован. Заходи через час.")

# === УСКОРЕНИЕ /RUSH ===
async def rush(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = get_player(user_id)
    if not player:
        await update.message.reply_text("❌ Сначала /start")
        return
    blunts = player[1]
    if blunts < 1:
        await update.message.reply_text("❌ Нет Блантов")
        return
    update_blunts(user_id, username, -1)
    # Сброс кулдауна
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('UPDATE players SET last_farm=NULL WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(format_message("⚡ Кулдаун /farm сброшен!\n-1 Блант"), reply_markup=get_back_to_menu_keyboard())

# === ОСТАЛЬНЫЕ ФУНКЦИИ (start, menu, add, check_rank_up, daily, proof, pin, catalog, claim, welcome_new_member, warden_whisper, check_secret_titles, русские команды) ===
# ... (все они остаются с аналогичной логикой выбора send_selfdestruct_message/private и форматированием)

# === ЗАПУСК ===
def main():
    return
    init_db()
    web_thread = Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()

    app = Application.builder().token(TOKEN).build()

    # Регистрация всех обработчиков (CommandHandler, MessageHandler, CallbackQueryHandler)
    # ... (полный список как в предыдущей версии плюс новые /rush, /collect и бесслешные)

    app.run_polling()

if __name__ == '__main__':
    main()
