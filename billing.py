"""
Биллинг: баланс публикаций, платежи переводом на карту, атомарная публикация.

Модуль зависит только от asyncpg: вся работа с деньгами и статусами выполняется здесь, в
транзакциях, а main.py лишь вызывает эти функции из хендлеров (тесты: tests/test_billing.py).

Что хранится отдельно
  • Бесплатное место — не флаг, а вычисляется: занято, пока есть опубликованное бесплатное
    объявление, срок которого не истёк (_free_slot_until).
  • Баланс — user_balances: balance (куплено и не потрачено) и reserved (публикации, которые
    прямо сейчас уходят в Telegram). Доступно = balance - reserved.
  • Объявление — drafts.status: hold → pending → publishing → published; publish_error
    (публикацию можно повторить); rejected | cancelled. drafts.funding — free | paid.
  • Платёж — payments.status: awaiting_receipt → receipt_received → confirmed | rejected;
    cancelled — заявку закрыл пользователь.
  • История — balance_ledger: только добавление записей; op_key UNIQUE делает операции идемпотентными.

Конкурентность: мьютекс пользователя — строка user_balances FOR UPDATE. Порядок блокировок
везде один: платёж/объявление → баланс. Публикация двухфазная: claim_publication (проверки и
захват) → отправка в Telegram → finalize_publication (списание) либо release_publication.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)

PACKAGES: dict[int, int] = {2: 100, 5: 200}     # количество публикаций → цена, ₽

# hold — форма принята, но доступных публикаций нет: админу объявление не отправляется
# и ждёт покупки пакета.
HOLD = "hold"
OPEN_STATUSES = ("pending", "publishing", "publish_error")
EDITABLE_STATUSES = (HOLD, "pending", "publish_error")      # можно отклонить или отменить
RETRIABLE_STATUSES = ("pending", "publish_error")           # можно публиковать

TZ_OFFSET = 3


def configure(tz_offset: int) -> None:
    global TZ_OFFSET
    TZ_OFFSET = tz_offset


class PendingDuplicate(Exception):
    """Такое же объявление уже ждёт модерации или покупки публикаций."""

    def __init__(self, draft_id: int):
        super().__init__(f"pending duplicate exists: {draft_id}")
        self.draft_id = draft_id


def make_fingerprint(ad_type: str, cities: list[str], travel_date: str,
                     phone: str, custom_username: Optional[str]) -> str:
    """Отпечаток объявления для поиска дубликатов."""
    route = json.dumps(cities, ensure_ascii=False)
    norm_phone = re.sub(r"[\s\-()]", "", phone or "")
    norm_username = (custom_username or "").casefold().strip().lstrip("@")
    raw = "|".join((ad_type or "carrier", route, str(travel_date or ""), norm_phone, norm_username))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# ── Время ────────────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_local(dt: datetime) -> datetime:
    """aware UTC → naive местное время (как now_local() в main.py)."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(hours=TZ_OFFSET)


def local_to_utc(dt: datetime) -> datetime:
    return (dt - timedelta(hours=TZ_OFFSET)).replace(tzinfo=timezone.utc)


def fmt_local(dt: Optional[datetime]) -> str:
    return to_local(dt).strftime("%d.%m.%Y %H:%M") if dt else "—"


