# bot.py — ANTY SOCIAL SHOP RPG v6.1 FINAL (исправлены критические ошибки)
import asyncio, logging, os, random, re, json, hashlib, html
from datetime import datetime, timedelta, date, time
from threading import Thread

import aiosqlite
from cachetools import TTLCache
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

# === ВЕБ-СЕРВЕР ===
web_app = Flask(__name__)
@web_app.route("/")
def home():
    return "Antysocialshop RPG Bot is alive!"

def run_web_server():
    port = int(os.getenv("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
FARM_COOLDOWN_HOURS = 0.5
FARM_MIN, FARM_MAX = 45, 100
HAPPY_HOUR_MULTIPLIER = 2
HAPPY_HOUR_DURATION_MIN = 30

player_cache = TTLCache(maxsize=500, ttl=60)  # увеличен TTL
def invalidate_cache(user_id): player_cache.pop(user_id, None)

# Регулярки
RE_FARM = re.compile(r'^/фарм$'); RE_SMOKE = re.compile(r'^/дунуть$')
RE_CRAFT = re.compile(r'^/крафт$'); RE_RITUAL = re.compile(r'^/ритуал$')
RE_TOP = re.compile(r'^/топ$'); RE_LUCK = re.compile(r'^/удача$|^/luck$')
RE_PROFILE = re.compile(r'^/профиль$|^/profile$')
RE_GUILD = re.compile(r'^/guild$|^/вступить$')
RE_SHORTCUTS = re.compile(r'^(фарм|farm|дунуть|smoke|крафт|craft|топ|top|удача|luck|профиль|profile)$', re.IGNORECASE)

WHISPERS = [
    "🩸 Искажение наблюдает за твоими нитями",
    "💠 Кристалл твоей судьбы пульсирует",
    "🕯️ Смотритель помнит всех",
    "🩸 Искажение шепчет твоё имя",
    "🌀 Нити реальности натянуты до предела"
]
NEURO_STATUSES = [
    "Альфа-ритмы нестабильны", "Сенсорная депривация 80%", "Фаза быстрого сна",
    "Нейро-шунт активен", "Предел синаптической проводимости", "Резонанс с Искажением: 12%"
]
FUNNY_REACTIONS = [
    "Выглядит как NFT, который никто не купит.", "Даже Бездна от такого закашлялась.",
    "Это не блант, это крик души.", "Искажение занесло это название в чёрный список.",
    "10/10, лучший блант для того чтобы спрятать его подальше.", "Пахнет так, будто его скрутил сам Ктулху.",
    "Этот блант вызывает желание помыть руки.", "С таким названием только в Бездну.",
    "Я бы такое не выкурил, но звучит гордо."
]

RANKS = [
    ("🪓 Рекрут", 0, 0),
    ("⚔️ Ветеран", 5000, 1500),
    ("🪦 Призрак", 20000, 6000)
]

# === Вспомогательные функции БД ===
async def get_db_connection():
    db = await aiosqlite.connect("players.db")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA journal_mode=WAL")
    db.row_factory = aiosqlite.Row
    return db

async def init_db():
    db = await get_db_connection()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS players (
            user_id INTEGER PRIMARY KEY,
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
            karma INTEGER DEFAULT 0,
            inhaled INTEGER DEFAULT 0,
            smoke_count INTEGER DEFAULT 0,
            farm_count INTEGER DEFAULT 0,
            craft_count INTEGER DEFAULT 0,
            ritual_count INTEGER DEFAULT 0,
            referral_count INTEGER DEFAULT 0,
            last_berserk TIMESTAMP,
            inventory TEXT DEFAULT '[]',
            invited_by INTEGER DEFAULT NULL,
            profile_skins TEXT DEFAULT '{}',
            login_streak INTEGER DEFAULT 0,
            last_login_date DATE,
            oath TEXT DEFAULT '',
            keys INTEGER DEFAULT 0,
            check_count INTEGER DEFAULT 0
        )
    """)
    # Проверка и добавление недостающих колонок
    cur = await db.execute("PRAGMA table_info(players)")
    cols = [r[1] for r in await cur.fetchall()]
    for col, col_type in [
        ("profile_skins","TEXT DEFAULT '{}'"),("login_streak","INTEGER DEFAULT 0"),
        ("last_login_date","DATE"),("oath","TEXT DEFAULT ''"),("keys","INTEGER DEFAULT 0"),
        ("check_count","INTEGER DEFAULT 0")
    ]:
        if col not in cols:
            await db.execute(f"ALTER TABLE players ADD COLUMN {col} {col_type}")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_balance ON players(balance DESC)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_last_farm ON players(last_farm)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS achievements_awarded (
            user_id INTEGER, ach_id TEXT, awarded_at TIMESTAMP,
            PRIMARY KEY (user_id, ach_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS guild_weekly (
            guild TEXT PRIMARY KEY, total_farmed INTEGER DEFAULT 0, week_start DATE
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS nft_registry (
            serial INTEGER PRIMARY KEY AUTOINCREMENT, blunt_id TEXT UNIQUE,
            created_by INTEGER, rarity TEXT DEFAULT 'common', created_at TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS crystals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT,
            description TEXT, amount_rub INTEGER, daily_oas INTEGER,
            total_earned INTEGER DEFAULT 0, start_date TIMESTAMP,
            cancelled INTEGER DEFAULT 0, completed INTEGER DEFAULT 0
        )
    """)
    await db.commit()
    await db.close()

async def get_player(user_id):
    db = await get_db_connection()
    cur = await db.execute(
        "SELECT * FROM players WHERE user_id=?", (user_id,)
    )
    row = await cur.fetchone()
    await db.close()
    if row:
        # Преобразуем Row в словарь для лёгкого доступа по именам
        return dict(row)
    return None

async def get_player_cached(user_id):
    if user_id in player_cache:
        return player_cache[user_id]
    p = await get_player(user_id)
    if p:
        player_cache[user_id] = p
    return p

# === ОБНОВЛЕНИЯ ===
async def update_balance(user_id, username, amount):
    db = await get_db_connection()
    await db.execute("INSERT OR IGNORE INTO players(user_id,username,balance,blunts) VALUES(?,?,0,0)",(user_id,username))
    await db.execute("UPDATE players SET balance=balance+?, username=? WHERE user_id=?",(amount,username,user_id))
    await db.commit()
    await db.close()
    invalidate_cache(user_id)

async def update_blunts(user_id, username, amount):
    db = await get_db_connection()
    await db.execute("INSERT OR IGNORE INTO players(user_id,username,balance,blunts) VALUES(?,?,0,0)",(user_id,username))
    await db.execute("UPDATE players SET blunts=blunts+?, username=? WHERE user_id=?",(amount,username,user_id))
    await db.commit()
    await db.close()
    invalidate_cache(user_id)

async def update_last_farm(user_id):
    now = datetime.now(); today = date.today()
    db = await get_db_connection()
    await db.execute("UPDATE players SET last_farm=?, last_farm_date=? WHERE user_id=?",(now,today,user_id))
    await db.commit()
    await db.close()
    invalidate_cache(user_id)

async def update_last_ritual(user_id):
    db = await get_db_connection()
    await db.execute("UPDATE players SET last_ritual=? WHERE user_id=?",(datetime.now(),user_id))
    await db.commit()
    await db.close()
    invalidate_cache(user_id)

async def update_last_daily(user_id):
    db = await get_db_connection()
    await db.execute("UPDATE players SET last_daily=? WHERE user_id=?",(datetime.now(),user_id))
    await db.commit()
    await db.close()
    invalidate_cache(user_id)

async def update_last_berserk(user_id):
    db = await get_db_connection()
    await db.execute("UPDATE players SET last_berserk=? WHERE user_id=?",(datetime.now(),user_id))
    await db.commit()
    await db.close()
    invalidate_cache(user_id)

async def increment_counter(user_id, field):
    db = await get_db_connection()
    await db.execute(f"UPDATE players SET {field}=COALESCE({field},0)+1 WHERE user_id=?",(user_id,))
    await db.commit()
    await db.close()
    invalidate_cache(user_id)

async def add_title(user_id, emoji):
    db = await get_db_connection()
    cur = await db.execute("SELECT titles FROM players WHERE user_id=?",(user_id,))
    row = await cur.fetchone()
    titles = row["titles"] if row and row["titles"] else ""
    if emoji not in titles:
        titles = (titles + " " + emoji).strip()
        await db.execute("UPDATE players SET titles=? WHERE user_id=?",(titles,user_id))
        await db.commit()
    await db.close()
    invalidate_cache(user_id)

def emoji_to_name(e):
    for m, n, _ in RANKS:
        if m == e: return n
    return ""

async def check_rank_up(context, user_id, username, old_balance, new_balance):
    for emoji, threshold, bonus in RANKS[1:]:
        if old_balance < threshold <= new_balance:
            if bonus: await update_balance(user_id, username, bonus)
            await context.bot.send_message(chat_id="@guild_antysocial",
                text=f"<b><i>🎉 РАНГ ПОВЫШЕН!</i></b>\n\n⚜️ @{html.escape(username)} теперь — {emoji} <b>{emoji_to_name(emoji)}</b>\n\n<b>+{bonus} OAC</b> 🍬 закапало на баланс", parse_mode='HTML')

async def grant_title(user_id, emoji, name, context):
    await add_title(user_id, emoji)
    try: await context.bot.send_message(chat_id=user_id, text=f"<b><i>🎉 ТИТУЛ РАЗБЛОКИРОВАН!</i></b>\n\nУ тебя новое достижение: <b>{name}</b> {emoji}", parse_mode='HTML')
    except: pass

# === Достижения ===
MILESTONES = {
    "farm":[1,10,50,100,500],"craft":[1,5,25,100,500],"smoke":[1,10,50,200,1000],
    "ritual":[1,5,25,100],"legendary":[1,3,10],"referral":[1,3,5,10,25],
    "balance":[5000,20000,50000,100000,500000],"check":[1,10,50,100]
}
ACH_NAMES = {
    "farm_1":"🕯️ Первый Шаг","craft_5":"🌿 Скрученный","smoke_10":"💨 Дымный странник",
    "ritual_5":"🕯️ Ритуальный слуга","balance_20000":"🪦 Призрак Бездны",
    "legendary_1":"🟡 Легенда Ткани","referral_1":"🩸 Пожиратель Душ",
    "balance_50000":"⚡ Электричество","check_10":"👁‍🗨 Всевидящий"
}
# Структура наград: для каждого достижения задан список кортежей (тип, значение)
ACH_REWARDS = {
    "farm_1": [("title","🕯️")],
    "craft_5": [("oac",100),("ramka","🫧")],
    "smoke_10": [("oac",100),("ramka","🫧")],
    "ritual_5": [("oac",100)],
    "legendary_1": [("oac",500),("ramka","🟡")],
    "referral_1": [("ramka","🩸")],
    "balance_20000": [("bg","🖤")],
    "balance_50000": [("oac",500),("ramka","⚡"),("bg","⛈️")],
    "check_10": [("bg","👁️")]
}

async def check_achievements(user_id, context):
    p = await get_player_cached(user_id)
    if not p: return
    inv_data = json.loads(p["inventory"]) if p["inventory"] else []
    legendary = sum(1 for it in inv_data if it.get("rarity")=="legendary")
    stats = {
        "farm": p["farm_count"],
        "craft": p["craft_count"],
        "smoke": p["smoke_count"],
        "ritual": p["ritual_count"],
        "legendary": legendary,
        "referral": p["referral_count"],
        "balance": p["balance"],
        "check": p["check_count"]
    }
    awarded = []
    db = await get_db_connection()
    cur = await db.execute("SELECT ach_id FROM achievements_awarded WHERE user_id=?",(user_id,))
    awarded = [r[0] for r in await cur.fetchall()]
    await db.close()
    for cat, ths in MILESTONES.items():
        cur_val = stats.get(cat,0)
        for hold in ths:
            ach_id = f"{cat}_{hold}"
            if ach_id not in awarded and cur_val >= hold:
                await award_ach(user_id, ach_id, context, p["username"])

async def award_ach(user_id, ach_id, context, username):
    rewards = ACH_REWARDS.get(ach_id, [("oac",50)])
    for r_type, r_val in rewards:
        if r_type == "title":
            await add_title(user_id, r_val)
        elif r_type == "oac":
            await update_balance(user_id, username, r_val)
        elif r_type == "ramka":
            await unlock_border(user_id, r_val)
        elif r_type == "bg":
            await unlock_bg(user_id, r_val)
    db = await get_db_connection()
    await db.execute("INSERT OR IGNORE INTO achievements_awarded(user_id,ach_id,awarded_at) VALUES(?,?,?)",
                     (user_id, ach_id, datetime.now()))
    await db.commit()
    await db.close()
    name = ACH_NAMES.get(ach_id, ach_id)
    plus = ""
    for r_type, r_val in rewards:
        if r_type == "oac":
            plus += f"+{r_val} OAC "
    await context.bot.send_message(chat_id=user_id, text=f"🎉 Достижение разблокировано!\n{name}\n{plus}", parse_mode='HTML')

async def unlock_border(user_id, emoji):
    db = await get_db_connection()
    cur = await db.execute("SELECT profile_skins FROM players WHERE user_id=?",(user_id,))
    row = await cur.fetchone()
    skins = json.loads(row["profile_skins"]) if row and row["profile_skins"] else {}
    borders = skins.get("unlocked_borders",[])
    if emoji not in borders: borders.append(emoji)
    skins["unlocked_borders"] = borders
    if not skins.get("active_border"): skins["active_border"] = emoji
    await db.execute("UPDATE players SET profile_skins=? WHERE user_id=?",(json.dumps(skins),user_id))
    await db.commit()
    await db.close()
    invalidate_cache(user_id)

async def unlock_bg(user_id, emoji):
    db = await get_db_connection()
    cur = await db.execute("SELECT profile_skins FROM players WHERE user_id=?",(user_id,))
    row = await cur.fetchone()
    skins = json.loads(row["profile_skins"]) if row and row["profile_skins"] else {}
    backs = skins.get("unlocked_backgrounds",[])
    if emoji not in backs: backs.append(emoji)
    skins["unlocked_backgrounds"] = backs
    if not skins.get("active_background"): skins["active_background"] = emoji
    await db.execute("UPDATE players SET profile_skins=? WHERE user_id=?",(json.dumps(skins),user_id))
    await db.commit()
    await db.close()
    invalidate_cache(user_id)

# === Меню ===
async def get_main_menu_keyboard(user_id):
    whisper = random.choice(WHISPERS)
    keyboard = [
        [InlineKeyboardButton("🍬 Фармить", callback_data="farm")],
        [InlineKeyboardButton("🌿 Крафт", callback_data="craft"), InlineKeyboardButton("💨 Дунуть", callback_data="smoke")],
        [InlineKeyboardButton("⚜️ Профиль", callback_data="profile"), InlineKeyboardButton("🏆 Топ", callback_data="top")],
        [InlineKeyboardButton("🕋 Гильдии", callback_data="guild_info"), InlineKeyboardButton("📜 Законы", callback_data="rules")],
        [InlineKeyboardButton("🎲 Удача", callback_data="luck")],
    ]
    p = await get_player_cached(user_id)
    if p:
        if p["guild"] == "BLACK":
            keyboard.append([InlineKeyboardButton("🕯️ Ритуал", callback_data="ritual")])
        if p["balance"] >= 5000:
            pc = p["passive_collected"]
            if pc:
                # Приводим к datetime
                if isinstance(pc, str):
                    last = datetime.fromisoformat(pc)
                else:
                    last = pc
                if (datetime.now() - last).total_seconds()/3600 >= 1:
                    keyboard.append([InlineKeyboardButton("🪴 Собрать урожай", callback_data="collect")])
            else:
                keyboard.append([InlineKeyboardButton("🪴 Куст", callback_data="collect")])
        else:
            keyboard.append([InlineKeyboardButton("🪴 Куст (⚔️ Ветеран)", callback_data="bush_preview")])
        keyboard.append([InlineKeyboardButton("🐾 Питомец (⚔️ Ветеран)", callback_data="pet_preview")])
    keyboard.append([InlineKeyboardButton("🪪 Скидка", callback_data="privilege"), InlineKeyboardButton("📦 Каталог", callback_data="catalog")])
    return InlineKeyboardMarkup(keyboard), whisper

def get_back_to_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 Меню", callback_data="menu")]])

def get_user_and_msg(update: Update):
    if update.callback_query: return update.callback_query.from_user, update.callback_query.message
    return update.effective_user, update.message

async def get_guild(user_id):
    p = await get_player_cached(user_id)
    return p["guild"] if p else None

async def set_guild(user_id, guild):
    db = await get_db_connection()
    await db.execute("UPDATE players SET guild=? WHERE user_id=?",(guild,user_id))
    await db.commit()
    await db.close()
    invalidate_cache(user_id)

async def get_top(limit=10):
    db = await get_db_connection()
    cur = await db.execute("SELECT username, balance, guild FROM players ORDER BY balance DESC LIMIT ?",(limit,))
    rows = await cur.fetchall()
    await db.close()
    return rows

async def count_guilds():
    db = await get_db_connection()
    cur = await db.execute("SELECT guild, COUNT(*) as cnt FROM players WHERE guild IS NOT NULL GROUP BY guild")
    rows = await cur.fetchall()
    await db.close()
    cnt = {"BLACK":0,"WHITE":0}
    for r in rows:
        if r["guild"] in cnt:
            cnt[r["guild"]] = r["cnt"]
    return cnt

# === Шёпот ===
async def send_whisper(context, chat_id, text, life_seconds=45):
    msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    context.job_queue.run_once(lambda c: c.bot.delete_message(chat_id, msg.message_id), when=life_seconds)

async def send_whisper_dm(update, context, text, life_seconds=20):
    if update.callback_query: chat_id = update.callback_query.message.chat.id
    else: chat_id = update.effective_chat.id
    msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    context.job_queue.run_once(lambda c: c.bot.delete_message(chat_id=chat_id, message_id=msg.message_id), when=life_seconds)

def format_date(iso_string):
    try:
        dt = datetime.fromisoformat(iso_string)
        return dt.strftime("%d.%m.%Y в %H:%M")
    except: return iso_string

# === Обработчики ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = get_user_and_msg(update)
    user_id = user.id
    username = html.escape(user.username or user.first_name)
    player = await get_player_cached(user_id)

    if context.args and context.args[0].startswith("blunt_"):
        ref_blunt_id = context.args[0].replace("blunt_", "")
        creator_id = None
        if not player:
            db = await get_db_connection()
            cur = await db.execute("SELECT user_id, inventory FROM players")
            rows = await cur.fetchall()
            await db.close()
            for row in rows:
                try:
                    inv = json.loads(row["inventory"])
                    for item in inv:
                        if item.get("id") == ref_blunt_id:
                            creator_id = row["user_id"]
                            break
                except: continue
                if creator_id: break
            if creator_id and creator_id != user_id:  # запрет самонакрутки
                # проверка, не перешёл ли уже по реферальной ссылке
                db = await get_db_connection()
                cur = await db.execute("SELECT invited_by FROM players WHERE user_id=?",(user_id,))
                row = await cur.fetchone()
                already = row and row["invited_by"] is not None
                if not already:
                    await db.execute("UPDATE players SET invited_by=? WHERE user_id=?",(creator_id,user_id))
                    await db.commit()
                    await update_balance(creator_id, username, 50)
                    new_name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа","Коготь Хаоса","Вздох Пожирателя"])
                    # даём рефереру легендарный блант
                    cur = await db.execute("SELECT inventory FROM players WHERE user_id=?",(creator_id,))
                    row = await cur.fetchone()
                    inv = json.loads(row["inventory"]) if row and row["inventory"] else []
                    inv.append({
                        "id":f"blunt_{creator_id}_{int(datetime.now().timestamp()*1000)}_{random.randint(1000,9999)}",
                        "name":new_name,"type":"named","created_at":datetime.now().isoformat(),
                        "rarity":"legendary","reaction":random.choice(FUNNY_REACTIONS),"rare_number":"L-0001"
                    })
                    await db.execute("UPDATE players SET inventory=? WHERE user_id=?",(json.dumps(inv),creator_id))
                    await db.commit()
                    invalidate_cache(creator_id)
                    await add_title(creator_id, "🩸")
                    await grant_title(creator_id, "🩸", "Пожиратель Душ", context)
                    await context.bot.send_message(chat_id="@guild_antysocial",
                        text=f"<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n⚜️ <b>@{username}</b> был призван нитью @{html.escape(str(creator_id))}.\n🕸️ Искажение становится плотнее...", parse_mode='HTML')
                await db.close()
        if not player:
            await update_balance(user_id, username, 0)
            await update_blunts(user_id, username, 0)
            await update_balance(user_id, username, 800)
            new_name = random.choice(["Крик Бездны","Пепел Короля","Шёпот Склепа"])
            db = await get_db_connection()
            cur = await db.execute("SELECT inventory FROM players WHERE user_id=?",(user_id,))
            row = await cur.fetchone()
            inv = json.loads(row["inventory"]) if row and row["inventory"] else []
            inv.append({
                "id":f"blunt_{user_id}_{int(datetime.now().timestamp()*1000)}_{random.randint(1000,9999)}",
                "name":new_name,"type":"named","created_at":datetime.now().isoformat(),
                "rarity":"common","reaction":random.choice(FUNNY_REACTIONS)
            })
            await db.execute("UPDATE players SET inventory=? WHERE user_id=?",(json.dumps(inv),user_id))
            await db.commit()
            await db.close()
            invalidate_cache(user_id)
            bonus = "🎁 Смотритель дарует тебе <code>800</code> 🍬 и твой первый именной блант!\n\n"
        else: bonus = ""
        if await get_guild(user_id):
            welcome = "<b><i>🎉 Добро пожаловать обратно в Гильдию Antysocialshop!</i></b>"
        else:
            welcome = ("<b><i>🎉 Добро пожаловать в Гильдию Antysocialshop!</i></b>\n\n"
                       "🕯️ <b>Тёмная Гильдия</b> — стабильность, ритуалы, тёмное благословение.\n"
                       "⚜️ <b>Светлая Гильдия</b> — азарт, удача, танец на лезвии.\n\n"
                       "▸ <i>Выбери свой путь:</i>")
            guild_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🕯️ Тёмная Гильдия", callback_data="guild_join_BLACK"),
                 InlineKeyboardButton("⚜️ Светлая Гильдия", callback_data="guild_join_WHITE")]
            ])
            await msg.reply_text(bonus + welcome, reply_markup=guild_kb, parse_mode='HTML')
            return

    if not player:
        await update_balance(user_id, username, 0)
        await update_blunts(user_id, username, 0)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ АКТИВИРОВАТЬ ТЕРМИНАЛ", callback_data="activate_menu")]])
        await msg.reply_text(
            "<b>👁‍🗨 Смотритель заметил тебя.</b>\n"
            "<i>🪄 Искажение реальности ждёт твоего шага.</i>\n"
            "▸ ⚜️ Здесь добываются редкие экземпляры, зарабатываются Очки АнтиСошл (<b>OAC</b> 🍬), курят <b>бланты</b> 🌿 и вступают в <b>гильдии</b>.\n\n"
            "🎁 <b>Активируйся и получи 800 OAC + свой первый именной блант! 💎</b>\n\n"
            "<i>🎯 Выполняй действия, открывай достижения и собирай <b>редкие</b> <b>скины</b> для своего профиля. 🫧</i>",
            reply_markup=kb, parse_mode='HTML')
        return

    await process_daily_login(user_id, context)
    guild = await get_guild(user_id)
    back = "<b>⚔️ С возвращением в Гильдию!</b>\n\n"
    if guild == "BLACK": back += "🕯️ Ты состоишь в <i>Тёмной Гильдии</i>.\n"
    elif guild == "WHITE": back += "⚜️ Ты состоишь в <i>Светлой Гильдии</i>.\n"
    else: back += "Ты пока не в Гильдии. Нажми /guild чтобы вступить.\n"
    kb, whisper = await get_main_menu_keyboard(user_id)
    text = f"<b>🎮 ГЛАВНОЕ МЕНЮ</b>\n\n<i>{whisper}</i>\n\n" + back
    await msg.reply_text(text, reply_markup=kb, parse_mode='HTML')

