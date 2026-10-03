"""
Freemium-биллинг: баланс публикаций, платежи переводом на карту, атомарная
публикация и ограничение «1 публикация за 24 часа».

Модуль намеренно НЕ зависит от aiogram/main.py — только asyncpg. Всё, что
связано с деньгами и статусами, живёт здесь и работает поверх пула соединений;
main.py лишь вызывает эти функции из хендлеров. Так логику можно тестировать
на живом PostgreSQL без Telegram (см. tests/test_billing.py).

━━━ Модель данных (шесть НЕЗАВИСИМЫХ понятий, ТЗ п.15) ─────────────────────
1. Бесплатное место  — НЕ хранится флагом. Вычисляется динамически:
   занято, если у пользователя есть drafts.status='published' AND
   funding='free' AND срок объявления ещё не истёк (free_slot_info).
2. Купленный баланс  — user_balances(balance, reserved). balance — сколько
   публикаций куплено и не потрачено; reserved — сколько из них «придержано»
   под публикацию, которая прямо сейчас уходит в Telegram. Доступно = balance-reserved.
3. Cooldown 24 ч     — вычисляется от max(drafts.published_at) среди
   status='published' (фактическое время последней успешной публикации).
4. Состояние объявления — drafts.status:
      pending → approved → publishing → published
                  ↘ publish_error (можно повторить)
      pending/approved/publish_error → rejected | cancelled
   Оплата объявления отдельно: drafts.funding ('free'|'paid') — ЧЕМ платим.
   «Ожидает оплаты» = funding='paid' и доступного баланса нет (производное).
5. Платёж            — payments (+ payment_receipts): awaiting_receipt →
   receipt_received → confirmed | rejected (→ awaiting_receipt снова).
6. История           — balance_ledger, append-only, op_key UNIQUE
   (pay:<id>, spend:<draft_id>, manual:<uuid>) — идемпотентность.

━━━ Конкурентность ────────────────────────────────────────────────────────
Мьютекс на пользователя = строка user_balances FOR UPDATE. Порядок блокировок
везде один: payment/draft → user_balances. Публикация двухфазная:
  claim (tx: проверки + status='publishing' + reserved+1)
  → отправка в Telegram (вне транзакции)
  → finalize (tx: status='published', balance-1, reserved-1, ledger)
    либо release (tx: status='publish_error', reserved-1).
Списание происходит ТОЛЬКО в finalize, после подтверждённого размещения.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)

# ── Конфигурация тарифов ─────────────────────────────────────────────────────
PACKAGES: dict[int, int] = {2: 100, 5: 200}     # количество публикаций → цена, ₽
COOLDOWN = timedelta(hours=24)

def make_fingerprint(ad_type: str, cities: list[str], travel_date: str,
                     phone: str, custom_username: Optional[str]) -> str:
    """Канонический fingerprint объявления для duplicate/cooldown-проверок."""
    route = json.dumps(cities, ensure_ascii=False)
    norm_phone = re.sub(r"[\s\-()]", "", phone or "")
    norm_username = (custom_username or "").casefold().strip().lstrip("@")
    raw = "|".join((ad_type or "carrier", route, str(travel_date or ""), norm_phone, norm_username))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


OPEN_STATUSES = ("pending", "approved", "publishing", "publish_error")
EDITABLE_STATUSES = ("pending", "approved", "publish_error")   # можно отклонить/отменить
RETRIABLE_STATUSES = ("approved", "publish_error")             # можно публиковать

TZ_OFFSET = 3


def configure(tz_offset: int) -> None:
    global TZ_OFFSET
    TZ_OFFSET = tz_offset


class OpenDraftExists(Exception):
    """Совместимость со старым API; общий лимит больше не используется."""
    def __init__(self, draft_id: int):
        super().__init__(f"open draft exists: {draft_id}")
        self.draft_id = draft_id


class PendingDuplicate(Exception):
    """Идентичное объявление уже находится на модерации."""
    def __init__(self, draft_id: int):
        super().__init__(f"pending duplicate exists: {draft_id}")
        self.draft_id = draft_id


# ── Время ────────────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_local(dt: datetime) -> datetime:
    """aware UTC → naive «местное время» (как now_local() в main.py)."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(hours=TZ_OFFSET)


def local_to_utc(dt: datetime) -> datetime:
    return (dt - timedelta(hours=TZ_OFFSET)).replace(tzinfo=timezone.utc)


def fmt_local(dt: Optional[datetime]) -> str:
    return to_local(dt).strftime("%d.%m.%Y %H:%M") if dt else "—"


# ── Схема БД и миграция ──────────────────────────────────────────────────────

