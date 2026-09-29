import asyncio
import difflib
import html
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
)
from aiogram.filters import ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import billing

from locales import t, LANGUAGES, DEFAULT_LANG, city_name, ANY_CITY_NAMES, POPULAR_CITIES
from emojis import e, raw, entity_emoji, city_flag, city_flag_entity, EMOJIS

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN     = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or ""
CHANNEL_ID    = os.getenv("CHANNEL_ID", "").strip()
BOT_URL       = os.getenv("BOT_URL", "").strip()
BOOST_URL     = os.getenv("BOOST_URL", "").strip()
HELP_USERNAME = os.getenv("HELP_USERNAME", "kabulbeg").strip()
TZ_OFFSET     = int(os.getenv("TZ_OFFSET", "3")) 
DATABASE_URL  = os.getenv("DATABASE_URL", "").strip()   # PostgreSQL, см. docker-compose.yml
# Реквизиты для ручной оплаты переводом (меняются в .env без правки кода)
CARD_NUMBER   = os.getenv("CARD_NUMBER", "").strip()
CARD_HOLDER   = os.getenv("CARD_HOLDER", "").strip()    # необязательно: имя получателя

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not ADMIN_IDS_RAW:
    raise RuntimeError("ADMIN_IDS is missing")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is missing")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing (проверь .env и docker-compose.yml)")


def parse_admin_ids(raw: str) -> list[int]:
    ids = [int(p.strip()) for p in raw.split(",") if p.strip()]
    if not ids:
        raise RuntimeError("No valid ADMIN_IDS")
    return ids


ADMIN_IDS = parse_admin_ids(ADMIN_IDS_RAW)

# Кэш username бота — заполняется в main()
BOT_USERNAME: str = ""

# 10 городов для кнопок быстрого выбора — берутся из cities.json
# (поле popular=true у нужных городов, см. locales.py:_load_cities_from_json).
# Любой другой город из cities.json доступен через "Другой город" (ручной
# ввод с поиском, см. find_city_match).
CITIES = POPULAR_CITIES

# Флаги городов теперь в emojis.py (CITY_FLAGS, city_flag)


# Разговорные названия/сокращения, которые по буквенному сходству не всегда
# похожи на официальное название (нечёткий поиск их не поймает или, хуже,
# спутает с другим городом — например "Питер" по буквам ближе к "Пермь").
_CITY_ALIASES: dict[str, str] = {
    "питер":      "Санкт-Петербург",
    "спб":        "Санкт-Петербург",
    "санкт петербург": "Санкт-Петербург",
    "нижний":     "Нижний Новгород",
    "нн":         "Нижний Новгород",
    "ростов":     "Ростов-на-Дону",
    "ашгабат":    "Ашхабад",
    "чарджоу":    "Туркменабад",
    "чарджев":    "Туркменабад",
    "ташауз":     "Дашогуз",
    "красноводск": "Туркменбаши",
    "небитдаг":   "Балканабад",
}


def _all_city_lookup_names() -> dict[str, str]:
    """
    Строит плоский словарь "любое известное написание -> канонический русский
    город" из ANY_CITY_NAMES (объединяя ru- и tk-варианты) — используется для
    поиска города по ручному вводу. Пересчитывается при каждом вызове
    find_city_match, но словарь маленький (~35 городов x 2 языка), стоимость
    пренебрежимо мала на фоне сетевого round-trip к Telegram API.
    """
    lookup: dict[str, str] = {}
    for lang_dict in ANY_CITY_NAMES.values():
        for canonical, localized in lang_dict.items():
            lookup[localized.casefold().strip()] = canonical
            lookup[canonical.casefold().strip()] = canonical
    return lookup


def find_city_match(query: str) -> tuple[Optional[str], list[str]]:
    """
    Ищет город по произвольному вводу пользователя среди ANY_CITY_NAMES.

    Возвращает (exact, suggestions):
      - exact — канонический русский город, если найдено точное совпадение
        (без учёта регистра/пробелов, по любому языковому варианту записи);
        иначе None.
      - suggestions — до 3 ближайших кандидатов (канонические русские имена)
        по нечёткому совпадению, если точного совпадения нет. Пустой список,
        если ничего похожего не нашлось совсем.
    """
    normalized = query.casefold().strip()

    if normalized in _CITY_ALIASES:
        return _CITY_ALIASES[normalized], []

    lookup = _all_city_lookup_names()

    if normalized in lookup:
        return lookup[normalized], []

    close = difflib.get_close_matches(normalized, lookup.keys(), n=5, cutoff=0.6)
    suggestions: list[str] = []
    for key in close:
        canonical = lookup[key]
        if canonical not in suggestions:
            suggestions.append(canonical)
        if len(suggestions) == 3:
            break
    return None, suggestions

# Команды для фильтрации городских групп: /moscow, /kazan, /spb
# Легко расширяется — просто добавь новую пару в словарь.
CITY_COMMANDS: dict[str, str] = {
    "moscow": "Москва",
    "kazan":  "Казань",
    "spb":    "Санкт-Петербург",
}


DATE_RE  = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
PHONE_RE = re.compile(r"^\+?\d{7,15}$")


# Все эмодзи (обычные и премиум) теперь в emojis.py — единый реестр EMOJIS.

# ── Entity builder (для постов в канале/группах — без parse_mode) ────────────


class EntityBuilder:
    """Строит текст + список MessageEntity для каналов/групп (без parse_mode)."""

    def __init__(self) -> None:
        self._text = ""
        self._entities: list[MessageEntity] = []

    def _u16(self, s: str) -> int:
        return len(s.encode("utf-16-le")) // 2

    def _cur(self) -> int:
        return self._u16(self._text)

    def t(self, s: str) -> "EntityBuilder":
        self._text += s
        return self

    def emoji(self, char: str, custom_emoji_id: str) -> "EntityBuilder":
        """
        Добавляет кастомный эмодзи. char — fallback-символ (отображается если
        Premium недоступен), custom_emoji_id — Telegram ID премиум-эмодзи.
        Обычно вызывается не напрямую, а через entity_emoji() из emojis.py.
        """
        off = self._cur()
        self._text += char
        self._entities.append(MessageEntity(
            type="custom_emoji", offset=off,
            length=self._u16(char), custom_emoji_id=custom_emoji_id,
        ))
        return self

    def link(self, label: str, url: str) -> "EntityBuilder":
        off = self._cur()
        self._text += label
        self._entities.append(MessageEntity(
            type="text_link", offset=off,
            length=self._u16(label), url=url,
        ))
        return self

    def apply(self, entity_type: str, fn) -> "EntityBuilder":
        start = self._cur()
        fn(self)
        length = self._cur() - start
        if length > 0:
            self._entities.append(MessageEntity(
                type=entity_type, offset=start, length=length,
            ))
        return self

    def build(self) -> tuple[str, list[MessageEntity]]:
        return self._text, list(self._entities)


def _eb_route(b: EntityBuilder, cities: list[str]) -> None:
    """Маршрут жирным: 🇷🇺 Москва ➜ 🇹🇲 Ашхабад (флаг у первого и последнего)."""
    def _body(b: EntityBuilder) -> None:
        for i, city in enumerate(cities):
            if (i == 0 or i == len(cities) - 1) and city_flag(city):
                city_flag_entity(b, city).t(" ")
            b.t(city)
            if i < len(cities) - 1:
                b.t(" ➜ ")
    b.apply("bold", _body)


def _build_post_entities(
    cities: list[str], travel_date: str, cargo: str,
    phone: str, custom_username: Optional[str],
    expired: bool = False,
    ad_type: str = "carrier",
) -> tuple[str, list[MessageEntity]]:
    """Полный пост для канала (с контактами)."""
    b = EntityBuilder()

    def _carrier_body(b: EntityBuilder) -> None:
        entity_emoji(b, "plane").t(" Лечу\n\n")
        _eb_route(b, cities)
        b.t("\n\n")
        entity_emoji(b, "calendar").t(f" {travel_date}\n\n")
        entity_emoji(b, "package").t(f" Возьму {cargo_lines(cargo)}\n\n")
        entity_emoji(b, "phone").t(f" {phone}")
        if custom_username:
            b.t("\n")
            entity_emoji(b, "telegram").t(f" @{custom_username}")

    def _seeker_body(b: EntityBuilder) -> None:
        entity_emoji(b, "search").t(" Ищу попутчика\n\n")
        _eb_route(b, cities)
        b.t("\n\n")
        entity_emoji(b, "calendar_alt").t(f" {travel_date}\n\n")
        entity_emoji(b, "package").t(f" Нужно передать {cargo_lines(cargo)}\n\n")
        entity_emoji(b, "mobile").t(f" {phone}")
        if custom_username:
            b.t("\n")
            entity_emoji(b, "telegram").t(f" @{custom_username}")

    _body = _seeker_body if ad_type == "seeker" else _carrier_body

    if expired:
        b.apply("strikethrough", _body)
    else:
        _body(b)

    b.t("\n\n\n")
    b.link("Создать объявление", get_bot_url())
    b.t(" || ")
    b.link("Найти попутчика", get_find_companion_url())

    return b.build()


def _build_group_post_entities(
    cities: list[str], travel_date: str, cargo: str,
    ad_type: str = "carrier",
    expired: bool = False,
) -> tuple[str, list[MessageEntity]]:
    """
    Пост для групп — без контактов, с кнопкой "Посмотреть контакты" (добавляется отдельно).
    Формат:
        ✈️ Лечу
        🇷🇺 СПб ➜ 🇹🇲 Дашогуз
        🗓 15.07.2026
        📦 Возьму только документы
    """
    b = EntityBuilder()

    def _carrier_body(b: EntityBuilder) -> None:
        entity_emoji(b, "plane").t(" Лечу\n\n")
        _eb_route(b, cities)
        b.t("\n\n")
        entity_emoji(b, "calendar").t(f" {travel_date}\n\n")
        entity_emoji(b, "package").t(f" Возьму {cargo_lines(cargo)}")

    def _seeker_body(b: EntityBuilder) -> None:
        entity_emoji(b, "search").t(" Ищу попутчика\n\n")
        _eb_route(b, cities)
        b.t("\n\n")
        entity_emoji(b, "calendar_alt").t(f" {travel_date}\n\n")
        entity_emoji(b, "package").t(f" Нужно передать {cargo_lines(cargo)}")

    _body = _seeker_body if ad_type == "seeker" else _carrier_body

    if expired:
        b.apply("strikethrough", _body)
    else:
        _body(b)

    return b.build()


# ── FSM ───────────────────────────────────────────────────────────────────────

class Form(StatesGroup):
    # Общие
    choosing_language = State()
    ad_type_choice  = State()
    # Перевозчик (carrier)
    city_count      = State()
    picking_city    = State()
    date            = State()
    cargo           = State()
    phone           = State()
    custom_username = State()
    # Ищу попутчика (seeker)
    seeker_origin   = State()
    seeker_dest     = State()
    seeker_days     = State()
    seeker_cargo    = State()
    seeker_results  = State()
    seeker_phone    = State()
    seeker_username = State()
    # Общее
    feedback        = State()
    custom_city_input = State()


class BroadcastForm(StatesGroup):
    waiting_content = State()   # ждём текст/фото/видео с подписью
    waiting_buttons = State()   # ждём кнопки построчно "Текст - ссылка"
    confirm         = State()  # подтверждение перед рассылкой


# ── Timezone ──────────────────────────────────────────────────────────────────

def now_local() -> datetime:
    return datetime.utcnow() + timedelta(hours=TZ_OFFSET)


def local_date() -> date:
    return now_local().date()


# ── Database (PostgreSQL via asyncpg) ─────────────────────────────────────────

_pool: Optional[asyncpg.Pool] = None


async def init_db() -> None:
    """Создаёт пул соединений и таблицы (если их ещё нет)."""
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)

    async with _pool.acquire() as conn:
        # Миграция со старой схемы: если group_chats существует со старым
        # составным PRIMARY KEY (chat_id, thread_id), пересоздаём таблицу —
        # PostgreSQL не разрешает NULL в PK-колонках, а thread_id обязан
        # быть NULL для чатов без топиков.
        old_pk_exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                WHERE tc.table_name = 'group_chats'
                    AND tc.constraint_type = 'PRIMARY KEY'
                    AND kcu.column_name = 'thread_id'
            )
            """
        )
        if old_pk_exists:
            logger.warning("Migrating group_chats: dropping old composite PRIMARY KEY schema")
            await conn.execute("DROP TABLE group_chats")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS drafts (
                id              SERIAL PRIMARY KEY,
                user_id         BIGINT NOT NULL,
                tg_username     TEXT,
                custom_username TEXT,
                full_name       TEXT NOT NULL,
                origin          TEXT NOT NULL,
                destination     TEXT NOT NULL,
                route           TEXT NOT NULL DEFAULT '[]',
                travel_date     TEXT NOT NULL,
                cargo           TEXT NOT NULL,
                phone           TEXT NOT NULL DEFAULT '',
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                published       BOOLEAN NOT NULL DEFAULT FALSE,
                pending_chat_id BIGINT,
                pending_msg_id  BIGINT,
                channel_msg_id  BIGINT,
                scheduled_at    TEXT,
                expired         BOOLEAN NOT NULL DEFAULT FALSE,
                ad_type         TEXT NOT NULL DEFAULT 'carrier'
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS teasers (
                id        SERIAL PRIMARY KEY,
                draft_id  INTEGER NOT NULL,
                chat_id   TEXT NOT NULL,
                msg_id    BIGINT NOT NULL
            )
        """)
        # Пользователи — для рассылок, статистики и хранения языка интерфейса
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      BIGINT PRIMARY KEY,
                tg_username  TEXT,
                full_name    TEXT,
                lang         TEXT NOT NULL DEFAULT 'ru',
                first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
                is_blocked   BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
        # Миграция: добавляем lang если таблица уже существовала без неё
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS lang TEXT NOT NULL DEFAULT 'ru'"
        )
        # Групповые чаты с топиками и фильтром городов.
        # thread_id может быть NULL (обычный чат без топиков), поэтому его
        # нельзя включать в PRIMARY KEY — в PostgreSQL PK-колонки всегда NOT NULL.
        # Вместо этого используем свой id + частичные уникальные индексы.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS group_chats (
                id          SERIAL PRIMARY KEY,
                chat_id     BIGINT NOT NULL,
                thread_id   BIGINT,
                cities      TEXT NOT NULL DEFAULT '[]',
                title       TEXT,
                added_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # Уникальность (chat_id, thread_id) вручную, с учётом NULL:
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_group_chats_with_thread
            ON group_chats (chat_id, thread_id)
            WHERE thread_id IS NOT NULL
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_group_chats_no_thread
            ON group_chats (chat_id)
            WHERE thread_id IS NULL
        """)
        # Рассылки — лог для статистики
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id          SERIAL PRIMARY KEY,
                admin_id    BIGINT NOT NULL,
                content     TEXT NOT NULL,
                sent_count  INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)

    billing.configure(TZ_OFFSET)
    await billing.init_schema(_pool)


def pool() -> asyncpg.Pool:
    assert _pool is not None, "DB pool is not initialized — call init_db() first"
    return _pool

# ── Drafts CRUD ────────────────────────────────────────────────────────────────

async def create_draft(d: dict[str, Any]) -> int:
    """Создаёт заявку (status='pending'). Бросает billing.OpenDraftExists, если у
    пользователя уже есть объявление в активном процессе — проверка в БД, под
    мьютексом пользователя и частичным UNIQUE-индексом (см. billing.create_draft)."""
    return await billing.create_draft(pool(), d)


async def send_repeat_to_moderation(bot: Bot, draft_id: int) -> bool:
    """
    "Повтор" уже существующего объявления пользователем (см. find_own_duplicate
    и confirm:repeat) — раньше эта функция публиковала объявление в канал
    НАПРЯМУЮ, в обход модерации. Это было ошибкой: пользователь не должен
    иметь возможность самостоятельно опубликовать что-либо в канал без
    решения админа, точно так же, как и при обычном создании объявления.

    Старый пост в канале НЕ трогается вообще — не зачёркивается и не
    затирается — он истечёт сам по расписанию, когда пройдёт указанная в нём
    дата вылета (обычный планировщик, get_to_expire). Вместо переиспользования
    старой записи создаётся НОВАЯ заявка (create_draft) с теми же данными и
    отправляется на обычную модерацию (уведомление ADMIN_IDS с кнопками
    Опубликовать/Отложить/Отклонить) — старый draft остаётся как есть, живёт
    своей жизнью независимо от исхода повтора.
    Возвращает True при успехе (заявка отправлена на модерацию).
    """
    old_row = await get_draft(draft_id)
    if old_row is None:
        return False

    cities = parse_route(old_row)
    try:
        new_draft_id = await create_draft({
        "user_id":         int(old_row["user_id"]),
        "tg_username":     old_row["tg_username"],
        "custom_username": old_row["custom_username"],
        "full_name":       old_row["full_name"] or "Пользователь",
        "cities":          cities,
        "travel_date":     str(old_row["travel_date"]),
        "cargo":           str(old_row["cargo"]),
        "phone":           str(old_row["phone"] or ""),
        "ad_type":         old_row["ad_type"] or "carrier",
        })
    except billing.OpenDraftExists:
        return False
    new_row = await get_draft(new_draft_id)

    admin_text = build_admin_notification(
        draft_id=new_draft_id,
        full_name=old_row["full_name"] or "Пользователь",
        user_id=int(old_row["user_id"]),
        tg_username=old_row["tg_username"],
        cities=cities,
        travel_date=str(old_row["travel_date"]),
        cargo=str(old_row["cargo"]),
        phone=str(old_row["phone"] or ""),
        custom_username=old_row["custom_username"],
        ad_type=old_row["ad_type"] or "carrier",
        funding=new_row["funding"] if new_row else None,
    )
    for admin_id in ADMIN_IDS:
        try:
            await _send(bot, admin_id, admin_text, markup=kb_admin(new_draft_id))
        except Exception as e:
            logger.exception("Admin notify failed (repeat, draft %s) %s: %s", new_draft_id, admin_id, e)

    return True


async def get_draft(draft_id: int) -> Optional[asyncpg.Record]:
    async with pool().acquire() as conn:
        return await conn.fetchrow("SELECT * FROM drafts WHERE id=$1", draft_id)


async def mark_published(draft_id: int, channel_msg_id: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE drafts SET published=TRUE, channel_msg_id=$1, scheduled_at=NULL WHERE id=$2",
            channel_msg_id, draft_id,
        )


async def mark_expired(draft_id: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute("UPDATE drafts SET expired=TRUE WHERE id=$1", draft_id)


async def set_scheduled(draft_id: int, scheduled_at: str) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE drafts SET scheduled_at=$1 WHERE id=$2", scheduled_at, draft_id,
        )


async def save_pending_msg(draft_id: int, chat_id: int, msg_id: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE drafts SET pending_chat_id=$1, pending_msg_id=$2 WHERE id=$3",
            chat_id, msg_id, draft_id,
        )


async def save_teaser(draft_id: int, chat_id: str, msg_id: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO teasers (draft_id, chat_id, msg_id) VALUES ($1,$2,$3)",
            draft_id, chat_id, msg_id,
        )


async def get_teasers(draft_id: int) -> list[tuple[str, int]]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT chat_id, msg_id FROM teasers WHERE draft_id=$1", draft_id
        )
    return [(r["chat_id"], r["msg_id"]) for r in rows]


def _normalize_phone(phone: str) -> str:
    """Убирает пробелы/дефисы/скобки для устойчивого сравнения телефонов —
    "+7 999 123-45-67" и "+79991234567" должны считаться одним номером."""
    return re.sub(r"[\s\-()]", "", phone or "")


async def find_own_duplicate(user_id: int, ad_type: str, cities: list[str],
                             travel_date: str, phone: str,
                             custom_username: Optional[str]) -> Optional[asyncpg.Record]:
    """
    Ищет среди АКТИВНЫХ объявлений этого же пользователя одно с тем же
    маршрутом, датой и контактами — используется перед публикацией нового
    объявления, чтобы предложить "Повторить" вместо создания дубликата.

    Идентичны, если совпадают: маршрут (тот же порядок городов), дата,
    телефон (после нормализации пробелов/дефисов) и custom_username
    (без учёта регистра, с учётом что оба могут быть None/пустыми).
    """
    active = await get_active_drafts(user_id)
    norm_phone = _normalize_phone(phone)
    norm_uname = (custom_username or "").casefold().strip()

    for row in active:
        if row["ad_type"] != ad_type:
            continue
        try:
            route: list[str] = json.loads(row["route"])
        except (json.JSONDecodeError, TypeError):
            continue
        if route != cities:
            continue
        if row["travel_date"] != travel_date:
            continue
        if _normalize_phone(row["phone"] or "") != norm_phone:
            continue
        if (row["custom_username"] or "").casefold().strip() != norm_uname:
            continue
        return row
    return None


async def get_active_drafts(user_id: int) -> list[asyncpg.Record]:
    today = local_date()
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM drafts WHERE user_id=$1 AND published=TRUE AND expired=FALSE ORDER BY id DESC",
            user_id,
        )
    active = []
    for row in rows:
        try:
            if row["ad_type"] == "seeker":
                active.append(row)
            elif datetime.strptime(row["travel_date"], "%d.%m.%Y").date() >= today:
                active.append(row)
        except ValueError:
            pass
    return active


async def find_matching_carriers(origin: str, destination: str, within_days: int) -> list[asyncpg.Record]:
    """
    Ищет опубликованные carrier-объявления, подходящие под seeker-запрос
    origin -> destination в пределах within_days от сегодня.

    Условия совпадения:
      1. Только будущие даты вылета (today <= travel_date <= today+within_days).
      2. И origin, и destination встречаются в маршруте carrier (route, JSON-список
         городов), причём origin строго РАНЬШЕ destination в этом списке — то есть
         seeker'а можно "подсадить" на часть более длинного маршрута carrier'а,
         но не в обратном направлении.
    """
    today   = local_date()
    horizon = today + timedelta(days=within_days)
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM drafts WHERE ad_type='carrier' AND published=TRUE AND expired=FALSE"
        )
    matches = []
    for row in rows:
        try:
            travel_date = datetime.strptime(row["travel_date"], "%d.%m.%Y").date()
        except ValueError:
            continue
        if not (today <= travel_date <= horizon):
            continue
        try:
            route: list[str] = json.loads(row["route"])
        except (json.JSONDecodeError, TypeError):
            continue
        if origin not in route or destination not in route:
            continue
        if route.index(origin) >= route.index(destination):
            continue
        matches.append(row)
    # Ближайшие даты вылета — первыми
    matches.sort(key=lambda r: datetime.strptime(r["travel_date"], "%d.%m.%Y").date())
    return matches


async def get_scheduled_ready() -> list[asyncpg.Record]:
    now = now_local()
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM drafts WHERE published=FALSE AND scheduled_at IS NOT NULL AND expired=FALSE "
            "AND status IN ('approved','publish_error')"
        )
    ready = []
    for row in rows:
        try:
            scheduled = datetime.strptime(row["scheduled_at"], "%d.%m.%Y %H:%M")
            if now >= scheduled:
                ready.append(row)
        except (ValueError, TypeError):
            pass
    return ready


async def get_to_expire() -> list[asyncpg.Record]:
    today = local_date()
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM drafts WHERE published=TRUE AND expired=FALSE AND channel_msg_id IS NOT NULL"
        )
    result = []
    for row in rows:
        try:
            if datetime.strptime(row["travel_date"], "%d.%m.%Y").date() < today:
                result.append(row)
        except ValueError:
            pass
    return result


def parse_route(row: asyncpg.Record) -> list[str]:
    raw = row["route"] if row["route"] else ""
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return [row["origin"], row["destination"]]


# ── Users ──────────────────────────────────────────────────────────────────────

async def user_exists(user_id: int) -> bool:
    """Использовалось ли /start раньше — чтобы не переспрашивать язык повторно."""
    async with pool().acquire() as conn:
        row = await conn.fetchval("SELECT 1 FROM users WHERE user_id=$1", user_id)
    return row is not None


async def upsert_user(user_id: int, tg_username: Optional[str], full_name: str,
                      lang: Optional[str] = None) -> None:
    """
    Регистрирует/обновляет пользователя при любом взаимодействии с ботом.
    lang передаётся только при первой регистрации (после выбора языка на /start);
    при обычных заходах язык не перетирается.
    """
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, tg_username, full_name, lang, first_seen, last_seen)
            VALUES ($1, $2, $3, COALESCE($4, 'ru'), now(), now())
            ON CONFLICT (user_id) DO UPDATE
                SET tg_username = EXCLUDED.tg_username,
                    full_name   = EXCLUDED.full_name,
                    last_seen   = now(),
                    is_blocked  = FALSE
            """,
            user_id, tg_username, full_name, lang,
        )


