import asyncio
import logging
import os
import random
import sqlite3
import time
from contextlib import closing
from html import escape

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web

# ---------------------------------------------------------------------------
# НАСТРОЙКИ (из переменных окружения — токен в коде не хранится)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CREATOR_ID = int(os.getenv("CREATOR_TELEGRAM_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "boxing_life.db")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")

# ---------------------------------------------------------------------------
# ИГРОВЫЕ ДАННЫЕ
# ---------------------------------------------------------------------------
STATS = {
    "strength": "💪 Сила",
    "speed": "⚡ Скорость",
    "endurance": "🫀 Выносливость",
    "defense": "🛡 Защита",
    "reaction": "👁 Реакция",
    "accuracy": "🎯 Точность",
    "crit": "💥 Крит (%)",
    "recovery": "♻️ Восстановление",
}

NPCS = {
    1: {"name": "Артём «Малыш» Волков", "level": 1, "health": 100, "strength": 8, "speed": 8, "defense": 4, "exp": 40, "money": 60},
    2: {"name": "Тимур «Быстрый» Садыков", "level": 3, "health": 120, "strength": 12, "speed": 16, "defense": 6, "exp": 70, "money": 110},
    3: {"name": "Денис «Кулак» Морозов", "level": 5, "health": 150, "strength": 15, "speed": 12, "defense": 9, "exp": 110, "money": 180},
    4: {"name": "Илья «Таран» Ковалёв", "level": 7, "health": 180, "strength": 18, "speed": 13, "defense": 12, "exp": 160, "money": 260},
    5: {"name": "Рустам «Зверь» Алиев", "level": 10, "health": 220, "strength": 22, "speed": 16, "defense": 15, "exp": 250, "money": 400},
}

ITEMS = {
    "energy": {"name": "⚡ Энергетик", "price": 40, "kind": "consumable", "desc": "+50 выносливости"},
    "medkit": {"name": "🩹 Аптечка", "price": 60, "kind": "consumable", "desc": "+50 здоровья"},
    "pro": {"name": "💉 Спортпит", "price": 150, "kind": "consumable", "desc": "полное восстановление ❤️ и ⚡"},
    "gloves": {"name": "🥊 Перчатки", "price": 300, "kind": "upgrade", "stat": "strength", "amount": 2, "desc": "+2 к силе навсегда"},
    "shoes": {"name": "👟 Боксёрки", "price": 300, "kind": "upgrade", "stat": "speed", "amount": 2, "desc": "+2 к скорости навсегда"},
    "guard": {"name": "🦷 Капа и защита", "price": 300, "kind": "upgrade", "stat": "defense", "amount": 2, "desc": "+2 к защите навсегда"},
}

LEGAL_TITLES = [(1, "Новичок"), (3, "Любитель"), (5, "Перспективный боец"), (8, "Профи"), (12, "Мастер ринга"), (20, "Чемпион")]
STREET_TITLES = [(1, "Уличный новичок"), (3, "Дворовый боец"), (5, "Гроза района"), (8, "Подпольный боец"), (12, "Король улиц"), (20, "Легенда подполья")]

DMG_SCALE = 1.6
TRAIN_COST = 20
HIT_COST = 5
POWER_COST = 15
GUARD_GAIN = 8

# Активные бои: {telegram_id: {"npc_id": int, "npc_hp": int, "round": int}}
FIGHTS: dict[int, dict] = {}

# ---------------------------------------------------------------------------
# БАЗА ДАННЫХ (SQLite)
# ---------------------------------------------------------------------------
USER_FIELDS = [
    "path_type", "level", "exp", "health", "max_health", "stamina", "max_stamina",
    "balance_dollars", "title", "strength", "speed", "endurance", "defense",
    "reaction", "accuracy", "crit", "recovery", "wins", "losses", "is_banned", "last_regen",
]