async def process_daily_login(user_id, context):
    p = await get_player_cached(user_id)
    if not p: return
    today = date.today()
    last = p["last_login_date"]
    streak = p["login_streak"]
    if last != today:
        if last and (today - last).days == 1:
            streak += 1
        else:
            streak = 1
        db = await get_db_connection()
        await db.execute("UPDATE players SET login_streak=?, last_login_date=? WHERE user_id=?",(streak,today,user_id))
        await db.commit()
        await db.close()
        invalidate_cache(user_id)
        reward = {1:10,2:20,3:30,4:40,5:50,6:60,7:70}.get(streak,10)
        await update_balance(user_id, p["username"], reward)
        if streak == 7:
            await add_title(user_id, "🔥")
            await grant_title(user_id, "🔥", "Верный Странник", context)
        await context.bot.send_message(chat_id=user_id, text=f"🎁 День {streak}/7: +{reward} OAC за ежедневный вход!\nПродолжай заходить каждый день, чтобы получить редкий скин.")

# Фарм
async def farm_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if p and p["last_farm"]:
        last = datetime.fromisoformat(p["last_farm"])
        if datetime.now() - last < timedelta(hours=FARM_COOLDOWN_HOURS):
            remain = int((timedelta(hours=FARM_COOLDOWN_HOURS) - (datetime.now()-last)).seconds/60)
            await send_whisper_dm(update, context, f"🍬 <i>OAC копятся</i> 🌿\n\n<b>Подожди {remain} мин.</b>", life_seconds=10)
            return
    earned = random.randint(FARM_MIN, FARM_MAX)
    if p and p["smoke_count"]: earned += int(earned*0.05)
    if context.user_data.get("last_smoke_time") and datetime.now() - context.user_data["last_smoke_time"] < timedelta(minutes=5):
        earned += random.randint(3,5)
    if context.bot_data.get("happy_hour"): earned *= HAPPY_HOUR_MULTIPLIER
    if random.randint(1,100) == 1:
        earned *= 10
        await send_whisper(context, "@guild_antysocial", f"🌟 @{uname} наткнулся на <i>Золотую жилу</i>! +{earned} 🍬", life_seconds=45)
    old_bal = p["balance"] if p else 0
    await update_balance(uid, uname, earned)
    await update_last_farm(uid)
    await increment_counter(uid, "farm_count")
    new_p = await get_player_cached(uid)
    new_bal = new_p["balance"]
    if new_p["farm_count"] == 1: await grant_title(uid, "🕯️", "Первый Шаг", context)
    if old_bal < 500 <= new_bal: await grant_title(uid, "✨", "Искра", context)
    next_ms = next((th for th in MILESTONES["farm"] if th > new_p["farm_count"]), None)
    perc = int(new_p["farm_count"]/next_ms*100) if next_ms else 100
    bar = "▓"*int(perc/10) + "░"*(10-int(perc/10))
    text = (
        f"<b>💎 Ты нафармил:</b> <i>+{earned} OAC</i> 🍬\n"
        f"<b>⚜️ У тебя:</b> <i>{new_bal} OAC</i> 🍬\n\n"
        f"🎯 <b>Фарминг:</b> <b>{new_p['farm_count']}/{next_ms}</b> {bar} <b>{perc}%</b>\n"
        f"⚔️ До Ветерана: <b>{5000 - new_bal} OAC</b> 🍬\n\n"
        f"⏳ Фарм через {int(FARM_COOLDOWN_HOURS*60)} мин"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌿 Крафт", callback_data="craft"), InlineKeyboardButton("⚜️ Профиль", callback_data="profile")]
    ])
    await send_whisper_dm(update, context, text, life_seconds=20)
    await check_rank_up(context, uid, uname, old_bal, new_bal)
    await check_achievements(uid, context)