async def init_schema(pool: asyncpg.Pool) -> None:
    """Идемпотентно создаёт/дополняет таблицы. Вызывать после создания drafts."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Сериализуем параллельный запуск нескольких экземпляров бота.
            await conn.execute("SELECT pg_advisory_xact_lock(727001)")

            for col, ddl in (
                ("status", "TEXT"),
                ("funding", "TEXT"),
                ("fingerprint", "TEXT"),
                ("published_at", "TIMESTAMPTZ"),
                ("publishing_started_at", "TIMESTAMPTZ"),
                ("status_changed_at", "TIMESTAMPTZ"),
                ("last_error", "TEXT"),
            ):
                await conn.execute(f"ALTER TABLE drafts ADD COLUMN IF NOT EXISTS {col} {ddl}")

            # Разовый бэкфилл (только строки без status, т.е. созданные до апдейта):
            #  • опубликованные → published + занимают бесплатное место до истечения
            #    (иначе апдейт «выдал бы» вторую бесплатную поверх активной, ТЗ п.1);
            #  • отложенные → approved (админ уже принял решение публиковать);
            #  • остальные неопубликованные: старше 7 дней считаем отклонёнными
            #    (в старой схеме «отклонено» нигде не хранилось), свежие — pending.
            await conn.execute("""
                UPDATE drafts SET status='published', funding='free',
                       published_at=COALESCE(published_at, created_at)
                WHERE status IS NULL AND published=TRUE
            """)
            await conn.execute("""
                UPDATE drafts SET status='approved', funding='free'
                WHERE status IS NULL AND published=FALSE AND scheduled_at IS NOT NULL
            """)
            await conn.execute("""
                UPDATE drafts SET status='rejected', funding='free'
                WHERE status IS NULL AND created_at < now() - interval '7 days'
            """)
            await conn.execute("UPDATE drafts SET status='pending', funding='free' WHERE status IS NULL")
            await conn.execute("UPDATE drafts SET funding='free' WHERE funding IS NULL")
            await conn.execute("ALTER TABLE drafts ALTER COLUMN status SET DEFAULT 'pending'")
            await conn.execute("ALTER TABLE drafts ALTER COLUMN status SET NOT NULL")
            await conn.execute("ALTER TABLE drafts ALTER COLUMN funding SET DEFAULT 'free'")
            await conn.execute("ALTER TABLE drafts ALTER COLUMN funding SET NOT NULL")

            # Разные объявления пользователя могут находиться в открытом процессе одновременно.
            await conn.execute("DROP INDEX IF EXISTS ux_drafts_one_open_per_user")
            await conn.execute("""
                UPDATE drafts SET fingerprint = md5(
                    concat_ws('|',
                        COALESCE(ad_type, 'carrier'),
                        COALESCE(route, '[]'),
                        COALESCE(travel_date, ''),
                        regexp_replace(COALESCE(phone, ''), '[\\s\\-()]', '', 'g'),
                        lower(trim(COALESCE(custom_username, '')))
                    )
                ) WHERE fingerprint IS NULL
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_drafts_user_fingerprint_status "
                "ON drafts (user_id, fingerprint, status)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_drafts_user_published ON drafts (user_id, published_at) "
                "WHERE status='published'"
            )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_balances (
                    user_id    BIGINT PRIMARY KEY,
                    balance    INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
                    reserved   INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    CHECK (reserved <= balance)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id          SERIAL PRIMARY KEY,
                    user_id     BIGINT NOT NULL,
                    draft_id    INTEGER,
                    qty         INTEGER NOT NULL,
                    amount      INTEGER NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'awaiting_receipt',
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                    reviewed_by BIGINT,
                    reviewed_at TIMESTAMPTZ
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS ix_payments_user ON payments (user_id, id DESC)")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS payment_receipts (
                    id         SERIAL PRIMARY KEY,
                    payment_id INTEGER NOT NULL REFERENCES payments(id),
                    user_id    BIGINT NOT NULL,
                    file_type  TEXT NOT NULL,
                    file_id    TEXT NOT NULL,
                    chat_id    BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS balance_ledger (
                    id            BIGSERIAL PRIMARY KEY,
                    user_id       BIGINT NOT NULL,
                    delta         INTEGER NOT NULL,
                    kind          TEXT NOT NULL,
                    balance_after INTEGER NOT NULL,
                    payment_id    INTEGER,
                    draft_id      INTEGER,
                    admin_id      BIGINT,
                    reason        TEXT,
                    op_key        TEXT NOT NULL UNIQUE,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_ledger_user ON balance_ledger (user_id, id DESC)"
            )


# ── Чистые функции: даты и расчёт доступных публикаций (ТЗ п.9) ──────────────

def parse_trip_date(value: Any) -> Optional[date]:
    try:
        return datetime.strptime(str(value), "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return None


def seeker_days(row: Any) -> int:
    m = re.search(r"\d+", str(row["travel_date"] or ""))
    return int(m.group()) if m else 30


def trip_passed(row: Any, now: Optional[datetime] = None) -> bool:
    """Дата поездки уже в прошлом? (Для seeker фиксированной даты нет → False.)"""
    if (row["ad_type"] or "carrier") == "seeker":
        return False
    d = parse_trip_date(row["travel_date"])
    return d is not None and d < to_local(now or utcnow()).date()


def draft_deadline(row: Any, now: Optional[datetime] = None) -> Optional[datetime]:
    """
    Местное (naive) время, до которого объявление актуально.
    carrier — конец дня поездки (как и в планировщике: истекает, когда дата < сегодня);
    seeker  — published_at (или сейчас, если ещё не опубликовано) + N дней из «В ближайшие N дней».
    """
    now = now or utcnow()
    base = to_local(row["published_at"] or now)
    if (row["ad_type"] or "carrier") == "seeker":
        return base + timedelta(days=seeker_days(row))
    d = parse_trip_date(row["travel_date"])
    if d is None:
        return base + timedelta(days=30)
    return datetime.combine(d + timedelta(days=1), time.min)


@dataclass(frozen=True)
class Window:
    next_at: datetime          # aware UTC: самый ранний момент следующей публикации
    slots: Optional[int]       # сколько публикаций (по одной в 24 ч) успеет до дедлайна; None — без дедлайна


def publication_window(last_pub_at: Optional[datetime], deadline_local: Optional[datetime],
                       now: Optional[datetime] = None) -> Window:
    """Единая точка расчёта cooldown-а и числа «слотов» до вылета."""
    now = now or utcnow()
    next_at = now if last_pub_at is None else max(now, last_pub_at + COOLDOWN)
    if deadline_local is None:
        return Window(next_at, None)
    dl = local_to_utc(deadline_local)
    if next_at >= dl:
        return Window(next_at, 0)
    return Window(next_at, math.ceil((dl - next_at) / COOLDOWN))


@dataclass(frozen=True)
class UsableCalc:
    slots: Optional[int]
    available_before: int          # бесплатная (0/1) + доступный баланс до покупки
    total_after: int               # то же + пакет
    usable_total: int              # сколько всего реально успеет уйти до вылета
    usable_from_package: int       # сколько из ПАКЕТА успеет
    unused_from_package: int       # сколько из пакета останется «на потом»
    next_at: datetime
    fits: bool                     # весь пакет успеет до вылета


def calc_usable_publications(*, now: Optional[datetime], last_pub_at: Optional[datetime],
                             deadline_local: Optional[datetime], free_available: bool,
                             balance_available: int, package_qty: int,
                             queued_ahead: int = 0) -> UsableCalc:
    """
    Сколько публикаций пользователь реально успеет использовать до вылета.

    Учитывает: текущее время, дедлайн (дата поездки), время последней успешной
    публикации, cooldown 24 ч, свободное место и баланс, выбранный пакет.
    queued_ahead — публикации ДРУГИХ объявлений, которые займут слоты раньше
    (текущее открытое объявление в расчёт не вычитается: оно и есть потребитель
    тех же публикаций — иначе слот считался бы дважды).

    Функция используется: (1) при покупке/предупреждении, (2) для админского
    отчёта о доступности, (3) claim_publication берёт из неё next_at.
    """
    w = publication_window(last_pub_at, deadline_local, now)
    before = (1 if free_available else 0) + max(0, balance_available)
    total = before + package_qty
    if w.slots is None:
        usable_total = total
    else:
        usable_total = min(total, max(0, w.slots - max(0, queued_ahead)))
    from_pkg = max(0, min(package_qty, usable_total - before))
    return UsableCalc(
        slots=w.slots, available_before=before, total_after=total,
        usable_total=usable_total, usable_from_package=from_pkg,
        unused_from_package=package_qty - from_pkg, next_at=w.next_at,
        fits=(from_pkg == package_qty),
    )


# ── Внутренние помощники (внутри транзакции) ─────────────────────────────────

async def _lock_balance(conn: asyncpg.Connection, user_id: int) -> asyncpg.Record:
    """Создаёт строку баланса при необходимости и блокирует её (мьютекс пользователя)."""
    await conn.execute(
        "INSERT INTO user_balances (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id
    )
    return await conn.fetchrow(
        "SELECT balance, reserved FROM user_balances WHERE user_id=$1 FOR UPDATE", user_id
    )


async def _free_slot_until(conn: asyncpg.Connection, user_id: int,
                           exclude_draft_id: Optional[int] = None,
                           now: Optional[datetime] = None) -> Optional[datetime]:
    """
    None — бесплатное место свободно; иначе местное время, когда освободится.
    Место занято активной ОПУБЛИКОВАННОЙ бесплатной заявкой (ТЗ п.1). Заявки на
    модерации/в оплате/в очереди, отклонённые и отменённые место не занимают.
    """
    now = now or utcnow()
    rows = await conn.fetch(
        """
        SELECT id, ad_type, travel_date, published_at, created_at FROM drafts
        WHERE user_id=$1 AND status='published' AND funding='free' AND expired=FALSE
          AND ($2::int IS NULL OR id<>$2)
        """,
        user_id, exclude_draft_id,
    )
    until: Optional[datetime] = None
    now_l = to_local(now)
    for r in rows:
        dl = draft_deadline(r, now)
        if dl is not None and dl > now_l and (until is None or dl > until):
            until = dl
    return until


async def _last_publication(conn: asyncpg.Connection, user_id: int,
                       fingerprint: Optional[str] = None) -> Optional[datetime]:
    if fingerprint:
        return await conn.fetchval(
            "SELECT max(published_at) FROM drafts WHERE user_id=$1 AND fingerprint=$2 AND status='published'",
            user_id, fingerprint)
    return await conn.fetchval(
        "SELECT max(published_at) FROM drafts WHERE user_id=$1 AND status='published'", user_id)


async def last_publication_for_draft(pool: asyncpg.Pool, draft_id: int) -> Optional[datetime]:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT max(published_at) FROM drafts d "
            "WHERE d.user_id=(SELECT user_id FROM drafts WHERE id=$1) "
            "AND d.fingerprint=(SELECT fingerprint FROM drafts WHERE id=$1) AND d.status='published'",
            draft_id)


async def _apply_ledger(conn: asyncpg.Connection, *, user_id: int, delta: int, kind: str,
                        op_key: str, payment_id: Optional[int] = None,
                        draft_id: Optional[int] = None, admin_id: Optional[int] = None,
                        reason: Optional[str] = None, consume_reserved: bool = False) -> Optional[int]:
    """
    Атомарно (в текущей транзакции, под блокировкой баланса) применяет операцию.
    Возвращает новый баланс либо None, если операция с таким op_key уже
    выполнялась (идемпотентность). ValueError — если баланс ушёл бы ниже
    зарезервированного.
    """
    bal = await _lock_balance(conn, user_id)
    new_balance = bal["balance"] + delta
    new_reserved = bal["reserved"] - (1 if consume_reserved else 0)
    if new_reserved < 0:
        new_reserved = 0
    if new_balance < new_reserved or new_balance < 0:
        raise ValueError("insufficient balance")
    inserted = await conn.fetchval(
        """
        INSERT INTO balance_ledger
            (user_id, delta, kind, balance_after, payment_id, draft_id, admin_id, reason, op_key)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        ON CONFLICT (op_key) DO NOTHING RETURNING id
        """,
        user_id, delta, kind, new_balance, payment_id, draft_id, admin_id, reason, op_key,
    )
    if inserted is None:
        return None
    await conn.execute(
        "UPDATE user_balances SET balance=$2, reserved=$3, updated_at=now() WHERE user_id=$1",
        user_id, new_balance, new_reserved,
    )
    return new_balance


# ── Черновики: создание и статусы ────────────────────────────────────────────

async def create_draft(pool: asyncpg.Pool, d: dict[str, Any]) -> int:
    """Создаёт самостоятельное объявление; разные drafts друг друга не блокируют."""
    cities = d["cities"]; uid = int(d["user_id"])
    ad_type = d.get("ad_type", "carrier"); travel_date = d["travel_date"]
    phone = d.get("phone", ""); custom_username = d.get("custom_username")
    fingerprint = make_fingerprint(ad_type, cities, travel_date, phone, custom_username)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_balance(conn, uid)
            existing = await conn.fetchval(
                "SELECT id FROM drafts WHERE user_id=$1 AND fingerprint=$2 AND status='pending' LIMIT 1",
                uid, fingerprint)
            if existing is not None:
                raise PendingDuplicate(int(existing))
            free_open = await conn.fetchval(
                "SELECT 1 FROM drafts WHERE user_id=$1 AND funding='free' "
                "AND status IN ('pending','approved','publishing','publish_error') LIMIT 1", uid)
            free_until = await _free_slot_until(conn, uid)
            funding = "free" if free_open is None and free_until is None else "paid"
            row = await conn.fetchrow(
                """INSERT INTO drafts
                   (user_id,tg_username,custom_username,full_name,origin,destination,route,
                    travel_date,cargo,phone,ad_type,status,funding,fingerprint,status_changed_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'pending',$12,$13,now())
                   RETURNING id""",
                uid,d.get("tg_username"),custom_username,d["full_name"],cities[0],cities[-1],
                json.dumps(cities,ensure_ascii=False),travel_date,d["cargo"],phone,ad_type,
                funding,fingerprint)
            return int(row["id"])


async def get_open_draft(pool: asyncpg.Pool, user_id: int) -> Optional[asyncpg.Record]:
    """Совместимость: последний открытый draft."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM drafts WHERE user_id=$1 AND status = ANY($2::text[]) ORDER BY id DESC LIMIT 1",
            user_id, list(OPEN_STATUSES))


async def get_open_drafts(pool: asyncpg.Pool, user_id: int, limit: int = 50) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM drafts WHERE user_id=$1 AND status = ANY($2::text[]) ORDER BY id DESC LIMIT $3",
            user_id, list(OPEN_STATUSES), limit)


async def get_user_drafts(pool: asyncpg.Pool, user_id: int, limit: int = 50) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM drafts WHERE user_id=$1 ORDER BY id DESC LIMIT $2", user_id, limit)


async def _transition(pool: asyncpg.Pool, draft_id: int, new_status: str,
                      allowed: tuple[str, ...], user_id: Optional[int] = None) -> Optional[asyncpg.Record]:
    """Атомарный переход статуса: сработает ровно один раз при гонке/повторе."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            UPDATE drafts SET status=$2, status_changed_at=now(), scheduled_at=NULL
            WHERE id=$1 AND status = ANY($3::text[]) AND ($4::bigint IS NULL OR user_id=$4)
            RETURNING *
            """,
            draft_id, new_status, list(allowed), user_id,
        )


async def approve_draft(pool: asyncpg.Pool, draft_id: int) -> Optional[asyncpg.Record]:
    """pending → approved. НЕ публикует (ТЗ п.6/п.16.8)."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "UPDATE drafts SET status='approved', status_changed_at=now() "
            "WHERE id=$1 AND status='pending' RETURNING *", draft_id,
        )


