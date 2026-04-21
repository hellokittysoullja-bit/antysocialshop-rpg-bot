# bot.py
import os
import random
import sqlite3
from datetime import datetime, timedelta
from threading import Thread
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

# === НАСТРОЙКИ ===
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
FARM_COOLDOWN_HOURS = 1
FARM_MIN = 1
FARM_MAX = 10

# === ИНИЦИАЛИЗАЦИЯ БД ===
def init_db():
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS players
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  balance INTEGER DEFAULT 0,
                  blunts INTEGER DEFAULT 0,
                  guild TEXT DEFAULT NULL,
                  last_farm TIMESTAMP,
                  last_ritual TIMESTAMP,
                  last_daily TIMESTAMP,
                  titles TEXT DEFAULT '')''')
    conn.commit()
    conn.close()
    print("База данных инициализирована.")

def get_player(user_id):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT balance, blunts, guild, last_farm, last_ritual, last_daily, titles FROM players WHERE user_id=?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def update_balance(user_id, username, amount):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO players (user_id, username, balance, blunts) VALUES (?, ?, 0, 0)', (user_id, username))
    c.execute('UPDATE players SET balance = balance + ?, username = ? WHERE user_id = ?', (amount, username, user_id))
    conn.commit()
    conn.close()

def update_blunts(user_id, username, amount):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO players (user_id, username, balance, blunts) VALUES (?, ?, 0, 0)', (user_id, username))
    c.execute('UPDATE players SET blunts = blunts + ?, username = ? WHERE user_id = ?', (amount, username, user_id))
    conn.commit()
    conn.close()

def update_last_farm(user_id):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('UPDATE players SET last_farm = ? WHERE user_id = ?', (datetime.now(), user_id))
    conn.commit()
    conn.close()

def update_last_ritual(user_id):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('UPDATE players SET last_ritual = ? WHERE user_id = ?', (datetime.now(), user_id))
    conn.commit()
    conn.close()

def update_last_daily(user_id):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('UPDATE players SET last_daily = ? WHERE user_id = ?', (datetime.now(), user_id))
    conn.commit()
    conn.close()

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
    c.execute('SELECT username, balance FROM players ORDER BY balance DESC LIMIT ?', (limit,))
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

# === ГЛАВНОЕ МЕНЮ (КНОПКИ) ===
def get_main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("🍬 Фармить ОАС", callback_data='farm')],
        [InlineKeyboardButton("💰 Баланс", callback_data='balance'),
         InlineKeyboardButton("🌿 Крафт Бланта", callback_data='craft')],
        [InlineKeyboardButton("💨 Дунуть", callback_data='smoke'),
         InlineKeyboardButton("🕯️ Ритуал", callback_data='ritual')],
        [InlineKeyboardButton("📊 Статус", callback_data='status'),
         InlineKeyboardButton("🏆 Топ", callback_data='top')],
        [InlineKeyboardButton("🕋 Гильдии", callback_data='guild_info'),
         InlineKeyboardButton("📜 Законы", callback_data='rules')],
        [InlineKeyboardButton("🪪 Привилегия", callback_data='privilege'),
         InlineKeyboardButton("🔒 Забрать", callback_data='claim_help')],
        [InlineKeyboardButton("🎡 Колесо", callback_data='daily')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_back_to_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 Меню", callback_data='menu')]])

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
        await query.message.reply_text("Используй команду `/claim #КОД` или `/забрать #КОД`, чтобы застолбить экземпляр на 24 часа.", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    elif data == 'daily':
        await daily(update, context)

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
    player = get_player(user_id)

    if player:
        balance, blunts, guild, last_farm_str, _, _, _ = player
        if last_farm_str:
            last_farm = datetime.fromisoformat(last_farm_str)
            if datetime.now() - last_farm < timedelta(hours=FARM_COOLDOWN_HOURS):
                remaining = timedelta(hours=FARM_COOLDOWN_HOURS) - (datetime.now() - last_farm)
                await msg.reply_text(f"⏳ Энергия восстанавливается. Попробуйте через {remaining.seconds//60} мин.", reply_markup=get_back_to_menu_keyboard())
                return

    earned = random.randint(FARM_MIN, FARM_MAX)
    old_balance = player[0] if player else 0
    update_balance(user_id, username, earned)
    update_last_farm(user_id)
    new_balance = get_player(user_id)[0]
    await msg.reply_text(f"🍬 Вы собрали *{earned}* ОАС.\n💰 Баланс: *{new_balance}* ОАС", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
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
        balance_val, blunts, _, _, _, _, _ = player
    await msg.reply_text(f"💰 ОАС: *{balance_val}*\n🌿 Бланты: *{blunts}*", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

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
        balance_val, _, guild, _, _, _, _ = player

    if balance_val < 5:
        await msg.reply_text("❌ Недостаточно ОАС. Нужно 5 ОАС для крафта 1 Бланта.", reply_markup=get_back_to_menu_keyboard())
        return

    update_balance(user_id, username, -5)
    update_blunts(user_id, username, 1)
    new_balance, new_blunts, _, _, _, _, _ = get_player(user_id)

    if guild == 'BLACK':
        craft_msg = "🌿 Ты сплетаешь тьму в Блант..."
    elif guild == 'WHITE':
        craft_msg = "🌿 Ты очищаешь волокна в Блант..."
    else:
        craft_msg = "🌿 Ты закрафтил 1 Блант!"

    await msg.reply_text(f"{craft_msg}\n💰 ОАС: {new_balance}\n🌿 Бланты: {new_blunts}", reply_markup=get_back_to_menu_keyboard())

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
        _, blunts, guild, _, _, _, _ = player

    if blunts < 1:
        await msg.reply_text("❌ У тебя нет Блантов. Используй /craft чтобы создать их.", reply_markup=get_back_to_menu_keyboard())
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
        spend_msg = "\n⚜️ *Белая Гильдия сохранила твой Блант!*"

    r = random.randint(1, 100)
    effect_name = ""
    flavor_text = ""
    oas_gain = 0

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

    message = f"💨 Ты скурил блант...\n\n{flavor_text}\n\n"
    message += f"👁‍🗨 **Эффект: {effect_name}**\n"
    if oas_gain > 0:
        new_balance, new_blunts, _, _, _, _, _ = get_player(user_id)
        message += f"🍬 +{oas_gain} ОАС\n💰 Баланс: {new_balance} ОАС"
    else:
        message += "✨ Никакого видимого эффекта."

    message += spend_msg
    await msg.reply_text(message, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
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
        await msg.reply_text("❌ Сначала зарегистрируйся через /start.", reply_markup=get_back_to_menu_keyboard())
        return

    balance, blunts, guild, _, last_ritual_str, _, _ = player
    if guild != 'BLACK':
        await msg.reply_text("❌ Только члены 🕯️ **Чёрной Гильдии** могут проводить Ритуал.", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
        return

    if last_ritual_str:
        last_ritual = datetime.fromisoformat(last_ritual_str)
        if datetime.now() - last_ritual < timedelta(hours=24):
            remaining = timedelta(hours=24) - (datetime.now() - last_ritual)
            await msg.reply_text(f"⏳ Ритуал можно проводить раз в 24 часа. Попробуйте через {remaining.seconds//3600} ч.", reply_markup=get_back_to_menu_keyboard())
            return

    old_balance = balance
    update_balance(user_id, username, 15)
    update_last_ritual(user_id)
    new_balance = get_player(user_id)[0]
    await msg.reply_text(
        f"🕯️ *Ритуал Чёрной Гильдии завершён.*\n"
        f"«Тьма одарила тебя стабильностью.»\n"
        f"🍬 +15 ОАС\n💰 Баланс: *{new_balance}* ОАС",
        parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard()
    )
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
        balance_val, _, guild, _, _, _, titles = player

    if balance_val >= 2000:
        rank = "👻 Призрак"
    elif balance_val >= 500:
        rank = "⚔️ Ветеран"
    else:
        rank = "💉 Рекрут"

    guild_emoji = ""
    if guild == 'BLACK':
        guild_emoji = " 🕯️"
        guild_name = "**Чёрная**"
    elif guild == 'WHITE':
        guild_emoji = " ⚜️"
        guild_name = "**Белая**"
    else:
        guild_name = "Нет"

    text = f"*{username}*{guild_emoji}\nРанг: {rank}\n💰 Баланс: *{balance_val}* ОАС\nГильдия: {guild_name}"
    if titles:
        text += f"\n🛡️ Титулы: {titles}"

    await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        msg = update.message

    top_players = get_top(10)
    if not top_players:
        await msg.reply_text("Топ пока пуст.", reply_markup=get_back_to_menu_keyboard())
        return
    text = "🏆 *ТОП-10 ИГРОКОВ* 🏆\n\n"
    for i, (name, bal) in enumerate(top_players, 1):
        medal = "🥇" if i==1 else "🥈" if i==2 else "🥉" if i==3 else f"{i}."
        text += f"{medal} {name}: {bal} ОАС\n"
    await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        msg = update.message

    await msg.reply_text(
        "📜 *ЗАКОНЫ ГИЛЬДИИ*\n\n"
        f"• /farm — раз в час: {FARM_MIN}-{FARM_MAX} ОАС\n"
        "• Репост поста/сторис с отметкой: +20 ОАС (вручную админом)\n"
        "• Покупка экземпляра: +5% от суммы заказа в ОАС\n"
        "• Фото распаковки/образа с отметкой: +50 ОАС\n"
        "• Приглашение друга, совершившего покупку: +100 ОАС\n\n"
        "🌿 *БЛАНТЫ*\n"
        "/craft или /крафт — обменять 5 ОАС на 1 Блант\n"
        "/balance или /баланс — проверить запасы\n"
        "/smoke или /дунуть — активировать Блант (эффекты Смотрителя)\n\n"
        "🪪 *ПРИВИЛЕГИИ*\n"
        "/privilege или /привилегия — твоя персональная скидка на экземпляры\n"
        "/claim или /забрать #КОД — застолбить экземпляр на 24 часа\n"
        "/daily или /дейли — ежедневное Колесо Смотрителя\n\n"
        "🕯️⚜️ *ГИЛЬДИИ*\n"
        "/guild join BLACK или /вступить BLACK — Чёрная Гильдия (Ритуал)\n"
        "/guild join WHITE или /вступить WHITE — Белая Гильдия (Сохранение Бланта)\n"
        "/guild info — статистика Гильдий\n\n"
        "Ранги:\n💉 Рекрут: 0-499 ОАС\n⚔️ Ветеран: 500-1999 ОАС\n👻 Призрак: 2000+ ОАС",
        parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard()
    )

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
        update_blunts(user_id, username, 0)

    current_guild = get_guild(user_id)
    if current_guild:
        emoji = "🕯️" if current_guild == 'BLACK' else "⚜️"
        await msg.reply_text(f"❌ Ты уже состоишь в Гильдии {emoji} **{current_guild}**.", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
        return

    try:
        guild_name = context.args[0].upper()
        if guild_name not in ['BLACK', 'WHITE']:
            await msg.reply_text("❌ Неверное название. Доступные Гильдии: **BLACK**, **WHITE**.\nПример: `/guild join BLACK`", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
            return

        set_guild(user_id, guild_name)
        emoji = "🕯️" if guild_name == 'BLACK' else "⚜️"
        await msg.reply_text(f"✅ Ты вступил в Гильдию {emoji} **{guild_name}**.", parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())
    except IndexError:
        await msg.reply_text("❌ Укажи название Гильдии: `/guild join BLACK` или `/guild join WHITE`.", reply_markup=get_back_to_menu_keyboard())

async def guild_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        user = update.callback_query.from_user
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        user = update.effective_user
        msg = update.message

    user_id = user.id
    current_guild = get_guild(user_id)
    counts = count_guilds()

    text = "🕋 **ГИЛЬДИИ ANTYSOCIALSHOP**\n\n"
    text += f"🕯️ **Чёрная**: {counts['BLACK']} чел.\n"
    text += f"⚜️ **Белая**: {counts['WHITE']} чел.\n\n"
    text += "🕯️ **Чёрная**: Ритуал — раз в 24 часа гарантированные +15 ОАС.\n"
    text += "⚜️ **Белая**: 20% шанс сохранить Блант при /smoke.\n\n"

    if current_guild:
        emoji = "🕯️" if current_guild == 'BLACK' else "⚜️"
        text += f"Твоя Гильдия: {emoji} **{current_guild}**"
    else:
        text += "Ты пока не в Гильдии. Вступи: `/guild join BLACK` или `/guild join WHITE`"

    await msg.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

# === ПРИВИЛЕГИЯ РАНГА ===
async def privilege(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    player = get_player(user_id)
    if not player:
        await update.message.reply_text("❌ Сначала зарегистрируйся: /start", reply_markup=get_back_to_menu_keyboard())
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
        guild_note = "🎲 Шанс 20% не потратить ОАС при скидке"
    else:
        guild_note = "🔒 Стабильно"

    text = (
        f"🪪 ПРИВИЛЕГИЯ РАНГА\n\n"
        f"Ранг: {rank} ({guild or 'Нет'})\n"
        f"Баланс: {balance} ОАС\n\n"
        f"🔹 Скидка: 1₽ за {divisor} ОАС\n"
        f"🔹 Лимит: {int(max_percent*100)}% от цены\n"
        f"🔹 {guild_note}\n"
    )

    if target:
        percent = int(balance / target * 100)
        filled = int(percent / 10)
        empty = 10 - filled
        bar = "🟩" * filled + "⬛" * empty
        text += f"\n⚔️ Путь к {'Ветерану' if target==500 else 'Призраку'}\n{bar} {percent}% ({balance}/{target} ОАС)\n"

        if percent < 30:
            phrase = "«Ты слышишь шёпот Фабрики, но она ещё не видит тебя. Продолжай.»"
        elif percent < 70:
            phrase = "«Ткань реальности начинает отзываться на твои действия. Ветераны уже смотрят.»"
        else:
            phrase = "«Смотритель чувствует твоё приближение. Ещё немного, и ты изменишь правила игры.»"
        text += f"\n👁‍🗨 _«{phrase}»_"

    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard())

# === СЕКРЕТНЫЕ ЗВАНИЯ ===
async def check_secret_titles(user_id, username, context):
    player = get_player(user_id)
    if not player:
        return
    balance, blunts, guild, last_farm, last_ritual, last_daily, titles = player

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

# === КАТАЛОГ ===
async def catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Перейти", url="https://t.me/antysocialshop")]
    ])
    await update.message.reply_text(
        "🕯️ ANTYSOCIALSHOP · КАТАЛОГ",
        reply_markup=keyboard
    )

# === ПРОВЕРКА ПОВЫШЕНИЯ РАНГА ===
async def check_rank_up(context: ContextTypes.DEFAULT_TYPE, user_id: int, username: str, old_balance: int, new_balance: int):
    if old_balance < 500 <= new_balance:
        await context.bot.send_message(
            chat_id="@guild_antysocial",
            text=f"🎉 @{username} достиг ранга **Ветеран**! Смотритель доволен.",
            parse_mode='Markdown'
        )
    if old_balance < 2000 <= new_balance:
        await context.bot.send_message(
            chat_id="@guild_antysocial",
            text=f"👻 @{username} стал **Призраком**! Ткань реальности дрожит.",
            parse_mode='Markdown'
        )

# === КОЛЕСО СМОТРИТЕЛЯ ===
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
            await msg.reply_text(f"⏳ Колесо перезарядится через {remaining.seconds//3600} ч.", reply_markup=get_back_to_menu_keyboard())
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
        await context.bot.send_message(
            chat_id="@guild_antysocial",
            text=f"🎡 @{username} сорвал **Джекпот** (+50 ОАС) на Колесе Смотрителя!",
            parse_mode='Markdown'
        )

    update_last_daily(user_id)
    await msg.reply_text(f"🎡 Колесо Смотрителя: {prize_text}", reply_markup=get_back_to_menu_keyboard())
    await check_secret_titles(user_id, username, context)

# === ПИН ЗА ОАС ===
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

# === СТАТИСТИКА ГИЛЬДИИ ===
async def proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*), SUM(balance) FROM players')
    total_players, total_oas = c.fetchone()
    conn.close()
    await update.message.reply_text(
        f"📊 **СТАТИСТИКА ГИЛЬДИИ**\n\n👥 Адептов: **{total_players or 0}**\n💰 ОАС в обращении: **{total_oas or 0}**",
        parse_mode='Markdown', reply_markup=get_back_to_menu_keyboard()
    )

# === РЕЗЕРВ ЭКЗЕМПЛЯРА ===
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

# === АВТО-ПРИВЕТСТВИЕ ===
async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot: continue
        await update.message.reply_text(f"🕋 [Смотритель] Новый сигнал. @{member.username or member.first_name}, докажи ценность: /start")

# === ШЁПОТ СМОТРИТЕЛЯ ===
async def warden_whisper(context: ContextTypes.DEFAULT_TYPE):
    whispers = [
        "🕯️ [Смотритель] Я вижу, как растёт напряжение между Гильдиями. Это... интересно.",
        "⚜️ [Смотритель] Сегодня удача благоволит Белым. Проверьте /smoke.",
        "🏭 [Смотритель] Фабрика №9 работает на пределе. Новые экземпляры скоро появятся.",
        "👁‍🗨 [Смотритель] Один из вас сегодня получит знак. Будьте внимательны."
    ]
    await context.bot.send_message(chat_id="@guild_antysocial", text=random.choice(whispers))

# === ОБРАБОТЧИКИ START, MENU, ADD ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    player = get_player(user_id)
    if not player:
        update_balance(user_id, username, 0)
        update_blunts(user_id, username, 0)
        guild = None
    else:
        guild = player[2]

    welcome_text = (
        "🎉 *Добро пожаловать в Гильдию antysocialshop!*\n\n"
        "▸ _Смотритель приветствует тебя._\n"
        "▸ _Здесь добываются редкие экземпляры, зарабатывают Очки Антисошл (ОАС), курят бланты и вступают в гильдии._\n\n"
        "🕯️ *ЧЁРНАЯ ГИЛЬДИЯ* — стабильность, ритуалы, власть.\n"
        "⚜️ *БЕЛАЯ ГИЛЬДИЯ* — азарт, удача, танец на лезвии.\n\n"
        "▸ _Выбери свой путь:_"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 ОТКРЫТЬ ТЕРМИНАЛ", callback_data='menu')]
    ])

    await update.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode='Markdown')

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

# === РУССКИЕ КОМАНДЫ ===
async def balance_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await balance(update, context)
async def craft_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await craft(update, context)
async def smoke_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await smoke(update, context)
async def ritual_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await ritual(update, context)
async def privilege_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await privilege(update, context)
async def claim_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await claim(update, context)
async def daily_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await daily(update, context)
async def catalog_ru(update: Update, context: ContextTypes.DEFAULT_TYPE): await catalog(update, context)

async def guild_join_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    parts = text.split()
    if len(parts) > 1:
        context.args = parts[1:]
    else:
        context.args = []
    await guild_join(update, context)

# === ЗАПУСК ===
def main():
    init_db()
    web_thread = Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()

    app = Application.builder().token(TOKEN).build()

    # Английские команды
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

    # Русские команды
    app.add_handler(MessageHandler(filters.Regex(r'^/баланс$'), balance_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/крафт$'), craft_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/дунуть$'), smoke_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/ритуал$'), ritual_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/привилегия$'), privilege_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/забрать(?:\s+(.+))?$'), claim_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/дейли$'), daily_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/каталог$'), catalog_ru))
    app.add_handler(MessageHandler(filters.Regex(r'^/вступить(?:\s+(.+))?$'), guild_join_ru))

    # Автоприветствие и кнопки
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Шёпот Смотрителя каждые 4 часа
    job_queue = app.job_queue
    job_queue.run_repeating(warden_whisper, interval=14400, first=10)

    print("Бот с финальными кирпичиками запущен...")
    app.run_polling()

if __name__ == '__main__':
    main()