# Крафт
async def craft_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    bal = p["balance"] if p else 0
    text = f"<b><i>🌿 КРАФТ БЛАНТА</i></b>\n\n🛡️ <i>у тебя:</i> <code>{bal}</code> 🍬"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌿 Обычный блант (15 🍬)", callback_data="craft_normal")],
        [InlineKeyboardButton("💍 Именной блант (50 🍬)", callback_data="craft_named")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
    ])
    await msg.edit_text(text, reply_markup=kb, parse_mode='HTML')

async def handle_craft_normal(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id; uname = html.escape(query.from_user.username or query.from_user.first_name)
    p = await get_player_cached(uid)
    if not p or p["balance"] < 15:
        await send_whisper_dm(update, context, "<b><i>🕳️ ИСКАЖЕНИЕ МОЛЧИТ</i></b>\n\n<i>🛡️ Недостаточно OAC.</i>\n🕯️ Требуется <b>15 OAC</b> 🍬.", life_seconds=20)
        return
    await update_balance(uid, uname, -15)
    await update_blunts(uid, uname, 1)
    await increment_counter(uid, "craft_count")
    if random.random() < 0.05:
        await update_blunts(uid, uname, 1)
        await send_whisper(context, "@guild_antysocial", f"⚡ @{uname} высек Искру Искажения из рутины. +1 🌿", life_seconds=45)
    new_p = await get_player_cached(uid)
    next_ms = next((th for th in MILESTONES["craft"] if th > new_p["craft_count"]), None)
    perc = int(new_p["craft_count"]/next_ms*100) if next_ms else 100
    bar = "▓"*int(perc/10) + "░"*(10-int(perc/10))
    text = (
        f"<b>🌿 Ты скрутил блант!</b>\n"
        f"🛡️ У тебя: <i>{new_p['balance']} OAC</i> 🍬\n\n"
        f"🎯 <b>Крафтинг:</b> <b>{new_p['craft_count']}/{next_ms}</b> {bar} <b>{perc}%</b>\n"
        f"🌿 В свёртке: <b>{new_p['blunts']}</b>"
    )
    await send_whisper_dm(update, context, text, life_seconds=20)
    await check_achievements(uid, context)

async def handle_craft_named(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    p = await get_player_cached(uid)
    if not p or p["balance"] < 50:
        await send_whisper_dm(update, context, "<b><i>🕳️ ИСКАЖЕНИЕ МОЛЧИТ</i></b>\n\n<i>🛡️ Недостаточно OAC.</i>\n🕯️ Требуется <b>50 OAC</b> 🍬.", life_seconds=20)
        return
    context.user_data['awaiting_named_blunt'] = True
    context.job_queue.run_once(lambda c: context.user_data.update({'awaiting_named_blunt': False}), 300)
    await query.message.edit_text("<b><i>💍 ИМЕННОЙ БЛАНТ</i></b>\n\n<i>Введи имя своего бланта (до 25 символов)</i>\n\n[❌ Отмена]",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_named")]]), parse_mode='HTML')

async def handle_named_name(update, context):
    if not context.user_data.get('awaiting_named_blunt'): return
    user = update.effective_user
    uid = user.id
    name = update.message.text.strip()[:25]
    if not name:
        msg = await update.message.reply_text("❌ Имя не может быть пустым.")
        context.job_queue.run_once(lambda c: c.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id), when=10)
        return
    # Экранирование
    name_escaped = html.escape(name)
    context.user_data['awaiting_named_blunt'] = False
    blunt_id = f"blunt_{uid}_{int(datetime.now().timestamp()*1000)}_{random.randint(1000,9999)}"
    db = await get_db_connection()
    cur = await db.execute("SELECT inventory FROM players WHERE user_id=?",(uid,))
    row = await cur.fetchone()
    inv = json.loads(row["inventory"]) if row and row["inventory"] else []
    rare = random.random()
    if rare < 0.02: rarity, color, prefix = "legendary","🟡","L"
    elif rare < 0.1: rarity, color, prefix = "epic","🟣","E"
    elif rare < 0.35: rarity, color, prefix = "rare","🔵","R"
    else: rarity, color, prefix = "common","🟢","C"
    cur = await db.execute("INSERT INTO nft_registry(blunt_id,created_by,rarity,created_at) VALUES(?,?,?,?)",
                           (blunt_id,uid,rarity,datetime.now()))
    await db.commit()
    serial = cur.lastrowid
    hash_hex = hashlib.sha256(f"{name}{serial}{datetime.now().timestamp()}".encode()).hexdigest()[:12].upper()
    short_hash = f"0x{hash_hex[:6]}...{hash_hex[-4:]}"
    cur = await db.execute("SELECT COUNT(*) as cnt FROM nft_registry WHERE rarity=?",(rarity,))
    row = await cur.fetchone()
    rare_count = row["cnt"]
    rare_number = f"{prefix}-{rare_count:04d}"
    reaction = random.choice(FUNNY_REACTIONS)
    inv.append({
        "id":blunt_id,"serial":serial,"name":name_escaped,"type":"named","created_at":datetime.now().isoformat(),
        "rarity":rarity,"rare_number":rare_number,"hash":short_hash,"reaction":reaction,
        "owner_history":[{"user_id":uid,"since":datetime.now().isoformat()}]
    })
    await db.execute("UPDATE players SET inventory=? WHERE user_id=?",(json.dumps(inv),uid))
    await db.commit()
    await db.close()
    uname = html.escape(user.username or user.first_name)
    await update_balance(uid, uname, -50)
    await increment_counter(uid, "craft_count")   # исправлено: учёт в статистике
    invalidate_cache(uid)
    text = (
        f"<b><i>💍 БЛАНТ СОТКАН</i></b>\n\n"
        f"🩸 <i>Ты вплёл в <b>Искажение</b> свой именной блант:</i>\n"
        f"{color} <b><i>«{name_escaped}»</i></b> <i>Редкость:</i> <b>{rarity}</b>\n\n"
        f"💎 <i>Он навсегда останется в твоей коллекции.</i>\n\n"
        f"🩸 <i>{reaction}</i>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Поделиться", callback_data=f"share_blunt_{blunt_id}")],
        [InlineKeyboardButton("📋 В меню", callback_data="menu")]
    ])
    await update.message.reply_text(text, reply_markup=kb, parse_mode='HTML')
    await context.bot.send_message(chat_id="@guild_antysocial",
        text=f"<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n⚜️ <b>@{uname}</b> создал свой блант {color} <b><i>«{name_escaped}»</i></b> 🌿\n<i>Редкость: {rarity}</i>\n🩸 <i>{reaction}</i>", parse_mode='HTML')
    await check_achievements(uid, context)