async def get_user_lang(user_id: int) -> str:
    """Возвращает язык интерфейса пользователя (по умолчанию 'ru')."""
    async with pool().acquire() as conn:
        lang = await conn.fetchval("SELECT lang FROM users WHERE user_id=$1", user_id)
    return lang or DEFAULT_LANG


async def set_user_lang(user_id: int, lang: str) -> None:
    """
    UPSERT вместо простого UPDATE: если строки пользователя ещё нет в БД
    (например /language вызван раньше полноценной регистрации через /start,
    либо запись была потеряна), UPDATE молча ничего не меняет и язык
    "не сохраняется" хотя бот и рапортует об успехе. INSERT ... ON CONFLICT
    гарантирует, что язык сохранится в любом случае.
    """
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, lang, first_seen, last_seen)
            VALUES ($1, $2, now(), now())
            ON CONFLICT (user_id) DO UPDATE
                SET lang = EXCLUDED.lang,
                    last_seen = now()
            """,
            user_id, lang,
        )


async def mark_user_blocked(user_id: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute("UPDATE users SET is_blocked=TRUE WHERE user_id=$1", user_id)


async def get_all_active_users() -> list[int]:
    """Все пользователи, не заблокировавшие бота — для рассылок."""
    async with pool().acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users WHERE is_blocked=FALSE")
    return [r["user_id"] for r in rows]


async def get_users_count() -> tuple[int, int]:
    """(всего, за последние 7 дней активных)"""
    async with pool().acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users")
        week  = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE last_seen > now() - interval '7 days'"
        )
    return int(total), int(week)


async def get_posts_stats() -> dict:
    """Статистика по объявлениям — для /stats."""
    async with pool().acquire() as conn:
        total       = await conn.fetchval("SELECT COUNT(*) FROM drafts")
        published   = await conn.fetchval("SELECT COUNT(*) FROM drafts WHERE published=TRUE")
        carrier     = await conn.fetchval("SELECT COUNT(*) FROM drafts WHERE ad_type='carrier' AND published=TRUE")
        seeker      = await conn.fetchval("SELECT COUNT(*) FROM drafts WHERE ad_type='seeker' AND published=TRUE")
        top_routes  = await conn.fetch(
            """
            SELECT origin, destination, COUNT(*) as cnt
            FROM drafts WHERE published=TRUE
            GROUP BY origin, destination
            ORDER BY cnt DESC LIMIT 5
            """
        )
    return {
        "total": total, "published": published,
        "carrier": carrier, "seeker": seeker,
        "top_routes": [(r["origin"], r["destination"], r["cnt"]) for r in top_routes],
    }


# ── Group chats (топики + фильтр городов) ─────────────────────────────────────

async def upsert_group_chat(chat_id: int, title: Optional[str]) -> None:
    """Регистрирует чат при добавлении бота (без топика и без фильтра городов)."""
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO group_chats (chat_id, thread_id, cities, title)
            VALUES ($1, NULL, '[]', $2)
            ON CONFLICT (chat_id) WHERE thread_id IS NULL
            DO UPDATE SET title = EXCLUDED.title
            """,
            chat_id, title,
        )


async def remove_group_chat(chat_id: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute("DELETE FROM group_chats WHERE chat_id=$1", chat_id)


async def set_group_topic(chat_id: int, thread_id: Optional[int], title: Optional[str]) -> None:
    """
    /settopic — привязывает конкретный топик супергруппы.
    Удаляет старую запись без топика (thread_id IS NULL) для этого chat_id,
    чтобы не дублировать рассылку в общий чат и в топик одновременно.
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM group_chats WHERE chat_id=$1 AND thread_id IS NULL",
                chat_id,
            )
            if thread_id is None:
                await conn.execute(
                    """
                    INSERT INTO group_chats (chat_id, thread_id, cities, title)
                    VALUES ($1, NULL, '[]', $2)
                    ON CONFLICT (chat_id) WHERE thread_id IS NULL
                    DO UPDATE SET title = EXCLUDED.title
                    """,
                    chat_id, title,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO group_chats (chat_id, thread_id, cities, title)
                    VALUES ($1, $2, '[]', $3)
                    ON CONFLICT (chat_id, thread_id) WHERE thread_id IS NOT NULL
                    DO UPDATE SET title = EXCLUDED.title
                    """,
                    chat_id, thread_id, title,
                )


async def add_city_filter(chat_id: int, thread_id: Optional[int], city: str) -> list[str]:
    """Добавляет город в фильтр этого чата/топика. Возвращает актуальный список городов."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT cities FROM group_chats WHERE chat_id=$1 AND thread_id IS NOT DISTINCT FROM $2",
            chat_id, thread_id,
        )
        if row is None:
            # Чат ещё не зарегистрирован (редкий случай) — создаём запись
            cities = [city]
            await conn.execute(
                "INSERT INTO group_chats (chat_id, thread_id, cities) VALUES ($1,$2,$3)",
                chat_id, thread_id, json.dumps(cities, ensure_ascii=False),
            )
        else:
            cities = json.loads(row["cities"]) if row["cities"] else []
            if city not in cities:
                cities.append(city)
            await conn.execute(
                "UPDATE group_chats SET cities=$1 WHERE chat_id=$2 AND thread_id IS NOT DISTINCT FROM $3",
                json.dumps(cities, ensure_ascii=False), chat_id, thread_id,
            )
        return cities


async def get_all_group_chats() -> list[asyncpg.Record]:
    """Все зарегистрированные групповые чаты/топики."""
    async with pool().acquire() as conn:
        return await conn.fetch("SELECT * FROM group_chats")


# ── Broadcasts ─────────────────────────────────────────────────────────────────

async def log_broadcast(admin_id: int, content: str, sent: int, failed: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO broadcasts (admin_id, content, sent_count, failed_count) VALUES ($1,$2,$3,$4)",
            admin_id, content, sent, failed,
        )

# ── Date validation ───────────────────────────────────────────────────────────

def validate_date_i18n(lang: str, text: str) -> Optional[str]:
    """Проверяет дату поездки, возвращает переведённое сообщение об ошибке (или None)."""
    try:
        travel = datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        return t(lang, "date_invalid")

    today    = local_date()
    tomorrow = today + timedelta(days=1)

    if travel < tomorrow:
        return t(lang, "date_too_early")

    y, m = today.year, today.month
    nm   = m + 1 if m < 12 else 1
    ny   = y if m < 12 else y + 1
    end_y, end_m = (ny, nm + 1) if nm < 12 else (ny + 1, 1)
    last_ok = date(end_y, end_m, 1) - timedelta(days=1)

    if travel < date(y, m, 1) or travel > last_ok:
        return t(
            lang, "date_out_of_range",
            start=tomorrow.strftime("%d.%m.%Y"),
            end=last_ok.strftime("%d.%m.%Y"),
        )
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def esc(t: str) -> str:
    return html.escape(t, quote=False)


def get_channel_url() -> str:
    if CHANNEL_ID.startswith("@"):
        return f"https://t.me/{CHANNEL_ID[1:]}"
    return f"https://t.me/c/{str(CHANNEL_ID).lstrip('-').removeprefix('100')}"


def get_bot_url() -> str:
    return BOT_URL or f"https://t.me/{BOT_USERNAME}"


def get_find_companion_url() -> str:
    """Deep-link в бот, сразу открывающий экран поиска попутчика (см. cmd_start)."""
    base = BOT_URL or f"https://t.me/{BOT_USERNAME}"
    return f"{base}?start=find"


def get_boost_url() -> str:
    if BOOST_URL:
        return BOOST_URL
    if CHANNEL_ID.startswith("@"):
        return f"https://t.me/{CHANNEL_ID[1:]}/boost"
    # CHANNEL_ID — числовой (приватный канал): без явного BOOST_URL
    # в .env ссылку на буст построить невозможно, t.me/boost без канала —
    # мёртвая ссылка. Предупреждаем в логах и возвращаем сам канал как
    # наименее плохой вариант (лучше открыть канал, чем 404).
    logger.warning(
        "BOOST_URL is not set and CHANNEL_ID is numeric — "
        "boost link will fall back to the channel URL. "
        "Set BOOST_URL in .env to fix this."
    )
    return get_channel_url()


def get_post_url(channel_msg_id: int) -> str:
    if CHANNEL_ID.startswith("@"):
        return f"https://t.me/{CHANNEL_ID[1:]}/{channel_msg_id}"
    clean = str(CHANNEL_ID).lstrip("-").removeprefix("100")
    return f"https://t.me/c/{clean}/{channel_msg_id}"


def build_search_card(cities: list[str], travel_date: str, cargo: str) -> str:
    """
    Карточка результата поиска попутчика — показывается seeker'у в личке бота.
    Формат как у тизера в группах: полный маршрут carrier'а, БЕЗ контактов
    (контакты открываются по кнопке-ссылке на сам пост в канале). Всегда на
    русском — это чужое объявление, а не превью автора, тот же принцип, что
    и у итогового поста в канале.
    """
    route = format_route_flags(cities, lang=None)
    return (
        f"{e('plane')} <b>Лечу</b>\n\n"
        f"<b>{route}</b>\n\n"
        f"{e('calendar')} {esc(travel_date)}\n\n"
        f"{e('package')} Возьму {cargo_lines(cargo)}"
    )


def cargo_lines(code: str) -> str:
    """Канонический русский текст типа груза — для итогового поста в канале."""
    return "документы и посылки" if code == "parcels" else "только документы"


def cargo_lines_i18n(lang: str, code: str) -> str:
    """Переведённый текст типа груза — для превью автору."""
    return t(lang, "cargo_parcels" if code == "parcels" else "cargo_docs")


def format_route(cities: list[str], lang: Optional[str] = None) -> str:
    """
    lang=None (по умолчанию) — канонические русские названия городов,
    используется для итогового поста в канале/группах.
    lang="ru"/"tk" — переведённые названия, используется для превью автору.
    """
    arrow = f" {e('arrow')} "
    names = [city_name(lang, c) for c in cities] if lang else cities
    return arrow.join(esc(c) for c in names)


def format_route_flags(cities: list[str], lang: Optional[str] = None) -> str:
    """См. format_route() — тот же принцип: lang=None значит канонический русский."""
    parts = []
    for i, city in enumerate(cities):
        display = city_name(lang, city) if lang else city
        if i == 0 or i == len(cities) - 1:
            flag = city_flag(city)
            parts.append(f"{flag} {esc(display)}" if flag else esc(display))
        else:
            parts.append(esc(display))
    return " ➜ ".join(parts)


# ── Post builders (HTML, для личных сообщений в боте) ─────────────────────────

def _post_body(cities: list[str], travel_date: str, cargo: str,
               phone: str, custom_username: Optional[str],
               ad_type: str = "carrier",
               lang: Optional[str] = None) -> str:
    """
    lang=None — канонический русский, используется ТОЛЬКО для итогового поста
    в канале/группах (весь текст, включая подписи "Лечу"/"Возьму"/города,
    остаётся русским).
    lang="ru"/"tk" — превью автору: все подписи и название города переведены
    на язык lang через t(lang, ...) и city_name(lang, ...).
    """
    tg_line = f"\n{e('telegram')} @{esc(custom_username)}" if custom_username else ""
    route   = format_route_flags(cities, lang)

    # Для итогового поста в канале (lang=None) текст должен быть побайтово
    # таким же, каким был до появления переводов — жёстко русский, без t().
    if lang is None:
        if ad_type == "seeker":
            return (
                f"{e('search')} <b>Ищу попутчика</b>\n\n"
                f"<b>{route}</b>\n\n"
                f"{e('calendar_alt')} {esc(travel_date)}\n\n"
                f"{e('package')} Нужно передать {cargo_lines(cargo)}\n\n"
                f"{e('mobile')} {esc(phone)}{tg_line}"
            )
        return (
            f"{e('plane')} <b>Лечу</b>\n\n"
            f"<b>{route}</b>\n\n"
            f"{e('calendar')} {esc(travel_date)}\n\n"
            f"{e('package')} Возьму {cargo_lines(cargo)}\n\n"
            f"{e('phone')} {esc(phone)}{tg_line}"
        )

    # Превью автору — все подписи переведены на его язык.
    if ad_type == "seeker":
        return (
            f"{e('search')} <b>{t(lang, 'preview_seeker_title')}</b>\n\n"
            f"<b>{route}</b>\n\n"
            f"{e('calendar_alt')} {esc(travel_date)}\n\n"
            f"{e('package')} {t(lang, 'preview_deliver_docs')} {cargo_lines_i18n(lang, cargo)}\n\n"
            f"{e('mobile')} {esc(phone)}{tg_line}"
        )
    return (
        f"{e('plane')} <b>{t(lang, 'preview_carrier_title')}</b>\n\n"
        f"<b>{route}</b>\n\n"
        f"{e('calendar')} {esc(travel_date)}\n\n"
        f"{e('package')} {t(lang, 'preview_take_docs')} {cargo_lines_i18n(lang, cargo)}\n\n"
        f"{e('phone')} {esc(phone)}{tg_line}"
    )


def _post_footer() -> str:
    return (
        f'<a href="{get_bot_url()}">Создать объявление</a>'
        f" || "
        f'<a href="{get_find_companion_url()}">Найти попутчика</a>'
    )


def build_channel_post(cities: list[str], travel_date: str, cargo: str,
                       phone: str, custom_username: Optional[str],
                       ad_type: str = "carrier") -> str:
    """Итоговый пост в канале — ВСЕГДА канонический русский (lang не передаём)."""
    body   = _post_body(cities, travel_date, cargo, phone, custom_username, ad_type)
    footer = _post_footer()
    return f"{body}\n\n\n{footer}"


def build_preview(cities: list[str], travel_date: str, cargo: str,
                  phone: str, custom_username: Optional[str],
                  ad_type: str = "carrier",
                  lang: str = DEFAULT_LANG) -> str:
    """Превью автору перед подтверждением — переведено на язык автора."""
    return _post_body(cities, travel_date, cargo, phone, custom_username, ad_type, lang=lang)


def build_admin_notification(
    draft_id: int, full_name: str, user_id: int, tg_username: Optional[str],
    cities: list[str], travel_date: str, cargo: str,
    phone: str, custom_username: Optional[str],
    ad_type: str = "carrier",
    funding: Optional[str] = None,
) -> str:
    uname      = f"@{esc(tg_username)}" if tg_username else "—"
    fund_line  = ""
    if funding:
        fund_line = f"\n💰 Публикация: {'платная' if funding == 'paid' else 'бесплатная'}"
    sep        = "—" * 32
    type_label = f"{e('plane')} Перевозчик" if ad_type == "carrier" else f"{e('search')} Ищу попутчика"
    return (
        f"{e('inbox')} <b>Новое объявление #{draft_id}</b> · {type_label}\n\n"
        f"{e('person')} Пользователь: {esc(full_name)}\n"
        f"{e('id_card')} ID: <code>{user_id}</code>\n"
        f"📱 Username: {uname}{fund_line}\n\n"
        f"{sep}\n\n"
        + build_preview(cities, travel_date, cargo, phone, custom_username, ad_type)
        + f"\n\n{sep}"
    )


# ── Keyboards ─────────────────────────────────────────────────────────────────

def kb_language() -> InlineKeyboardMarkup:
    """Кнопки выбора языка — по одной на каждый язык из LANGUAGES."""
    b = InlineKeyboardBuilder()
    for code, name in LANGUAGES.items():
        b.button(text=name, callback_data=f"set_lang:{code}")
    b.adjust(1)
    return b.as_markup()


def kb_ad_type(lang: str) -> InlineKeyboardMarkup:
    # Экран верхнего уровня флоу создания объявления — Back ведёт на Welcome,
    # который сам по себе главный экран без Back. Кнопка нужна здесь.
    return kb_with_back(lang, [
        [InlineKeyboardButton(text=t(lang, "btn_ad_carrier"), callback_data="ad_type:carrier")],
        [InlineKeyboardButton(text=t(lang, "btn_ad_seeker"),  callback_data="ad_type:seeker")],
    ])


def kb_seeker_days(lang: str) -> InlineKeyboardMarkup:
    return kb_with_back(lang, [
        [InlineKeyboardButton(text=t(lang, "days_10"), callback_data="sdays:10")],
        [InlineKeyboardButton(text=t(lang, "days_20"), callback_data="sdays:20")],
        [InlineKeyboardButton(text=t(lang, "days_30"), callback_data="sdays:30")],
    ])


def kb_subscribe(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(lang, "btn_subscribe"), url=get_channel_url()),
        InlineKeyboardButton(text=t(lang, "btn_subscribed"), callback_data="check_sub"),
    ]])


def kb_city_count_lang(lang: str) -> InlineKeyboardMarkup:
    return kb_with_back(lang, [[
        InlineKeyboardButton(text="2", callback_data="city_count:2"),
        InlineKeyboardButton(text="3", callback_data="city_count:3"),
        InlineKeyboardButton(text="4", callback_data="city_count:4"),
    ]])


def _with_city_hint(lang: str, base_text: str) -> str:
    """Добавляет подсказку про ручной ввод города ко всем экранам выбора
    города — единая точка, чтобы не редактировать 5+ разных мест вызова
    kb_cities() по отдельности."""
    return f"{base_text}\n\n{t(lang, 'city_pick_hint')}"


def kb_cities(lang: str, prefix: str, exclude: Optional[str] = None,
             with_back: bool = True) -> InlineKeyboardMarkup:
    """
    Кнопки городов. Callback остаётся числовым индексом в CITIES (канонический
    русский список — источник правды для БД), а текст на кнопке — переведён
    на язык пользователя через city_name(). Плюс кнопка "Другой город" —
    ведёт на текстовый ввод с поиском по расширенному справочнику
    ANY_CITY_NAMES (см. Form.custom_city_input).
    """
    b = InlineKeyboardBuilder()
    for i, city in enumerate(CITIES):
        if city == exclude:
            continue
        b.button(text=city_name(lang, city), callback_data=f"{prefix}:{i}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text=t(lang, "btn_other_city"), callback_data=f"{prefix}:custom"))
    if not with_back:
        return b.as_markup()
    b.row(InlineKeyboardButton(text=t(lang, "btn_back"), callback_data="nav:back"))
    return b.as_markup()


def kb_cargo(lang: str) -> InlineKeyboardMarkup:
    return kb_with_back(lang, [
        [InlineKeyboardButton(text=t(lang, "btn_cargo_docs"),    callback_data="cargo:docs")],
        [InlineKeyboardButton(text=t(lang, "btn_cargo_parcels"), callback_data="cargo:parcels")],
    ])


def kb_skip(lang: str) -> InlineKeyboardMarkup:
    return kb_with_back(lang, [[
        InlineKeyboardButton(text=t(lang, "btn_skip"), callback_data="skip_username"),
    ]])


def kb_confirm(lang: str) -> InlineKeyboardMarkup:
    return kb_with_back(lang, [[
        InlineKeyboardButton(text=t(lang, "btn_confirm"), callback_data="confirm:yes"),
        InlineKeyboardButton(text=t(lang, "btn_restart"), callback_data="confirm:restart"),
    ]])


def kb_back_only(lang: str) -> InlineKeyboardMarkup:
    """Для экранов текстового ввода (дата, телефон), где нет других кнопок,
    кроме Back — сам ввод пользователь делает текстом в чат."""
    return kb_with_back(lang, [])


def kb_admin(draft_id: int, approved: bool = False) -> InlineKeyboardMarkup:
    """До одобрения — модерация (Одобрить/Отклонить). После — очередь публикации:
    отдельная кнопка «Опубликовать» (публикация только вручную, по одному объявлению)."""
    if not approved:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{raw('check')} Одобрить",   callback_data=f"approve:{draft_id}")],
            [InlineKeyboardButton(text=f"{raw('cross')} Отклонить",  callback_data=f"reject:{draft_id}")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📢 Опубликовать",                 callback_data=f"publish:{draft_id}")],
        [InlineKeyboardButton(text=f"{raw('clock')} Отложить",         callback_data=f"sched:{draft_id}")],
        [InlineKeyboardButton(text=f"{raw('cross')} Отклонить",        callback_data=f"reject:{draft_id}")],
    ])


def kb_schedule_dates(draft_id: int) -> InlineKeyboardMarkup:
    today = local_date()
    b = InlineKeyboardBuilder()
    for offset in range(7):
        d     = today + timedelta(days=offset)
        label = d.strftime("%d.%m")
        if offset == 0:
            label = f"Сегодня {label}"
        elif offset == 1:
            label = f"Завтра {label}"
        b.button(text=label, callback_data=f"sd:{draft_id}:{offset}")
    b.adjust(2)
    return b.as_markup()


def kb_schedule_times(draft_id: int, day_offset: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for hour in range(6, 24):
        b.button(text=f"{hour:02d}:00", callback_data=f"st:{draft_id}:{day_offset}:{hour}")
    b.adjust(4)
    return b.as_markup()


def kb_teaser(post_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{raw('phone')} Показать контакты", url=post_url),
    ]])


def kb_group_post(post_url: str) -> InlineKeyboardMarkup:
    """Кнопка «Посмотреть контакты» для постов в группах."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{raw('phone')} Посмотреть контакты", url=post_url),
    ]])