def parse_trip_date(value: Any) -> Optional[date]:
    try:
        return datetime.strptime(str(value), "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return None


def seeker_days(row: Any) -> int:
    m = re.search(r"\d+", str(row["travel_date"] or ""))
    return int(m.group()) if m else 30


def trip_passed(row: Any, now: Optional[datetime] = None) -> bool:
    """Дата поездки уже в прошлом? У объявлений «ищу посылку» фиксированной даты нет."""
    if (row["ad_type"] or "carrier") == "seeker":
        return False
    d = parse_trip_date(row["travel_date"])
    return d is not None and d < to_local(now or utcnow()).date()


def draft_deadline(row: Any, now: Optional[datetime] = None) -> Optional[datetime]:
    """
    Местное время, до которого объявление актуально: для перевозчика — конец дня поездки,
    для «ищу посылку» — момент публикации плюс N дней из «В ближайшие N дней».
    """
    now = now or utcnow()
    base = to_local(row["published_at"] or now)
    if (row["ad_type"] or "carrier") == "seeker":
        return base + timedelta(days=seeker_days(row))
    d = parse_trip_date(row["travel_date"])
    if d is None:
        return base + timedelta(days=30)
    return datetime.combine(d + timedelta(days=1), time.min)


# ── Схема БД и миграция ──────────────────────────────────────────────────────

async def init_schema(pool: asyncpg.Pool) -> None:
    """Идемпотентно создаёт и дополняет таблицы. Вызывать после создания drafts."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Параллельный запуск нескольких экземпляров бота выполняет миграцию по очереди.
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

            # Строки без status созданы до появления биллинга. Опубликованные занимают бесплатное
            # место до истечения срока (иначе апдейт выдал бы вторую бесплатную публикацию
            # поверх активной); неопубликованные старше 7 дней считаем отклонёнными, потому что
            # раньше «отклонено» нигде не хранилось.
            await conn.execute("""
                UPDATE drafts SET status='published', funding='free',
                       published_at=COALESCE(published_at, created_at)
                WHERE status IS NULL AND published=TRUE
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
            # Шаг «Одобрить» убран: ранее одобренные объявления ждут публикации как pending.
            await conn.execute("UPDATE drafts SET status='pending' WHERE status='approved'")

            # Первая версия биллинга ограничивала пользователя одним открытым объявлением.
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


# ── Внутренние помощники (вызываются внутри транзакции) ──────────────────────

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
    None — бесплатное место свободно; иначе местное время, когда оно освободится.
    Место занимает только активное ОПУБЛИКОВАННОЕ бесплатное объявление; заявки на модерации,
    ожидающие оплаты, отклонённые и отменённые место не занимают.
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


async def _apply_ledger(conn: asyncpg.Connection, *, user_id: int, delta: int, kind: str,
                        op_key: str, payment_id: Optional[int] = None,
                        draft_id: Optional[int] = None, admin_id: Optional[int] = None,
                        reason: Optional[str] = None, consume_reserved: bool = False) -> Optional[int]:
    """
    Атомарно (под блокировкой баланса) применяет операцию и пишет её в историю.
    Возвращает новый баланс либо None, если операция с таким op_key уже выполнялась.
    ValueError — если баланс ушёл бы ниже нуля или ниже зарезервированного.
    """
    bal = await _lock_balance(conn, user_id)
    new_balance = bal["balance"] + delta
    new_reserved = max(0, bal["reserved"] - (1 if consume_reserved else 0))
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


async def _allocation(conn: asyncpg.Connection, uid: int,
                      exclude_draft_id: Optional[int] = None) -> tuple[bool, int]:
    """
    Что у пользователя ещё не закреплено за другими объявлениями (вызывать под блокировкой
    баланса): (бесплатное место свободно, число свободных платных публикаций).
    Каждое объявление в процессе занимает одну публикацию — бесплатную (funding='free')
    или платную (funding='paid').
    """
    bal = await _lock_balance(conn, uid)
    held_paid = await conn.fetchval(
        "SELECT count(*) FROM drafts WHERE user_id=$1 AND funding='paid' "
        "AND status IN ('pending','publish_error') AND ($2::int IS NULL OR id<>$2)",
        uid, exclude_draft_id)
    free_open = await conn.fetchval(
        "SELECT 1 FROM drafts WHERE user_id=$1 AND funding='free' "
        "AND status IN ('pending','publishing','publish_error') "
        "AND ($2::int IS NULL OR id<>$2) LIMIT 1", uid, exclude_draft_id)
    free_until = await _free_slot_until(conn, uid, exclude_draft_id=exclude_draft_id)
    free_ok = free_open is None and free_until is None
    return free_ok, max(0, bal["balance"] - bal["reserved"] - int(held_paid))


# ── Объявления: создание и статусы ───────────────────────────────────────────

async def create_draft(pool: asyncpg.Pool, d: dict[str, Any]) -> int:
    """
    Принимает объявление по обычной форме. Есть доступная публикация (свободное бесплатное
    место или платная на балансе) — status='pending', объявление уйдёт админу. Нет —
    status='hold': оно сохранено, но админу не отправляется, пока пользователь не купит пакет.
    PendingDuplicate — такое же объявление уже ждёт модерации или покупки.
    """
    cities = d["cities"]
    uid = int(d["user_id"])
    ad_type = d.get("ad_type", "carrier")
    travel_date = d["travel_date"]
    phone = d.get("phone", "")
    custom_username = d.get("custom_username")
    fingerprint = make_fingerprint(ad_type, cities, travel_date, phone, custom_username)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_balance(conn, uid)
            existing = await conn.fetchval(
                "SELECT id FROM drafts WHERE user_id=$1 AND fingerprint=$2 AND status IN ('pending','hold') LIMIT 1",
                uid, fingerprint)
            if existing is not None:
                raise PendingDuplicate(int(existing))
            free_ok, paid_free = await _allocation(conn, uid)
            if free_ok:
                funding, status = "free", "pending"
            elif paid_free >= 1:
                funding, status = "paid", "pending"
            else:
                funding, status = "paid", HOLD
            row = await conn.fetchrow(
                """INSERT INTO drafts
                   (user_id,tg_username,custom_username,full_name,origin,destination,route,
                    travel_date,cargo,phone,ad_type,status,funding,fingerprint,status_changed_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,now())
                   RETURNING id""",
                uid, d.get("tg_username"), custom_username, d["full_name"], cities[0], cities[-1],
                json.dumps(cities, ensure_ascii=False), travel_date, d["cargo"], phone, ad_type,
                status, funding, fingerprint)
            return int(row["id"])


async def release_holds(pool: asyncpg.Pool, user_id: int) -> list[asyncpg.Record]:
    """
    После пополнения баланса: объявления из hold по порядку переходят в pending (то есть
    уходят на модерацию), пока хватает публикаций. Возвращает перешедшие объявления.
    """
    released: list[asyncpg.Record] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_balance(conn, user_id)
            held = await conn.fetch(
                "SELECT id FROM drafts WHERE user_id=$1 AND status='hold' ORDER BY id FOR UPDATE", user_id)
            for h in held:
                free_ok, paid_free = await _allocation(conn, user_id, exclude_draft_id=int(h["id"]))
                if free_ok:
                    funding = "free"
                elif paid_free >= 1:
                    funding = "paid"
                else:
                    break
                released.append(await conn.fetchrow(
                    "UPDATE drafts SET status='pending', funding=$2, status_changed_at=now() "
                    "WHERE id=$1 RETURNING *", int(h["id"]), funding))
    return released


async def get_context_draft(pool: asyncpg.Pool, user_id: int) -> Optional[asyncpg.Record]:
    """Объявление, ради которого пользователь покупает публикации: ожидающее (hold), иначе последнее в процессе."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM drafts WHERE user_id=$1 AND status = ANY($2::text[]) "
            "ORDER BY (status='hold') DESC, id DESC LIMIT 1",
            user_id, [HOLD, *OPEN_STATUSES])


async def get_open_drafts(pool: asyncpg.Pool, user_id: int, limit: int = 50) -> list[asyncpg.Record]:
    """Объявления пользователя, которые ещё не опубликованы и не закрыты (включая hold)."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM drafts WHERE user_id=$1 AND status = ANY($2::text[]) ORDER BY id DESC LIMIT $3",
            user_id, [HOLD, *OPEN_STATUSES], limit)


