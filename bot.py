import os
import logging
import random
import sqlite3
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# === Фальшивый веб-сервер для Render (чтобы порт был открыт) ===
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Antysocialshop RPG Bot is alive!"

def run_web_server():
    port = int(os.getenv("PORT", 10000))
    web_app.run(host='0.0.0.0', port=port)

# === НАСТРОЙКИ БОТА ===
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Бери из переменной окружения или 0
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
                  last_farm TIMESTAMP)''')
    conn.commit()
    conn.close()

def get_player(user_id):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT balance, last_farm FROM players WHERE user_id=?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def update_balance(user_id, username, amount):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO players (user_id, username, balance) VALUES (?, ?, 0)', (user_id, username))
    c.execute('UPDATE players SET balance = balance + ?, username = ? WHERE user_id = ?', (amount, username, user_id))
    conn.commit()
    conn.close()

def update_last_farm(user_id):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('UPDATE players SET last_farm = ? WHERE user_id = ?', (datetime.now(), user_id))
    conn.commit()
    conn.close()

def get_top(limit=10):
    conn = sqlite3.connect('players.db')
    c = conn.cursor()
    c.execute('SELECT username, balance FROM players ORDER BY balance DESC LIMIT ?', (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

# === ОБРАБОТЧИКИ КОМАНД ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚔️ *Добро пожаловать в Гильдию antysocialshop!*\n\n"
        "Доступные команды:\n"
        "/farm — собирать ОАС (раз в час)\n"
        "/status — ваш баланс и ранг\n"
        "/top — топ-10 игроков\n"
        "/rules — правила начисления",
        parse_mode='Markdown'
    )

async def farm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = get_player(user_id)
    if player:
        balance, last_farm_str = player
        if last_farm_str:
            last_farm = datetime.fromisoformat(last_farm_str)
            if datetime.now() - last_farm < timedelta(hours=FARM_COOLDOWN_HOURS):
                remaining = timedelta(hours=FARM_COOLDOWN_HOURS) - (datetime.now() - last_farm)
                await update.message.reply_text(f"⏳ Энергия восстанавливается. Попробуйте через {remaining.seconds//60} мин.")
                return
    earned = random.randint(FARM_MIN, FARM_MAX)
    update_balance(user_id, username, earned)
    update_last_farm(user_id)
    new_balance = get_player(user_id)[0]
    await update.message.reply_text(f"🧵 Вы собрали *{earned}* ОАС.\nБаланс: *{new_balance}* ОАС", parse_mode='Markdown')

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    player = get_player(user_id)
    if not player:
        update_balance(user_id, username, 0)
        balance = 0
    else:
        balance = player[0]
    if balance >= 2000:
        rank = "👻 Призрак"
    elif balance >= 500:
        rank = "⚔️ Ветеран"
    else:
        rank = "🛡️ Рекрут"
    await update.message.reply_text(f"*{username}*\nРанг: {rank}\nБаланс: *{balance}* ОАС", parse_mode='Markdown')

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top_players = get_top(10)
    if not top_players:
        await update.message.reply_text("Топ пока пуст.")
        return
    text = "🏆 *ТОП-10 ИГРОКОВ* 🏆\n\n"
    for i, (name, bal) in enumerate(top_players, 1):
        medal = "🥇" if i==1 else "🥈" if i==2 else "🥉" if i==3 else f"{i}."
        text += f"{medal} {name}: {bal} ОАС\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📜 *ПРАВИЛА НАЧИСЛЕНИЯ ОАС*\n\n"
        f"• /farm — раз в час: {FARM_MIN}-{FARM_MAX} ОАС\n"
        "• Репост поста/сторис с отметкой: +20 ОАС (вручную админом)\n"
        "• Покупка: +5% от суммы заказа в ОАС\n"
        "• Фото распаковки/образа с отметкой: +50 ОАС\n"
        "• Приглашение друга, совершившего покупку: +100 ОАС\n\n"
        "Ранги:\n🛡️ Рекрут: 0-499 ОАС\n⚔️ Ветеран: 500-1999 ОАС\n👻 Призрак: 2000+ ОАС",
        parse_mode='Markdown'
    )

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
    # Запускаем веб-сервер в отдельном потоке
    web_thread = Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()

    # Запускаем Telegram-бота
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("farm", farm))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("add", add))
    print("Бот запущен...")
    app.run_polling()

if __name__ == '__main__':
    main()