def kb_after_publish(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_leave_feedback"), callback_data="leave_feedback")],
        [InlineKeyboardButton(text=t(lang, "btn_create_ad"),      callback_data="create_ad")],
    ])


def kb_after_feedback(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(lang, "btn_create_ad"), callback_data="create_ad"),
    ]])


def kb_create(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_create_ad"), callback_data="create_ad")],
        [InlineKeyboardButton(text=t(lang, "btn_buy"),       callback_data="buy:menu")],
    ])


def kb_broadcast_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{raw('check')} Разослать всем", callback_data="bcast:send"),
        InlineKeyboardButton(text=f"{raw('cross')} Отменить",       callback_data="bcast:cancel"),
    ]])


def kb_broadcast_skip_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Без кнопок ➡️", callback_data="bcast:no_buttons"),
    ]])


def parse_broadcast_buttons(text: str) -> list[InlineKeyboardButton]:
    """
    Парсит кнопки построчно в формате "Текст кнопки - ссылка" (как в LivegramBot).
    Строки без " - " или без валидного URL игнорируются.
    """
    buttons = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or " - " not in line:
            continue
        label, url = line.rsplit(" - ", 1)
        label, url = label.strip(), url.strip()
        if not label or not url.startswith(("http://", "https://", "tg://")):
            continue
        buttons.append(InlineKeyboardButton(text=label, url=url))
    return buttons


def kb_from_buttons(buttons: list[InlineKeyboardButton]) -> Optional[InlineKeyboardMarkup]:
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons])

# ── Subscription ──────────────────────────────────────────────────────────────

async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_ID, user_id)
        return m.status not in ("left", "kicked", "banned")
    except Exception as e:
        logger.warning("Sub check error: %s", e)
        return True


# ── Message management ────────────────────────────────────────────────────────

async def _delete(bot: Bot, chat_id: int, msg_id: Optional[int]) -> None:
    if not msg_id:
        return
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


async def _edit(bot: Bot, chat_id: int, msg_id: int,
                text: str, markup=None) -> Optional[Message]:
    try:
        return await bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=text, reply_markup=markup,
            parse_mode="HTML", disable_web_page_preview=True,
        )
    except Exception:
        return None


async def _send(bot: Bot, chat_id: int, text: str, markup=None) -> Message:
    return await bot.send_message(
        chat_id=chat_id, text=text,
        reply_markup=markup,
        parse_mode="HTML", disable_web_page_preview=True,
    )


async def replace_bot_msg(bot: Bot, chat_id: int, old_id: Optional[int],
                          text: str, markup=None) -> Message:
    if old_id:
        edited = await _edit(bot, chat_id, old_id, text, markup)
        if edited:
            return edited
        await _delete(bot, chat_id, old_id)
    return await _send(bot, chat_id, text, markup)


async def after_user_msg(bot: Bot, chat_id: int, state: FSMContext,
                         text: str, markup=None) -> Message:
    data = await state.get_data()
    await _delete(bot, chat_id, data.get("bot_msg_id"))
    await _delete(bot, chat_id, data.get("aux_msg_id"))
    sent = await _send(bot, chat_id, text, markup)
    await state.update_data(bot_msg_id=sent.message_id, aux_msg_id=None)
    return sent