async def _transition(pool: asyncpg.Pool, draft_id: int, new_status: str,
                      user_id: Optional[int] = None) -> Optional[asyncpg.Record]:
    """Атомарно закрывает объявление: при гонке или повторе сработает ровно один раз."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            UPDATE drafts SET status=$2, status_changed_at=now(), scheduled_at=NULL
            WHERE id=$1 AND status = ANY($3::text[]) AND ($4::bigint IS NULL OR user_id=$4)
            RETURNING *
            """,
            draft_id, new_status, list(EDITABLE_STATUSES), user_id,
        )


async def reject_draft(pool: asyncpg.Pool, draft_id: int) -> Optional[asyncpg.Record]:
    return await _transition(pool, draft_id, "rejected")


async def cancel_draft(pool: asyncpg.Pool, draft_id: int,
                       user_id: Optional[int] = None) -> Optional[asyncpg.Record]:
    """Отмена объявления. С user_id — только если оно принадлежит этому пользователю."""
    return await _transition(pool, draft_id, "cancelled", user_id)


# ── Публикация: claim → (Telegram) → finalize / release ──────────────────────

@dataclass
class ClaimResult:
    ok: bool
    code: str
    draft: Optional[asyncpg.Record] = None


async def claim_publication(pool: asyncpg.Pool, draft_id: int,
                            now: Optional[datetime] = None) -> ClaimResult:
    """
    Фаза 1. В одной транзакции, под блокировкой объявления и баланса, проверяет все условия
    и переводит объявление в 'publishing'. Параллельный второй claim получит 'in_progress'.
    Коды отказа: not_found, on_hold, in_progress, already_published, rejected, cancelled,
    trip_passed, no_free, no_balance.
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
                return ClaimResult(False, "on_hold", d)
            if trip_passed(d, now):
                return ClaimResult(False, "trip_passed", d)
            if d["funding"] == "free":
                if await _free_slot_until(conn, uid, exclude_draft_id=draft_id, now=now) is not None:
                    return ClaimResult(False, "no_free", d)
            elif bal["balance"] - bal["reserved"] < 1:
                return ClaimResult(False, "no_balance", d)

            fresh = await conn.fetchrow(
                "UPDATE drafts SET status='publishing', publishing_started_at=$2, "
                "status_changed_at=$2, last_error=NULL WHERE id=$1 RETURNING *",
                draft_id, now,
            )
            if d["funding"] == "paid":
                await conn.execute(
                    "UPDATE user_balances SET reserved=reserved+1, updated_at=now() WHERE user_id=$1", uid
                )
            return ClaimResult(True, "ok", fresh)


async def finalize_publication(pool: asyncpg.Pool, draft_id: int, channel_msg_id: int,
                               now: Optional[datetime] = None) -> dict[str, Any]:
    """
    Фаза 2, после подтверждённого размещения в канале. Атомарно помечает объявление
    опубликованным, фиксирует время и для платного списывает ровно 1 публикацию (op_key
    spend:<id>). Повторный вызов безопасен.
    Возвращает {'result': 'published'|'published_unbilled'|'already'|'invalid_status'|'not_found',
    'funding', 'balance'}.
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
    Публикация не удалась (ошибка Telegram и т.п.): 'publishing' → 'publish_error', резерв
    снимается, ничего не списывается, повтор разрешён.
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
    Вызывается при старте: всё, что осталось в 'publishing' (бот упал между claim и finalize),
    переводится в 'publish_error', резерв снимается. Списания не было. Админ должен проверить
    канал вручную — пост мог успеть уйти — и только потом повторять публикацию.
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