def db() -> sqlite3.Connection:
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                path_type TEXT DEFAULT 'legal',
                level INTEGER DEFAULT 1,
                exp INTEGER DEFAULT 0,
                health INTEGER DEFAULT 100,
                max_health INTEGER DEFAULT 100,
                stamina INTEGER DEFAULT 100,
                max_stamina INTEGER DEFAULT 100,
                balance_dollars REAL DEFAULT 500.0,
                title TEXT DEFAULT 'Новичок',
                strength INTEGER DEFAULT 10,
                speed INTEGER DEFAULT 10,
                endurance INTEGER DEFAULT 10,
                defense INTEGER DEFAULT 10,
                reaction INTEGER DEFAULT 10,
                accuracy INTEGER DEFAULT 10,
                crit INTEGER DEFAULT 5,
                recovery INTEGER DEFAULT 10,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                last_regen REAL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory (
                telegram_id INTEGER,
                item_id TEXT,
                qty INTEGER DEFAULT 0,
                PRIMARY KEY (telegram_id, item_id)
            )
            """
        )
        conn.commit()


def title_for(path: str, level: int) -> str:
    titles = LEGAL_TITLES if path == "legal" else STREET_TITLES
    result = titles[0][1]
    for min_level, name in titles:
        if level >= min_level:
            result = name
    return result


def save_user(u: dict) -> None:
    sets = ", ".join(f"{f}=?" for f in USER_FIELDS)
    values = [u[f] for f in USER_FIELDS] + [u["telegram_id"]]
    with closing(db()) as conn:
        conn.execute(f"UPDATE users SET {sets} WHERE telegram_id=?", values)
        conn.commit()


def load_user(uid: int) -> dict | None:
    """Загружает игрока и начисляет пассивное восстановление (кроме времени боя)."""
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
    if not row:
        return None
    u = dict(row)
    if uid not in FIGHTS:
        ticks = int((time.time() - u["last_regen"]) // 10)
        if ticks > 0:
            rec = u["recovery"]
            u["stamina"] = min(u["max_stamina"], u["stamina"] + ticks * max(1, rec // 5))
            u["health"] = min(u["max_health"], u["health"] + ticks * max(1, rec // 10))
            u["last_regen"] = u["last_regen"] + ticks * 10
            save_user(u)
    return u


def create_user(uid: int, username: str | None, path: str) -> None:
    with closing(db()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username, path_type, title, last_regen) VALUES (?, ?, ?, ?, ?)",
            (uid, username, path, title_for(path, 1), time.time()),
        )
        conn.commit()


def inv_get(uid: int) -> dict[str, int]:
    with closing(db()) as conn:
        rows = conn.execute("SELECT item_id, qty FROM inventory WHERE telegram_id=? AND qty>0", (uid,)).fetchall()
    return {r["item_id"]: r["qty"] for r in rows}


def inv_add(uid: int, item_id: str, delta: int) -> None:
    with closing(db()) as conn:
        conn.execute("INSERT OR IGNORE INTO inventory (telegram_id, item_id, qty) VALUES (?, ?, 0)", (uid, item_id))
        conn.execute("UPDATE inventory SET qty = MAX(0, qty + ?) WHERE telegram_id=? AND item_id=?", (delta, uid, item_id))
        conn.commit()


def add_exp(u: dict, amount: int) -> int:
    u["exp"] += amount
    ups = 0
    while u["exp"] >= u["level"] * 100:
        u["exp"] -= u["level"] * 100
        u["level"] += 1
        u["max_health"] += 10
        u["max_stamina"] += 5
        u["health"] = u["max_health"]
        u["stamina"] = u["max_stamina"]
        ups += 1
    u["title"] = title_for(u["path_type"], u["level"])
    return ups


# ---------------------------------------------------------------------------
# БОЕВАЯ СИСТЕМА
# ---------------------------------------------------------------------------
def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def npc_skill(npc: dict) -> int:
    return 6 + 2 * npc["level"]


def stamina_cost(u: dict, action: str) -> int:
    base = POWER_COST if action == "power" else HIT_COST
    return max(1, base - u["endurance"] // 10)


def resolve_round(u: dict, npc: dict, state: dict, action: str) -> list[str]:
    log: list[str] = []
    npc_action = random.choices(["hit", "power", "guard"], weights=[50, 25, 25])[0]

    # --- ход игрока ---
    if action == "guard":
        u["stamina"] = min(u["max_stamina"], u["stamina"] + GUARD_GAIN)
        log.append(f"🛡 Вы прикрылись и перевели дух (+{GUARD_GAIN} ⚡).")
    else:
        mult = 1.8 if action == "power" else 1.0
        u["stamina"] -= stamina_cost(u, action)
        chance = clamp(0.6 + (u["accuracy"] - npc_skill(npc)) * 0.01 - (0.15 if action == "power" else 0), 0.2, 0.95)
        if random.random() < chance:
            dmg = (u["strength"] + u["speed"] * 0.3) * DMG_SCALE * mult * random.uniform(0.8, 1.2) - npc["defense"] * 0.4
            crit = random.random() * 100 < u["crit"]
            if crit:
                dmg *= 1.7
            if npc_action == "guard":
                dmg *= 0.4
            dmg = max(1, int(dmg))
            state["npc_hp"] -= dmg
            log.append(
                f"{'💥 КРИТ! ' if crit else '👊 '}Попадание: -{dmg} ❤️ противнику"
                + (" (он блокировал)" if npc_action == "guard" else "")
            )
        else:
            log.append("💨 Вы промахнулись.")

    # --- ход противника ---
    if npc_action == "guard":
        log.append("🛡 Противник прикрывается.")
    else:
        mult = 1.8 if npc_action == "power" else 1.0
        chance = clamp(0.5 + (npc_skill(npc) - u["reaction"]) * 0.01 - (0.15 if npc_action == "power" else 0), 0.15, 0.9)
        if random.random() < chance:
            dmg = (npc["strength"] + npc["speed"] * 0.3) * DMG_SCALE * mult * random.uniform(0.8, 1.2) - u["defense"] * 0.4
            if action == "guard":
                dmg *= 0.4
            if u["path_type"] == "street":
                dmg *= 1.2
            dmg = max(1, int(dmg))
            u["health"] = max(0, u["health"] - dmg)
            log.append(f"{'🔥 Сильный удар! ' if npc_action == 'power' else '🥊 '}Вам прилетело: -{dmg} ❤️")
        else:
            log.append("😮‍💨 Вы увернулись от удара.")
    return log


def finish_fight(u: dict, npc: dict, won: bool, surrender: bool = False) -> str:
    FIGHTS.pop(u["telegram_id"], None)
    u["last_regen"] = time.time()
    if won:
        exp, money = npc["exp"], npc["money"]
        if u["level"] > npc["level"] + 3:
            exp //= 2
            money //= 2
        if u["path_type"] == "legal":
            exp = int(exp * 1.25)
        else:
            money = int(money * 1.5)
        u["balance_dollars"] += money
        u["wins"] += 1
        ups = add_exp(u, exp)
        text = f"🏆 <b>ПОБЕДА!</b>\n+{exp} опыта, +${money}"
        if ups:
            text += f"\n🎉 Новый уровень: <b>{u['level']}</b>! Здоровье и выносливость восстановлены."
    else:
        u["losses"] += 1
        if surrender:
            u["health"] = max(1, u["health"])
            text = "🏳️ Вы сдались. Деньги не потеряны."
        else:
            loss = min(int(u["balance_dollars"]), 20)
            u["balance_dollars"] -= loss
            u["health"] = max(1, u["max_health"] // 10)
            text = f"💀 <b>НОКАУТ!</b> Вы проиграли и потеряли ${loss}."
    save_user(u)
    return text


# ---------------------------------------------------------------------------
# ИНТЕРФЕЙС
# ---------------------------------------------------------------------------
def bar(cur: float, mx: float, size: int = 10) -> str:
    filled = int(round(size * max(0, cur) / mx)) if mx else 0
    return "█" * filled + "░" * (size - filled)


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def main_menu() -> InlineKeyboardMarkup:
    return kb([
        [("👤 Профиль", "menu:profile"), ("🥊 Бой с NPC", "menu:npcs")],
        [("🏋️ Тренировка", "menu:train"), ("📊 Характеристики", "menu:stats")],
        [("🛒 Магазин", "menu:shop"), ("🎒 Инвентарь", "menu:inv")],
        [("⚙️ Помощь", "menu:help")],
    ])


BACK = kb([[("🏠 Меню", "menu:main")]])
NEED_START = "❌ Вы не зарегистрированы! Напишите /start"


def view_profile(u: dict, name: str) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "👤 <b>ПРОФИЛЬ БОЙЦА</b>\n\n"
        f"Боец: {escape(name)}\n"
        f"🏆 Титул: {u['title']}\n"
        f"⭐ Уровень: {u['level']} (опыт {u['exp']}/{u['level'] * 100})\n"
        f"❤️ Здоровье: {u['health']}/{u['max_health']}\n"
        f"⚡ Выносливость: {u['stamina']}/{u['max_stamina']}\n"
        f"💰 Баланс: ${u['balance_dollars']:.0f}\n"
        f"🥊 Путь: {'Легальный бокс' if u['path_type'] == 'legal' else 'Бокс без правил'}\n"
        f"📈 Побед: {u['wins']} · Поражений: {u['losses']}"
    )
    return text, BACK


def view_stats(u: dict) -> tuple[str, InlineKeyboardMarkup]:
    lines = "\n".join(f"{label}: {u[key]}" for key, label in STATS.items())
    return f"📊 <b>ХАРАКТЕРИСТИКИ</b>\n\n{lines}", BACK


def view_train(u: dict, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    text = (
        (f"{note}\n\n" if note else "")
        + f"🏋️ <b>ТРЕНИРОВКА</b>\nКаждое занятие стоит {TRAIN_COST} ⚡ и даёт +1 к выбранной характеристике.\n"
        f"⚡ Сейчас: {u['stamina']}/{u['max_stamina']}"
    )
    keys = list(STATS.items())
    rows = [[(f"{keys[i][1]} ({u[keys[i][0]]})", f"train:{keys[i][0]}"), (f"{keys[i + 1][1]} ({u[keys[i + 1][0]]})", f"train:{keys[i + 1][0]}")] for i in range(0, len(keys), 2)]
    rows.append([("🏠 Меню", "menu:main")])
    return text, kb(rows)


def view_npcs(u: dict) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🥊 <b>ПРОТИВНИКИ</b>\nВыберите соперника. Для боя нужно ❤️ ≥ 20 и ⚡ ≥ 10.\n"
        f"❤️ {u['health']}/{u['max_health']} · ⚡ {u['stamina']}/{u['max_stamina']}"
    )
    rows = [[(f"{n['name']} (Ур. {n['level']})", f"fight:start:{i}")] for i, n in NPCS.items()]
    rows.append([("🏠 Меню", "menu:main")])
    return text, kb(rows)


def view_shop(u: dict, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    lines = "\n".join(f"{it['name']} — ${it['price']}: {it['desc']}" for it in ITEMS.values())
    text = (f"{note}\n\n" if note else "") + f"🛒 <b>МАГАЗИН</b>\n💰 Баланс: ${u['balance_dollars']:.0f}\n\n{lines}"
    rows = [[(f"{it['name']} · ${it['price']}", f"shop:buy:{key}")] for key, it in ITEMS.items()]
    rows.append([("🏠 Меню", "menu:main")])
    return text, kb(rows)


def view_inventory(u: dict, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    inv = inv_get(u["telegram_id"])
    if not inv:
        return (f"{note}\n\n" if note else "") + "🎒 Ваш инвентарь пуст. Загляните в магазин!", BACK
    lines = "\n".join(f"{ITEMS[k]['name']} ×{q} — {ITEMS[k]['desc']}" for k, q in inv.items() if k in ITEMS)
    rows = [[(f"Использовать: {ITEMS[k]['name']} (×{q})", f"use:{k}")] for k, q in inv.items() if k in ITEMS]
    rows.append([("🏠 Меню", "menu:main")])
    return (f"{note}\n\n" if note else "") + f"🎒 <b>ИНВЕНТАРЬ</b>\n\n{lines}", kb(rows)


def view_help(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "ℹ️ <b>ПОМОЩЬ</b>\n\n"
        "Текстовые команды: <code>меню</code>, <code>профиль</code>\n\n"
        "• Тренируйтесь, чтобы расти в характеристиках.\n"
        "• Побеждайте NPC ради опыта и денег.\n"
        "• ❤️ и ⚡ восстанавливаются со временем (быстрее при высоком «Восстановлении»).\n"
        "• Легальный бокс: +25% опыта. Бокс без правил: +50% денег, но вы получаете на 20% больше урона."
    )
    if uid == CREATOR_ID:
        text += "\n\n<b>Админ:</b> <code>админ панель</code>, /give id сумма, /ban id, /unban id"
    return text, BACK


def view_fight(u: dict, state: dict, log: list[str] | None = None) -> tuple[str, InlineKeyboardMarkup]:
    npc = NPCS[state["npc_id"]]
    text = (
        f"🥊 <b>{npc['name']}</b> · Раунд {state['round']}\n\n"
        f"Вы: ❤️ {bar(u['health'], u['max_health'])} {u['health']}/{u['max_health']}\n"
        f"⚡ {u['stamina']}/{u['max_stamina']}\n\n"
        f"Противник: ❤️ {bar(state['npc_hp'], npc['health'])} {max(0, state['npc_hp'])}/{npc['health']}"
    )
    if log:
        text += "\n\n" + "\n".join(log)
    keyboard = kb([
        [(f"👊 Удар (−{stamina_cost(u, 'hit')}⚡)", "fight:act:hit"), (f"💥 Сильный (−{stamina_cost(u, 'power')}⚡)", "fight:act:power")],
        [(f"🛡 Защита (+{GUARD_GAIN}⚡)", "fight:act:guard"), ("🏳️ Сдаться", "fight:surrender")],
    ])
    return text, keyboard


async def render(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


def build_section(section: str, u: dict, name: str) -> tuple[str, InlineKeyboardMarkup]:
    if section == "profile":
        return view_profile(u, name)
    if section == "stats":
        return view_stats(u)
    if section == "train":
        return view_train(u)
    if section == "npcs":
        return view_npcs(u)
    if section == "shop":
        return view_shop(u)
    if section == "inv":
        return view_inventory(u)
    if section == "help":
        return view_help(u["telegram_id"])
    return "🏠 <b>Главное меню BOXING LIFE</b>", main_menu()


# ---------------------------------------------------------------------------
# БОТ
# ---------------------------------------------------------------------------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id != CREATOR_ID:
            with closing(db()) as conn:
                row = conn.execute("SELECT is_banned FROM users WHERE telegram_id=?", (user.id,)).fetchone()
            if row and row["is_banned"]:
                if isinstance(event, CallbackQuery):
                    await event.answer("🚫 Вы заблокированы", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("🚫 Вы заблокированы.")
                return None
        return await handler(event, data)


dp.message.outer_middleware(BanMiddleware())
dp.callback_query.outer_middleware(BanMiddleware())


# --- старт и выбор пути ---
@dp.message(CommandStart())
async def cmd_start(message: Message):
    u = load_user(message.from_user.id)
    if u:
        await message.answer("🏠 <b>С возвращением в BOXING LIFE!</b>", reply_markup=main_menu())
        return
    await message.answer(
        "🥊 <b>ДОБРО ПОЖАЛОВАТЬ В BOXING LIFE</b> 🥊\n\n"
        "Выберите свой путь в мире бокса (потом изменить нельзя):\n\n"
        "• <b>Легальный бокс</b>: официальные лиги, +25% опыта.\n"
        "• <b>Бокс без правил</b>: подпольные бои, +50% денег, но больше урона по вам.",
        reply_markup=kb([[("🥊 Легальный бокс", "path:legal")], [("🩸 Бокс без правил", "path:street")]]),
    )


@dp.callback_query(F.data.startswith("path:"))
async def set_path(cb: CallbackQuery):
    await cb.answer()
    path = cb.data.split(":")[1]
    if path not in ("legal", "street"):
        return
    if load_user(cb.from_user.id):
        await render(cb, "🏠 Вы уже зарегистрированы. Путь менять нельзя.", main_menu())
        return
    create_user(cb.from_user.id, cb.from_user.username, path)
    await render(
        cb,
        f"✅ Вы зарегистрированы! Путь: <b>{'Легальный бокс' if path == 'legal' else 'Бокс без правил'}</b>.\n\nГлавное меню:",
        main_menu(),
    )


# --- текстовые команды без слэша ---
@dp.message(F.text.casefold() == "меню")
@dp.message(Command("menu"))
async def txt_menu(message: Message):
    u = load_user(message.from_user.id)
    if not u:
        await message.answer(NEED_START)
        return
    text, markup = build_section("main", u, message.from_user.full_name)
    await message.answer(text, reply_markup=markup)


@dp.message(F.text.casefold() == "профиль")
@dp.message(Command("profile"))
async def txt_profile(message: Message):
    u = load_user(message.from_user.id)
    if not u:
        await message.answer(NEED_START)
        return
    text, markup = view_profile(u, message.from_user.full_name)
    await message.answer(text, reply_markup=markup)


# --- кнопки меню ---
@dp.callback_query(F.data.startswith("menu:"))
async def menu_handler(cb: CallbackQuery):
    await cb.answer()
    u = load_user(cb.from_user.id)
    if not u:
        await cb.message.answer(NEED_START)
        return
    text, markup = build_section(cb.data.split(":")[1], u, cb.from_user.full_name)
    await render(cb, text, markup)


# --- тренировка ---
@dp.callback_query(F.data.startswith("train:"))
async def train_handler(cb: CallbackQuery):
    u = load_user(cb.from_user.id)
    if not u:
        await cb.answer(NEED_START, show_alert=True)
        return
    stat = cb.data.split(":")[1]
    if stat not in STATS:
        await cb.answer()
        return
    if cb.from_user.id in FIGHTS:
        await cb.answer("Вы в бою!", show_alert=True)
        return
    if u["stamina"] < TRAIN_COST:
        await cb.answer(f"Не хватает выносливости (нужно {TRAIN_COST} ⚡)", show_alert=True)
        return
    u["stamina"] -= TRAIN_COST
    u[stat] += 1
    if stat == "endurance":
        u["max_stamina"] += 2
    ups = add_exp(u, 5)
    save_user(u)
    note = f"✅ {STATS[stat]} +1 (теперь {u[stat]}), +5 опыта"
    if ups:
        note += f"\n🎉 Новый уровень: {u['level']}!"
    await cb.answer()
    text, markup = view_train(u, note)
    await render(cb, text, markup)


# --- бой ---
@dp.callback_query(F.data.startswith("fight:start:"))
async def fight_start(cb: CallbackQuery):
    u = load_user(cb.from_user.id)
    if not u:
        await cb.answer(NEED_START, show_alert=True)
        return
    uid = cb.from_user.id
    if uid in FIGHTS:
        await cb.answer()
        text, markup = view_fight(u, FIGHTS[uid])
        await render(cb, text, markup)
        return
    try:
        npc_id = int(cb.data.split(":")[2])
    except ValueError:
        await cb.answer()
        return
    npc = NPCS.get(npc_id)
    if not npc:
        await cb.answer()
        return
    if u["health"] < 20 or u["stamina"] < 10:
        await cb.answer("Вы слишком слабы: нужно ❤️ ≥ 20 и ⚡ ≥ 10. Отдохните или используйте предметы.", show_alert=True)
        return
    FIGHTS[uid] = {"npc_id": npc_id, "npc_hp": npc["health"], "round": 1}
    await cb.answer("Гонг! Бой начался!")
    text, markup = view_fight(u, FIGHTS[uid])
    await render(cb, text, markup)


@dp.callback_query(F.data.startswith("fight:act:"))
async def fight_act(cb: CallbackQuery):
    uid = cb.from_user.id
    state = FIGHTS.get(uid)
    u = load_user(uid)
    if not u or not state:
        await cb.answer("Этот бой уже завершён.", show_alert=True)
        if u:
            text, markup = build_section("main", u, cb.from_user.full_name)
            await render(cb, text, markup)
        return
    action = cb.data.split(":")[2]
    if action not in ("hit", "power", "guard"):
        await cb.answer()
        return
    if action != "guard" and u["stamina"] < stamina_cost(u, action):
        await cb.answer("Не хватает выносливости! Выберите «Защита», чтобы отдышаться.", show_alert=True)
        return

    npc = NPCS[state["npc_id"]]
    log = resolve_round(u, npc, state, action)
    state["round"] += 1

    if state["npc_hp"] <= 0:
        result = finish_fight(u, npc, won=True)
        await cb.answer()
        await render(cb, "\n".join(log) + "\n\n" + result, main_menu())
    elif u["health"] <= 0:
        result = finish_fight(u, npc, won=False)
        await cb.answer()
        await render(cb, "\n".join(log) + "\n\n" + result, main_menu())
    else:
        save_user(u)
        await cb.answer()
        text, markup = view_fight(u, state, log)
        await render(cb, text, markup)


@dp.callback_query(F.data == "fight:surrender")
async def fight_surrender(cb: CallbackQuery):
    uid = cb.from_user.id
    state = FIGHTS.get(uid)
    u = load_user(uid)
    if not u or not state:
        await cb.answer("Бой уже завершён.", show_alert=True)
        return
    result = finish_fight(u, NPCS[state["npc_id"]], won=False, surrender=True)
    await cb.answer()
    await render(cb, result, main_menu())


# --- магазин и инвентарь ---
@dp.callback_query(F.data.startswith("shop:buy:"))
async def shop_buy(cb: CallbackQuery):
    u = load_user(cb.from_user.id)
    if not u:
        await cb.answer(NEED_START, show_alert=True)
        return
    key = cb.data.split(":")[2]
    item = ITEMS.get(key)
    if not item:
        await cb.answer()
        return
    if u["balance_dollars"] < item["price"]:
        await cb.answer("Недостаточно денег 💸", show_alert=True)
        return
    u["balance_dollars"] -= item["price"]
    if item["kind"] == "upgrade":
        u[item["stat"]] += item["amount"]
        note = f"✅ Куплено: {item['name']} ({item['desc']})"
    else:
        inv_add(u["telegram_id"], key, 1)
        note = f"✅ Куплено: {item['name']}. Предмет в инвентаре."
    save_user(u)
    await cb.answer()
    text, markup = view_shop(u, note)
    await render(cb, text, markup)


@dp.callback_query(F.data.startswith("use:"))
async def use_item(cb: CallbackQuery):
    u = load_user(cb.from_user.id)
    if not u:
        await cb.answer(NEED_START, show_alert=True)
        return
    if cb.from_user.id in FIGHTS:
        await cb.answer("Нельзя использовать предметы во время боя!", show_alert=True)
        return
    key = cb.data.split(":")[1]
    item = ITEMS.get(key)
    if not item or item["kind"] != "consumable" or inv_get(u["telegram_id"]).get(key, 0) < 1:
        await cb.answer("Предмета нет в инвентаре.", show_alert=True)
        return
    if key == "energy":
        u["stamina"] = min(u["max_stamina"], u["stamina"] + 50)
    elif key == "medkit":
        u["health"] = min(u["max_health"], u["health"] + 50)
    elif key == "pro":
        u["health"], u["stamina"] = u["max_health"], u["max_stamina"]
    inv_add(u["telegram_id"], key, -1)
    save_user(u)
    await cb.answer()
    text, markup = view_inventory(u, f"✅ Использовано: {item['name']}")
    await render(cb, text, markup)


# --- админка ---
def is_admin(message: Message) -> bool:
    return CREATOR_ID != 0 and message.from_user.id == CREATOR_ID


@dp.message(F.text.casefold() == "админ панель")
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message):
        await message.answer("❌ У вас нет прав администратора.")
        return
    with closing(db()) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        banned = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
    await message.answer(
        "⚙️ <b>Панель администратора BOXING LIFE</b>\n\n"
        f"👥 Игроков: {total}\n🚫 Заблокировано: {banned}\n🥊 Боёв сейчас: {len(FIGHTS)}\n\n"
        "Команды:\n"
        "<code>/give id сумма</code> — выдать деньги\n"
        "<code>/ban id</code> — заблокировать\n"
        "<code>/unban id</code> — разблокировать"
    )


@dp.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    try:
        target, amount = (command.args or "").split()
        target, amount = int(target), float(amount)
    except ValueError:
        await message.answer("Использование: /give id сумма")
        return
    with closing(db()) as conn:
        cur = conn.execute("UPDATE users SET balance_dollars = balance_dollars + ? WHERE telegram_id=?", (amount, target))
        conn.commit()
    await message.answer("✅ Выдано." if cur.rowcount else "❌ Игрок не найден.")


async def _set_ban(message: Message, command: CommandObject, value: int):
    if not is_admin(message):
        return
    try:
        target = int((command.args or "").strip())
    except ValueError:
        await message.answer("Использование: /ban id  или  /unban id")
        return
    with closing(db()) as conn:
        cur = conn.execute("UPDATE users SET is_banned=? WHERE telegram_id=?", (value, target))
        conn.commit()
    FIGHTS.pop(target, None)
    await message.answer("✅ Готово." if cur.rowcount else "❌ Игрок не найден.")


@dp.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    await _set_ban(message, command, 1)


@dp.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    await _set_ban(message, command, 0)


# ---------------------------------------------------------------------------
# ЗАПУСК (бот + мини-веб-сервер для Render)
# ---------------------------------------------------------------------------
async def start_web() -> None:
    async def ok(_request):
        return web.Response(text="Boxing Life bot is running")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Health-сервер запущен на порту %s", PORT)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()
    await start_web()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("🥊 Бот BOXING LIFE запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