async def cleanup_aux(bot: Bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    await _delete(bot, chat_id, data.get("aux_msg_id"))
    await state.update_data(aux_msg_id=None)


# ── Navigation stack ───────────────────────────────────────────────────────────
#
# Экран объявления объявления (ad_type -> ... -> preview) устроен как дерево —
# каждый узел знает своего родителя. Вместо того чтобы хардкодить "откуда куда"
# в каждом хендлере, храним явный стек имён экранов в state.data["nav_stack"].
#
# Правило: при переходе ВПЕРЁД (пользователь выбрал вариант) — push_screen().
# При нажатии "Назад" — общий go_back() снимает текущий экран и рендерит тот,
# что оказался на вершине стека, вызывая ТУ ЖЕ render_* функцию, что и обычный
# прямой переход. Так UI одного экрана определяется в одном месте.
#
# picking_city — особый узел: N последовательных подшагов (город 1..N) хранится
# не как N разных записей в стеке, а как один узел "carrier:picking_city" +
# счётчик step, лежащий в cities (его длина == step). Back на этом узле просто
# укорачивает cities на 1, а не выкидывает узел из стека, пока step > 0.

async def push_screen(state: FSMContext, screen: str) -> None:
    data = await state.get_data()
    stack: list[str] = list(data.get("nav_stack", []))
    if not stack or stack[-1] != screen:
        stack.append(screen)
    await state.update_data(nav_stack=stack)


async def pop_screen(state: FSMContext) -> Optional[str]:
    """Убирает текущий (верхний) экран и возвращает имя нового текущего (уже
    предыдущего) экрана — либо None, если стек пуст/содержит только 1 узел."""
    data = await state.get_data()
    stack: list[str] = list(data.get("nav_stack", []))
    if len(stack) <= 1:
        return None
    stack.pop()
    await state.update_data(nav_stack=stack)
    return stack[-1]


def kb_with_back(lang: str, rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    """Добавляет строку [⬅️ Назад] под существующими рядами кнопок экрана."""
    return InlineKeyboardMarkup(inline_keyboard=rows + [[
        InlineKeyboardButton(text=t(lang, "btn_back"), callback_data="nav:back"),
    ]])


# ── Publish helpers (используются хендлером и планировщиком) ─────────────────

async def _do_publish(bot: Bot, draft: asyncpg.Record) -> Optional[int]:
    """Публикует пост в канал. Возвращает channel_msg_id или None при ошибке."""
    msg_id, _ = await _do_publish_checked(bot, draft)
    return msg_id


async def _do_publish_checked(bot: Bot, draft: asyncpg.Record) -> tuple[Optional[int], str]:
    """Как _do_publish, но возвращает и текст ошибки: (channel_msg_id, "") | (None, error)."""
    try:
        cities  = parse_route(draft)
        ad_type = draft["ad_type"] or "carrier"
        text, entities = _build_post_entities(
            cities=cities,
            travel_date=str(draft["travel_date"]),
            cargo=str(draft["cargo"]),
            phone=str(draft["phone"] or ""),
            custom_username=draft["custom_username"],
            expired=False,
            ad_type=ad_type,
        )
        msg = await bot.send_message(
            CHANNEL_ID, text,
            entities=entities,
            disable_web_page_preview=True,
        )
        return msg.message_id, ""
    except Exception as e:
        logger.exception("Publish to channel failed (draft %s): %s", draft["id"], e)
        return None, f"{type(e).__name__}: {e}"


async def _notify_user_published(bot: Bot, user_id: int, balance_after: Optional[int] = None) -> None:
    lang = await get_user_lang(user_id)
    try:
        text = t(lang, "ad_published_thanks")
        if balance_after is not None:
            text += "\n\n" + t(lang, "ad_published_paid_note", balance=balance_after)
        await _send(
            bot, user_id,
            text,
            markup=kb_after_publish(lang),
        )
    except Exception as e:
        logger.warning("Cannot notify user %s after publish: %s", user_id, e)


async def _publish_to_groups(bot: Bot, draft: asyncpg.Record, channel_msg_id: int) -> None:
    """
    Публикует тизер во все зарегистрированные групповые чаты.
    Фильтр городов: если у чата задан список городов — публикуем только
    если хотя бы один город маршрута входит в этот список.
    Если список городов пуст — публикуем всегда (без фильтра).
    """
    groups = await get_all_group_chats()
    if not groups:
        return

    cities   = parse_route(draft)
    ad_type  = draft["ad_type"] or "carrier"
    post_url = get_post_url(channel_msg_id)
    text, entities = _build_group_post_entities(
        cities=cities,
        travel_date=str(draft["travel_date"]),
        cargo=str(draft["cargo"]),
        ad_type=ad_type,
        expired=False,
    )
    markup = kb_group_post(post_url)

    for g in groups:
        chat_cities = json.loads(g["cities"]) if g["cities"] else []
        if chat_cities and not any(c in chat_cities for c in cities):
            continue  # фильтр задан, но маршрут не подходит

        kwargs: dict = dict(
            entities=entities,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        if g["thread_id"]:
            kwargs["message_thread_id"] = g["thread_id"]

        try:
            msg = await bot.send_message(g["chat_id"], text, **kwargs)
            await save_teaser(int(draft["id"]), str(g["chat_id"]), msg.message_id)
            logger.info(
                "Group post sent chat=%s thread=%s (msg %s)",
                g["chat_id"], g["thread_id"], msg.message_id,
            )
        except Exception as e:
            logger.exception(
                "Group post failed chat=%s thread=%s: %s",
                g["chat_id"], g["thread_id"], e,
            )


async def _expire_channel_post(bot: Bot, row: asyncpg.Record) -> None:
    """
    Зачёркивает пост в канале и удаляет тизеры в группах — общая логика,
    используемая и фоновым планировщиком (когда истёк срок объявления), и
    republish-флоу "Повторить" (когда пользователь пересоздаёт то же
    объявление — старый пост нужно завершить перед публикацией нового).
    Помечает draft как expired=TRUE.
    """
    draft_id = int(row["id"])
    try:
        cities  = parse_route(row)
        ad_type = row["ad_type"] or "carrier"
        exp_text, exp_entities = _build_post_entities(
            cities=cities,
            travel_date=str(row["travel_date"]),
            cargo=str(row["cargo"]),
            phone=str(row["phone"] or ""),
            custom_username=row["custom_username"],
            expired=True,
            ad_type=ad_type,
        )
        if row["channel_msg_id"]:
            await bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=int(row["channel_msg_id"]),
                text=exp_text,
                entities=exp_entities,
                disable_web_page_preview=True,
            )
        await mark_expired(draft_id)

        # В группах — удаляем тизер полностью (не зачёркиваем)
        for t_chat_id, t_msg_id in await get_teasers(draft_id):
            try:
                await bot.delete_message(chat_id=t_chat_id, message_id=t_msg_id)
            except Exception as te:
                logger.warning("Group teaser delete failed %s/%s: %s", t_chat_id, t_msg_id, te)
    except Exception as e:
        logger.exception("Expire edit failed (draft %s): %s", draft_id, e)


# ── Background scheduler ──────────────────────────────────────────────────────

# ── Публикация с биллингом: claim → Telegram → finalize/release ───────────────

async def clear_scheduled(draft_id: int) -> None:
    async with pool().acquire() as conn:
        await conn.execute("UPDATE drafts SET scheduled_at=NULL WHERE id=$1", draft_id)


async def _notify_admins(bot: Bot, text: str, markup=None, exclude: Optional[int] = None) -> None:
    for admin_id in ADMIN_IDS:
        if exclude is not None and admin_id == exclude:
            continue
        try:
            await _send(bot, admin_id, text, markup=markup)
        except Exception as ex:
            logger.warning("Admin notify failed %s: %s", admin_id, ex)


def _cooldown_text(claim: "billing.ClaimResult") -> str:
    return (
        "⏳ Пока нельзя опубликовать следующее объявление этого пользователя.\n\n"
        f"Последняя публикация: {billing.fmt_local(claim.last_at)}\n"
        f"Следующая публикация доступна: {billing.fmt_local(claim.next_at)}"
    )


def _claim_fail_text(claim: "billing.ClaimResult") -> str:
    if claim.code == "cooldown":
        return _cooldown_text(claim)
    return {
        "not_found":         "Заявка не найдена.",
        "not_approved":      "Объявление не одобрено — сначала нажмите «Одобрить».",
        "in_progress":       "Публикация уже выполняется.",
        "already_published": "Уже опубликовано.",
        "rejected":          "Объявление отклонено.",
        "cancelled":         "Объявление отменено.",
        "trip_passed":       "Дата поездки уже прошла — публикация невозможна.",
        "other_publishing":  "У этого пользователя сейчас уже публикуется другое объявление.",
        "no_free":           "Бесплатная публикация пользователя занята активным объявлением, а оплата не выбрана.",
        "no_balance":        "Платная публикация: оплата не подтверждена или на балансе нет публикаций.",
    }.get(claim.code, f"Публикация невозможна ({claim.code}).")


async def _execute_publication(bot: Bot, draft_id: int) -> dict[str, Any]:
    """
    Единственный путь публикации объявления в канал (ручной и отложенной).
      1. billing.claim_publication — все проверки + атомарный захват (защита от гонок);
      2. отправка в Telegram (вне транзакции);
      3. ошибка → billing.release_publication (ничего не списано, повтор возможен);
         успех → billing.finalize_publication (списание ровно 1 платной, время публикации).
    Возвращает {'status': 'published'|'claim_failed'|'telegram_error'|'finalize_failed', ...}.
    """
    claim = await billing.claim_publication(pool(), draft_id)
    if not claim.ok:
        return {"status": "claim_failed", "claim": claim}
    draft = claim.draft

    channel_msg_id, err = await _do_publish_checked(bot, draft)
    if not channel_msg_id:
        await billing.release_publication(pool(), draft_id, err)
        await _notify_admins(bot, f"❌ Ошибка публикации #{draft_id}: {esc(err)}\n"
                                  "Публикация не списана, объявление не считается опубликованным.")
        return {"status": "telegram_error", "err": err, "draft": draft}

    fin = None
    for attempt in range(3):
        try:
            fin = await billing.finalize_publication(pool(), draft_id, channel_msg_id)
            break
        except Exception as ex:
            logger.exception("finalize attempt %s failed (draft %s): %s", attempt + 1, draft_id, ex)
            await asyncio.sleep(1 + attempt)
    if fin is None:
        logger.critical("Draft %s is in channel (msg %s) but DB not updated", draft_id, channel_msg_id)
        await _notify_admins(
            bot, f"🚨 Объявление #{draft_id} УЖЕ размещено в канале (сообщение {channel_msg_id}), но не удалось "
                 "обновить базу. НЕ публикуйте повторно — проверьте состояние вручную.")
        return {"status": "finalize_failed", "err": "db", "draft": draft}
    if fin["result"] == "published_unbilled":
        await _notify_admins(bot, f"🚨 Объявление #{draft_id} опубликовано, но списать публикацию не удалось "
                                  "(баланс изменился). Проверьте баланс пользователя.")

    fresh = await get_draft(draft_id)
    try:
        await _publish_to_groups(bot, fresh, channel_msg_id)
    except Exception as ex:
        logger.exception("groups publish failed (draft %s): %s", draft_id, ex)
    if draft["pending_chat_id"] and draft["pending_msg_id"]:
        await _delete(bot, draft["pending_chat_id"], draft["pending_msg_id"])
    await _notify_user_published(
        bot, int(draft["user_id"]),
        balance_after=fin.get("balance") if fin.get("funding") == "paid" else None,
    )
    return {"status": "published", "fin": fin, "draft": draft, "channel_msg_id": channel_msg_id}


_sched_warned: set[int] = set()


async def scheduler_loop(bot: Bot) -> None:
    """
    Каждые 60 секунд:
    1. Публикует запланированные посты у которых время наступило.
    2. Зачёркивает истёкшие посты в канале.
    3. Удаляет истёкшие тизеры в группах (вместо зачёркивания — сразу удаление).
    """
    while True:
        await asyncio.sleep(60)

        # ── Публикуем запланированные (только одобренные; через тот же claim →
        #    Telegram → finalize, что и ручная публикация: все проверки п.7 ТЗ) ──
        for row in await get_scheduled_ready():
            draft_id = int(row["id"])
            try:
                logger.info("Scheduler: publishing draft %s", draft_id)
                res = await _execute_publication(bot, draft_id)
                if res["status"] == "published":
                    _sched_warned.discard(draft_id)
                    await _notify_admins(bot, f"✅ Отложенное объявление #{draft_id} опубликовано.")
                    continue
                if res["status"] == "claim_failed":
                    claim = res["claim"]
                    if claim.code == "cooldown":
                        # остаётся в расписании — опубликуется, как только пройдёт 24 ч
                        if draft_id not in _sched_warned:
                            _sched_warned.add(draft_id)
                            await _notify_admins(
                                bot, f"⏳ Отложенное объявление #{draft_id} ждёт окончания 24-часового "
                                     f"ограничения.\n\n" + _cooldown_text(claim))
                        continue
                    if claim.code in ("in_progress", "other_publishing"):
                        continue
                    await clear_scheduled(draft_id)
                    await _notify_admins(
                        bot, f"⚠️ Отложенная публикация #{draft_id} отменена: {_claim_fail_text(claim)}",
                        markup=kb_admin(draft_id, approved=True) if claim.code in ("no_balance", "no_free") else None)
                    continue
                # ошибка Telegram / БД: баланс не тронут, расписание снимаем, чтобы не спамить
                await clear_scheduled(draft_id)
                await _notify_admins(
                    bot, f"❌ Ошибка отложенной публикации #{draft_id}: {esc(res.get('err', ''))}\n"
                         "Баланс не списан. Повторите вручную.",
                    markup=kb_admin(draft_id, approved=True))
            except Exception as ex:
                logger.exception("Scheduler publish crashed (draft %s): %s", draft_id, ex)

        # ── Зачёркиваем истёкшие в канале + удаляем тизеры в группах ──────────
        for row in await get_to_expire():
            logger.info("Scheduler: expiring draft %s", int(row["id"]))
            await _expire_channel_post(bot, row)


# ── Route helpers ─────────────────────────────────────────────────────────────

def _city_step_text(lang: str, step: int, total: int, cities_so_far: list[str]) -> str:
    if step == 0:
        label = t(lang, "city_from")
    elif step == total - 1:
        label = t(lang, "city_to")
    else:
        label = t(lang, "city_step_n", step=step + 1, total=total)

    progress = ""
    if cities_so_far:
        arrow        = f" {e('arrow')} "
        route_so_far = arrow.join(esc(city_name(lang, c)) for c in cities_so_far) + f" {e('arrow')} ..."
        progress     = t(lang, "route_so_far", route=route_so_far)

    return f"{label}{progress}"


# ── Router ────────────────────────────────────────────────────────────────────

router = Router()


# ── Приём чека (регистрируется ПЕРВЫМ, чтобы фото/файл не перехватили FSM-хендлеры) ──

async def _receipt_filter(message: Message, state: FSMContext) -> bool:
    if message.from_user is None or message.chat.type != "private":
        return False
    if not (message.photo or message.document):
        return False
    cur = await state.get_state()
    if cur and cur.startswith("BroadcastForm"):
        return False
    return await billing.find_receipt_target(pool(), message.from_user.id) is not None


@router.message(_receipt_filter)
async def receive_receipt(message: Message, bot: Bot) -> None:
    """Чек привязывается к заявке на оплату, пересылается админам с кнопками
    «Подтвердить / Отклонить», заявка переходит в receipt_received."""
    user = message.from_user
    lang = await get_user_lang(user.id)
    target = await billing.find_receipt_target(pool(), user.id)
    if target is None:
        return
    if message.photo:
        ftype, fid = "photo", message.photo[-1].file_id
    else:
        ftype, fid = "document", message.document.file_id
    res = await billing.attach_receipt(pool(), int(target["id"]), user.id, ftype, fid,
                                       message.chat.id, message.message_id)
    if res is None:
        return
    p, _first = res
    await message.reply(t(lang, "pay_receipt_received", id=p["id"]))

    uname = f"@{esc(user.username)}" if user.username else "—"
    caption = (
        f"🧾 <b>Чек по заявке #{p['id']}</b> · ожидает проверки оплаты\n\n"
        f"{e('person')} {esc(user.full_name or 'Пользователь')} ({uname})\n"
        f"{e('id_card')} ID: <code>{user.id}</code>\n"
        f"Пакет: {p['qty']} публикаций\n"
        f"Сумма: {p['amount']} ₽\n"
        f"Статус: {PAYMENT_STATUS_RU[p['status']]}"
    )
    for admin_id in ADMIN_IDS:
        try:
            kw = dict(caption=caption, parse_mode="HTML", reply_markup=kb_pay_admin(int(p["id"])))
            if ftype == "photo":
                await bot.send_photo(admin_id, fid, **kw)
            else:
                await bot.send_document(admin_id, fid, **kw)
        except Exception as ex:
            logger.exception("Receipt forward to admin %s failed: %s", admin_id, ex)


# ════════════════════════════════════════════════════════════════════════════
# ГРУППОВЫЕ ЧАТЫ: авто-регистрация + /settopic + команды городов
# ════════════════════════════════════════════════════════════════════════════

@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER))
async def on_bot_added_to_group(event: ChatMemberUpdated) -> None:
    """Бот добавлен в группу/супергруппу с правом писать — авто-регистрация чата."""
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return
    await upsert_group_chat(chat.id, chat.title)
    logger.info("Bot added to group %s (%s) — auto-registered", chat.id, chat.title)


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER))
async def on_bot_removed_from_group(event: ChatMemberUpdated) -> None:
    """Бот удалён/кикнут из группы — убираем регистрацию."""
    chat = event.chat
    await remove_group_chat(chat.id)
    logger.info("Bot removed from group %s — unregistered", chat.id)


@router.message(Command("settopic"))
async def cmd_settopic(message: Message) -> None:
    """
    Привязывает текущий топик супергруппы к рассылке объявлений.
    Должна вызываться АДМИНОМ БОТА (не обязательно админом группы — простое
    ограничение доступа) прямо в нужном топике.
    """
    if message.from_user.id not in ADMIN_IDS:
        return  # тихо игнорируем, чтобы не шуметь в группе
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Эта команда работает только в группах.")
        return

    thread_id = message.message_thread_id  # None если это General-топик или обычная группа
    await set_group_topic(message.chat.id, thread_id, message.chat.title)

    label = f"топик #{thread_id}" if thread_id else "этот чат"
    await message.reply(f"{raw('check')} Объявления теперь будут приходить в {label}.")


# Регекс собирается динамически из ключей CITY_COMMANDS — так новые города
# не пересекаются с системными командами (/start, /help, /settopic и т.д.)
_city_cmd_pattern = r"^/(" + "|".join(re.escape(c) for c in CITY_COMMANDS) + r")(?:@\w+)?$"

@router.message(F.text.regexp(_city_cmd_pattern))
async def cmd_city_filter(message: Message) -> None:
    """
    /moscow, /kazan, /spb и другие команды из CITY_COMMANDS — добавляют город
    в фильтр текущего чата/топика. Можно вызвать несколько раз для нескольких городов.
    """
    if message.from_user.id not in ADMIN_IDS:
        return
    if message.chat.type not in ("group", "supergroup"):
        return

    cmd  = message.text.lstrip("/").split("@")[0].lower()
    city = CITY_COMMANDS.get(cmd)
    if not city:
        return

    thread_id = message.message_thread_id
    cities = await add_city_filter(message.chat.id, thread_id, city)
    await message.reply(
        f"{raw('check')} Фильтр обновлён. Этот чат получает объявления по городам: "
        f"{', '.join(cities)}"
    )


# ════════════════════════════════════════════════════════════════════════════
# ПОЛЬЗОВАТЕЛЬСКИЙ ФЛОУ (личные сообщения)
# ════════════════════════════════════════════════════════════════════════════

# /start
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    # deep-link параметр: /start find -> сразу экран поиска попутчика
    # (кнопка "Найти попутчика" в постах канала ведёт именно сюда).
    arg = (message.text or "").split(maxsplit=1)
    deeplink = arg[1].strip() if len(arg) > 1 else None

    is_new = not await user_exists(message.from_user.id)

    await upsert_user(
        message.from_user.id, message.from_user.username,
        message.from_user.full_name or "Пользователь",
        lang=(DEFAULT_LANG if is_new and deeplink == "find" else None),
    )

    data = await state.get_data()
    await _delete(bot, message.chat.id, data.get("bot_msg_id"))
    await _delete(bot, message.chat.id, data.get("aux_msg_id"))
    await state.clear()

    # Пользователь уже выбирал язык раньше (любой предыдущий /start) — не
    # переспрашиваем повторно, сразу показываем нужный экран на его языке.
    # Deep-link "find" для НОВОГО пользователя — тоже пропускаем выбор языка,
    # по умолчанию русский, чтобы не создавать лишний шаг между кликом по
    # кнопке в канале и экраном поиска.
    if not is_new or deeplink == "find":
        lang = await get_user_lang(message.from_user.id)
        if deeplink == "find" and await is_subscribed(bot, message.from_user.id):
            await state.update_data(nav_stack=["welcome", "ad_type"])
            await render_seeker_origin(bot, message.chat.id, state, lang, old_id=None)
        else:
            # Не подписан (или deep-link не задан) — обычный вход через Welcome.
            # Подписавшись и нажав "Создать объявление", пользователь просто
            # окажется на обычном экране выбора типа — deep-link один раз не
            # сработал молча, это лучше, чем пускать в поиск без подписки.
            await _show_welcome(bot, message.chat.id, state, lang, old_id=None)
        return

    # Первый /start без deep-link — выбор языка (единственный раз).
    await state.set_state(Form.choosing_language)
    sent = await _send(
        bot, message.chat.id,
        f"{e('globe')} {t(DEFAULT_LANG, 'choose_language')}",
        markup=kb_language(),
    )
    await state.update_data(bot_msg_id=sent.message_id)


async def _show_welcome(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                        old_id: Optional[int] = None) -> None:
    """Приветствие + кнопка подписки — общий шаг после выбора/смены языка.
    Это главный экран флоу (дно nav_stack), поэтому кнопки Back у него нет."""
    await state.update_data(nav_stack=["welcome"])
    sent = await replace_bot_msg(
        bot, chat_id, old_id,
        t(lang, "start_welcome"),
        markup=kb_subscribe(lang),
    )
    await state.update_data(bot_msg_id=sent.message_id)


