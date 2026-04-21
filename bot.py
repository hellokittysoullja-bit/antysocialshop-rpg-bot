# bot.py
import os
import logging
import random
import sqlite3
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# === ВЕБ-СЕРВЕР ДЛЯ RENDER (ЧТОБЫ НЕ ЗАСЫПАЛ) ===
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
                  last_ritual TIMESTAMP)''')
    conn.commit()
    conn.close()
    print("База данных инициализирована (с гильдиями и ритуалом).")

def get_player(user_id):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT balance, blunts, guild, last_farm, last_ritual FROM players WHERE user_id=?', (user_id,))
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
        return {'smoke_save_chance': 0, 'ritual_available': True}
    elif guild == 'WHITE':
        return {'smoke_save_chance': 20, 'ritual_available': False}
    else:
        return {'smoke_save_chance': 0, 'ritual_available': False}

# === ГЕНЕРАЦИЯ ГЛАВНОГО МЕНЮ (КНОПКИ) ===
def get_main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("🧵 Фармить ОАС", callback_data='farm')],
        [InlineKeyboardButton("💰 Баланс", callback_data='balance'),
         InlineKeyboardButton("🌿 Крафт Бланта", callback_data='craft')],
        [InlineKeyboardButton("💨 Дунуть", callback_data='smoke'),
         InlineKeyboardButton("🕯️ Ритуал", callback_data='ritual')],
        [InlineKeyboardButton("📊 Статус", callback_data='status'),
         InlineKeyboardButton("🏆 Топ", callback_data='top')],
        [InlineKeyboardButton("🕋 Гильдии", callback_data='guild_info'),
         InlineKeyboardButton("📜 Правила", callback_data='rules')]
    ]
    return InlineKeyboardMarkup(keyboard)

# === ОБРАБОТЧИК НАЖАТИЙ НА КНОПКИ (ИСПРАВЛЕНО) ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'farm':
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

# === АДАПТАЦИЯ ФУНКЦИЙ ДЛЯ РАБОТЫ С CALLBACK_QUERY ===
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
        balance, blunts, guild, last_farm_str, _ = player
        if last_farm_str:
            last_farm = datetime.fromisoformat(last_farm_str)
            if datetime.now() - last_farm < timedelta(hours=FARM_COOLDOWN_HOURS):
                remaining = timedelta(hours=FARM_COOLDOWN_HOURS) - (datetime.now() - last_farm)
                await msg.reply_text(f"⏳ Энергия восстанавливается. Попробуйте через {remaining.seconds//60} мин.")
                return

    earned = random.randint(FARM_MIN, FARM_MAX)
    update_balance(user_id, username, earned)
    update_last_farm(user_id)
    new_balance = get_player(user_id)[0]
    await msg.reply_text(f"🍬 Вы собрали *{earned}* ОАС.\n💰 Баланс: *{new_balance}* ОАС", parse_mode='Markdown')

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
        balance_val, blunts, _, _, _ = player
    await msg.reply_text(f"💰 ОАС: *{balance_val}*\n🌿 Бланты: *{blunts}*", parse_mode='Markdown')

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
        balance_val, _, guild, _, _ = player

    if balance_val < 5:
        await msg.reply_text("❌ Недостаточно ОАС. Нужно 5 ОАС для крафта 1 Бланта.")
        return

    update_balance(user_id, username, -5)
    update_blunts(user_id, username, 1)
    new_balance, new_blunts, _, _, _ = get_player(user_id)

    if guild == 'BLACK':
        craft_msg = "🌿 Ты сплетаешь тьму в Блант..."
    elif guild == 'WHITE':
        craft_msg = "🌿 Ты очищаешь волокна в Блант..."
    else:
        craft_msg = "🌿 Ты закрафтил 1 Блант!"

    await msg.reply_text(f"{craft_msg}\n💰 ОАС: {new_balance}\n🌿 Бланты: {new_blunts}")

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
        _, blunts, guild, _, _ = player

    if blunts < 1:
        await msg.reply_text("❌ У тебя нет Блантов. Используй /craft чтобы создать их.")
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
        new_balance, new_blunts, _, _, _ = get_player(user_id)
        message += f"🍬 +{oas_gain} ОАС\n💰 Баланс: {new_balance} ОАС"
    else:
        message += "✨ Никакого видимого эффекта."

    message += spend_msg
    await msg.reply_text(message, parse_mode='Markdown')

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
        await msg.reply_text("❌ Сначала зарегистрируйся через /start.")
        return

    balance, blunts, guild, _, last_ritual_str = player
    if guild != 'BLACK':
        await msg.reply_text("❌ Только члены 🕯️ **Чёрной Гильдии** могут проводить Ритуал.", parse_mode='Markdown')
        return

    if last_ritual_str:
        last_ritual = datetime.fromisoformat(last_ritual_str)
        if datetime.now() - last_ritual < timedelta(hours=24):
            remaining = timedelta(hours=24) - (datetime.now() - last_ritual)
            await msg.reply_text(f"⏳ Ритуал можно проводить раз в 24 часа. Попробуйте через {remaining.seconds//3600} ч.")
            return

    update_balance(user_id, username, 15)
    update_last_ritual(user_id)
    new_balance = get_player(user_id)[0]
    await msg.reply_text(
        f"🕯️ *Ритуал Чёрной Гильдии завершён.*\n"
        f"«Тьма одарила тебя стабильностью.»\n"
        f"🍬 +15 ОАС\n💰 Баланс: *{new_balance}* ОАС",
        parse_mode='Markdown'
    )

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
    else:
        balance_val, _, guild, _, _ = player

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
    await msg.reply_text(text, parse_mode='Markdown')

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        msg = update.message

    top_players = get_top(10)
    if not top_players:
        await msg.reply_text("Топ пока пуст.")
        return
    text = "🏆 *ТОП-10 ИГРОКОВ* 🏆\n\n"
    for i, (name, bal) in enumerate(top_players, 1):
        medal = "🥇" if i==1 else "🥈" if i==2 else "🥉" if i==3 else f"{i}."
        text += f"{medal} {name}: {bal} ОАС\n"
    await msg.reply_text(text, parse_mode='Markdown')

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        msg = update.callback_query.message
        await update.callback_query.answer()
    else:
        msg = update.message

    await msg.reply_text(
        "📜 *ПРАВИЛА НАЧИСЛЕНИЯ ОАС*\n\n"
        f"• /farm — раз в час: {FARM_MIN}-{FARM_MAX} ОАС\n"
        "• Репост поста/сторис с отметкой: +20 ОАС (вручную админом)\n"
        "• Покупка: +5% от суммы заказа в ОАС\n"
        "• Фото распаковки/образа с отметкой: +50 ОАС\n"
        "• Приглашение друга, совершившего покупку: +100 ОАС\n\n"
        "🌿 *БЛАНТЫ*\n"
        "/craft или /крафт — обменять 5 ОАС на 1 Блант\n"
        "/balance или /баланс — проверить запасы\n"
        "/smoke или /дунуть — активировать Блант (эффекты Смотрителя)\n\n"
        "🕯️⚜️ *ГИЛЬДИИ*\n"
        "/guild join BLACK или /вступить BLACK — Чёрная Гильдия (Ритуал)\n"
        "/guild join WHITE или /вступить WHITE — Белая Гильдия (Сохранение Бланта)\n"
        "/guild info — статистика Гильдий\n\n"
        "Ранги:\n💉 Рекрут: 0-499 ОАС\n⚔️ Ветеран: 500-1999 ОАС\n👻 Призрак: 2000+ ОАС",
        parse_mode='Markdown'
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
        await msg.reply_text(f"❌ Ты уже состоишь в Гильдии {emoji} **{current_guild}**.", parse_mode='Markdown')
        return

    try:
        guild_name = context.args[0].upper()
        if guild_name not in ['BLACK', 'WHITE']:
            await msg.reply_text("❌ Неверное название. Доступные Гильдии: **BLACK**, **WHITE**.\nПример: `/guild join BLACK`", parse_mode='Markdown')
            return

        set_guild(user_id, guild_name)
        emoji = "🕯️" if guild_name == 'BLACK' else "⚜️"
        await msg.reply_text(f"✅ Ты вступил в Гильдию {emoji} **{guild_name}**.", parse_mode='Markdown')
    except IndexError:
        await msg.reply_text("❌ Укажи название Гильдии: `/guild join BLACK` или `/guild join WHITE`.")

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

    await msg.reply_text(text, parse_mode='Markdown')

# === ОБРАБОТЧИКИ КОМАНД ===
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

    welcome_text = "⚔️ *Добро пожаловать в Гильдию antysocialshop!*\n\n"
    if guild == 'BLACK':
        welcome_text += "🕯️ Ты состоишь в **Чёрной Гильдии**.\n"
        welcome_text += "Раз в 24 часа тебе доступен `/ritual` (гарантированные +15 ОАС).\n\n"
    elif guild == 'WHITE':
        welcome_text += "⚜️ Ты состоишь в **Белой Гильдии**.\n"
        welcome_text += "При использовании `/smoke` есть 20% шанс сохранить Блант.\n\n"
    else:
        welcome_text += "Ты пока не в Гильдии.\n"
        welcome_text += "Вступи, чтобы получить уникальные бонусы:\n"
        welcome_text += "`/guild join BLACK` — 🕯️ Чёрная (Ритуал)\n"
        welcome_text += "`/guild join WHITE` — ⚜️ Белая (Сохранение Бланта)\n\n"

    welcome_text += "🎮 *Используй кнопки ниже, чтобы играть:*"

    await update.message.reply_text(welcome_text, reply_markup=get_main_menu_keyboard(), parse_mode='Markdown')

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎮 *Главное меню*", reply_markup=get_main_menu_keyboard(), parse_mode='Markdown')

# === АЛЬТЕРНАТИВНЫЕ КОМАНДЫ (РУССКИЕ) ===
async def balance_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await balance(update, context)

async def craft_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await craft(update, context)

async def smoke_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await smoke(update, context)

async def ritual_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ritual(update, context)

async def guild_join_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await guild_join(update, context)

# === АДМИН-КОМАНДЫ ===
async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Недостаточно прав.")
        return
    try:
        if update.message.reply_to_message:
            target_user = update.message.reply_to_message.from_user
            target_id = target_user.id
            target_name = target_user.username or target_user.first_name
            amount = int(context.args[0])
            update_balance(target_id, target_name, amount)
            await update.message.reply_text(f"✅ Игроку {target_name} начислено {amount} ОАС.")
        else:
            await update.message.reply_text("Ответьте на сообщение пользователя и напишите `/add <сумма>`")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: ответьте на сообщение игрока и напишите `/add <сумма>`")

# === ЗАПУСК ===
def main():
    init_db()
    web_thread = Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()

    app = Application.builder().token(TOKEN).build()
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
    app.add_handler(CommandHandler("add", add))
    # Альтернативные команды
    app.add_handler(CommandHandler("баланс", balance_ru))
    app.add_handler(CommandHandler("крафт", craft_ru))
    app.add_handler(CommandHandler("дунуть", smoke_ru))
    app.add_handler(CommandHandler("ритуал", ritual_ru))
    app.add_handler(CommandHandler("вступить", guild_join_ru))
    # Обработчик кнопок
    app.add_handler(CallbackQueryHandler(button_handler))

    print("Бот с кнопками запущен...")
    app.run_polling()

if __name__ == '__main__':
    main()