async def reject_draft(pool: asyncpg.Pool, draft_id: int) -> Optional[asyncpg.Record]:
    return await _transition(pool, draft_id, "rejected", EDITABLE_STATUSES)


async def cancel_draft(pool: asyncpg.Pool, draft_id: int, user_id: int) -> Optional[asyncpg.Record]:
    return await _transition(pool, draft_id, "cancelled", EDITABLE_STATUSES, user_id)


async def set_funding(pool: asyncpg.Pool, draft_id: int, user_id: int, funding: str) -> tuple[bool, str]:
    """Явный выбор пользователя: бесплатная или платная публикация (ТЗ п.1)."""
    if funding not in ("free", "paid"):
        return False, "bad_funding"
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_balance(conn, user_id)
            d = await conn.fetchrow("SELECT * FROM drafts WHERE id=$1 AND user_id=$2 FOR UPDATE", draft_id, user_id)
            if d is None or d["status"] not in EDITABLE_STATUSES:
                return False, "not_editable"
            if funding == "free" and await _free_slot_until(conn, user_id, exclude_draft_id=draft_id) is not None:
                return False, "free_taken"
            await conn.execute("UPDATE drafts SET funding=$2 WHERE id=$1", draft_id, funding)
            return True, "ok"


# ── Публикация: claim → (Telegram) → finalize / release ──────────────────────

