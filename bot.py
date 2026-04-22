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
    print("База данных инициализирована.")

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

def format_message(text):
    lines = text.split('\n')
    max_len = max(len(line) for line in lines) if lines else 0
    top_bottom = '─' * (max_len + 2)
    formatted = f"┌{top_bottom}┐\n"
    for line in lines:
        formatted += f"│ {line.ljust(max_len)} │\n"
    formatted += f"└{top_bottom}┘"
    return formatted

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

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == 'menu': await menu(update, context)
    elif data == 'farm': await farm(update, context)
    elif data == 'balance': await balance(update, context)
    elif data == 'craft': await craft(update, context)
    elif data == 'smoke': await smoke(update, context)
    elif data == 'ritual': await ritual(update, context)
    elif data == 'status': await status(update, context)
    elif data == 'top': await top(update, context)
    elif data == 'guild_info': await guild_info(update, context)
    elif data == 'rules': await rules(update, context)
    elif data == 'privilege': await privilege(update, context)
    elif data == 'claim_help': await query.message.reply_text("Используй `/claim #КОД` или `/забрать #КОД`, чтобы застолбить экземпляр на 24 часа.", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    elif data == 'daily': await daily(update, context)
    elif data == 'collect': await collect(update, context)
    elif data == 'rush_help': await query.message.reply_text("Используй `/rush` — потрать 1 Блант и мгновенно сбрось кулдаун `/farm`.", reply_markup=get_back_to_menu_keyboard())
    elif data == 'activate_menu':
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.first_name
        player = get_player(user_id)
        if not player:
            update_balance(user_id, username, 0)
            update_blunts(user_id, username, 0)
            update_balance(user_id, username, 100)
            bonus_msg = "🎁 Смотритель дарует тебе 100 🍬.\n\n"
        else:
            bonus_msg = ""
        welcome_text = (
            "🎉 *Добро пожаловать в Гильдию antysocialshop!*\n\n"
            "▸ _Смотритель приветствует тебя._\n"
            "▸ _Здесь добываются редкие экземпляры, зарабатывают Очки Антисошл (🍬), курят бланты и вступают в гильдии._\n\n"
            "🕯️ *ЧЁРНАЯ ГИЛЬДИЯ* — стабильность, ритуалы, власть.\n"
            "⚜️ *БЕЛАЯ ГИЛЬДИЯ* — азарт, удача, танец на лезвии.\n\n"
            "▸ _Выбери свой путь:_"
        )
        full_text = bonus_msg + welcome_text
        await query.message.edit_text(
            full_text,
            reply_markup=get_main_menu_keyboard(user_id),
            parse_mode='Markdown'
        )
        await query.answer()
        return

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
    if 'player' not in context.user_data:
        context.user_data['player'] = get_player(user_id)
    player = context.user_data['player']

    if player:
        balance, blunts, guild, last_farm_str, _, _, _, _, _, _, _ = player
        if last_farm_str:
            last_farm = datetime.fromisoformat(last_farm_str)
            if datetime.now() - last_farm < timedelta(hours=FARM_COOLDOWN_HOURS):
                remaining = timedelta(hours=FARM_COOLDOWN_HOURS) - (datetime.now() - last_farm)
                text = f"⏳ Жди {remaining.seconds//60} мин"
                if update.effective_chat.type == "private":
                    await msg.reply_text(text, reply_markup=get_back_to_menu_keyboard())
                else:
                    await send_selfdestruct_message(update, context, text, reply_markup=get_back_to_menu_keyboard())
                return

    earned = random.randint(FARM_MIN, FARM_MAX)
    if random.randint(1, 100) == 1:
        earned *= 5
        await context.bot.send_message(chat_id="@guild_antysocial", text=f"🌟 @{username} наткнулся на золотую жилу! +{earned} ОАС!")
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
        balance_val, blunts, _, _, _, _, _, _, _, _, _ = player

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
        balance_val, _, guild, _, _, _, _, _, _, _, _ = player

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
        _, blunts, guild, _, _, _, _, _, _, _, _ = player

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

    balance, blunts, guild, _, last_ritual_str, _, _, _, _, _, _ = player
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
        balance_val, blunts, guild, _, _, _, titles, _, _, _, karma = player

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

    text = f"{username}{guild_emoji}\nРанг: {rank}\n💰 ОАС: {balance_val}\n🌿 Бланты: {blunts}\nГильдия: {guild_name}\n🔰 Карма: {karma}"
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
        if percent < 30: phrase = "«Ты слышишь шёпот Фабрики...»"
        elif percent < 70: phrase = "«Ткань реальности отзывается...»"
        else: phrase = "«Смотритель чувствует твоё приближение...»"
        text += f"\n👁‍🗨 {phrase}"

    await update.message.reply_text(format_message(text), parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

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
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('UPDATE players SET last_farm=NULL WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(format_message("⚡ Кулдаун /farm сброшен!\n-1 Блант"), reply_markup=get_back_to_menu_keyboard())

async def check_secret_titles(user_id, username, context):
    player = get_player(user_id)
    if not player:
        return
    balance, blunts, guild, last_farm, last_ritual, last_daily, titles, _, _, _, _ = player

    if last_farm and '🐾' not in (titles or ''):
        add_title(user_id, '🐾')
        await context.bot.send_message(chat_id=user_id,
            text="👁‍🗨 [СМОТРИТЕЛЬ]\n«Ты сделал первый шаг. Отныне ты известен как **Первый Шаг** 🐾.»\n\nНоси это звание с честью. Или не носи. Мне всё равно.")

    if balance >= 50 and '✨' not in (titles or ''):
        add_title(user_id, '✨')
        await context.bot.send_message(chat_id=user_id,
            text="👁‍🗨 [СМОТРИТЕЛЬ]\n«В тебе зажглась Искра ✨. Гильдия чувствует твоё присутствие.»")

    if balance >= 2000 and '👻' not in (titles or ''):
        add_title(user_id, '👻')
        await context.bot.send_message(chat_id=user_id,
            text="👁‍🗨 [СМОТРИТЕЛЬ]\n«Ты достиг ранга Призрака, не совершив ни одной покупки. Ты — **Призрачный Гончий** 👻. Редкая порода.»")

async def check_rank_up(context: ContextTypes.DEFAULT_TYPE, user_id: int, username: str, old_balance: int, new_balance: int):
    if old_balance < 500 <= new_balance:
        await context.bot.send_message(chat_id="@guild_antysocial",
            text=f"🎉 @{username} достиг ранга **Ветеран**! Смотритель доволен.", parse_mode='Markdown')
    if old_balance < 2000 <= new_balance:
        await context.bot.send_message(chat_id="@guild_antysocial",
            text=f"👻 @{username} стал **Призраком**! Ткань реальности дрожит.", parse_mode='Markdown')

async def catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Перейти", url="https://t.me/antysocialshop")]
    ])
    await update.message.reply_text("🕯️ ANTYSOCIALSHOP · КАТАЛОГ", reply_markup=keyboard)

reservations = {}
async def claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = get_player(user_id)
    if not player:
        await update.message.reply_text("❌ Сначала /start", reply_markup=get_back_to_menu_keyboard())
        return
    balance = player[0]
    if balance >= 2000: cost = 100
    elif balance >= 500: cost = 150
    else: cost = 200
    if balance < cost:
        await update.message.reply_text(f"❌ Недостаточно ОАС. Нужно {cost}.", reply_markup=get_back_to_menu_keyboard())
        return
    try:
        art = context.args[0]
        if not art.startswith('#'): art = '#' + art
    except IndexError:
        await update.message.reply_text("❌ Укажи код: /claim #BAL001", reply_markup=get_back_to_menu_keyboard())
        return
    if art in reservations:
        await update.message.reply_text("❌ Уже зарезервирован.", reply_markup=get_back_to_menu_keyboard())
        return
    update_balance(user_id, username, -cost)
    reservations[art] = {"user_id": user_id, "username": username, "expires": datetime.now() + timedelta(hours=24)}
    await update.message.reply_text(
        f"🔒 Экземпляр {art} закреплён за тобой на 24 часа.\nАктивируй в ЛС Смотрителя.",
        reply_markup=get_back_to_menu_keyboard()
    )
    await context.bot.send_message(
        chat_id="@guild_antysocial",
        text=f"🔒 [РЕЗЕРВ] @{username} застолбил {art} на 24 часа."
    )

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        last_daily = None
    else:
        last_daily = player[5]

    if last_daily:
        last = datetime.fromisoformat(last_daily)
        if datetime.now() - last < timedelta(hours=24):
            remaining = timedelta(hours=24) - (datetime.now() - last)
            await msg.reply_text(f"⏳ Жди {remaining.seconds//3600} ч", reply_markup=get_back_to_menu_keyboard())
            return

    r = random.randint(1, 100)
    if r <= 40:
        prize = 5; prize_text = "+5 ОАС"; update_balance(user_id, username, 5)
    elif r <= 65:
        prize = 10; prize_text = "+10 ОАС"; update_balance(user_id, username, 10)
    elif r <= 80:
        prize = 1; prize_text = "+1 Блант"; update_blunts(user_id, username, 1)
    elif r <= 90:
        prize = 20; prize_text = "+20 ОАС"; update_balance(user_id, username, 20)
    elif r <= 97:
        prize = 2; prize_text = "+2 Бланта"; update_blunts(user_id, username, 2)
    else:
        prize = 50; prize_text = "🎡 ДЖЕКПОТ! +50 ОАС"; update_balance(user_id, username, 50)
        await context.bot.send_message(chat_id="@guild_antysocial",
            text=f"🎡 @{username} сорвал **Джекпот** (+50 ОАС) на Колесе Смотрителя!", parse_mode='Markdown')

    update_last_daily(user_id)
    await msg.reply_text(f"🎡 Колесо: {prize_text}", reply_markup=get_back_to_menu_keyboard())
    await check_secret_titles(user_id, username, context)

async def proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*), SUM(balance) FROM players')
    total_players, total_oas = c.fetchone()
    conn.close()
    await update.message.reply_text(
        f"📊 СТАТИСТИКА ГИЛЬДИИ\n\n👥 Адептов: {total_players or 0}\n💰 ОАС в обращении: {total_oas or 0}",
        reply_markup=get_back_to_menu_keyboard()
    )