async def cancel_named(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_named_blunt'] = False
    await craft_callback(update, context)

# Дым
async def smoke_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p or p["blunts"] < 1:
        await msg.edit_text("<b><i>💨 ДУНУТЬ</i></b>\n\n🌿 <i>свёрток пуст</i>",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="menu")]]), parse_mode='HTML')
        return
    await msg.edit_text(f"<b><i>💨 ДУНУТЬ</i></b>\n\n🌿 <i>блантов в свёртке:</i> <b>{p['blunts']}</b>",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("💨 Дунуть", callback_data="do_smoke")],
                            [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
                        ]), parse_mode='HTML')

async def do_smoke(update, context):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id; uname = html.escape(query.from_user.username or query.from_user.first_name)
    p = await get_player_cached(uid)
    if not p or p["blunts"] < 1:
        await query.answer("Свёрток пуст."); return
    save = (p["guild"]=="WHITE" and random.randint(1,100)<=20)
    if not save: await update_blunts(uid, uname, -1)
    r = random.random()
    if r <= 0.5:
        earned = random.randint(15,40)
        if context.bot_data.get("happy_hour"): earned *= HAPPY_HOUR_MULTIPLIER
        await update_balance(uid, uname, earned)
        effect = f"<b><i>💨 ДЫМ РАССЕЯЛСЯ</i></b>\n\n💨 <i>Лёгкий приход</i>\n💡 «Станки Фабрики №9 работают в ритме твоего сердца...»\n\n🍬 <b>+{earned} OAC</b>"
    elif r <= 0.75: effect = "<b><i>💨 ДЫМ РАССЕЯЛСЯ</i></b>\n\n💨 <i>Паранойя...</i>\n💡 «Смотритель наблюдает...»\n✨ Никакого видимого эффекта."
    else: effect = "<b><i>💨 ДЫМ РАССЕЯЛСЯ</i></b>\n\n💨 <i>Плацебо</i>\n💡 «Дым рассеялся, ничего не изменилось...»"
    if p and not p["inhaled"]:
        await add_title(uid, "💨")
        db = await get_db_connection()
        await db.execute("UPDATE players SET inhaled=1 WHERE user_id=?",(uid,))
        await db.commit()
        await db.close()
        invalidate_cache(uid)
        effect += "\n\n<b><i>🎉 ТИТУЛ РАЗБЛОКИРОВАН!</i></b>\n💨 Ты теперь — <b>Красные Глаза</b>"
    context.user_data["last_smoke_time"] = datetime.now()
    await increment_counter(uid, "smoke_count")
    new_p = await get_player_cached(uid)
    bl_left = new_p["blunts"] if new_p else 0
    next_ms = next((th for th in MILESTONES["smoke"] if th > new_p["smoke_count"]), None)
    perc = int(new_p["smoke_count"]/next_ms*100) if next_ms else 100
    bar = "▓"*int(perc/10) + "░"*(10-int(perc/10))
    text = f"{effect}\n\n🍃 В свёртке: <b>{bl_left}</b>\n💨 <b>Дым:</b> <b>{new_p['smoke_count']}/{next_ms}</b> {bar} <b>{perc}%</b>"
    if save: text += "\n⚜️ <i>Светлая Гильдия сохранила твой Блант!</i>"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💨 Дунуть ещё", callback_data="do_smoke") if bl_left>=1 else InlineKeyboardButton("🌿 Крафтить ещё", callback_data="craft")],
        [InlineKeyboardButton("🔙 В меню", callback_data="menu")]
    ])
    await query.message.edit_text(text, reply_markup=kb, parse_mode='HTML')
    await check_achievements(uid, context)