@dataclass
class ClaimResult:
    ok: bool
    code: str
    draft: Optional[asyncpg.Record] = None
    last_at: Optional[datetime] = None
    next_at: Optional[datetime] = None


async def claim_publication(pool: asyncpg.Pool, draft_id: int,
                            now: Optional[datetime] = None) -> ClaimResult:
    """
    Фаза 1. В ОДНОЙ транзакции, под блокировкой заявки и баланса пользователя,
    повторно проверяет ВСЕ условия (ТЗ п.7) и переводит заявку в 'publishing'.
    Второй параллельный claim увидит 'publishing' и получит code='in_progress'.

    Коды отказа: not_found, not_approved, in_progress, already_published,
    rejected, cancelled, trip_passed, other_publishing, no_free, no_balance, cooldown.
    """
    now = now or utcnow()
    async with pool.acquire() as conn:
        async with conn.transaction():
            d = await conn.fetchrow("SELECT * FROM drafts WHERE id=$1 FOR UPDATE", draft_id)
            if d is None:
                return ClaimResult(False, "not_found")
            uid = int(d["user_id"])
            bal = await _lock_balance(conn, uid)

            st = d["status"]
            if st == "published" or d["published"]:
                return ClaimResult(False, "already_published", d)
            if st == "publishing":
                return ClaimResult(False, "in_progress", d)
            if st in ("rejected", "cancelled"):
                return ClaimResult(False, st, d)
            if st not in RETRIABLE_STATUSES:
                return ClaimResult(False, "not_approved", d)
            if trip_passed(d, now):
                return ClaimResult(False, "trip_passed", d)
            funding = d["funding"] or "free"
            if funding == "free":
                if await _free_slot_until(conn, uid, exclude_draft_id=draft_id, now=now) is not None:
                    return ClaimResult(False, "no_free", d)
            else:
                if bal["balance"] - bal["reserved"] < 1:
                    return ClaimResult(False, "no_balance", d)

            last = await _last_publication(conn, uid, d["fingerprint"])
            w = publication_window(last, None, now)
            if last is not None and now < last + COOLDOWN:
                return ClaimResult(False, "cooldown", d, last_at=last, next_at=last + COOLDOWN)

            fresh = await conn.fetchrow(
                "UPDATE drafts SET status='publishing', publishing_started_at=$2, "
                "status_changed_at=$2, last_error=NULL WHERE id=$1 RETURNING *",
                draft_id, now,
            )
            if funding == "paid":
                await conn.execute(
                    "UPDATE user_balances SET reserved=reserved+1, updated_at=now() WHERE user_id=$1", uid
                )
            return ClaimResult(True, "ok", fresh, last_at=last, next_at=w.next_at)