@router.callback_query(Form.choosing_language, F.data.startswith("set_lang:"))
async def choose_language_first_time(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Выбор языка при первом /start."""
    await callback.answer()
    lang = callback.data.split(":")[1]
    await set_user_lang(callback.from_user.id, lang)
    data = await state.get_data()
    await _show_welcome(bot, callback.message.chat.id, state, lang,
                        old_id=data.get("bot_msg_id") or callback.message.message_id)


# /language — сменить язык в любой момент.
# Если пользователь был в процессе заполнения формы объявления — этот процесс
# прерывается (state сбрасывается), т.к. продолжать заполнение на новом языке
# с сохранением прогресса на старом было бы запутанно. Пользователь увидит
# кнопку "Создать объявление" после смены языка и сможет начать заново.
@router.message(Command("language"))
async def cmd_language(message: Message, state: FSMContext, bot: Bot) -> None:
    current_lang = await get_user_lang(message.from_user.id)
    data = await state.get_data()
    await _delete(bot, message.chat.id, data.get("bot_msg_id"))
    await _delete(bot, message.chat.id, data.get("aux_msg_id"))
    await state.clear()
    sent = await _send(
        bot, message.chat.id,
        f"{e('globe')} {t(current_lang, 'choose_language')}",
        markup=kb_language(),
    )
    await state.update_data(bot_msg_id=sent.message_id)


@router.callback_query(F.data.startswith("set_lang:"))
async def change_language(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """
    Смена языка через /language. Срабатывает для ЛЮБОГО состояния (кроме
    choosing_language при первом /start, который обрабатывается отдельным
    хендлером выше — aiogram проверяет хендлеры по порядку регистрации,
    более специфичный State-хендлер имеет приоритет).
    """
    await callback.answer()
    lang = callback.data.split(":")[1]
    await set_user_lang(callback.from_user.id, lang)
    await callback.message.edit_text(
        t(lang, "language_changed"),
        reply_markup=kb_create(lang),
    )


# /help
@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext, bot: Bot) -> None:
    lang = await get_user_lang(message.from_user.id)
    data = await state.get_data()
    await _delete(bot, message.chat.id, data.get("bot_msg_id"))
    await _delete(bot, message.chat.id, data.get("aux_msg_id"))
    sent = await _send(
        bot, message.chat.id,
        t(lang, "help_text", username=esc(HELP_USERNAME)),
        markup=kb_create(lang),
    )
    await state.update_data(bot_msg_id=sent.message_id, aux_msg_id=None)


# /posts
@router.message(Command("posts"))
async def cmd_posts(message: Message, state: FSMContext, bot: Bot) -> None:
    lang = await get_user_lang(message.from_user.id)
    data = await state.get_data()
    await _delete(bot, message.chat.id, data.get("bot_msg_id"))
    await _delete(bot, message.chat.id, data.get("aux_msg_id"))

    active = await get_active_drafts(message.from_user.id)
    if not active:
        sent = await _send(bot, message.chat.id, t(lang, "no_active_ads"), markup=kb_create(lang))
        await state.update_data(bot_msg_id=sent.message_id, aux_msg_id=None)
        return

    # Список объявлений (сами маршруты/даты не переводятся — это данные объявления)
    lines = [t(lang, "your_active_ads", count=len(active))]
    for i, row in enumerate(active, 1):
        cities  = parse_route(row)
        tg_line = f"\n   {e('telegram')} @{esc(row['custom_username'])}" if row["custom_username"] else ""
        lines.append(
            f"<b>{i}.</b> {e('plane')} {format_route(cities, lang)}\n"
            f"   {e('calendar')} {esc(row['travel_date'])} · {cargo_lines(row['cargo'])}\n"
            f"   {e('phone')} {esc(row['phone'])}{tg_line}\n"
        )
    sent = await _send(bot, message.chat.id, "\n".join(lines), markup=kb_create(lang))
    await state.update_data(bot_msg_id=sent.message_id, aux_msg_id=None)

async def render_ad_type(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                         old_id: Optional[int]) -> None:
    """Экран выбора типа объявления (carrier/seeker). Родитель: welcome."""
    await state.set_state(Form.ad_type_choice)
    await push_screen(state, "ad_type")
    sent = await replace_bot_msg(
        bot, chat_id, old_id,
        t(lang, "choose_ad_type"),
        markup=kb_ad_type(lang),
    )
    await state.update_data(bot_msg_id=sent.message_id)


# «Я подписан»
@router.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    if not await is_subscribed(bot, callback.from_user.id):
        await callback.answer(t(lang, "not_subscribed_yet"), show_alert=True)
        return

    data = await state.get_data()
    await render_ad_type(
        bot, callback.message.chat.id, state, lang,
        old_id=data.get("bot_msg_id") or callback.message.message_id,
    )


# «Создать объявление»
@router.callback_query(F.data == "create_ad")
async def create_ad(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    if not await is_subscribed(bot, callback.from_user.id):
        await callback.answer(t(lang, "subscribe_first"), show_alert=True)
        return

    if await _block_if_open_draft(bot, callback.message.chat.id, callback.from_user.id, lang,
                                  callback.message.message_id):
        return

    data = await state.get_data()
    await _delete(bot, callback.message.chat.id, data.get("aux_msg_id"))
    await state.clear()
    await state.update_data(nav_stack=["welcome"])
    await render_ad_type(
        bot, callback.message.chat.id, state, lang,
        old_id=callback.message.message_id,
    )


# Выбор типа объявления
@router.callback_query(Form.ad_type_choice, F.data.startswith("ad_type:"))
async def pick_ad_type(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    ad_type = callback.data.split(":")[1]
    await state.update_data(ad_type=ad_type)
    data = await state.get_data()
    old_id = data.get("bot_msg_id") or callback.message.message_id

    if ad_type == "carrier":
        await render_city_count(bot, callback.message.chat.id, state, lang, old_id)
    else:
        await render_seeker_origin(bot, callback.message.chat.id, state, lang, old_id)


async def render_city_count(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                            old_id: Optional[int]) -> None:
    """Экран выбора числа городов маршрута (carrier). Родитель: ad_type."""
    await state.set_state(Form.city_count)
    await push_screen(state, "carrier:city_count")
    sent = await replace_bot_msg(
        bot, chat_id, old_id,
        t(lang, "how_many_cities"),
        markup=kb_city_count_lang(lang),
    )
    await state.update_data(bot_msg_id=sent.message_id)


async def render_seeker_origin(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                               old_id: Optional[int]) -> None:
    """Экран выбора города-откуда (seeker). Родитель: ad_type."""
    await state.set_state(Form.seeker_origin)
    await push_screen(state, "seeker:origin")
    sent = await replace_bot_msg(
        bot, chat_id, old_id,
        _with_city_hint(lang, t(lang, "seeker_city_from")),
        markup=kb_cities(lang, "sorigin"),
    )
    await state.update_data(bot_msg_id=sent.message_id)


# ── Seeker flow ───────────────────────────────────────────────────────────────

@router.callback_query(Form.seeker_origin, F.data.startswith("sorigin:"))
async def seeker_pick_origin(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    arg = callback.data.split(":")[1]

    if arg == "custom":
        await start_custom_city_input(bot, callback.message.chat.id, state, lang,
                                      return_prefix="sorigin",
                                      old_id=callback.message.message_id)
        return

    city = CITIES[int(arg)]
    await _seeker_origin_chosen(bot, callback.message.chat.id, state, lang, city,
                                old_id=callback.message.message_id)


async def _seeker_origin_chosen(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                                city: str, old_id: Optional[int]) -> None:
    """Общая логика после выбора города-откуда — вызывается и с кнопки, и
    после ручного ввода города через start_custom_city_input."""
    await state.update_data(seeker_origin=city)
    await state.set_state(Form.seeker_dest)
    await push_screen(state, "seeker:dest")
    data = await state.get_data()
    sent = await replace_bot_msg(
        bot, chat_id,
        data.get("bot_msg_id") or old_id,
        _with_city_hint(lang, t(lang, "seeker_from_confirmed", city=esc(city_name(lang, city)))),
        markup=kb_cities(lang, "sdest", exclude=city),
    )
    await state.update_data(bot_msg_id=sent.message_id)


@router.callback_query(Form.seeker_dest, F.data.startswith("sdest:"))
async def seeker_pick_dest(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    lang = await get_user_lang(callback.from_user.id)
    arg = callback.data.split(":")[1]

    if arg == "custom":
        await callback.answer()
        await start_custom_city_input(bot, callback.message.chat.id, state, lang,
                                      return_prefix="sdest",
                                      old_id=callback.message.message_id)
        return

    city = CITIES[int(arg)]
    data = await state.get_data()
    if data.get("seeker_origin") == city:
        await callback.answer(t(lang, "seeker_choose_other_city"), show_alert=True)
        return
    await callback.answer()
    await _seeker_dest_chosen(bot, callback.message.chat.id, state, lang, city,
                              old_id=callback.message.message_id)


async def _seeker_dest_chosen(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                              city: str, old_id: Optional[int]) -> None:
    """Общая логика после выбора города-куда — вызывается и с кнопки, и
    после ручного ввода города."""
    data = await state.get_data()
    await state.update_data(seeker_dest=city)
    await render_seeker_days(bot, chat_id, state, lang,
                             data.get("bot_msg_id") or old_id)


async def render_seeker_days(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                             old_id: Optional[int]) -> None:
    """Экран выбора срока (seeker). Родитель: seeker:dest."""
    await state.set_state(Form.seeker_days)
    await push_screen(state, "seeker:days")
    sent = await replace_bot_msg(
        bot, chat_id, old_id,
        t(lang, "seeker_when"),
        markup=kb_seeker_days(lang),
    )
    await state.update_data(bot_msg_id=sent.message_id)


@router.callback_query(Form.seeker_days, F.data.startswith("sdays:"))
async def seeker_pick_days(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    days = int(callback.data.split(":")[1])
    # days_label — канонический русский, попадёт в поле объявления (travel_date)
    # и в итоговый пост канала, поэтому здесь ВСЕГДА русский текст.
    # days_num сохраняем отдельно, чтобы превью автору могло показать
    # переведённую метку через t(lang, f"days_{days_num}").
    days_label = f"В ближайшие {days} дней"
    await state.update_data(seeker_days=days_label, seeker_days_num=days)
    data = await state.get_data()
    await render_seeker_cargo(bot, callback.message.chat.id, state, lang,
                              data.get("bot_msg_id") or callback.message.message_id)


async def render_seeker_cargo(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                              old_id: Optional[int]) -> None:
    """Экран выбора типа груза (seeker). Родитель: seeker:days."""
    await state.set_state(Form.seeker_cargo)
    await push_screen(state, "seeker:cargo")
    sent = await replace_bot_msg(
        bot, chat_id, old_id,
        t(lang, "seeker_what_to_pass"),
        markup=kb_cargo(lang),
    )
    await state.update_data(bot_msg_id=sent.message_id)


@router.callback_query(Form.seeker_cargo, F.data.startswith("cargo:"))
async def seeker_pick_cargo(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    code = callback.data.split(":")[1]
    if code not in ("docs", "parcels"):
        await callback.answer(); return
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    await state.update_data(seeker_cargo=code)
    data = await state.get_data()
    await render_search_results(bot, callback.message.chat.id, state, lang,
                                data.get("bot_msg_id") or callback.message.message_id)


async def render_search_results(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                                 old_id: Optional[int]) -> None:
    """
    Ищет подходящие carrier-объявления под seeker-запрос и показывает их
    карточками. Родитель: seeker:cargo.

    Если совпадений нет — предлагает разместить объявление (единственная
    кнопка ведёт в обычный флоу phone/username/preview).
    Если совпадения есть — показывает карточки одним списком сообщений,
    а под последней — текст «если варианты не подошли» + кнопки
    «Разместить объявление» и «Назад».

    Карточки — это отдельные сообщения (не одно editMessage), поэтому при
    повторном заходе на этот экран (например, через Back и снова вперёд)
    старые карточки нужно явно удалить, иначе они задублируются в чате.
    """
    await state.set_state(Form.seeker_results)
    await push_screen(state, "seeker:results")

    data     = await state.get_data()
    origin      = data.get("seeker_origin", "")
    destination = data.get("seeker_dest", "")
    days_num    = data.get("seeker_days_num", 30)

    # Чистим карточки от предыдущего захода на этот экран, если они были.
    for old_card_id in data.get("search_card_ids", []):
        await _delete(bot, chat_id, old_card_id)
    await state.update_data(search_card_ids=[])

    # Экран "идёт поиск" — заменяем текущее сообщение, карточки пойдут следом
    # новыми сообщениями (список объявлений не сворачивается в одно editMessage).
    sent = await replace_bot_msg(bot, chat_id, old_id, t(lang, "search_looking"))
    await state.update_data(bot_msg_id=sent.message_id)

    matches = await find_matching_carriers(origin, destination, days_num)

    if not matches:
        await replace_bot_msg(
            bot, chat_id, sent.message_id,
            t(lang, "search_none_found"),
            markup=kb_with_back(lang, [[
                InlineKeyboardButton(text=t(lang, "btn_place_ad"), callback_data="seeker:place_ad"),
            ]]),
        )
        return

    await replace_bot_msg(bot, chat_id, sent.message_id, t(lang, "search_found_header"))

    card_ids: list[int] = []
    for row in matches:
        try:
            route: list[str] = json.loads(row["route"])
        except (json.JSONDecodeError, TypeError):
            route = [row["origin"], row["destination"]]
        if not row["channel_msg_id"]:
            continue  # объявление ещё не опубликовано в канале — ссылки нет, пропускаем
        card_text = build_search_card(route, row["travel_date"], row["cargo"])
        post_url  = get_post_url(int(row["channel_msg_id"]))
        card_msg  = await _send(bot, chat_id, card_text, markup=kb_group_post(post_url))
        card_ids.append(card_msg.message_id)

    footer = await _send(
        bot, chat_id, t(lang, "search_not_matched"),
        markup=kb_with_back(lang, [[
            InlineKeyboardButton(text=t(lang, "btn_place_ad"), callback_data="seeker:place_ad"),
        ]]),
    )
    card_ids.append(footer.message_id)
    await state.update_data(bot_msg_id=footer.message_id, search_card_ids=card_ids)


@router.callback_query(Form.seeker_results, F.data == "seeker:place_ad")
async def seeker_place_ad_anyway(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    data = await state.get_data()
    await render_seeker_phone(bot, callback.message.chat.id, state, lang,
                              data.get("bot_msg_id") or callback.message.message_id)


async def render_seeker_phone(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                              old_id: Optional[int]) -> None:
    """Экран ввода телефона (seeker, текстовый ввод). Родитель: seeker:results."""
    await state.set_state(Form.seeker_phone)
    await push_screen(state, "seeker:phone")
    sent = await replace_bot_msg(
        bot, chat_id, old_id,
        t(lang, "ask_phone"),
        markup=kb_back_only(lang),
    )
    await state.update_data(bot_msg_id=sent.message_id)


@router.message(Form.seeker_phone)
async def seeker_get_phone(message: Message, state: FSMContext, bot: Bot) -> None:
    lang = await get_user_lang(message.from_user.id)
    text = (message.text or "").strip()
    if not PHONE_RE.match(text):
        await after_user_msg(bot, message.chat.id, state, t(lang, "phone_bad_format"),
                             markup=kb_back_only(lang))
        return
    await state.update_data(seeker_phone=text)
    await state.set_state(Form.seeker_username)
    await push_screen(state, "seeker:username")
    await after_user_msg(
        bot, message.chat.id, state,
        t(lang, "ask_username"),
        markup=kb_skip(lang),
    )


@router.callback_query(Form.seeker_username, F.data == "skip_username")
async def seeker_skip_username(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await state.update_data(seeker_username=None)
    await push_screen(state, "seeker:preview")
    await _show_seeker_preview(callback.message.chat.id, state, bot,
                               old_id=callback.message.message_id)


@router.message(Form.seeker_username)
async def seeker_get_username(message: Message, state: FSMContext, bot: Bot) -> None:
    lang = await get_user_lang(message.from_user.id)
    raw = (message.text or "").strip().lstrip("@")
    if not raw or not re.match(r"^[a-zA-Z0-9_]{3,32}$", raw):
        await after_user_msg(
            bot, message.chat.id, state,
            t(lang, "username_invalid"),
            markup=kb_skip(lang),
        )
        return
    await state.update_data(seeker_username=raw)
    await push_screen(state, "seeker:preview")
    await _show_seeker_preview(message.chat.id, state, bot, prev_user_msg=True)


async def _show_seeker_preview(
    chat_id: int, state: FSMContext, bot: Bot,
    old_id: Optional[int] = None, prev_user_msg: bool = False,
) -> None:
    data = await state.get_data()
    lang = await get_user_lang(chat_id)
    cities   = [data["seeker_origin"], data["seeker_dest"]]
    days_lbl = data.get("seeker_days", "")
    days_num = data.get("seeker_days_num")
    cargo    = data.get("seeker_cargo", "docs")
    phone    = data.get("seeker_phone", "")
    cuname   = data.get("seeker_username")

    # Превью автору: показываем переведённую метку срока ("В ближайшие N
    # дней" -> "Indiki N günde"), а не канонический русский days_lbl.
    # Сам days_lbl (русский) сохраняется в БД как travel_date объявления
    # и используется таким же в итоговом посте канала — это не трогаем.
    preview_days = t(lang, f"days_{days_num}") if days_num else days_lbl

    preview = build_preview(cities, preview_days, cargo, phone, cuname, ad_type="seeker", lang=lang)
    text    = t(lang, "preview_check_ad") + preview

    if prev_user_msg:
        sent = await after_user_msg(bot, chat_id, state, text, markup=kb_confirm(lang))
    else:
        oid  = old_id or (await state.get_data()).get("bot_msg_id")
        sent = await replace_bot_msg(bot, chat_id, oid, text, markup=kb_confirm(lang))
    await state.update_data(bot_msg_id=sent.message_id)


# ── Carrier flow ──────────────────────────────────────────────────────────────

@router.callback_query(Form.city_count, F.data.startswith("city_count:"))
async def pick_city_count(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    count = int(callback.data.split(":")[1])
    await state.update_data(city_count=count, cities=[])
    data = await state.get_data()
    await render_picking_city(bot, callback.message.chat.id, state, lang,
                              data.get("bot_msg_id") or callback.message.message_id)


async def render_picking_city(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                              old_id: Optional[int]) -> None:
    """Экран выбора города на текущем шаге маршрута (carrier). N подряд идущих
    подшагов (шаг = len(cities)) живут под ОДНИМ узлом стека "carrier:picking_city" —
    Back на этом экране укорачивает cities на 1, а не уходит из узла, пока
    ещё остаются выбранные города."""
    await state.set_state(Form.picking_city)
    await push_screen(state, "carrier:picking_city")
    data  = await state.get_data()
    cities: list[str] = data.get("cities", [])
    count: int        = data.get("city_count", 2)
    step  = len(cities)
    last  = cities[-1] if cities else None
    sent = await replace_bot_msg(
        bot, chat_id, old_id,
        _with_city_hint(lang, _city_step_text(lang, step, count, cities)),
        markup=kb_cities(lang, "city", exclude=last),
    )
    await state.update_data(bot_msg_id=sent.message_id)


# ── Ручной ввод города ("Другой город") ─────────────────────────────────────
#
# Один общий механизм для всех трёх мест выбора города (seeker origin/dest,
# carrier picking_city). return_prefix, сохранённый в state, говорит куда
# продолжить флоу после того, как город определён — так же, как это делает
# сам числовой callback ({prefix}:{index}), но в обход кнопок.

async def start_custom_city_input(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                                  return_prefix: str, old_id: Optional[int]) -> None:
    await state.update_data(custom_city_return_prefix=return_prefix)
    await state.set_state(Form.custom_city_input)
    sent = await replace_bot_msg(
        bot, chat_id, old_id,
        t(lang, "ask_custom_city"),
        markup=kb_back_only(lang),
    )
    await state.update_data(bot_msg_id=sent.message_id)


async def _continue_after_city_chosen(bot: Bot, chat_id: int, state: FSMContext,
                                      lang: str, city: str, old_id: Optional[int]) -> None:
    """Продолжает нужный флоу после того, как город определён вручную —
    диспетчер по return_prefix, сохранённому в start_custom_city_input."""
    data = await state.get_data()
    return_prefix = data.get("custom_city_return_prefix")

    if return_prefix == "sorigin":
        await _seeker_origin_chosen(bot, chat_id, state, lang, city, old_id)
    elif return_prefix == "sdest":
        if data.get("seeker_origin") == city:
            await _send(bot, chat_id, t(lang, "seeker_choose_other_city"))
            await render_seeker_dest_screen(bot, chat_id, state, lang, old_id)
            return
        await _seeker_dest_chosen(bot, chat_id, state, lang, city, old_id)
    elif return_prefix == "city":
        cities: list[str] = data.get("cities", [])
        if cities and cities[-1] == city:
            await _send(bot, chat_id, t(lang, "city_already_used"))
            await render_picking_city(bot, chat_id, state, lang, old_id)
            return
        await _picking_city_chosen(bot, chat_id, state, lang, city, old_id)


async def render_seeker_dest_screen(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                                    old_id: Optional[int]) -> None:
    """Переотрисовка экрана seeker:dest (например, после невалидного выбора
    того же города, что и origin, при ручном вводе)."""
    data = await state.get_data()
    origin = data.get("seeker_origin")
    await state.set_state(Form.seeker_dest)
    await push_screen(state, "seeker:dest")
    sent = await replace_bot_msg(
        bot, chat_id, old_id,
        _with_city_hint(lang, t(lang, "seeker_from_confirmed", city=esc(city_name(lang, origin))) if origin
                       else t(lang, "seeker_city_to")),
        markup=kb_cities(lang, "sdest", exclude=origin),
    )
    await state.update_data(bot_msg_id=sent.message_id)


@router.message(Form.custom_city_input)
async def receive_custom_city(message: Message, state: FSMContext, bot: Bot) -> None:
    lang = await get_user_lang(message.from_user.id)
    query = (message.text or "").strip()

    if not query:
        await after_user_msg(bot, message.chat.id, state, t(lang, "ask_custom_city"),
                             markup=kb_back_only(lang))
        return

    exact, suggestions = find_city_match(query)

    if exact:
        data = await state.get_data()
        await cleanup_aux(bot, message.chat.id, state)
        await _continue_after_city_chosen(bot, message.chat.id, state, lang, exact,
                                          old_id=data.get("bot_msg_id"))
        return

    if not suggestions:
        await after_user_msg(
            bot, message.chat.id, state,
            t(lang, "city_not_found"),
            markup=kb_back_only(lang),
        )
        return

    b = InlineKeyboardBuilder()
    for city in suggestions:
        b.button(text=city_name(lang, city), callback_data=f"citysuggest:{city}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text=t(lang, "btn_back"), callback_data="nav:back"))
    await after_user_msg(
        bot, message.chat.id, state,
        t(lang, "city_suggestions"),
        markup=b.as_markup(),
    )


@router.callback_query(Form.custom_city_input, F.data.startswith("citysuggest:"))
async def pick_city_suggestion(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    city = callback.data.split(":", 1)[1]
    data = await state.get_data()
    await _continue_after_city_chosen(
        bot, callback.message.chat.id, state, lang, city,
        old_id=data.get("bot_msg_id") or callback.message.message_id,
    )


@router.callback_query(Form.picking_city, F.data.startswith("city:"))
async def pick_city(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    lang = await get_user_lang(callback.from_user.id)
    arg  = callback.data.split(":")[1]

    if arg == "custom":
        await callback.answer()
        await start_custom_city_input(bot, callback.message.chat.id, state, lang,
                                      return_prefix="city",
                                      old_id=callback.message.message_id)
        return

    city = CITIES[int(arg)]
    data = await state.get_data()
    cities: list[str] = data.get("cities", [])

    if cities and cities[-1] == city:
        await callback.answer(t(lang, "city_already_used"), show_alert=True)
        return

    await callback.answer()
    await _picking_city_chosen(bot, callback.message.chat.id, state, lang, city,
                               old_id=callback.message.message_id)


async def _picking_city_chosen(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                               city: str, old_id: Optional[int]) -> None:
    """Общая логика после выбора очередного города маршрута (carrier) —
    вызывается и с кнопки, и после ручного ввода города."""
    data = await state.get_data()
    cities: list[str] = data.get("cities", [])
    count: int        = data.get("city_count", 2)

    cities = cities + [city]
    await state.update_data(cities=cities)

    step = len(cities)

    if step < count:
        await render_picking_city(
            bot, chat_id, state, lang,
            data.get("bot_msg_id") or old_id,
        )
    else:
        await render_date(bot, chat_id, state, lang, cities,
                          data.get("bot_msg_id") or old_id)


async def render_date(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                      cities: list[str], old_id: Optional[int]) -> None:
    """Экран ввода даты (carrier, текстовый ввод). Родитель: carrier:picking_city."""
    await state.set_state(Form.date)
    await push_screen(state, "carrier:date")
    tomorrow  = (local_date() + timedelta(days=1)).strftime("%d.%m.%Y")
    route_str = f" {e('arrow')} ".join(esc(city_name(lang, c)) for c in cities)
    sent = await replace_bot_msg(
        bot, chat_id, old_id,
        t(lang, "route_confirmed", route=route_str, example=tomorrow),
        markup=kb_back_only(lang),
    )
    await state.update_data(bot_msg_id=sent.message_id)


@router.message(Form.date)
async def get_date(message: Message, state: FSMContext, bot: Bot) -> None:
    lang = await get_user_lang(message.from_user.id)
    text = (message.text or "").strip()

    if not DATE_RE.match(text):
        tomorrow = (local_date() + timedelta(days=1)).strftime("%d.%m.%Y")
        err = t(lang, "date_bad_format", example=tomorrow)
    else:
        err = validate_date_i18n(lang, text)

    if err:
        await after_user_msg(bot, message.chat.id, state, err, markup=kb_back_only(lang))
        return

    await state.update_data(travel_date=text)
    await render_carrier_cargo(bot, message.chat.id, state, lang, from_text_input=True)


async def render_carrier_cargo(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                               old_id: Optional[int] = None,
                               from_text_input: bool = False) -> None:
    """Экран выбора типа груза (carrier). Родитель: carrier:date."""
    await state.set_state(Form.cargo)
    await push_screen(state, "carrier:cargo")
    if from_text_input:
        await after_user_msg(bot, chat_id, state, t(lang, "what_to_take"), markup=kb_cargo(lang))
    else:
        sent = await replace_bot_msg(bot, chat_id, old_id, t(lang, "what_to_take"), markup=kb_cargo(lang))
        await state.update_data(bot_msg_id=sent.message_id)


@router.callback_query(Form.cargo, F.data.startswith("cargo:"))
async def pick_cargo(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    code = callback.data.split(":")[1]
    if code not in ("docs", "parcels"):
        await callback.answer()
        return
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    await state.update_data(cargo=code)
    data = await state.get_data()
    await render_carrier_phone(bot, callback.message.chat.id, state, lang,
                               data.get("bot_msg_id") or callback.message.message_id)


async def render_carrier_phone(bot: Bot, chat_id: int, state: FSMContext, lang: str,
                               old_id: Optional[int]) -> None:
    """Экран ввода телефона (carrier, текстовый ввод). Родитель: carrier:cargo."""
    await state.set_state(Form.phone)
    await push_screen(state, "carrier:phone")
    sent = await replace_bot_msg(
        bot, chat_id, old_id,
        t(lang, "ask_phone"),
        markup=kb_back_only(lang),
    )
    await state.update_data(bot_msg_id=sent.message_id)


@router.message(Form.phone)
async def get_phone(message: Message, state: FSMContext, bot: Bot) -> None:
    lang = await get_user_lang(message.from_user.id)
    text = (message.text or "").strip()
    if not PHONE_RE.match(text):
        await after_user_msg(bot, message.chat.id, state, t(lang, "phone_bad_format"),
                             markup=kb_back_only(lang))
        return

    await state.update_data(phone=text)
    await state.set_state(Form.custom_username)
    await push_screen(state, "carrier:username")
    await after_user_msg(bot, message.chat.id, state,
                         t(lang, "ask_username"), markup=kb_skip(lang))


@router.callback_query(Form.custom_username, F.data == "skip_username")
async def skip_username(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    await state.update_data(custom_username=None)
    await cleanup_aux(bot, callback.message.chat.id, state)
    await push_screen(state, "carrier:preview")
    data = await state.get_data()
    sent = await replace_bot_msg(
        bot, callback.message.chat.id,
        data.get("bot_msg_id") or callback.message.message_id,
        _preview_text(lang, data), markup=kb_confirm(lang),
    )
    await state.update_data(bot_msg_id=sent.message_id)


@router.message(Form.custom_username)
async def get_username(message: Message, state: FSMContext, bot: Bot) -> None:
    lang = await get_user_lang(message.from_user.id)
    raw = (message.text or "").strip().lstrip("@")
    if not raw or not re.match(r"^[a-zA-Z0-9_]{3,32}$", raw):
        await after_user_msg(
            bot, message.chat.id, state,
            t(lang, "username_invalid"),
            markup=kb_skip(lang),
        )
        return
    await state.update_data(custom_username=raw)
    await push_screen(state, "carrier:preview")
    data = await state.get_data()
    await after_user_msg(bot, message.chat.id, state, _preview_text(lang, data), markup=kb_confirm(lang))


def _preview_text(lang: str, data: dict) -> str:
    # Превью автору переводится (город на языке автора). Итоговый пост
    # в канале при публикации всегда останется русским — build_channel_post()
    # использует канонические названия городов независимо от lang автора.
    return (
        t(lang, "preview_check_ad")
        + build_preview(
            cities=data.get("cities", []),
            travel_date=data["travel_date"],
            cargo=data["cargo"],
            phone=data.get("phone", ""),
            custom_username=data.get("custom_username"),
            ad_type=data.get("ad_type", "carrier"),
            lang=lang,
        )
    )


# Начать заново — сбрасывает весь прогресс (не Back!), возвращает к выбору
# типа объявления. Отдельная функция от go_back: это самостоятельное
# действие "с нуля", а не "на шаг назад".
@router.callback_query(F.data == "confirm:restart")
async def restart(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    data = await state.get_data()
    await _delete(bot, callback.message.chat.id, data.get("aux_msg_id"))
    await state.clear()
    await state.update_data(nav_stack=["welcome"])
    await render_ad_type(bot, callback.message.chat.id, state, lang,
                         old_id=callback.message.message_id)


# Подтвердить
@router.callback_query(F.data == "confirm:yes")
async def confirm_yes(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data    = await state.get_data()
    ad_type = data.get("ad_type", "carrier")

    if ad_type == "seeker":
        cities   = [data.get("seeker_origin", ""), data.get("seeker_dest", "")]
        travel_d = data.get("seeker_days", "")
        cargo    = data.get("seeker_cargo", "docs")
        phone    = data.get("seeker_phone", "")
        cuname   = data.get("seeker_username")
    else:
        cities   = data.get("cities", [])
        travel_d = data.get("travel_date", "")
        cargo    = data.get("cargo", "docs")
        phone    = data.get("phone", "")
        cuname   = data.get("custom_username")

    lang = await get_user_lang(callback.from_user.id)
    if not cities or not travel_d or not phone:
        await callback.answer(t(lang, "data_lost_restart"), show_alert=True)
        await state.clear()
        return
    await callback.answer()

    user      = callback.from_user
    full_name = user.full_name or "Пользователь"

    # Защита от повторной отправки: проверка по состоянию заявок в БД (не по UI).
    if await _block_if_open_draft(bot, callback.message.chat.id, user.id, lang,
                                  data.get("bot_msg_id") or callback.message.message_id):
        return

    duplicate = await find_own_duplicate(user.id, ad_type, cities, travel_d, phone, cuname)
    if duplicate is not None:
        await state.update_data(duplicate_draft_id=int(duplicate["id"]))
        await push_screen(state, "duplicate_warning")
        await replace_bot_msg(
            bot, callback.message.chat.id,
            data.get("bot_msg_id") or callback.message.message_id,
            t(lang, "duplicate_found"),
            markup=kb_with_back(lang, [[
                InlineKeyboardButton(text=t(lang, "btn_repeat_ad"), callback_data="confirm:repeat"),
            ]]),
        )
        return

    try:
        draft_id = await create_draft({
            "user_id":         user.id,
            "tg_username":     user.username,
            "custom_username": cuname,
            "full_name":       full_name,
            "cities":          cities,
            "travel_date":     travel_d,
            "cargo":           cargo,
            "phone":           phone,
            "ad_type":         ad_type,
        })
    except billing.OpenDraftExists:
        # гонка / повторное нажатие: заявка уже создана параллельным запросом
        await _show_open_draft_block(bot, callback.message.chat.id, lang,
                                     data.get("bot_msg_id") or callback.message.message_id)
        return
    new_row = await get_draft(draft_id)

    admin_text = build_admin_notification(
        draft_id=draft_id,
        full_name=full_name, user_id=user.id, tg_username=user.username,
        cities=cities, travel_date=travel_d,
        cargo=cargo, phone=phone,
        custom_username=cuname, ad_type=ad_type,
        funding=new_row["funding"],
    )

    for admin_id in ADMIN_IDS:
        try:
            await _send(bot, admin_id, admin_text, markup=kb_admin(draft_id))
        except Exception as e:
            logger.exception("Admin notify failed %s: %s", admin_id, e)

    await state.clear()
    sent = await replace_bot_msg(
        bot, callback.message.chat.id,
        callback.message.message_id,
        t(lang, "ad_sent_for_review"),
    )
    await save_pending_msg(draft_id, callback.message.chat.id, sent.message_id)

    # Платная публикация без средств на балансе → сообщаем, что заявка ждёт оплаты
    if new_row["funding"] == "paid":
        bal, reserved = await billing.get_balance(pool(), user.id)
        if bal - reserved < 1:
            try:
                await _send(bot, callback.message.chat.id, t(lang, "ad_waiting_payment_user"),
                            markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text=t(lang, "btn_buy"), callback_data="buy:menu")]]))
            except Exception as ex:
                logger.warning("waiting-payment notice failed: %s", ex)


@router.callback_query(F.data == "confirm:repeat")
async def confirm_repeat(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Пользователь подтвердил повтор уже существующего активного объявления
    вместо создания дубликата (см. find_own_duplicate в confirm_yes).
    Повтор идёт на обычную модерацию — не публикуется в канал напрямую."""
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    data = await state.get_data()
    draft_id = data.get("duplicate_draft_id")

    old_id = data.get("bot_msg_id") or callback.message.message_id

    if await _block_if_open_draft(bot, callback.message.chat.id, callback.from_user.id, lang, old_id):
        return

    if not draft_id:
        await replace_bot_msg(bot, callback.message.chat.id, old_id, t(lang, "data_lost_restart"))
        await state.clear()
        return

    ok = await send_repeat_to_moderation(bot, int(draft_id))
    await state.clear()

    if ok:
        await replace_bot_msg(bot, callback.message.chat.id, old_id, t(lang, "ad_sent_for_review"))
    else:
        await replace_bot_msg(bot, callback.message.chat.id, old_id, t(lang, "ad_repeat_failed"))


# ── Back navigation ────────────────────────────────────────────────────────────
#
# Один хендлер на всё дерево создания объявления. Смотрит верхний элемент
# nav_stack ПОСЛЕ снятия текущего экрана и рендерит его — вызывая ровно ту же
# render_* функцию, что и обычный переход вперёд, поэтому текст/клавиатура
# экрана определены только в одном месте.

@router.callback_query(F.data == "nav:back")
async def go_back(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    lang    = await get_user_lang(callback.from_user.id)
    chat_id = callback.message.chat.id
    data    = await state.get_data()
    old_id  = data.get("bot_msg_id") or callback.message.message_id

    # carrier:picking_city — особый случай: пока внутри узла остались выбранные
    # города, Back откатывает на один город назад, а не покидает узел.
    stack: list[str] = list(data.get("nav_stack", []))
    if stack and stack[-1] == "carrier:picking_city" and data.get("cities"):
        cities = data.get("cities", [])[:-1]
        await state.update_data(cities=cities)
        await render_picking_city(bot, chat_id, state, lang, old_id)
        return

    prev = await pop_screen(state)
    if prev is None:
        # Стек пуст/не инициализирован (например, старый state с деплоя до
        # этой правки) — просто откатываем на главный экран.
        await _show_welcome(bot, chat_id, state, lang, old_id=old_id)
        return

    data = await state.get_data()  # nav_stack уже укорочен внутри pop_screen

    # Каждая render_* сама пушит своё имя узла обратно на вершину стека
    # (push_screen не дублирует, если он уже там) — здесь просто вызываем
    # ту же функцию, что и обычный переход вперёд.
    if prev == "welcome":
        await _show_welcome(bot, chat_id, state, lang, old_id=old_id)
    elif prev == "ad_type":
        await render_ad_type(bot, chat_id, state, lang, old_id)
    elif prev == "carrier:city_count":
        await render_city_count(bot, chat_id, state, lang, old_id)
    elif prev == "carrier:picking_city":
        await render_picking_city(bot, chat_id, state, lang, old_id)
    elif prev == "carrier:date":
        await render_date(bot, chat_id, state, lang, data.get("cities", []), old_id)
    elif prev == "carrier:cargo":
        await render_carrier_cargo(bot, chat_id, state, lang, old_id)
    elif prev == "carrier:phone":
        await render_carrier_phone(bot, chat_id, state, lang, old_id)
    elif prev == "carrier:username":
        await state.set_state(Form.custom_username)
        await push_screen(state, "carrier:username")
        sent = await replace_bot_msg(bot, chat_id, old_id, t(lang, "ask_username"), markup=kb_skip(lang))
        await state.update_data(bot_msg_id=sent.message_id)
    elif prev == "carrier:preview":
        await state.set_state(Form.custom_username)  # confirm — не отдельный Form.state
        await push_screen(state, "carrier:preview")
        sent = await replace_bot_msg(bot, chat_id, old_id, _preview_text(lang, data), markup=kb_confirm(lang))
        await state.update_data(bot_msg_id=sent.message_id)
    elif prev == "seeker:origin":
        await render_seeker_origin(bot, chat_id, state, lang, old_id)
    elif prev == "seeker:dest":
        await render_seeker_dest_screen(bot, chat_id, state, lang, old_id)
    elif prev == "seeker:days":
        await render_seeker_days(bot, chat_id, state, lang, old_id)
    elif prev == "seeker:cargo":
        await render_seeker_cargo(bot, chat_id, state, lang, old_id)
    elif prev == "seeker:results":
        await render_search_results(bot, chat_id, state, lang, old_id)
    elif prev == "seeker:phone":
        await render_seeker_phone(bot, chat_id, state, lang, old_id)
    elif prev == "seeker:username":
        await state.set_state(Form.seeker_username)
        await push_screen(state, "seeker:username")
        sent = await replace_bot_msg(bot, chat_id, old_id, t(lang, "ask_username"), markup=kb_skip(lang))
        await state.update_data(bot_msg_id=sent.message_id)
    elif prev == "seeker:preview":
        await state.set_state(Form.seeker_username)  # confirm — не отдельный Form.state
        await push_screen(state, "seeker:preview")
        await _show_seeker_preview(chat_id, state, bot, old_id=old_id)
    else:
        # Неизвестный/устаревший узел — безопасный откат на главный экран.
        await _show_welcome(bot, chat_id, state, lang, old_id=old_id)


# ════════════════════════════════════════════════════════════════════════════
# АДМИН: отложить / отклонить / опубликовать
# ════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("sched:"))
async def sched_show_dates(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    draft_id = int(callback.data.split(":")[1])
    draft    = await get_draft(draft_id)
    if not draft:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if draft["status"] not in billing.RETRIABLE_STATUSES:
        await callback.answer("Отложить можно только одобренное объявление", show_alert=True)
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=kb_schedule_dates(draft_id))
    except Exception:
        pass


@router.callback_query(F.data.startswith("sd:"))
async def sched_pick_date(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    _, draft_id_s, offset_s = callback.data.split(":")
    draft_id, day_offset = int(draft_id_s), int(offset_s)
    chosen_date = (local_date() + timedelta(days=day_offset)).strftime("%d.%m.%Y")
    try:
        await callback.message.edit_reply_markup(
            reply_markup=kb_schedule_times(draft_id, day_offset)
        )
        await callback.answer(f"Дата: {chosen_date}. Выберите время:")
    except Exception:
        pass


@router.callback_query(F.data.startswith("st:"))
async def sched_pick_time(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return

    parts      = callback.data.split(":")
    draft_id   = int(parts[1])
    day_offset = int(parts[2])
    hour       = int(parts[3])

    draft = await get_draft(draft_id)
    if not draft:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if draft["published"]:
        await callback.answer("Уже опубликовано", show_alert=True)
        return
    if draft["status"] not in billing.RETRIABLE_STATUSES:
        await callback.answer("Отложить можно только одобренное объявление", show_alert=True)
        return

    chosen_date  = local_date() + timedelta(days=day_offset)
    scheduled_at = f"{chosen_date.strftime('%d.%m.%Y')} {hour:02d}:00"
    await set_scheduled(draft_id, scheduled_at)

    await callback.answer(f"Запланировано на {scheduled_at} ✅")
    try:
        current_text = callback.message.text or callback.message.caption or ""
        new_text = current_text + f"\n\n🕐 Запланировано: {scheduled_at}"
        await callback.message.edit_text(
            new_text, parse_mode="HTML",
            disable_web_page_preview=True, reply_markup=None,
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("approve:"))
async def approve_draft_cb(callback: CallbackQuery, bot: Bot) -> None:
    """Модерация: pending → approved. Объявление НЕ публикуется — только попадает
    в очередь, откуда админ публикует его отдельной кнопкой «📢 Опубликовать»."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    draft_id = int(callback.data.split(":")[1])
    row = await billing.approve_draft(pool(), draft_id)
    if row is None:
        d = await get_draft(draft_id)
        await callback.answer(f"Нельзя одобрить (статус: {d['status'] if d else 'не найдено'})", show_alert=True)
        return
    await callback.answer("Одобрено")
    try:
        await callback.message.edit_reply_markup(reply_markup=kb_admin(draft_id, approved=True))
    except Exception:
        pass
    status_line = await _readiness_line(row)
    await callback.message.reply(f"{raw('check')} Объявление #{draft_id} одобрено.\n{status_line}\n"
                                 "Автоматически не публикуется — нажмите «📢 Опубликовать».")
    uid = int(row["user_id"])
    lang = await get_user_lang(uid)
    try:
        await _send(bot, uid, t(lang, "ad_approved_user"))
        bal, reserved = await billing.get_balance(pool(), uid)
        if row["funding"] == "paid" and bal - reserved < 1:
            await _send(bot, uid, t(lang, "ad_waiting_payment_user"),
                        markup=InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text=t(lang, "btn_buy"), callback_data="buy:menu")]]))
    except Exception as ex:
        logger.warning("Cannot notify user %s about approval: %s", uid, ex)


@router.callback_query(F.data.startswith("reject:"))
async def reject_draft(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return

    draft_id = int(callback.data.split(":")[1])
    draft    = await get_draft(draft_id)
    if not draft:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    # Атомарный переход в rejected. Баланс не трогаем: платные публикации резервируются
    # только на время самой отправки в Telegram, поэтому у отклонённого объявления
    # ничего не списано и ничего возвращать не нужно — купленное остаётся на балансе.
    row = await billing.reject_draft(pool(), draft_id)
    if row is None:
        await callback.answer(f"Нельзя отклонить (статус: {draft['status']})", show_alert=True)
        return
    await callback.answer()

    if draft["pending_chat_id"] and draft["pending_msg_id"]:
        await _delete(bot, draft["pending_chat_id"], draft["pending_msg_id"])

    try:
        user_lang = await get_user_lang(int(draft["user_id"]))
        text = t(user_lang, "ad_rejected")
        if draft["funding"] == "paid":
            text += "\n\n" + t(user_lang, "ad_rejected_paid_note")
        await _send(bot, int(draft["user_id"]), text, markup=kb_create(user_lang))
    except Exception as e:
        logger.warning("Cannot notify user: %s", e)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.reply(f"{raw('cross')} Отклонено.")


@router.callback_query(F.data.startswith("publish:"))
async def publish_draft(callback: CallbackQuery, bot: Bot) -> None:
    """Ручная публикация одного объявления. Все проверки — внутри
    billing.claim_publication в момент нажатия; повторное/параллельное нажатие
    получит 'in_progress'/'already_published' и ничего не продублирует и не спишет."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return

    draft_id = int(callback.data.split(":")[1])
    res = await _execute_publication(bot, draft_id)

    if res["status"] == "claim_failed":
        claim = res["claim"]
        if claim.code == "cooldown":
            await callback.answer()
            await callback.message.reply(_cooldown_text(claim))
        elif claim.code == "trip_passed":
            await callback.answer("Дата поездки прошла", show_alert=True)
            await _cancel_trip_passed(bot, claim.draft)
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        else:
            await callback.answer(_claim_fail_text(claim)[:190], show_alert=True)
            if claim.code in ("already_published", "rejected", "cancelled"):
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
        return

    if res["status"] != "published":
        await callback.answer("Ошибка публикации — баланс не списан", show_alert=True)
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer(f"{raw('check')} Опубликовано!")
    fin = res["fin"]
    extra = ""
    if fin.get("funding") == "paid" and fin.get("balance") is not None:
        extra = f" Списана 1 платная публикация, остаток: {fin['balance']}."
    elif fin.get("funding") == "free":
        extra = " Бесплатная публикация (платный баланс не тронут)."
    await callback.message.reply(f"{raw('check')} Объявление #{draft_id} опубликовано.{extra}")


# ════════════════════════════════════════════════════════════════════════════
# ПУБЛИКАЦИИ, ОПЛАТА ПЕРЕВОДОМ, СТАТУС (пользователь)
# ════════════════════════════════════════════════════════════════════════════

PAYMENT_STATUS_RU = {
    "awaiting_receipt": "ожидает чек",
    "receipt_received": "ожидает проверки оплаты",
    "confirmed":        "оплата подтверждена",
    "rejected":         "чек отклонён",
}
DRAFT_STATUS_RU = {
    "pending": "на модерации", "approved": "одобрено (в очереди)", "publishing": "публикуется",
    "publish_error": "ошибка публикации", "published": "опубликовано",
    "rejected": "отклонено", "cancelled": "отменено",
}


def _plural_key(n: int) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return "1"
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return "2"
    return "5"


def _pub_word(lang: str, n: int) -> str:
    return t(lang, f"unit_pub_{_plural_key(n)}")


def _days_text(lang: str, n: int) -> str:
    return t(lang, "today_word") if n <= 0 else f"{n} {t(lang, 'unit_day_' + _plural_key(n))}"


def kb_pay_admin(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"padm:ok:{payment_id}")],
        [InlineKeyboardButton(text="❌ Отклонить чек",      callback_data=f"padm:no:{payment_id}")],
    ])


def kb_buy_menu(lang: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=t(lang, "btn_pkg", qty=q, word=_pub_word(lang, q), price=pr), callback_data=f"buy:pkg:{q}")]
        for q, pr in billing.PACKAGES.items()]
    return kb_with_back_close(lang, rows)