# Ритуал
async def ritual_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p: await send_whisper_dm(update, context, "🕳️ Ты ещё не активирован. /start", life_seconds=10); return
    if p["guild"] != "BLACK": await send_whisper_dm(update, context, "❌ Только Тёмная Гильдия.", life_seconds=10); return
    if p["last_ritual"]:
        last = datetime.fromisoformat(p["last_ritual"])
        if datetime.now() - last < timedelta(hours=24):
            remain = int((timedelta(hours=24) - (datetime.now()-last)).seconds/3600)
            await send_whisper_dm(update, context, f"⏳ Жди {remain} ч.", life_seconds=10); return
    old_bal = p["balance"]
    reward = 150
    if context.bot_data.get("happy_hour"): reward *= HAPPY_HOUR_MULTIPLIER
    await update_balance(uid, uname, reward)
    await update_last_ritual(uid)
    await increment_counter(uid, "ritual_count")
    extra = 15 if random.random() < 0.1 else 0
    if extra: await update_balance(uid, uname, extra)
    new_p = await get_player_cached(uid)
    new_bal = new_p["balance"]
    next_ms = next((th for th in MILESTONES["ritual"] if th > new_p["ritual_count"]), None)
    perc = int(new_p["ritual_count"]/next_ms*100) if next_ms else 100
    bar = "▓"*int(perc/10) + "░"*(10-int(perc/10))
    text = (f"<b><i>🕯️ РИТУАЛ ЗАВЕРШЁН</i></b>\n\n"
            f"Ритуал принёс тебе <b>{reward} OAC</b> 🍬\n\n"
            f"⚜️ У тебя: <b>{new_bal} OAC</b>\n"
            f"🕯️ Ритуалы: <b>{new_p['ritual_count']}/{next_ms}</b> {bar} <b>{perc}%</b>")
    await send_whisper_dm(update, context, text, life_seconds=20)
    await check_rank_up(context, uid, uname, old_bal, new_bal)
    await check_achievements(uid, context)

# Куст
async def collect_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p: await send_whisper_dm(update, context, "🕳️ Ты ещё не активирован. /start", life_seconds=10); return
    bal = p["balance"]
    if bal < 5000:
        await msg.reply_text(f"🪴 <i>Выращивать кусты — привилегия Ветерана</i> 💎\n⚔️ <i>До ранга Ветеран осталось:</i> {5000-bal}/5000 🍬", parse_mode='HTML'); return
    lvl = 3 if bal >= 20000 else 2
    pc = p["passive_collected"]
    if pc:
        last = datetime.fromisoformat(pc) if isinstance(pc,str) else pc
        hrs = (datetime.now() - last).total_seconds()/3600
        earned = int(hrs * 30 * lvl)
        if context.bot_data.get("happy_hour"): earned *= HAPPY_HOUR_MULTIPLIER
        if earned >= 1:
            await update_balance(uid, uname, earned)
            db = await get_db_connection()
            await db.execute("UPDATE players SET passive_collected=? WHERE user_id=?",(datetime.now(),uid))
            await db.commit()
            await db.close()
            invalidate_cache(uid)
            new_bal = (await get_player_cached(uid))["balance"]
            await send_whisper_dm(update, context, f"<b><i>🪴 УРОЖАЙ СОБРАН</i></b>\n\nТвой куст принёс <b>{earned} OAC</b> 🍬.\n\n💎 <i>У тебя:</i> <b>{new_bal} OAC</b> 🍬", life_seconds=20)
        else: await send_whisper_dm(update, context, "⏳ Пока нечего собирать.", life_seconds=10)
    else:
        db = await get_db_connection()
        await db.execute("UPDATE players SET passive_collected=? WHERE user_id=?",(datetime.now(),uid))
        await db.commit()
        await db.close()
        invalidate_cache(uid)
        await send_whisper_dm(update, context, "⏳ Авто‑сборщик активирован. Заходи через час.", life_seconds=10)

# Профиль
async def profile_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p: await msg.reply_text("Сначала активируйся: /start"); return
    bal, bl, guild = p["balance"], p["blunts"], p["guild"]
    rank_emoji, rank_name = "🪓", "Рекрут"
    for emoji, threshold, _ in RANKS:
        if bal >= threshold: rank_emoji, rank_name = emoji, emoji_to_name(emoji)
    if guild == "BLACK": g_emoji = " 🕯️ Тёмная Гильдия"
    elif guild == "WHITE": g_emoji = " ⚜️ Светлая Гильдия"
    else: g_emoji = ""
    titles = p["titles"] if p["titles"] else "—"
    neuro = random.choice(NEURO_STATUSES)
    # Парсим profile_skins
    try:
        skins = json.loads(p["profile_skins"]) if p["profile_skins"] else {}
    except:
        skins = {}
    border = skins.get("active_border","")
    bg = skins.get("active_background","")
    # Ближайшая цель
    inv_data = json.loads(p["inventory"]) if p["inventory"] else []
    legendary = sum(1 for it in inv_data if it.get("rarity")=="legendary")
    stats = {
        "farm": p["farm_count"],"craft": p["craft_count"],"smoke": p["smoke_count"],
        "ritual": p["ritual_count"],"legendary": legendary,"referral": p["referral_count"],
        "balance": bal,"check": p["check_count"]
    }
    closest = None
    for act, ths in MILESTONES.items():
        cur = stats.get(act,0)
        for th in ths:
            if cur < th:
                if not closest or (th - cur) < closest["remain"]:
                    closest = {"action":act,"goal":th,"remain":th-cur}
                break
    ach_text = ""
    if closest:
        action_map = {"farm":"Фарминг","craft":"Крафтинг","smoke":"Дым","ritual":"Ритуалы","legendary":"Легендарные","referral":"Рефералы","balance":"Богатство","check":"Проверки"}
        an = action_map.get(closest["action"], closest["action"])
        cur_val = stats.get(closest["action"],0)
        perc = int(cur_val/closest["goal"]*100) if closest["goal"] else 100
        bar = "▓"*int(perc/10) + "░"*(10-int(perc/10))
        ach_text = f"\n🏆 <b>Ближайшая цель:</b> {an} <b>{cur_val}/{closest['goal']}</b> {bar} <b>{perc}%</b>\n"
    text = (
        f"<b>⚜️ ПРОФИЛЬ</b>\n\n"
        f"{border} <b>{uname}</b> {border} {g_emoji}\n"
        f"🫧 Фон: {bg} {bg}\n\n"
        f"⚜️ <b>Ранг:</b> <i>{rank_name}</i>\n"
        f"🛡️ <b>ОАС:</b> <i>{bal} OAC</i> 🍬\n"
        f"🌿 <b>Блантов в свёртке:</b> <i>{bl}</i>\n"
        f"🪴 <b>Куст:</b> <i>+{30 * (3 if bal>=20000 else 2 if bal>=5000 else 0)} OAC/ч</i>\n"
        f"🧬 <b>Титулы:</b> {titles}\n"
        f"🧠 <b>Нейро-статус:</b> <i>{neuro}</i>"
        + ach_text
    )
    named = [it for it in inv_data if it.get("type")=="named"]
    if named:
        text += "\n\n💍 Именные бланты (NFT):"
        detail_buttons = []
        for item in named:
            name = item["name"]; rarity = item.get("rarity","common")
            color = {"legendary":"🟡","epic":"🟣","rare":"🔵"}.get(rarity,"🟢")
            rare_number = item.get("rare_number","?-????")
            hash_code = item.get("hash","0x????...????")
            text += f"\n   {color} <b>{name}</b>\n   🩸 <b><i>Серийный номер:</i></b> <b><i>#{rare_number}</i></b> · <i>{hash_code}</i>\n"
            detail_buttons.append([InlineKeyboardButton(f"🩸 Детали #{rare_number}", callback_data=f"blunt_details_{item['id']}")])
        final_kb = InlineKeyboardMarkup(detail_buttons + [[InlineKeyboardButton("📋 В меню", callback_data="menu")]])
    else:
        final_kb = InlineKeyboardMarkup([[InlineKeyboardButton("📋 В меню", callback_data="menu")]])
    await msg.reply_text(text, parse_mode='HTML', reply_markup=final_kb)