async def finalize_publication(pool: asyncpg.Pool, draft_id: int, channel_msg_id: int,
                               now: Optional[datetime] = None) -> dict[str, Any]:
    """
    Фаза 2 (после ПОДТВЕРЖДЁННОГО размещения в канале). Атомарно: помечает
    опубликованным, фиксирует фактическое время, для платной публикации
    списывает ровно 1 и пишет операцию в историю (op_key='spend:<draft_id>').
    Повторный вызов безопасен. Возвращает {'result', 'funding', 'balance'}.
    """
    now = now or utcnow()
    async with pool.acquire() as conn:
        async with conn.transaction():
            d = await conn.fetchrow("SELECT * FROM drafts WHERE id=$1 FOR UPDATE", draft_id)
            if d is None:
                return {"result": "not_found"}
            if d["status"] == "published":
                return {"result": "already", "funding": d["funding"], "balance": None}
            if d["status"] != "publishing":
                logger.critical("finalize: draft %s in status %s (post %s is already in channel!)",
                                draft_id, d["status"], channel_msg_id)
                return {"result": "invalid_status", "status": d["status"]}
            uid = int(d["user_id"])
            balance_after: Optional[int] = None
            billed = True
            if d["funding"] == "paid":
                await _lock_balance(conn, uid)
                try:
                    balance_after = await _apply_ledger(
                        conn, user_id=uid, delta=-1, kind="spend", op_key=f"spend:{draft_id}",
                        draft_id=draft_id, reason="Публикация объявления", consume_reserved=True,
                    )
                except ValueError:
                    billed = False
                    logger.critical("finalize: cannot charge draft %s user %s — published unbilled", draft_id, uid)
            await conn.execute(
                """
                UPDATE drafts SET status='published', published=TRUE, channel_msg_id=$2,
                       published_at=$3, status_changed_at=$3, scheduled_at=NULL, last_error=NULL
                WHERE id=$1
                """,
                draft_id, channel_msg_id, now,
            )
            return {"result": "published" if billed else "published_unbilled",
                    "funding": d["funding"], "balance": balance_after}