def kb_with_back_close(lang: str, rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows + [[
        InlineKeyboardButton(text=t(lang, "btn_back"), callback_data="buy:close")]])


def _admin_user_line(user_id: int, full_name: Optional[str], tg_username: Optional[str]) -> str:
    uname = f"@{esc(tg_username)}" if tg_username else "—"
    return f"{e('person')} {esc(full_name or 'Пользователь')} ({uname})\n{e('id_card')} ID: <code>{user_id}</code>"


async def _user_row(user_id: int) -> Optional[asyncpg.Record]:
    async with pool().acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)


async def _readiness_line(row: asyncpg.Record) -> str:
    """Короткий вывод для админа: можно ли публиковать объявление прямо сейчас."""
    uid = int(row["user_id"])
    ov = await billing.get_overview(pool(), uid)
    if billing.trip_passed(row):
        return "⚠️ Дата поездки прошла — публикация невозможна."
    if row["funding"] == "paid" and ov["available"] < 1:
        return "💳 Ожидает оплаты: платных публикаций на балансе нет."
    if row["funding"] == "free" and not ov["free_available"]:
        return f"⚠️ Бесплатное место занято до {billing.fmt_local(ov['free_until'])}."
    if ov["next_at"]:
        return f"⏳ Готово, но 24-часовое ограничение до {billing.fmt_local(ov['next_at'])}."
    return "✅ Готово к публикации."