# Топ
async def top_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    top = await get_top(10)
    if not top: await msg.reply_text("🏆 Топ пока пуст."); return
    text = "<b>🏆 ТОП-10 ИГРОКОВ</b>\n\n"
    for i, row in enumerate(top, 1):
        name = html.escape(row["username"])
        bal = row["balance"]
        guild = row["guild"]
        medal = "🥇" if i==1 else "🥈" if i==2 else "🥉" if i==3 else f"{i}."
        g = "🕯️" if guild=="BLACK" else "⚜️" if guild=="WHITE" else ""
        text += f"{medal} <b>{name}</b> {g} — <b>{bal} OAC</b> 🍬\n"
    db = await get_db_connection()
    cur = await db.execute("SELECT COUNT(*) as cnt FROM players WHERE balance > (SELECT balance FROM players WHERE user_id=?)",(uid,))
    row = await cur.fetchone()
    await db.close()
    pos = row["cnt"] + 1 if row else 1
    text += f"\n📊 <i>Твоя позиция:</i> {pos}"
    await msg.reply_text(text, parse_mode='HTML', reply_markup=get_back_to_menu_keyboard())

# Гильдии
async def guild_info_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    counts = await count_guilds()
    guild = await get_guild(uid)
    text = (f"<b>🕋 ГИЛЬДИИ</b>\n\n"
            f"🕯️ <b>Тёмная</b>: <code>{counts['BLACK']}</code> странников\n"
            f"⚜️ <b>Светлая</b>: <code>{counts['WHITE']}</code> странников\n\n"
            f"🕯️ <b>Ритуал</b>: <code>+150</code> 🍬 раз в 24 ч.\n"
            f"⚜️ <b>Удача</b>: <code>20%</code> сохранить Блант при 💨.")
    if guild:
        g_emoji = "🕯️" if guild == "BLACK" else "⚜️"
        g_name = "Тёмная" if guild == "BLACK" else "Светлая"
        text += f"\n\n✅ Ты состоишь в {g_emoji} <b>{g_name} Гильдии</b>."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📋 В меню", callback_data="menu")]])
    else:
        text += "\n\nТы пока не в Гильдии."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🕯️ Вступить в Тёмную", callback_data="guild_join_BLACK"),
             InlineKeyboardButton("⚜️ Вступить в Светлую", callback_data="guild_join_WHITE")],
            [InlineKeyboardButton("📋 В меню", callback_data="menu")]
        ])
    await msg.reply_text(text, parse_mode='HTML', reply_markup=kb)

# Законы
async def rules_callback(update, context):
    user, msg = get_user_and_msg(update)
    text = (
        "<b><i>📜 КОДЕКС ГИЛЬДИИ</i></b>\n\n"
        "<b>⚙️ Основные действия</b>\n"
        "🍬 <code>/farm</code> — добыча ОАС\n"
        "🌿 <code>/craft</code> — создание блантов\n"
        "💨 <code>/smoke</code> — дунуть блант\n"
        "🎲 <code>/luck</code> — раздел Удачи\n\n"
        "<b>💍 Именные бланты</b>\n"
        "💎 ▸ Создай свой <b>вечный именной Блант</b> через меню «<b>Крафт</b>». Он не курится, получает редкость и <b>навсегда</b> остаётся в твоей коллекции. Показать свой блант в чат — через «<b>Профиль</b>».\n\n"
        "<b>🕋 Гильдии и развитие</b>\n"
        "🕯️ <b>Тёмная</b>: <code>/ritual</code> (+150 OAC раз в 24 ч)\n"
        "⚜️ <b>Светлая</b>: 20% шанс сохранить блант при 💨\n"
        "🪴 <b>Куст</b>: пассивный доход с ранга ⚔️ <b>Ветеран</b>\n"
        "🐾 <b>Питомец</b>: доступен с ранга ⚔️ <b>Ветеран</b>\n\n"
        "<b>ℹ️ Информация</b>\n"
        "⚜️ <code>/profile</code> — твой профиль и коллекция\n"
        "🏆 <code>/top</code> — список сильнейших\n\n"
        "<b>🛡️ Магазин (будущее)</b>\n"
        "<code>/privilege</code> — твоя скидка\n"
        "<code>/catalog</code> — ссылка на каталог\n\n"
        "<b><i>Ранг даёт власть. Гильдия даёт путь. Искажение награждает верных.</i></b>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💍 Создать именной блант", callback_data="craft_named")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
    ])
    await msg.reply_text(text, parse_mode='HTML', reply_markup=kb)

# Скидка
async def privilege_callback(update, context):
    user, msg = get_user_and_msg(update)
    uid = user.id
    p = await get_player_cached(uid)
    if not p: await msg.reply_text("🕳️ Ты ещё не активирован. /start"); return
    bal = p["balance"]
    rank_emoji, rank_name = "🪓", "Рекрут"
    for emoji, threshold, _ in RANKS:
        if bal >= threshold: rank_emoji, rank_name = emoji, emoji_to_name(emoji)
    if bal >= 20000: percent = 100; active = 10
    elif bal >= 5000: percent = min(100, int((bal - 5000) / (20000 - 5000) * 100)); active = percent // 10
    else: percent = min(100, int(bal / 5000 * 100)); active = percent // 10
    inactive = 10 - active
    progress_bar = "🟪" * active + "⬛️" * inactive
    quote = "🩸 <i>Кровь питает Искажение. Павшие дают скидку</i>"
    text = (
        f"<b><i>🪪 ТВОЯ СКИДКА</i></b>\n\n"
        f"⚜️ <i>Ранг:</i> {rank_emoji} <b>{rank_name}</b>\n"
        f"💎 <i>OAC:</i> <b>{bal} OAC</b> 🍬\n\n"
        f"🔮 <b><i>До след. уровня силы:</i></b>\n"
        f"{progress_bar} <b>{percent}%</b>\n\n"
        f"<i>{quote}</i>"
    )
    await msg.reply_text(text, parse_mode='HTML', reply_markup=get_back_to_menu_keyboard())

# Каталог
async def catalog_callback(update, context):
    user, msg = get_user_and_msg(update)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Перейти", url="https://t.me/antysocialshop")]])
    await msg.reply_text("<b>🕯️ ANTYSOCIALSHOP · КАТАЛОГ</b>", parse_mode='HTML', reply_markup=kb)

# Удача (полностью переписана логика начисления)
async def luck_callback(update, context, action=None):
    user, msg = get_user_and_msg(update)
    uid = user.id; uname = html.escape(user.username or user.first_name)
    p = await get_player_cached(uid)
    if not p: await msg.reply_text("Сначала активируйся: /start"); return
    bal = p["balance"]; now = datetime.now()
    last_daily = p["last_daily"]
    wheel_available = not (last_daily and (now - datetime.fromisoformat(last_daily)) < timedelta(hours=24))
    last_berserk = p["last_berserk"]
    berserk_available = (bal >= 300 and (not last_berserk or (now - datetime.fromisoformat(last_berserk)) > timedelta(hours=24)))
    text = f"<b><i>🎲 ИСПЫТАНИЕ СУДЬБЫ</i></b>\n\n🛡️ <i>ты держишь:</i> <code>{bal}</code> 🍬\n\n"
    kb_rows = []
    if wheel_available:
        kb_rows.append([InlineKeyboardButton("🎡 Крутить", callback_data="luck_wheel")])
    else:
        diff = timedelta(hours=24) - (now - datetime.fromisoformat(last_daily))
        hrs = int(diff.seconds // 3600); mins = int((diff.seconds % 3600) // 60)
        kb_rows.append([InlineKeyboardButton(f"🎡 {hrs} ч {mins} мин", callback_data="luck_wheel")])
    if berserk_available:
        kb_rows.append([InlineKeyboardButton("🎲 Рискнуть", callback_data="luck_berserk")])
    else:
        if bal < 300:
            kb_rows.append([InlineKeyboardButton(f"🎲 нужно ещё {300 - bal} 🍬", callback_data="luck_berserk")])
        else:
            diff = timedelta(hours=24) - (now - datetime.fromisoformat(last_berserk))
            hrs = int(diff.seconds // 3600); mins = int((diff.seconds % 3600) // 60)
            kb_rows.append([InlineKeyboardButton(f"🎲 {hrs} ч {mins} мин", callback_data="luck_berserk")])
    kb_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="menu")])
    kb = InlineKeyboardMarkup(kb_rows)
    if action == "luck_wheel":
        if not wheel_available:
            remain = timedelta(hours=24) - (now - datetime.fromisoformat(last_daily))
            hrs = int(remain.total_seconds()//3600); mins = int((remain.total_seconds()%3600)//60)
            await send_whisper_dm(update, context, f"<b><i>🎡 Колесо не готово</i></b>\n\n💎 Испытаешь через <b>{hrs} ч {mins} мин</b>.", life_seconds=20)
            return
        await update_last_daily(uid)
        r = random.random()
        if r <= 0.4: prize = 30; prize_type = "oac"
        elif r <= 0.65: prize = 75; prize_type = "oac"
        elif r <= 0.8: prize = 1; prize_type = "blunt"
        elif r <= 0.9: prize = 150; prize_type = "oac"
        elif r <= 0.97: prize = 2; prize_type = "blunt"
        else:
            prize_type = "jackpot"
            prize = 1000
            double = random.random() < 0.5
            if double: prize *= 2
        # Happy hour применяется до начисления
        final_prize = prize
        if context.bot_data.get("happy_hour") and prize_type == "oac" and prize <= 150:
            final_prize = prize * HAPPY_HOUR_MULTIPLIER
        elif context.bot_data.get("happy_hour") and prize_type == "jackpot":
            final_prize = prize * HAPPY_HOUR_MULTIPLIER

        if prize_type == "jackpot":
            await update_balance(uid, uname, final_prize)
            txt = f"🌟 <b><i>ДЖЕКПОТ!</i></b> <code>+{final_prize} OAC</code> 🍬"
            await grant_title(uid, "🧛🏻‍♀️", "Призрачный Гончий", context)
            await context.bot.send_message(chat_id="@guild_antysocial", text=f"🌟 @{uname} сорвал Джекпот! {txt}", parse_mode='HTML')
        elif prize_type == "oac":
            await update_balance(uid, uname, final_prize)
            txt = f"+{final_prize} OAC 🍬"
        else:  # бланты
            await update_blunts(uid, uname, prize)
            txt = f"+{prize} 🌿 Блант"
        new_bal = (await get_player_cached(uid))["balance"]
        text = f"<b><i>🎲 КОЛЕСО СМОТРИТЕЛЯ</i></b>\n\n{txt} → 💰 <b>{new_bal} OAC</b> 🍬"
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="luck")]]), parse_mode='HTML')
        return
    if action == "luck_berserk":
        if not berserk_available:
            if bal < 300: await send_whisper_dm(update, context, f"<b><i>🎲 Бездна требует жертву</i></b>\n\n⚠️ Недостаточно OAC (нужно ещё <b>{300-bal}</b>).", life_seconds=20)
            else:
                diff = timedelta(hours=24) - (now - datetime.fromisoformat(last_berserk))
                hrs = int(diff.total_seconds()//3600); mins = int((diff.total_seconds()%3600)//60)
                await send_whisper_dm(update, context, f"<b><i>🎲 Бездна молчит</i></b>\n\n🕳️ Примет тебя через <b>{hrs} ч {mins} мин</b>.", life_seconds=20)
            return
        await update_last_berserk(uid)
        if random.random() < 0.6: await update_balance(uid, uname, 200); res_text = f"<b><i>🎲 БЕЗДНА ОТВЕТИЛА</i></b>\n\nИскажение благосклонно! +<b>200 OAC</b> 🍬."
        else: await update_balance(uid, uname, -300); res_text = f"<b><i>🕳️ БЕЗДНА МОЛЧИТ</i></b>\n\nИскажение промолчало. –<b>300 OAC</b>."
        await msg.edit_text(res_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="luck")]]), parse_mode='HTML')
        return
    await msg.edit_text(text, reply_markup=kb, parse_mode='HTML')