async def pin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    player = get_player(user_id)
    if not player or player[0] < 200:
        await update.message.reply_text("❌ Недостаточно ОАС. Нужно 200.", reply_markup=get_back_to_menu_keyboard())
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение, которое хотите закрепить.", reply_markup=get_back_to_menu_keyboard())
        return
    update_balance(user_id, update.effective_user.username, -200)
    await update.message.reply_to_message.pin()
    await update.message.reply_text("📌 Закреплено на 1 час. Смотритель наблюдает.", reply_markup=get_back_to_menu_keyboard())

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot: continue
        await update.message.reply_text(f"🕋 [Смотритель] Новый сигнал. @{member.username or member.first_name}, докажи ценность: /start")

async def warden_whisper(context: ContextTypes.DEFAULT_TYPE):
    whispers = [
        "🕯️ [Смотритель] Я вижу, как растёт напряжение между Гильдиями. Это... интересно.",
        "⚜️ [Смотритель] Сегодня удача благоволит Белым. Проверьте /smoke.",
        "🏭 [Смотритель] Фабрика №9 работает на пределе. Новые экземпляры скоро появятся.",
        "👁‍🗨 [Смотритель] Один из вас сегодня получит знак. Будьте внимательны."
    ]
    await context.bot.send_message(chat_id="@guild_antysocial", text=random.choice(whispers))

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
    conn.close()

    channel_desc = f"👥 Адептов: {total_players} | 💰 ОАС: {total_oas} | 📦 Экземпляров: 0"
    chat_pin_text = (
        f"🕋 **СТАНЦИЯ ПРЕДЕЛ | ПУЛЬС**\n\n"
        f"🕯️ Чёрная: {black} | ⚜️ Белая: {white}\n"
        f"👥 Всего адептов: {total_players}\n"
        f"💰 ОАС в системе: {total_oas}\n"
        f"📦 Экземпляров доставлено: 0\n\n"
        f"_Смотритель наблюдает. Система жива._"
    )

    try:
        await context.bot.set_chat_description(chat_id="@antysocialshop", description=channel_desc)
        if 'pin_message_id' in context.bot_data:
            try:
                await context.bot.edit_message_text(
                    chat_id="@guild_antysocial",
                    message_id=context.bot_data['pin_message_id'],
                    text=chat_pin_text,
                    parse_mode='Markdown'
                )
            except:
                msg = await context.bot.send_message(
                    chat_id="@guild_antysocial",
                    text=chat_pin_text,
                    parse_mode='Markdown'
                )
                await msg.pin()
                context.bot_data['pin_message_id'] = msg.message_id
        else:
            msg = await context.bot.send_message(
                chat_id="@guild_antysocial",
                text=chat_pin_text,
                parse_mode='Markdown'
            )
            await msg.pin()
            context.bot_data['pin_message_id'] = msg.message_id
    except Exception as e:
        print(f"Ошибка обновления пульса: {e}")