async def _cancel_trip_passed(bot: Bot, draft: asyncpg.Record) -> None:
    """Дата поездки прошла: заявка не может быть опубликована → отменяем (баланс не
    затрагивается) и предлагаем пользователю создать объявление с актуальной датой."""
    row = await billing._transition(pool(), int(draft["id"]), "cancelled", billing.EDITABLE_STATUSES)
    if row is None:
        return
    uid = int(draft["user_id"])
    lang = await get_user_lang(uid)
    try:
        await _send(bot, uid, t(lang, "trip_passed_no_pay"), markup=kb_create(lang))
    except Exception as ex:
        logger.warning("trip-passed notify failed %s: %s", uid, ex)
    await _notify_admins(bot, f"⚠️ Объявление #{draft['id']} отменено: дата поездки прошла.")


async def _open_draft_or_stale(bot: Bot, user_id: int) -> tuple[Optional[asyncpg.Record], bool]:
    """(открытая заявка | None, было_ли_только_что_отменено_из-за_прошедшей_даты)."""
    row = await billing.get_open_draft(pool(), user_id)
    if row is not None and row["status"] != "publishing" and billing.trip_passed(row):
        await _cancel_trip_passed(bot, row)
        return None, True
    return row, False


def kb_block(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_status"),       callback_data="mystatus")],
        [InlineKeyboardButton(text=t(lang, "btn_cancel_short"), callback_data="dismiss")],
    ])


async def _show_open_draft_block(bot: Bot, chat_id: int, lang: str, old_id: Optional[int]) -> None:
    await replace_bot_msg(bot, chat_id, old_id, t(lang, "open_draft_block"), markup=kb_block(lang))


async def _block_if_open_draft(bot: Bot, chat_id: int, user_id: int, lang: str,
                               old_id: Optional[int]) -> bool:
    row, _ = await _open_draft_or_stale(bot, user_id)
    if row is None:
        return False
    await _show_open_draft_block(bot, chat_id, lang, old_id)
    return True


@router.callback_query(F.data == "dismiss")
async def dismiss_cb(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    await state.clear()
    await _show_welcome(bot, callback.message.chat.id, state, lang, old_id=callback.message.message_id)


# ── Статус заявки пользователя ────────────────────────────────────────────────

async def _status_screen(bot: Bot, user_id: int, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    row, stale = await _open_draft_or_stale(bot, user_id)
    ov = await billing.get_overview(pool(), user_id)
    if row is None:
        text = t(lang, "trip_passed_no_pay") if stale else t(lang, "status_no_open")
        text += "\n\n" + t(lang, "status_balance", n=ov["available"])
        return text, kb_create(lang)

    lines = [t(lang, "status_head", id=row["id"]), t(lang, f"status_{row['status']}")]
    lines.append(t(lang, "status_funding_paid" if row["funding"] == "paid" else "status_funding_free"))
    lines.append(t(lang, "status_balance", n=ov["available"]))
    waiting = row["funding"] == "paid" and ov["available"] < 1
    if waiting:
        lines.append("\n" + t(lang, "status_waiting_payment"))
    if ov["next_at"]:
        lines.append(t(lang, "status_next_at", dt=billing.fmt_local(ov["next_at"])))

    rows: list[list[InlineKeyboardButton]] = []
    editable = row["status"] in billing.EDITABLE_STATUSES
    if waiting:
        rows.append([InlineKeyboardButton(text=t(lang, "btn_buy"), callback_data="buy:menu")])
    if editable and row["funding"] == "paid" and ov["free_available"]:
        rows.append([InlineKeyboardButton(text=t(lang, "btn_use_free"), callback_data=f"myfund:{row['id']}:free")])
    if editable and row["funding"] == "free" and ov["available"] >= 1:
        rows.append([InlineKeyboardButton(text=t(lang, "btn_use_paid"), callback_data=f"myfund:{row['id']}:paid")])
    if editable:
        rows.append([InlineKeyboardButton(text=t(lang, "btn_cancel_ad"), callback_data=f"mycancel:{row['id']}")])
    rows.append([InlineKeyboardButton(text=t(lang, "btn_close"), callback_data="dismiss")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "mystatus")
async def my_status_cb(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    text, kb = await _status_screen(bot, callback.from_user.id, lang)
    await replace_bot_msg(bot, callback.message.chat.id, callback.message.message_id, text, kb)


@router.message(Command("status"))
async def cmd_status(message: Message, bot: Bot) -> None:
    lang = await get_user_lang(message.from_user.id)
    text, kb = await _status_screen(bot, message.from_user.id, lang)
    await _send(bot, message.chat.id, text, kb)


@router.callback_query(F.data.startswith("mycancel:"))
async def my_cancel_cb(callback: CallbackQuery, bot: Bot) -> None:
    lang = await get_user_lang(callback.from_user.id)
    draft_id = int(callback.data.split(":")[1])
    row = await billing.cancel_draft(pool(), draft_id, callback.from_user.id)
    if row is None:
        await callback.answer(t(lang, "cancel_failed"), show_alert=True)
        return
    await callback.answer()
    await replace_bot_msg(bot, callback.message.chat.id, callback.message.message_id,
                          t(lang, "cancel_done"), kb_create(lang))
    await _notify_admins(bot, f"🗑 Пользователь {row['user_id']} отменил объявление #{draft_id}.")


@router.callback_query(F.data.startswith("myfund:"))
async def my_funding_cb(callback: CallbackQuery, bot: Bot) -> None:
    """Явный выбор: бесплатная или платная публикация. Ничего не списывается."""
    lang = await get_user_lang(callback.from_user.id)
    _, draft_id_s, funding = callback.data.split(":")
    if funding == "paid":
        bal, reserved = await billing.get_balance(pool(), callback.from_user.id)
        if bal - reserved < 1:
            await callback.answer(t(lang, "funding_no_balance"), show_alert=True)
            return
    ok, code = await billing.set_funding(pool(), int(draft_id_s), callback.from_user.id, funding)
    if not ok:
        await callback.answer(t(lang, "funding_free_taken" if code == "free_taken" else "cancel_failed"),
                              show_alert=True)
        return
    await callback.answer(t(lang, "funding_changed"))
    text, kb = await _status_screen(bot, callback.from_user.id, lang)
    await replace_bot_msg(bot, callback.message.chat.id, callback.message.message_id, text, kb)


# ── Баланс ────────────────────────────────────────────────────────────────────

async def _own_balance_text(user_id: int, lang: str) -> str:
    ov = await billing.get_overview(pool(), user_id)
    free = (t(lang, "balance_free_yes") if ov["free_available"]
            else t(lang, "balance_free_no", dt=billing.fmt_local(billing.local_to_utc(ov["free_until"]))))
    return t(lang, "balance_text", balance=ov["available"], free=free)


# ── Покупка пакета ────────────────────────────────────────────────────────────

@router.message(Command("buy"))
async def cmd_buy(message: Message, bot: Bot) -> None:
    lang = await get_user_lang(message.from_user.id)
    await _send(bot, message.chat.id, t(lang, "buy_menu"), kb_buy_menu(lang))


@router.callback_query(F.data == "buy:menu")
async def buy_menu_cb(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    text = await _own_balance_text(callback.from_user.id, lang) + "\n\n" + t(lang, "buy_menu")
    await replace_bot_msg(bot, callback.message.chat.id, callback.message.message_id, text, kb_buy_menu(lang))


@router.callback_query(F.data == "buy:close")
async def buy_close_cb(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    await state.clear()
    await _show_welcome(bot, callback.message.chat.id, state, lang, old_id=callback.message.message_id)


async def _package_warning(bot: Bot, user_id: int, lang: str, qty: int) -> tuple[Optional[str], bool]:
    """
    Предупреждение ДО оплаты (единый расчёт billing.calc_usable_publications).
    Возвращает (текст_предупреждения | None, дата_поездки_прошла).
    Покупку это никогда не блокирует — купленные публикации бессрочные.
    """
    row, stale = await _open_draft_or_stale(bot, user_id)
    if stale:
        return None, True
    if row is None:
        return None, False
    ov = await billing.get_overview(pool(), user_id)
    deadline = billing.draft_deadline(row)
    calc = billing.calc_usable_publications(
        now=None, last_pub_at=ov["last_pub_at"], deadline_local=deadline,
        free_available=ov["free_available"], balance_available=ov["available"], package_qty=qty,
    )
    if calc.fits:
        return None, False
    if (row["ad_type"] or "carrier") == "seeker":
        days_left = max(0, (deadline - billing.to_local(billing.utcnow())).days)
    else:
        days_left = (billing.parse_trip_date(row["travel_date"]) - billing.to_local(billing.utcnow()).date()).days
    return t(lang, "buy_warning", days_text=_days_text(lang, days_left), qty=qty,
             word=_pub_word(lang, qty)), False


@router.callback_query(F.data.startswith("buy:pkg:"))
async def buy_pkg_cb(callback: CallbackQuery, bot: Bot) -> None:
    qty = int(callback.data.split(":")[2])
    lang = await get_user_lang(callback.from_user.id)
    if qty not in billing.PACKAGES:
        await callback.answer()
        return
    await callback.answer()
    warning, passed = await _package_warning(bot, callback.from_user.id, lang, qty)
    chat_id, mid = callback.message.chat.id, callback.message.message_id
    if passed:
        await replace_bot_msg(bot, chat_id, mid, t(lang, "trip_passed_no_pay"), kb_create(lang))
        return
    if warning is None:
        await _start_payment(bot, callback.from_user, lang, qty, chat_id, mid)
        return
    price = billing.PACKAGES[qty]
    await replace_bot_msg(bot, chat_id, mid, warning, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_buy_pkg", qty=qty, word=_pub_word(lang, qty), price=price),
                              callback_data=f"buy:go:{qty}")],
        [InlineKeyboardButton(text=t(lang, "btn_other_pkg"),    callback_data="buy:menu")],
        [InlineKeyboardButton(text=t(lang, "btn_cancel_short"), callback_data="buy:close")],
    ]))


@router.callback_query(F.data.startswith("buy:go:"))
async def buy_go_cb(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    qty = int(callback.data.split(":")[2])
    lang = await get_user_lang(callback.from_user.id)
    if qty not in billing.PACKAGES:
        return
    _, passed = await _package_warning(bot, callback.from_user.id, lang, qty)
    if passed:
        await replace_bot_msg(bot, callback.message.chat.id, callback.message.message_id,
                              t(lang, "trip_passed_no_pay"), kb_create(lang))
        return
    await _start_payment(bot, callback.from_user, lang, qty, callback.message.chat.id,
                         callback.message.message_id)


def _pay_screen_text(lang: str, p: asyncpg.Record) -> str:
    holder = f"\n{esc(CARD_HOLDER)}" if CARD_HOLDER else ""
    return t(lang, "pay_screen", id=p["id"], qty=p["qty"], word=_pub_word(lang, int(p["qty"])),
             price=p["amount"], card=esc(CARD_NUMBER), holder=holder)


async def _start_payment(bot: Bot, user, lang: str, qty: int, chat_id: int, msg_id: int) -> None:
    if not CARD_NUMBER:
        await replace_bot_msg(bot, chat_id, msg_id, t(lang, "pay_no_card"), kb_create(lang))
        return
    open_row = await billing.get_open_draft(pool(), user.id)
    p, created = await billing.create_payment(pool(), user.id, qty, int(open_row["id"]) if open_row else None)
    await replace_bot_msg(bot, chat_id, msg_id, _pay_screen_text(lang, p), kb_with_back_close(lang, []))
    if created:
        await _notify_admins(
            bot,
            f"💳 <b>Новая заявка на оплату #{p['id']}</b>\n\n"
            f"{_admin_user_line(user.id, user.full_name, user.username)}\n"
            f"Пакет: {p['qty']} публикаций\nСумма: {p['amount']} ₽\n"
            f"Статус: {PAYMENT_STATUS_RU[p['status']]}",
        )


@router.callback_query(F.data.startswith("payretry:"))
async def pay_retry_cb(callback: CallbackQuery, bot: Bot) -> None:
    """После отклонения чека — снова принимать чек по той же заявке."""
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    p = await billing.reopen_payment(pool(), int(callback.data.split(":")[1]), callback.from_user.id)
    if p is None:
        return
    await replace_bot_msg(bot, callback.message.chat.id, callback.message.message_id,
                          _pay_screen_text(lang, p) + "\n\n" + t(lang, "pay_resend_prompt", id=p["id"]),
                          kb_with_back_close(lang, []))


# ── Админ: проверка оплаты ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("padm:"))
async def payment_admin_cb(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, action, pid_s = callback.data.split(":")
    pid = int(pid_s)
    admin_id = callback.from_user.id

    async def _strip() -> None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    if action == "ok":
        res = await billing.confirm_payment(pool(), pid, admin_id)
        p = res["payment"]
        if res["result"] == "already":
            await callback.answer("Уже подтверждено — повторного начисления нет", show_alert=True)
            await _strip()
            return
        if res["result"] != "credited":
            await callback.answer(f"Нельзя подтвердить (статус: {PAYMENT_STATUS_RU.get(p['status'], '?') if p else 'нет заявки'})",
                                  show_alert=True)
            return
        await callback.answer("Оплата подтверждена")
        await _strip()
        await callback.message.reply(
            f"✅ Оплата #{pid} подтверждена: пользователю {p['user_id']} начислено {p['qty']} публ. "
            f"Баланс: {res['balance']}.\nОбъявления автоматически НЕ публикуются.")
        uid = int(p["user_id"])
        lang = await get_user_lang(uid)
        try:
            await _send(bot, uid, t(lang, "pay_confirmed_user", id=pid, qty=p["qty"],
                                    word=_pub_word(lang, int(p["qty"])), balance=res["balance"]),
                        markup=kb_create(lang))
        except Exception as ex:
            logger.warning("Cannot notify user %s about payment: %s", uid, ex)
        # объявление продолжает жить в своём процессе; если оно уже одобрено — сообщаем админам
        row = await billing.get_open_draft(pool(), uid)
        if row is not None and row["status"] in billing.RETRIABLE_STATUSES:
            try:
                await _send(bot, uid, t(lang, "ad_approved_user"))
            except Exception:
                pass
            await _notify_admins(
                bot, f"📢 Объявление #{row['id']} готово к ручной публикации.\n{await _readiness_line(row)}",
                markup=kb_admin(int(row["id"]), approved=True))
    else:
        status, p = await billing.reject_payment(pool(), pid, admin_id)
        if status != "rejected":
            await callback.answer(f"Нельзя отклонить (статус: {PAYMENT_STATUS_RU.get(p['status'], '?') if p else 'нет заявки'})",
                                  show_alert=True)
            return
        await callback.answer("Чек отклонён")
        await _strip()
        await callback.message.reply(f"❌ Чек по заявке #{pid} отклонён. Публикации не начислены.")
        uid = int(p["user_id"])
        lang = await get_user_lang(uid)
        try:
            await _send(bot, uid, t(lang, "pay_rejected_user", id=pid), markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t(lang, "btn_resend_receipt"), callback_data=f"payretry:{pid}")],
                [InlineKeyboardButton(text=t(lang, "btn_new_payment"),   callback_data="buy:menu")],
            ]))
        except Exception as ex:
            logger.warning("Cannot notify user %s about rejection: %s", uid, ex)