async def release_publication(pool: asyncpg.Pool, draft_id: int, error: str) -> bool:
    """
    Публикация не удалась (ошибка Telegram и т.п.): 'publishing' → 'publish_error',
    резерв снимается, НИЧЕГО не списывается, повтор разрешён.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            d = await conn.fetchrow("SELECT * FROM drafts WHERE id=$1 FOR UPDATE", draft_id)
            if d is None or d["status"] != "publishing":
                return False
            await conn.execute(
                "UPDATE drafts SET status='publish_error', last_error=$2, status_changed_at=now() WHERE id=$1",
                draft_id, (error or "")[:500],
            )
            if d["funding"] == "paid":
                await _lock_balance(conn, int(d["user_id"]))
                await conn.execute(
                    "UPDATE user_balances SET reserved=GREATEST(reserved-1,0), updated_at=now() WHERE user_id=$1",
                    int(d["user_id"]),
                )
            return True


async def recover_stuck_publications(pool: asyncpg.Pool) -> list[int]:
    """
    Вызывается при старте. Всё, что осталось в 'publishing' (бот упал между
    claim и finalize), переводится в 'publish_error' с освобождением резерва.
    Списание не происходило, объявление не помечено опубликованным. Админ
    должен проверить канал вручную (пост мог успеть уйти) и только потом повторять.
    """
    async with pool.acquire() as conn:
        ids = [int(r["id"]) for r in await conn.fetch("SELECT id FROM drafts WHERE status='publishing'")]
    done = []
    for i in ids:
        if await release_publication(
            pool, i, "Перезапуск бота во время публикации — проверьте канал вручную перед повтором"
        ):
            done.append(i)
    return done


# ── Платежи ──────────────────────────────────────────────────────────────────

async def create_payment(pool: asyncpg.Pool, user_id: int, qty: int,
                         draft_id: Optional[int] = None) -> tuple[asyncpg.Record, bool]:
    """
    Создаёт заявку на оплату. Повторное нажатие «Купить» (тот же пакет, чек ещё
    не отправлен) возвращает уже существующую заявку вместо дубликата.
    Возвращает (заявка, создана_новая).
    """
    if qty not in PACKAGES:
        raise ValueError("unknown package")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_balance(conn, user_id)
            existing = await conn.fetchrow(
                "SELECT * FROM payments WHERE user_id=$1 AND qty=$2 AND status='awaiting_receipt' "
                "ORDER BY id DESC LIMIT 1", user_id, qty,
            )
            if existing is not None:
                return existing, False
            row = await conn.fetchrow(
                "INSERT INTO payments (user_id, draft_id, qty, amount) VALUES ($1,$2,$3,$4) RETURNING *",
                user_id, draft_id, qty, PACKAGES[qty],
            )
            return row, True


async def get_payment(pool: asyncpg.Pool, payment_id: int) -> Optional[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM payments WHERE id=$1", payment_id)


async def find_receipt_target(pool: asyncpg.Pool, user_id: int) -> Optional[asyncpg.Record]:
    """Заявка, к которой относится присланный чек: свежая awaiting_receipt, иначе свежая receipt_received."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT * FROM payments WHERE user_id=$1 AND status IN ('awaiting_receipt','receipt_received')
            ORDER BY (status='awaiting_receipt') DESC, id DESC LIMIT 1
            """, user_id,
        )


async def attach_receipt(pool: asyncpg.Pool, payment_id: int, user_id: int, file_type: str,
                         file_id: str, chat_id: int, message_id: int) -> Optional[tuple[asyncpg.Record, bool]]:
    """
    Привязывает чек к заявке. Возвращает (заявка, first) — first=True, если это
    первый чек и статус перешёл в receipt_received; None — заявка недоступна.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            p = await conn.fetchrow(
                "SELECT * FROM payments WHERE id=$1 AND user_id=$2 FOR UPDATE", payment_id, user_id
            )
            if p is None or p["status"] not in ("awaiting_receipt", "receipt_received"):
                return None
            await conn.execute(
                "INSERT INTO payment_receipts (payment_id, user_id, file_type, file_id, chat_id, message_id) "
                "VALUES ($1,$2,$3,$4,$5,$6)", payment_id, user_id, file_type, file_id, chat_id, message_id,
            )
            first = p["status"] == "awaiting_receipt"
            if first:
                p = await conn.fetchrow(
                    "UPDATE payments SET status='receipt_received', updated_at=now() WHERE id=$1 RETURNING *",
                    payment_id,
                )
            return p, first