async def refresh_pulse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update_pulse(context)
    await update.message.reply_text("✅ Пульс обновлён.")

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

        welcome_text = (
            "🎉 *Добро пожаловать в Гильдию antysocialshop!*\n\n"
            "▸ _Смотритель приветствует тебя._\n"
            "▸ _Здесь добываются редкие экземпляры, зарабатывают Очки Антисошл (🍬), курят бланты и вступают в гильдии._\n\n"
            "🕯️ *ЧЁРНАЯ ГИЛЬДИЯ* — стабильность, ритуалы, власть.\n"
            "⚜️ *БЕЛАЯ ГИЛЬДИЯ* — азарт, удача, танец на лезвии.\n\n"
            "▸ _Выбери свой путь:_"
        )
        full_text = bonus_msg + welcome_text
        await update.message.reply_text(
            full_text,
            reply_markup=get_main_menu_keyboard(user_id),
            parse_mode='Markdown'
        )
        return

    if not player:
        update_balance(user_id, username, 0)
        update_blunts(user_id, username, 0)
        activation_text = (
            "👁‍🗨 *Смотритель заметил тебя.*\n"
            "🎁 Нажми, чтобы получить 100 🍬 и войти в 🔒 закрытый сектор."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ АКТИВИРОВАТЬ ТЕРМИНАЛ", callback_data='activate_menu')]
        ])
        await update.message.reply_text(
            activation_text,
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        return

    guild = player[2]
    welcome_back = "⚔️ *С возвращением в Гильдию!*\n\n"
    if guild == 'BLACK':
        welcome_back += "🕯️ Ты состоишь в Чёрной Гильдии.\n"
    elif guild == 'WHITE':
        welcome_back += "⚜️ Ты состоишь в Белой Гильдии.\n"
    else:
        welcome_back += "Ты пока не в Гильдии. Вступи, чтобы получить бонусы.\n"
    welcome_back += "\n🎮 *Твой терминал:*"
    await update.message.reply_text(
        welcome_back,
        reply_markup=get_main_menu_keyboard(user_id),
        parse_mode='Markdown'
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        msg = update.message
    await msg.reply_text("🎮 *Главное меню*", reply_markup=get_main_menu_keyboard(), parse_mode='Markdown')

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Недостаточно прав.", reply_markup=get_back_to_menu_keyboard())
        return
    try:
        if update.message.reply_to_message:
            target_user = update.message.reply_to_message.from_user
            target_id = target_user.id
            target_name = target_user.username or target_user.first_name
            amount = int(context.args[0])
            update_balance(target_id, target_name, amount)
            await update.message.reply_text(f"✅ Игроку {target_name} начислено {amount} ОАС.", reply_markup=get_back_to_menu_keyboard())
        else:
            await update.message.reply_text("Ответьте на сообщение пользователя и напишите `/add <сумма>`", reply_markup=get_back_to_menu_keyboard())
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: ответьте на сообщение игрока и напишите `/add <сумма>`", reply_markup=get_back_to_menu_keyboard())

# Русские команды
async def balance_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await balance(update, context)
async def craft_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await craft(update, context)
async def smoke_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await smoke(update, context)
async def ritual_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await ritual(update, context)
async def privilege_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await privilege(update, context)
async def claim_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await claim(update, context)
async def daily_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await daily(update, context)
async def catalog_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await catalog(update, context)
async def rush_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await rush(update, context)

async def guild_join_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    parts = text.split()
    if len(parts) > 1:
        context.args = parts[1:]
    else:
        context.args = []
    await guild_join(update, context)

async def handle_chat_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    mapping = {
        'фарм': farm, 'farm': farm,
        'дунуть': smoke, 'smoke': smoke,
        'крафт': craft, 'craft': craft,
        'баланс': balance, 'balance': balance,
        'колесо': daily, 'daily': daily,
        'ускорение': rush, 'rush': rush
    }
    if text in mapping:
        await mapping[text](update, context)

def main():
    print("=== [DEBUG] main() started ===")
    try:
        init_db()
        print("=== [DEBUG] init_db() completed ===")
    except Exception as e:
        print(f"=== [DEBUG] init_db() FAILED: {e} ===")
        raise

    print("=== [DEBUG] Starting web server thread... ===")
    web_thread = Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    print("=== [DEBUG] Web server thread started ===")

    print("=== [DEBUG] Building Application... ===")
    app = Application.builder().token(TOKEN).build()
    print("=== [DEBUG] Application built, registering handlers... ===")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("farm", farm))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("craft", craft))
    app.add_handler(CommandHandler("smoke", smoke))
    app.add_handler(CommandHandler("ritual", ritual))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("guild", guild_join))
    app.add_handler(CommandHandler("privilege", privilege))
    app.add_handler(CommandHandler("claim", claim))
    app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("proof", proof))
    app.add_handler(CommandHandler("pin", pin_message))
    app.add_handler(CommandHandler("catalog", catalog))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("rush", rush))
    app.add_handler(CommandHandler("collect", collect))
    app.add_handler(CommandHandler("pulse", refresh_pulse))

    app.add_handler(MessageHandler(filters.Regex(r'^/баланс$'), balance_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/крафт$'), craft_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/дунуть$'), smoke_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/ритуал$'), ritual_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/привилегия$'), privilege_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/забрать(?:\s+(.+))?$'), claim_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/дейли$'), daily_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/каталог$'), catalog_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/вступить(?:\s+(.+))?$'), guild_join_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/ускорение$'), rush_ru))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r'^(фарм|farm|дунуть|smoke|крафт|craft|баланс|balance|колесо|daily|ускорение|rush)$'), handle_chat_shortcut))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(CallbackQueryHandler(button_handler))

    job_queue = app.job_queue
    job_queue.run_repeating(warden_whisper, interval=14400, first=10)
    job_queue.run_repeating(update_pulse, interval=3600, first=10)

    print("=== [DEBUG] Handlers registered. Starting polling... ===")
    try:
        app.run_polling()
    except Exception as e:
        print(f"=== [DEBUG] run_polling() CRASHED: {e} ===")
        import traceback
        traceback.print_exc()
        raise

if __name__ == '__main__':
    print("=== [DEBUG] Script started ===")
    try:
        main()
    except Exception as e:
        print(f"=== [DEBUG] FATAL ERROR in main: {e} ===")
        import traceback
        traceback.print_exc()