# ════════════════════════════════════════════════════════════════════════════
# АДМИН: баланс, история, ручные операции, очередь
# ════════════════════════════════════════════════════════════════════════════

def _is_admin(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in ADMIN_IDS


def _fmt_ledger(rows: list) -> str:
    if not rows:
        return "  —"
    out = []
    for r in rows:
        who = f", админ {r['admin_id']}" if r["admin_id"] else ""
        ref = f", заявка #{r['payment_id']}" if r["payment_id"] else (f", объявление #{r['draft_id']}" if r["draft_id"] else "")
        out.append(f"  {billing.fmt_local(r['created_at'])} · {r['delta']:+d} · {r['kind']}{ref}{who}"
                   f"{' · ' + esc(r['reason']) if r['reason'] else ''} → {r['balance_after']}")
    return "\n".join(out)


@router.message(Command("balance"))
async def cmd_balance(message: Message, bot: Bot) -> None:
    parts = (message.text or "").split()
    if _is_admin(message) and len(parts) > 1:
        try:
            uid = int(parts[1])
        except ValueError:
            await message.answer("Использование: /balance <user_id>")
            return
        ov = await billing.get_overview(pool(), uid)
        u = await _user_row(uid)
        pays = await billing.user_payments(pool(), uid, 5)
        open_row = await billing.get_open_draft(pool(), uid)
        lines = [
            f"💼 <b>Баланс пользователя</b>\n{_admin_user_line(uid, u['full_name'] if u else None, u['tg_username'] if u else None)}",
            f"\nПлатных на балансе: <b>{ov['balance']}</b> (зарезервировано: {ov['reserved']}, доступно: {ov['available']})",
            "Бесплатная публикация: " + ("доступна" if ov["free_available"]
                                         else f"занята до {billing.fmt_local(billing.local_to_utc(ov['free_until']))}"),
            f"Последняя публикация: {billing.fmt_local(ov['last_pub_at'])}",
            "Следующая доступна: " + (billing.fmt_local(ov["next_at"]) if ov["next_at"] else "сейчас"),
            "Объявление в процессе: " + (f"#{open_row['id']} · {DRAFT_STATUS_RU[open_row['status']]}" if open_row else "нет"),
            "\nПоследние заявки на оплату:",
            "\n".join(f"  #{p['id']} · {p['qty']} шт · {p['amount']} ₽ · {PAYMENT_STATUS_RU[p['status']]}" for p in pays) or "  —",
            f"\n/history {uid} · /credit {uid} N причина · /adjust {uid} ±N причина",
        ]
        await message.answer("\n".join(lines), parse_mode="HTML")
        return
    lang = await get_user_lang(message.from_user.id)
    await _send(bot, message.chat.id, await _own_balance_text(message.from_user.id, lang),
                kb_create(lang))


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Использование: /history <user_id>")
        return
    uid = int(parts[1])
    cr = await billing.ledger_history(pool(), uid, credits=True, limit=15)
    db = await billing.ledger_history(pool(), uid, credits=False, limit=15)
    await message.answer(f"📥 <b>Начисления</b> (пользователь {uid}):\n{_fmt_ledger(cr)}\n\n"
                         f"📤 <b>Списания</b>:\n{_fmt_ledger(db)}", parse_mode="HTML")


async def _manual_op(message: Message, bot: Bot, kind: str, sign_required: bool) -> None:
    if not _is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=3)
    cmd = parts[0]
    if len(parts) < 4:
        await message.answer(f"Использование: {cmd} <user_id> <±количество> <причина>\nПричина обязательна.")
        return
    try:
        uid, delta = int(parts[1]), int(parts[2])
    except ValueError:
        await message.answer("user_id и количество должны быть целыми числами.")
        return
    if sign_required and delta <= 0:
        await message.answer("Для /credit количество должно быть > 0. Для уменьшения используйте /adjust.")
        return
    try:
        new_bal = await billing.manual_adjust(pool(), message.from_user.id, uid, delta, parts[3], kind)
    except ValueError as ex:
        await message.answer(f"❌ Не выполнено: {esc(str(ex))} (нельзя уйти ниже зарезервированного/нуля).")
        return
    logger.info("MANUAL %s admin=%s user=%s delta=%s reason=%r", kind, message.from_user.id, uid, delta, parts[3])
    await message.answer(f"✅ {kind}: пользователь {uid}, {delta:+d}. Баланс: {new_bal}. Записано в историю.")
    lang = await get_user_lang(uid)
    try:
        await _send(bot, uid, t(lang, "manual_credit_user", delta=delta, balance=new_bal))
    except Exception:
        pass


@router.message(Command("credit"))
async def cmd_credit(message: Message, bot: Bot) -> None:
    await _manual_op(message, bot, "manual_credit", True)


@router.message(Command("adjust"))
async def cmd_adjust(message: Message, bot: Bot) -> None:
    await _manual_op(message, bot, "manual_adjust", False)


@router.message(Command("queue"))
async def cmd_queue(message: Message, bot: Bot) -> None:
    """Админская очередь: одобренные объявления с кнопкой «📢 Опубликовать» у каждого."""
    if not _is_admin(message):
        return
    rows = await billing.queue_ready(pool(), 15)
    if not rows:
        await message.answer("Очередь публикации пуста.")
        return
    for r in rows:
        head = build_admin_notification(
            draft_id=int(r["id"]), full_name=r["full_name"] or "Пользователь", user_id=int(r["user_id"]),
            tg_username=r["tg_username"], cities=parse_route(r), travel_date=str(r["travel_date"]),
            cargo=str(r["cargo"]), phone=str(r["phone"] or ""), custom_username=r["custom_username"],
            ad_type=r["ad_type"] or "carrier", funding=r["funding"],
        )
        try:
            await _send(bot, message.chat.id, head + "\n" + await _readiness_line(r),
                        markup=kb_admin(int(r["id"]), approved=True))
        except Exception as ex:
            logger.warning("queue item %s failed: %s", r["id"], ex)


# ════════════════════════════════════════════════════════════════════════════
# ОТЗЫВ
# ════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "leave_feedback")
async def leave_feedback(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    lang = await get_user_lang(callback.from_user.id)
    await state.set_state(Form.feedback)
    sent = await replace_bot_msg(
        bot, callback.message.chat.id,
        callback.message.message_id,
        t(lang, "ask_feedback"),
    )
    await state.update_data(bot_msg_id=sent.message_id, aux_msg_id=None)


@router.message(Form.feedback)
async def receive_feedback(message: Message, state: FSMContext, bot: Bot) -> None:
    text      = (message.text or "").strip()
    user      = message.from_user
    full_name = user.full_name or "Пользователь"
    uname     = f"@{user.username}" if user.username else "—"

    for admin_id in ADMIN_IDS:
        try:
            await _send(
                bot, admin_id,
                f"{e('telegram')} <b>Отзыв от пользователя</b>\n\n"
                f"{e('person')} {esc(full_name)} ({uname})\n"
                f"{e('id_card')} <code>{user.id}</code>\n\n"
                f"{esc(text)}",
            )
        except Exception as ex:
            logger.exception("Feedback to admin %s failed: %s", admin_id, ex)

    lang = await get_user_lang(message.from_user.id)
    data = await state.get_data()
    await _delete(bot, message.chat.id, data.get("bot_msg_id"))
    await _delete(bot, message.chat.id, data.get("aux_msg_id"))
    await state.clear()

    sent = await _send(
        bot, message.chat.id,
        t(lang, "feedback_thanks"),
        markup=kb_after_feedback(lang),
    )
    await state.update_data(bot_msg_id=sent.message_id)


# ════════════════════════════════════════════════════════════════════════════
# АДМИН: рассылки (/broadcast)
# ════════════════════════════════════════════════════════════════════════════

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await state.set_state(BroadcastForm.waiting_content)
    await message.answer(
        f"{e('megaphone')} <b>Рассылка</b>\n\n"
        "Пришлите сообщение для рассылки: текст, фото или видео с подписью.\n\n"
        "Отменить — /cancel",
        parse_mode="HTML",
    )


@router.message(Command("cancel"), BroadcastForm.waiting_content)
@router.message(Command("cancel"), BroadcastForm.waiting_buttons)
@router.message(Command("cancel"), BroadcastForm.confirm)
async def cmd_broadcast_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Рассылка отменена.")


@router.message(BroadcastForm.waiting_content)
async def broadcast_get_content(message: Message, state: FSMContext) -> None:
    """Сохраняем контент (текст/фото/видео) и переходим к вопросу про кнопки."""
    content: dict = {"type": "text", "text": "", "file_id": None}

    if message.photo:
        content = {"type": "photo", "text": message.caption or "", "file_id": message.photo[-1].file_id}
    elif message.video:
        content = {"type": "video", "text": message.caption or "", "file_id": message.video.file_id}
    elif message.text:
        content = {"type": "text", "text": message.text, "file_id": None}
    else:
        await message.answer("Поддерживаются только текст, фото или видео. Попробуйте ещё раз.")
        return

    await state.update_data(bcast_content=content)
    await state.set_state(BroadcastForm.waiting_buttons)
    await message.answer(
        "Добавить кнопки со ссылками?\n\n"
        "Пришлите построчно в формате:\n"
        "<code>Текст кнопки - https://ссылка</code>\n\n"
        "Например:\n"
        "<code>Наш канал - https://t.me/channel\n"
        "Написать нам - https://t.me/kabulbeg</code>\n\n"
        "Или нажмите «Без кнопок».",
        parse_mode="HTML",
        reply_markup=kb_broadcast_skip_buttons(),
    )


@router.callback_query(BroadcastForm.waiting_buttons, F.data == "bcast:no_buttons")
async def broadcast_no_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(bcast_buttons=[])
    await _show_broadcast_preview(callback.message, state)


@router.message(BroadcastForm.waiting_buttons)
async def broadcast_get_buttons(message: Message, state: FSMContext) -> None:
    buttons = parse_broadcast_buttons(message.text or "")
    if not buttons:
        await message.answer(
            "Не удалось распознать ни одной кнопки. Формат:\n"
            "<code>Текст - https://ссылка</code>\n\n"
            "Попробуйте ещё раз или нажмите «Без кнопок».",
            parse_mode="HTML",
            reply_markup=kb_broadcast_skip_buttons(),
        )
        return
    # Сериализуем кнопки в простой список словарей
    await state.update_data(bcast_buttons=[{"text": b.text, "url": b.url} for b in buttons])
    await _show_broadcast_preview(message, state)


async def _show_broadcast_preview(message: Message, state: FSMContext) -> None:
    data    = await state.get_data()
    content = data["bcast_content"]
    buttons_data = data.get("bcast_buttons", [])
    buttons = [InlineKeyboardButton(text=b["text"], url=b["url"]) for b in buttons_data]
    markup  = kb_from_buttons(buttons)

    await state.set_state(BroadcastForm.confirm)

    preview_caption = f"{raw('eyes')} Превью рассылки:\n\n{content['text']}" if content["text"] else f"{raw('eyes')} Превью рассылки (без текста)"

    if content["type"] == "photo":
        await message.answer_photo(content["file_id"], caption=preview_caption, reply_markup=markup)
    elif content["type"] == "video":
        await message.answer_video(content["file_id"], caption=preview_caption, reply_markup=markup)
    else:
        await message.answer(preview_caption, reply_markup=markup)

    await message.answer(
        f"Рассылка готова. Получателей: узнаем при отправке.",
        reply_markup=kb_broadcast_confirm(),
    )


@router.callback_query(BroadcastForm.confirm, F.data == "bcast:cancel")
async def broadcast_cancel_btn(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Рассылка отменена.")


@router.callback_query(BroadcastForm.confirm, F.data == "bcast:send")
async def broadcast_send(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer("Рассылка запущена…")
    data    = await state.get_data()
    content = data["bcast_content"]
    buttons_data = data.get("bcast_buttons", [])
    buttons = [InlineKeyboardButton(text=b["text"], url=b["url"]) for b in buttons_data]
    markup  = kb_from_buttons(buttons)
    await state.clear()

    user_ids = await get_all_active_users()
    sent, failed = 0, 0

    await callback.message.edit_text(f"{raw('outbox')} Рассылка началась. Получателей: {len(user_ids)}")

    for uid in user_ids:
        try:
            if content["type"] == "photo":
                await bot.send_photo(uid, content["file_id"], caption=content["text"] or None, reply_markup=markup)
            elif content["type"] == "video":
                await bot.send_video(uid, content["file_id"], caption=content["text"] or None, reply_markup=markup)
            else:
                await bot.send_message(uid, content["text"], reply_markup=markup)
            sent += 1
        except Exception as e:
            failed += 1
            err_str = str(e).lower()
            if "blocked" in err_str or "deactivated" in err_str or "not found" in err_str:
                await mark_user_blocked(uid)
        # Небольшая пауза чтобы не упереться в rate limit Telegram (~30 msg/sec)
        await asyncio.sleep(0.05)

    await log_broadcast(callback.from_user.id, content["text"][:200], sent, failed)
    await bot.send_message(
        callback.from_user.id,
        f"{raw('check')} Рассылка завершена.\n\nДоставлено: {sent}\nНе доставлено: {failed}",
    )


# ════════════════════════════════════════════════════════════════════════════
# АДМИН: статистика
# ════════════════════════════════════════════════════════════════════════════

@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return

    total_users, week_users = await get_users_count()
    posts = await get_posts_stats()
    groups = await get_all_group_chats()

    routes_lines = "\n".join(
        f"  • {esc(o)} → {esc(d)}: {cnt}" for o, d, cnt in posts["top_routes"]
    ) or "  нет данных"

    text = (
        f"{e('chart')} <b>Статистика бота</b>\n\n"
        f"{e('group')} <b>Пользователи</b>\n"
        f"Всего запускали бота: {total_users}\n"
        f"Активны за 7 дней: {week_users}\n\n"
        f"{e('clipboard')} <b>Объявления</b>\n"
        f"Всего создано: {posts['total']}\n"
        f"Опубликовано: {posts['published']}\n"
        f"  ✈️ Перевозчики: {posts['carrier']}\n"
        f"  🔎 Ищут попутчика: {posts['seeker']}\n\n"
        f"{e('trend_up')} <b>Топ направлений</b>\n{routes_lines}\n\n"
        f"{e('group')} <b>Групповые чаты</b>: {len(groups)}\n"
    )
    await message.answer(text, parse_mode="HTML")


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    global BOT_USERNAME
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher()
    dp.include_router(router)

    me           = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info("Bot started as @%s", BOT_USERNAME)

    # Перезапуск во время публикации: 'publishing' → 'publish_error', резерв снят, списания нет.
    stuck = await billing.recover_stuck_publications(pool())
    if stuck:
        logger.warning("Recovered stuck publications: %s", stuck)
        await _notify_admins(
            bot, "⚠️ После перезапуска бота публикации " + ", ".join(f"#{i}" for i in stuck) +
                 " были прерваны. Проверьте канал вручную (пост мог успеть уйти) и только затем "
                 "повторите публикацию. Баланс не списан.")

    asyncio.create_task(scheduler_loop(bot))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