async def publication_queue(pool: asyncpg.Pool, limit: int = 20) -> list[asyncpg.Record]:
    """Админская очередь: объявления, ожидающие решения или повторной публикации."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM drafts WHERE status = ANY($2::text[]) ORDER BY id LIMIT $1",
            limit, list(RETRIABLE_STATUSES),
        )


# ── Платежи ──────────────────────────────────────────────────────────────────

async def create_payment(pool: asyncpg.Pool, user_id: int, qty: int,
                         draft_id: Optional[int] = None) -> tuple[asyncpg.Record, bool]:
    """
    Создаёт заявку на оплату. Повторное нажатие «Купить» (тот же пакет, чек ещё не отправлен)
    возвращает уже существующую заявку. Возвращает (заявка, создана_новая).
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
                         file_id: str, chat_id: int, message_id: int) -> Optional[asyncpg.Record]:
    """
    Привязывает чек к заявке; первый чек переводит её в receipt_received.
    Возвращает заявку либо None, если принимать чеки по ней уже нельзя.
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
            if p["status"] == "awaiting_receipt":
                p = await conn.fetchrow(
                    "UPDATE payments SET status='receipt_received', updated_at=now() WHERE id=$1 RETURNING *",
                    payment_id,
                )
            return p


async def confirm_payment(pool: asyncpg.Pool, payment_id: int, admin_id: int) -> dict[str, Any]:
    """
    Подтверждение оплаты админом: статус → confirmed и начисление qty публикаций одной
    транзакцией. Повторное подтверждение (или гонка двух админов) не начисляет повторно:
    срабатывает только переход из receipt_received, а ledger.op_key pay:<id> уникален.
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
            if balance is None:
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


async def cancel_payment(pool: asyncpg.Pool, payment_id: int, user_id: int) -> tuple[str, Optional[asyncpg.Record]]:
    """
    Пользователь закрывает заявку на оплату: можно, пока чек не ушёл на проверку (awaiting_receipt)
    или после его отклонения (rejected). Возвращает (код, заявка):
    'cancelled' | 'under_review' | 'confirmed' | 'already' | 'not_found'.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            p = await conn.fetchrow("SELECT * FROM payments WHERE id=$1 AND user_id=$2 FOR UPDATE", payment_id, user_id)
            if p is None:
                return "not_found", None
            st = p["status"]
            if st in ("awaiting_receipt", "rejected"):
                p = await conn.fetchrow(
                    "UPDATE payments SET status='cancelled', updated_at=now() WHERE id=$1 RETURNING *", payment_id)
                return "cancelled", p
            return {"receipt_received": "under_review", "confirmed": "confirmed"}.get(st, "already"), p


# ── Баланс: чтение и ручные операции ─────────────────────────────────────────

async def get_balance(pool: asyncpg.Pool, user_id: int) -> tuple[int, int]:
    """(balance, reserved). Доступно для публикации = balance - reserved."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance, reserved FROM user_balances WHERE user_id=$1", user_id)
    return (row["balance"], row["reserved"]) if row else (0, 0)


async def get_overview(pool: asyncpg.Pool, user_id: int, now: Optional[datetime] = None) -> dict[str, Any]:
    balance, reserved = await get_balance(pool, user_id)
    async with pool.acquire() as conn:
        free_until = await _free_slot_until(conn, user_id, now=now)
    return {
        "balance": balance, "reserved": reserved, "available": balance - reserved,
        "free_available": free_until is None, "free_until": free_until,
    }


async def manual_adjust(pool: asyncpg.Pool, admin_id: int, user_id: int, delta: int,
                        reason: str, kind: str = "manual_adjust") -> int:
    """
    Ручная операция админа: начисление (delta > 0) или коррекция. Причина обязательна.
    Пишется в balance_ledger: кто, кому, сколько, тип, причина, когда.
    ValueError — пустая причина, нулевая дельта или баланс ушёл бы ниже зарезервированного.
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