async def confirm_payment(pool: asyncpg.Pool, payment_id: int, admin_id: int) -> dict[str, Any]:
    """
    Подтверждение оплаты админом: статус → confirmed и начисление qty публикаций —
    одной транзакцией, под блокировкой заявки. Повторное подтверждение (или гонка
    двух админов) начисления НЕ повторяет: сработает только переход из
    receipt_received, плюс ledger.op_key='pay:<id>' UNIQUE.
    Возвращает {'result': 'credited'|'already'|'invalid', 'payment', 'balance'}.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            p = await conn.fetchrow("SELECT * FROM payments WHERE id=$1 FOR UPDATE", payment_id)
            if p is None:
                return {"result": "invalid", "payment": None}
            if p["status"] == "confirmed":
                return {"result": "already", "payment": p}
            if p["status"] != "receipt_received":
                return {"result": "invalid", "payment": p}
            balance = await _apply_ledger(
                conn, user_id=int(p["user_id"]), delta=int(p["qty"]), kind="purchase",
                op_key=f"pay:{payment_id}", payment_id=payment_id, admin_id=admin_id,
                reason=f"Оплата заявки #{payment_id}",
            )
            p = await conn.fetchrow(
                "UPDATE payments SET status='confirmed', reviewed_by=$2, reviewed_at=now(), updated_at=now() "
                "WHERE id=$1 RETURNING *", payment_id, admin_id,
            )
            if balance is None:      # op_key уже был — начисление ранее состоялось
                return {"result": "already", "payment": p}
            return {"result": "credited", "payment": p, "balance": balance}


async def reject_payment(pool: asyncpg.Pool, payment_id: int, admin_id: int) -> tuple[str, Optional[asyncpg.Record]]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE payments SET status='rejected', reviewed_by=$2, reviewed_at=now(), updated_at=now() "
            "WHERE id=$1 AND status='receipt_received' RETURNING *", payment_id, admin_id,
        )
        if row is not None:
            return "rejected", row
        return "invalid", await conn.fetchrow("SELECT * FROM payments WHERE id=$1", payment_id)


async def reopen_payment(pool: asyncpg.Pool, payment_id: int, user_id: int) -> Optional[asyncpg.Record]:
    """После отклонения чека: снова принимать чек по той же заявке."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "UPDATE payments SET status='awaiting_receipt', updated_at=now() "
            "WHERE id=$1 AND user_id=$2 AND status='rejected' RETURNING *", payment_id, user_id,
        )


# ── Баланс: чтение и ручные операции ─────────────────────────────────────────