# Проверка бланта
async def check_blunt(update, context):
    if not context.args: await update.message.reply_text("Укажи NFT-код бланта: /check VOID-BONE-ASH-0042"); return
    nft_id = context.args[0].strip().upper()
    db = await get_db_connection()
    cur = await db.execute("SELECT blunt_id, created_by, serial FROM nft_registry WHERE blunt_id LIKE ?", (f"%{nft_id}%",))
    row = await cur.fetchone()
    if not row:
        await update.message.reply_text("🕳️ Блант с таким кодом не найден в Искажении.")
        await db.close()
        return
    blunt_id, creator_id, serial = row["blunt_id"], row["created_by"], row["serial"]
    cur = await db.execute("SELECT user_id, inventory FROM players WHERE inventory LIKE ?", (f"%{blunt_id}%",))
    owner_id = None; item = None
    async for user_row in cur:
        try:
            inv = json.loads(user_row["inventory"])
            for it in inv:
                if it.get("id") == blunt_id: owner_id = user_row["user_id"]; item = it; break
        except: continue
        if owner_id: break
    await db.close()
    if not item: await update.message.reply_text("Блант найден в реестре, но его владелец не обнаружен."); return
    name = item["name"]; rarity = item.get("rarity","common")
    color = {"legendary":"🟡","epic":"🟣","rare":"🔵"}.get(rarity,"🟢")
    reaction = item.get("reaction",""); rare_number = item.get("rare_number","?-????"); hash_code = item.get("hash","0x????...????")
    details = (
        f"<b>ДЕТАЛИ NFT БЛАНТА 💎</b>\n\n"
        f"{color} <b>{name}</b>\n\n"
        f"<b>Редкость:</b> <i>{rarity}</i> {color}\n\n"
        f"🩸 <b>Серийный номер:</b> <b>#{rare_number}</b>\n"
        f"🔗 <b>Хеш:</b> <b>{hash_code}</b>\n"
        f"📜 <b>Реакция:</b> <i>{reaction}</i>\n"
    )
    if "owner_history" in item:
        details += "\n🔄 История владения:\n"
        for entry in item["owner_history"]:
            date_str = format_date(entry.get('since',''))
            details += f"   @{entry.get('user_id','?')} — {date_str}\n"
    await update.message.reply_text(details, parse_mode='HTML')
    # Увеличиваем счётчик проверок
    await increment_counter(update.effective_user.id, "check_count")

# Колбэк handler
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid = q.from_user.id
    try:
        if data == "menu":
            await q.answer()
            kb, whisper = await get_main_menu_keyboard(uid)
            text = f"<b>🎮 ГЛАВНОЕ МЕНЮ</b>\n\n<i>{whisper}</i>"
            await q.message.edit_text(text, reply_markup=kb, parse_mode='HTML')
        elif data == "farm": await q.answer(); await farm_callback(update, context)
        elif data == "craft": await q.answer(); await craft_callback(update, context)
        elif data == "smoke": await q.answer(); await smoke_callback(update, context)
        elif data == "ritual": await q.answer(); await ritual_callback(update, context)
        elif data == "collect": await q.answer(); await collect_callback(update, context)
        elif data == "profile": await q.answer(); await profile_callback(update, context)
        elif data == "top": await q.answer(); await top_callback(update, context)
        elif data == "guild_info": await q.answer(); await guild_info_callback(update, context)
        elif data == "rules": await q.answer(); await rules_callback(update, context)
        elif data == "privilege": await q.answer(); await privilege_callback(update, context)
        elif data == "catalog": await q.answer(); await catalog_callback(update, context)
        elif data == "luck": await q.answer(); await luck_callback(update, context)
        elif data in ("luck_wheel", "luck_berserk"): await q.answer(); await luck_callback(update, context, action=data)
        elif data == "craft_normal": await q.answer(); await handle_craft_normal(update, context)
        elif data == "craft_named": await q.answer(); await handle_craft_named(update, context)
        elif data == "cancel_named": await q.answer(); await cancel_named(update, context)
        elif data == "do_smoke": await q.answer(); await do_smoke(update, context)
        elif data.startswith("share_blunt_"):
            await q.answer()
            blunt_id = data.replace("share_blunt_", "")
            p = await get_player_cached(uid)
            if not p: return
            bot_username = (await context.bot.get_me()).username
            ref_link = f"https://t.me/{bot_username}?start=blunt_{blunt_id}"
            inv = json.loads(p["inventory"]) if p["inventory"] else []
            item = next((it for it in inv if it.get("id")==blunt_id), None)
            username = html.escape(p["username"])
            if item:
                name = item["name"]; rarity = item.get("rarity","common")
                color = {"legendary":"🟡","epic":"🟣","rare":"🔵"}.get(rarity,"🟢")
                text = (
                    f"<b>{username}</b>\n\n"
                    f"{color} <b>Имя NFT бланта: «{name}»</b>\n"
                    f"🧬 <b>Редкость:</b> {rarity} {color}\n"
                    f"🩸 <b>Серийный номер:</b> #{item.get('rare_number','?-????')}\n"
                    f"📜 <b>Реакция:</b> <i>{item.get('reaction','')}</i>\n\n"
                    f"<i>Присоединяйся к Искажению:</i>\n{ref_link}"
                )
            else:
                text = f"Блант не найден.\n{ref_link}"
            await send_whisper_dm(update, context, text, life_seconds=30)
        elif data.startswith("blunt_details_"):
            await q.answer()
            blunt_id = data.replace("blunt_details_", "")
            p = await get_player_cached(uid)
            if not p: return
            inv = json.loads(p["inventory"]) if p["inventory"] else []
            item = next((it for it in inv if it.get("id")==blunt_id), None)
            if not item: await q.answer("Блант не найден."); return
            name = item["name"]; rarity = item.get("rarity","common")
            color = {"legendary":"🟡","epic":"🟣","rare":"🔵"}.get(rarity,"🟢")
            reaction = item.get("reaction",""); rare_number = item.get("rare_number","?-????"); hash_code = item.get("hash","0x????...????")
            details = (
                f"<b>ДЕТАЛИ NFT БЛАНТА 💎</b>\n\n"
                f"{color} <b>{name}</b>\n\n"
                f"<b>Редкость:</b> <i>{rarity}</i> {color}\n\n"
                f"🩸 <b>Серийный номер:</b> <b>#{rare_number}</b>\n"
                f"🔗 <b>Хеш:</b> <b>{hash_code}</b>\n"
                f"📜 <b>Реакция:</b> <i>{reaction}</i>\n"
            )
            if "owner_history" in item:
                details += "\n🔄 История владения:\n"
                for entry in item["owner_history"]:
                    date_str = format_date(entry.get('since',''))
                    details += f"   @{entry.get('user_id','?')} — {date_str}\n"
            await send_whisper_dm(update, context, details, life_seconds=20)
        elif data == "pet_preview": await q.answer(); await send_whisper_dm(update, context, "<b><i>🐾 ПИТОМЕЦ</i></b>\n\n⚔️ Доступен с ранга Ветеран (5000 OAC).", life_seconds=10)
        elif data == "bush_preview": await q.answer(); await send_whisper_dm(update, context, "<b><i>🪴 КУСТ</i></b>\n\n⚔️ Доступен с ранга Ветеран (5000 OAC).", life_seconds=10)
        elif data == "activate_menu":
            await q.answer()
            context.args = ["activate"]
            await start(update, context)
        elif data in ("guild_join_BLACK", "guild_join_WHITE"):
            await q.answer()
            guild = "BLACK" if data == "guild_join_BLACK" else "WHITE"
            await set_guild(uid, guild)
            g_emoji = "🕯️" if guild=="BLACK" else "⚜️"; g_name = "Тёмная" if guild=="BLACK" else "Светлая"
            uname = html.escape(q.from_user.username or q.from_user.first_name)
            await q.message.edit_text(
                f"<b><i>🕋 ГИЛЬДИЯ ТЕБЯ ПРИНЯЛА</i></b>\n\n"
                f"✅ Теперь <b>ты</b> — {g_emoji} <b>{g_name} Гильдия</b> ·\n\n"
                f"<i>🩸 Искажение стало плотнее...</i>",
                parse_mode='HTML'
            )
            await context.bot.send_message(chat_id="@guild_antysocial",
                text=f"<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n⚜️ <b>@{uname}</b> вплёл свою нить в {g_emoji} <b>{g_name} Гильдию</b>.\n<i>🕯️ Искажение приняло нового странника.</i>",
                parse_mode='HTML')
        else: await q.answer("Неизвестная команда.")
    except Exception as e:
        logger.error(f"Button error: {e}")