async def get_balance(pool: asyncpg.Pool, user_id: int) -> tuple[int, int]:
    """(balance, reserved). Доступно для публикации = balance - reserved."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance, reserved FROM user_balances WHERE user_id=$1", user_id)
    return (row["balance"], row["reserved"]) if row else (0, 0)


async def get_overview(pool: asyncpg.Pool, user_id: int, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or utcnow()
    balance, reserved = await get_balance(pool, user_id)
    async with pool.acquire() as conn:
        free_until = await _free_slot_until(conn, user_id, now=now)
        last = await _last_publication(conn, user_id)
    return {
        "balance": balance, "reserved": reserved, "available": balance - reserved,
        "free_available": free_until is None, "free_until": free_until,
        "last_pub_at": last,
        "next_at": (last + COOLDOWN) if last and now < last + COOLDOWN else None,
    }


async def get_publication_mode(pool: asyncpg.Pool, user_id: int) -> str:
    async with pool.acquire() as conn:
        mode = await conn.fetchval("SELECT publication_mode FROM users WHERE user_id=$1", user_id)
    return mode or "manual"


async def set_publication_mode(pool: asyncpg.Pool, user_id: int, mode: str) -> list[int]:
    if mode not in ("auto", "manual"):
        raise ValueError("invalid publication mode")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, publication_mode) VALUES ($1,$2) "
            "ON CONFLICT (user_id) DO UPDATE SET publication_mode=EXCLUDED.publication_mode, last_seen=now()",
            user_id, mode)
    return await auto_schedule_user(pool, user_id) if mode == "auto" else []


async def auto_schedule_user(pool: asyncpg.Pool, user_id: int) -> list[int]:
    now = utcnow(); now_local = to_local(now)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM drafts WHERE user_id=$1 AND status IN ('approved','publish_error') "
            "AND scheduled_at IS NULL ORDER BY id", user_id)
        existing = await conn.fetch(
            "SELECT fingerprint, scheduled_at FROM drafts WHERE user_id=$1 "
            "AND status IN ('approved','publish_error') AND scheduled_at IS NOT NULL", user_id)
    last_by_fp: dict[str, datetime] = {}
    for r in existing:
        try:
            dt = datetime.strptime(r["scheduled_at"], "%d.%m.%Y %H:%M"); fp = r["fingerprint"]
            if fp and (fp not in last_by_fp or dt > last_by_fp[fp]): last_by_fp[fp] = dt
        except (ValueError, TypeError): pass
    scheduled=[]; n=len(rows)
    if not n: return scheduled
    async with pool.acquire() as conn:
        for i,row in enumerate(rows):
            deadline=draft_deadline(row,now)
            if deadline<=now_local: continue
            candidate=now_local+(deadline-now_local)*((i+1)/(n+1))
            fp=row["fingerprint"]; previous=last_by_fp.get(fp) if fp else None
            if previous is not None and candidate<previous+COOLDOWN: candidate=previous+COOLDOWN
            if candidate<now_local: candidate=now_local
            if candidate>=deadline: continue
            scheduled_at=candidate.strftime("%d.%m.%Y %H:%M")
            await conn.execute(
                "UPDATE drafts SET scheduled_at=$2 WHERE id=$1 AND status IN ('approved','publish_error') "
                "AND scheduled_at IS NULL", int(row["id"]), scheduled_at)
            if fp: last_by_fp[fp]=candidate
            scheduled.append(int(row["id"]))
    return scheduled


async def manual_adjust(pool: asyncpg.Pool, admin_id: int, user_id: int, delta: int,
                        reason: str, kind: str = "manual_adjust") -> int:
    """
    Ручная операция админа: начисление (delta>0) или коррекция. Причина обязательна.
    Логируется в balance_ledger (кто, кому, сколько, тип, причина, когда).
    ValueError — пустая причина/нулевая дельта/баланс ушёл бы ниже зарезервированного.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("reason required")
    if delta == 0:
        raise ValueError("delta is zero")
    async with pool.acquire() as conn:
        async with conn.transaction():
            new_balance = await _apply_ledger(
                conn, user_id=user_id, delta=delta, kind=kind,
                op_key=f"manual:{uuid.uuid4()}", admin_id=admin_id, reason=reason,
            )
    assert new_balance is not None
    return new_balance


async def ledger_history(pool: asyncpg.Pool, user_id: int, credits: bool, limit: int = 20) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            f"SELECT * FROM balance_ledger WHERE user_id=$1 AND delta {'>' if credits else '<'} 0 "
            "ORDER BY id DESC LIMIT $2", user_id, limit,
        )


async def user_payments(pool: asyncpg.Pool, user_id: int, limit: int = 10) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM payments WHERE user_id=$1 ORDER BY id DESC LIMIT $2", user_id, limit)


async def queue_ready(pool: asyncpg.Pool, limit: int = 20) -> list[asyncpg.Record]:
    """Админская очередь: одобренные объявления, готовые к ручной публикации."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM drafts WHERE status = ANY($2::text[]) ORDER BY id LIMIT $1",
            limit, list(RETRIABLE_STATUSES),
        )


async def queue_pending(pool: asyncpg.Pool, limit: int = 20) -> list[asyncpg.Record]:
    """Объявления на модерации (ещё не одобрены) — для /queue после обновления/потери сообщений."""
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM drafts WHERE status='pending' ORDER BY id LIMIT $1", limit)