# Остальные обработчики
async def welcome_new_member(update, context):
    for member in update.message.new_chat_members:
        if member.is_bot: continue
        await update.message.reply_text(
            f"<b><i>🕯️ ДОБРО ПОЖАЛОВАТЬ</i></b>\n\n"
            f"⚜️ <b>{html.escape(member.username or member.first_name)}</b>, добро пожаловать в <b><i>Гильдию</i></b>\n"
            f"<i>Твой первый /farm уже ждёт</i>"
        )

async def guild_join_ru(update, context):
    user_id = update.effective_user.id
    if await get_guild(user_id): await update.message.reply_text("❌ Ты уже состоишь в Гильдии."); return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🕯️ Тёмная", callback_data="guild_join_BLACK"),
         InlineKeyboardButton("⚜️ Светлая", callback_data="guild_join_WHITE")]
    ])
    await update.message.reply_text("🕋 Выбери свою Гильдию, Странник:", reply_markup=kb)

async def handle_chat_shortcut(update, context):
    text = update.message.text.strip().lower()
    mapping = {"фарм": farm_callback, "дунуть": smoke_callback, "крафт": craft_callback,
               "топ": top_callback, "удача": luck_callback, "профиль": profile_callback}
    if text in mapping: await mapping[text](update, context)

# === ДЖОБЫ ===
async def update_pulse(context):
    db = await get_db_connection()
    cur = await db.execute("SELECT COUNT(*) as total, COALESCE(SUM(balance),0) as sum_bal FROM players")
    row = await cur.fetchone()
    total_oas = row["sum_bal"]
    cur = await db.execute("SELECT COUNT(*) as cnt FROM players WHERE guild='BLACK'")
    black = (await cur.fetchone())["cnt"]
    cur = await db.execute("SELECT COUNT(*) as cnt FROM players WHERE guild='WHITE'")
    white = (await cur.fetchone())["cnt"]
    cur = await db.execute("SELECT COUNT(DISTINCT user_id) as cnt FROM players WHERE last_farm > ?", (datetime.now()-timedelta(hours=1),))
    online = (await cur.fetchone())["cnt"]
    await db.close()
    desc = f"🕯️{black} ▰▱⚜️{white} | 👥{online}"
    try: await context.bot.set_chat_description(chat_id="@guild_antysocial", description=desc)
    except: pass

async def happy_hour_trigger(context):
    context.bot_data["happy_hour"] = True
    context.bot_data["happy_hour_end"] = datetime.now() + timedelta(minutes=HAPPY_HOUR_DURATION_MIN)
    await context.bot.send_message(chat_id="@guild_antysocial", text="🌟 <b>ЧАС УДАЧИ!</b> Все действия приносят x2 🍬 30 минут!", parse_mode='HTML')
    context.job_queue.run_once(reset_happy_hour, HAPPY_HOUR_DURATION_MIN*60)

async def reset_happy_hour(context):
    context.bot_data["happy_hour"] = False
    await context.bot.send_message(chat_id="@guild_antysocial", text="⏳ Час Удачи завершён.")

async def echo_of_distortion(context):
    db = await get_db_connection()
    cur = await db.execute("SELECT user_id, username, inventory FROM players WHERE inventory IS NOT NULL AND inventory != '[]'")
    rows = await cur.fetchall()
    all_named = []
    for row in rows:
        try:
            inv = json.loads(row["inventory"])
            for item in inv:
                if item.get("type")=="named":
                    all_named.append((row["user_id"], row["username"], item))
        except: continue
    await db.close()
    if len(all_named)==0: return
    sample = random.sample(all_named, min(3,len(all_named)))
    text = "<b><i>🩸 ЭХО ИСКАЖЕНИЯ</i></b>\n\n"
    for uid, uname, item in sample:
        name = item["name"]; rarity = item.get("rarity","common")
        color = {"legendary":"🟡","epic":"🟣","rare":"🔵"}.get(rarity,"🟢")
        reaction = item.get("reaction","")
        text += f"⚜️ <b>@{html.escape(uname)}</b> создал свой блант {color} <b><i>«{html.escape(name)}»</i></b> 🌿\n<i>Редкость: {rarity}</i>\n🩸 <i>{reaction}</i>\n\n"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💍 Создать свой блант", callback_data="craft_named")]])
    await context.bot.send_message(chat_id="@guild_antysocial", text=text, parse_mode='HTML', reply_markup=kb)

async def weekly_guild_rating(context):
    db = await get_db_connection()
    await db.execute("UPDATE guild_weekly SET total_farmed=0")
    await db.execute("UPDATE guild_weekly SET total_farmed = (SELECT COALESCE(SUM(balance),0) FROM players WHERE guild = guild_weekly.guild)")
    await db.execute("UPDATE guild_weekly SET week_start=?",(date.today(),))
    await db.commit()
    cur = await db.execute("SELECT guild, total_farmed FROM guild_weekly")
    rows = await cur.fetchall()
    await db.close()
    if len(rows)>=2:
        black = next((r["total_farmed"] for r in rows if r["guild"]=="BLACK"),0)
        white = next((r["total_farmed"] for r in rows if r["guild"]=="WHITE"),0)
        winner = "BLACK" if black > white else "WHITE"
        wrd = "ритуалу" if winner=="BLACK" else "сохранению бланта"
        winner_name = "Тёмная" if winner=="BLACK" else "Светлая"
        await context.bot.send_message(chat_id="@guild_antysocial",
            text=f"<b><i>🎉 ЧАС ТРИУМФА</i></b>\n🕯️ {winner_name} Гильдия получает благословение: <code>+5%</code> к {wrd} на неделю", parse_mode='HTML')

# === ЗАПУСК ===
if __name__ == "__main__":
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop); loop.run_until_complete(init_db())
    Thread(target=run_web_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    for cmd, cbk in [
        ("start", start), ("farm", farm_callback), ("craft", craft_callback),
        ("smoke", smoke_callback), ("ritual", ritual_callback),
        ("profile", profile_callback), ("top", top_callback), ("rules", rules_callback),
        ("privilege", privilege_callback), ("catalog", catalog_callback),
        ("luck", luck_callback), ("collect", collect_callback),
        ("check", check_blunt)
    ]:
        app.add_handler(CommandHandler(cmd, cbk))
    app.add_handler(MessageHandler(filters.Regex(RE_FARM), farm_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_CRAFT), craft_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_SMOKE), smoke_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_RITUAL), ritual_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_TOP), top_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_LUCK), luck_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_PROFILE), profile_callback))
    app.add_handler(MessageHandler(filters.Regex(RE_GUILD), guild_join_ru))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(RE_SHORTCUTS), handle_chat_shortcut))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_named_name))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(CallbackQueryHandler(button_handler))
    job = app.job_queue
    job.run_repeating(update_pulse, interval=300, first=10)
    job.run_once(lambda c: job.run_repeating(happy_hour_trigger, interval=random.randint(14400,28800), first=random.randint(3600,10800)), when=1)
    job.run_daily(echo_of_distortion, time=time(hour=18, minute=0))
    now = datetime.now()
    days_until_saturday = (5 - now.weekday()) % 7
    next_saturday = (now + timedelta(days=days_until_saturday)).replace(hour=12, minute=0, second=0, microsecond=0)
    if next_saturday <= now: next_saturday += timedelta(days=7)
    job.run_repeating(weekly_guild_rating, interval=7*24*3600, first=max(1, (next_saturday - now).total_seconds()))
    print("BOT READY"); app.run_polling(); loop.close()
