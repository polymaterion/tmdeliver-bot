"""
Интеграционные тесты на настоящем PostgreSQL (pgserver), Telegram имитируется.
Запуск: pip install -r requirements-dev.txt && pytest -q tests
"""
import asyncio
import os
import sys
import tempfile
from datetime import timedelta

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pgserver  # noqa: E402

_pg = pgserver.get_server(tempfile.mkdtemp(prefix="pgtest_"))
os.environ.update(
    BOT_TOKEN="123:TEST", ADMIN_IDS="1", CHANNEL_ID="@test", CARD_NUMBER="0000 1111 2222 3333",
    DATABASE_URL=_pg.get_uri(),
)
import main  # noqa: E402
import billing  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="module")


# ── Имитация Telegram ────────────────────────────────────────────────────────

class Msg:
    def __init__(self, i):
        self.message_id = i


class FakeBot:
    def __init__(self):
        self.fail = False
        self.log = []                 # dict(kind, chat_id, text, markup)
        self.channel_posts = 0
        self.delay = 0.0
        self._id = 100

    async def send_message(self, chat_id, text, **kw):
        if str(chat_id) == main.CHANNEL_ID:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("telegram down")
            self.channel_posts += 1
        self._id += 1
        self.log.append(dict(kind="send", chat_id=chat_id, text=text, markup=kw.get("reply_markup")))
        return Msg(self._id)

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.log.append(dict(kind="edit", chat_id=chat_id, text=text, markup=kw.get("reply_markup")))
        return Msg(message_id)

    async def delete_message(self, *a, **k):
        return True

    def to(self, chat_id):
        return [m for m in self.log if m["chat_id"] == chat_id]

    @staticmethod
    def buttons(m):
        mk = m["markup"]
        return [b.callback_data for row in (mk.inline_keyboard if mk else []) for b in row]


class FakeUser:
    def __init__(self, uid, name="Тест", username="tester"):
        self.id, self.full_name, self.username = uid, name, username


class FakeChat:
    def __init__(self, i):
        self.id = i


class FakeMsg:
    def __init__(self, uid, mid=500):
        self.chat, self.message_id, self.replies = FakeChat(uid), mid, []

    async def edit_reply_markup(self, **kw):
        return True

    async def reply(self, text, **kw):
        self.replies.append(text)


class FakeCB:
    def __init__(self, uid, data, mid=500):
        self.data, self.from_user = data, FakeUser(uid)
        self.message, self.answers = FakeMsg(uid, mid), []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeState:
    def __init__(self, data=None):
        self.d = dict(data or {})

    async def get_data(self):
        return dict(self.d)

    async def update_data(self, **kw):
        self.d.update(kw)

    async def clear(self):
        self.d.clear()

    async def set_state(self, *a):
        pass

    async def get_state(self):
        return None


# ── Вспомогательное ──────────────────────────────────────────────────────────

def future(days):
    return (billing.to_local(billing.utcnow()) + timedelta(days=days)).strftime("%d.%m.%Y")


def past(days):
    return future(-days)


_uid = [1000]


def new_uid():
    _uid[0] += 1
    return _uid[0]


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def db():
    await main.init_db()
    yield main.pool()
    await main.pool().close()


async def mk(uid, date=None, ad_type="carrier", phone="+79990000000", force_pending=True):
    """
    Создаёт объявление. Без доступных публикаций оно получает статус hold; force_pending=True
    переводит такое объявление в pending, чтобы проверять правила публикации на уровне claim.
    """
    d = await billing.create_draft(main.pool(), dict(
        user_id=uid, full_name="U", cities=["Москва", "Ашхабад"], travel_date=date or future(10),
        cargo="docs", phone=phone, ad_type=ad_type))
    if force_pending:
        await main.pool().execute("UPDATE drafts SET status='pending' WHERE id=$1 AND status='hold'", d)
    return d


async def credit(uid, n, reason="test"):
    return await billing.manual_adjust(main.pool(), 1, uid, n, reason)


async def occupy_free(uid):
    """Пользователь уже использовал бесплатную публикацию: она активна."""
    await publish(FakeBot(), await mk(uid, date=future(30), phone="+70000000000"))


async def publish(bot, draft_id):
    return await main._execute_publication(bot, draft_id)


async def status_of(draft_id):
    return (await main.get_draft(draft_id))["status"]


def form(**over):
    d = dict(ad_type="carrier", cities=["Москва", "Ашхабад"], travel_date=future(10),
             cargo="docs", phone="+79990000001", custom_username=None, bot_msg_id=500)
    d.update(over)
    return d


async def last_draft_id(uid):
    return (await main.pool().fetchrow("SELECT id FROM drafts WHERE user_id=$1 ORDER BY id DESC LIMIT 1", uid))["id"]


# ── Бесплатная публикация ────────────────────────────────────────────────────

async def test_new_user_has_free_slot():
    ov = await billing.get_overview(main.pool(), new_uid())
    assert ov["free_available"] and ov["available"] == 0


async def test_free_publication_takes_slot_and_does_not_touch_balance():
    uid, bot = new_uid(), FakeBot()
    await credit(uid, 3)
    d = await mk(uid)
    assert (await main.get_draft(d))["funding"] == "free"
    res = await publish(bot, d)
    assert res["status"] == "published" and res["fin"]["funding"] == "free"
    assert await billing.get_balance(main.pool(), uid) == (3, 0)
    assert not (await billing.get_overview(main.pool(), uid))["free_available"]
    assert (await main.get_draft(await mk(uid, phone="+79990000555")))["funding"] == "paid"


async def test_free_slot_returns_after_expiry():
    uid = new_uid()
    d = await mk(uid, date=future(1))
    await publish(FakeBot(), d)
    assert not (await billing.get_overview(main.pool(), uid))["free_available"]
    await main.pool().execute("UPDATE drafts SET travel_date=$2 WHERE id=$1", d, past(1))
    assert (await billing.get_overview(main.pool(), uid))["free_available"]
    await main.pool().execute("UPDATE drafts SET travel_date=$2, expired=TRUE WHERE id=$1", d, future(1))
    assert (await billing.get_overview(main.pool(), uid))["free_available"]
    assert (await main.get_draft(await mk(uid, phone="+79990000556")))["funding"] == "free"


async def test_restart_does_not_regrant_free_publication():
    uid = new_uid()
    await publish(FakeBot(), await mk(uid))
    await billing.init_schema(main.pool())
    await billing.init_schema(main.pool())
    assert not (await billing.get_overview(main.pool(), uid))["free_available"]


async def test_rejected_and_cancelled_do_not_take_free_slot():
    uid = new_uid()
    d1 = await mk(uid, phone="+79990000601")
    await billing.reject_draft(main.pool(), d1)
    d2 = await mk(uid, phone="+79990000602")
    await billing.cancel_draft(main.pool(), d2, uid)
    assert (await billing.get_overview(main.pool(), uid))["free_available"]


async def test_seeker_deadline_frees_slot():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid, date="В ближайшие 10 дней", ad_type="seeker")
    assert (await publish(bot, d))["status"] == "published"
    assert not (await billing.get_overview(main.pool(), uid))["free_available"]
    await main.pool().execute("UPDATE drafts SET published_at=now()-interval '11 days' WHERE id=$1", d)
    assert (await billing.get_overview(main.pool(), uid))["free_available"]


# ── Миграция ─────────────────────────────────────────────────────────────────

async def test_migration_published_legacy_row_occupies_free_slot():
    uid = new_uid()
    await main.pool().execute("ALTER TABLE drafts ALTER COLUMN status DROP NOT NULL")
    await main.pool().execute("ALTER TABLE drafts ALTER COLUMN funding DROP NOT NULL")
    await main.pool().execute("ALTER TABLE drafts ALTER COLUMN status DROP DEFAULT")
    await main.pool().execute(
        "INSERT INTO drafts (user_id, full_name, origin, destination, route, travel_date, cargo, phone, published) "
        "VALUES ($1,'U','A','B','[]',$2,'docs','1',TRUE)", uid, future(5))
    await billing.init_schema(main.pool())
    row = await main.pool().fetchrow("SELECT status, funding, published_at FROM drafts WHERE user_id=$1", uid)
    assert row["status"] == "published" and row["funding"] == "free" and row["published_at"]
    assert not (await billing.get_overview(main.pool(), uid))["free_available"]


async def test_migration_turns_approved_into_pending():
    uid = new_uid()
    d = await mk(uid)
    await main.pool().execute("UPDATE drafts SET status='approved' WHERE id=$1", d)
    await billing.init_schema(main.pool())
    assert await status_of(d) == "pending"


# ── Несколько объявлений и дубликаты ─────────────────────────────────────────

async def test_different_ads_allowed_identical_pending_blocked():
    uid = new_uid()
    await credit(uid, 2)
    d1 = await mk(uid, phone="+79990000701")
    d2 = await mk(uid, phone="+79990000702")
    assert d1 != d2
    with pytest.raises(billing.PendingDuplicate) as ei:
        await mk(uid, phone="+79990000701")
    assert ei.value.draft_id == d1


async def test_concurrent_identical_submissions_create_one_draft():
    uid = new_uid()
    res = await asyncio.gather(*[mk(uid, phone="+79990000703") for _ in range(8)], return_exceptions=True)
    assert sum(isinstance(r, int) for r in res) == 1
    assert sum(isinstance(r, billing.PendingDuplicate) for r in res) == 7


async def test_identical_ad_after_rejection_is_allowed():
    uid = new_uid()
    d = await mk(uid, phone="+79990000704")
    await billing.reject_draft(main.pool(), d)
    assert await mk(uid, phone="+79990000704") != d


# ── Публикация ───────────────────────────────────────────────────────────────

async def test_pending_ad_is_published_by_admin_without_approval_step():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid)
    row = await main.get_draft(d)
    assert row["status"] == "pending" and not row["published"]
    assert bot.channel_posts == 0
    assert (await publish(bot, d))["status"] == "published"
    assert bot.channel_posts == 1


async def test_hold_ad_cannot_be_published():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    d = await mk(uid, phone="+79990000801", force_pending=False)
    assert await status_of(d) == "hold"
    res = await publish(bot, d)
    assert res["claim"].code == "on_hold" and bot.channel_posts == 0


async def test_same_ad_can_be_published_again_without_waiting():
    uid, bot = new_uid(), FakeBot()
    await credit(uid, 2)
    d1 = await mk(uid)
    assert (await publish(bot, d1))["status"] == "published"
    await main.pool().execute("UPDATE drafts SET status='rejected' WHERE id=$1", d1)   # снимаем дубликат
    d2 = await mk(uid)
    assert (await publish(bot, d2))["status"] == "published"
    assert bot.channel_posts == 2


async def test_paid_publication_charges_exactly_one():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    await credit(uid, 5)
    d = await mk(uid, phone="+79990000802")
    assert (await main.get_draft(d))["funding"] == "paid"
    res = await publish(bot, d)
    assert res["status"] == "published" and res["fin"]["balance"] == 4
    assert await billing.get_balance(main.pool(), uid) == (4, 0)
    spends = await main.pool().fetch("SELECT * FROM balance_ledger WHERE user_id=$1 AND kind='spend'", uid)
    assert len(spends) == 1 and spends[0]["delta"] == -1 and spends[0]["draft_id"] == d


async def test_balance_of_5_is_spent_one_per_published_ad():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    await credit(uid, 5)
    ids = [await mk(uid, phone=f"+7999000090{i}") for i in range(3)]
    for d in ids:
        assert (await publish(bot, d))["status"] == "published"
    assert await billing.get_balance(main.pool(), uid) == (2, 0)


async def test_telegram_error_charges_nothing_and_can_be_retried():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    await credit(uid, 2)
    d = await mk(uid, phone="+79990000803")
    bot.fail = True
    res = await publish(bot, d)
    assert res["status"] == "telegram_error"
    row = await main.get_draft(d)
    assert row["status"] == "publish_error" and not row["published"] and row["published_at"] is None
    assert await billing.get_balance(main.pool(), uid) == (2, 0)
    bot.fail = False
    assert (await publish(bot, d))["status"] == "published"
    assert await billing.get_balance(main.pool(), uid) == (1, 0)


async def test_double_click_and_two_admins_single_post_single_charge():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    await credit(uid, 3)
    d = await mk(uid, phone="+79990000804")
    bot.delay = 0.3
    before = bot.channel_posts
    results = await asyncio.gather(*[publish(bot, d) for _ in range(6)])
    assert [r["status"] for r in results].count("published") == 1
    assert bot.channel_posts == before + 1
    assert {r["claim"].code for r in results if r["status"] == "claim_failed"} <= {"in_progress", "already_published"}
    assert await billing.get_balance(main.pool(), uid) == (2, 0)
    assert await main.pool().fetchval("SELECT count(*) FROM balance_ledger WHERE op_key=$1", f"spend:{d}") == 1


async def test_reserve_protects_balance_while_publishing():
    uid = new_uid()
    await credit(uid, 1)
    d = await mk(uid, phone="+79990000805")
    await main.pool().execute("UPDATE drafts SET funding='paid' WHERE id=$1", d)
    claim = await billing.claim_publication(main.pool(), d)
    assert claim.ok and await billing.get_balance(main.pool(), uid) == (1, 1)
    with pytest.raises(ValueError):
        await billing.manual_adjust(main.pool(), 1, uid, -1, "ниже резерва")
    await billing.release_publication(main.pool(), d, "x")
    assert await billing.get_balance(main.pool(), uid) == (1, 0)


async def test_restart_during_publishing_recovers_without_charge():
    uid = new_uid()
    await credit(uid, 1)
    d = await mk(uid, phone="+79990000806")
    await main.pool().execute("UPDATE drafts SET funding='paid' WHERE id=$1", d)
    assert (await billing.claim_publication(main.pool(), d)).ok
    assert await billing.recover_stuck_publications(main.pool()) == [d]
    row = await main.get_draft(d)
    assert row["status"] == "publish_error" and not row["published"]
    assert await billing.get_balance(main.pool(), uid) == (1, 0)


async def test_paid_ad_without_balance_cannot_be_published():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    d = await mk(uid, phone="+79990000807")
    res = await publish(bot, d)
    assert res["claim"].code == "no_balance" and await status_of(d) == "pending"


async def test_free_ad_cannot_be_published_while_free_slot_is_busy():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    d = await mk(uid, phone="+79990000808")
    await main.pool().execute("UPDATE drafts SET funding='free' WHERE id=$1", d)
    assert (await publish(bot, d))["claim"].code == "no_free"


async def test_closed_ads_cannot_be_published_and_published_not_twice():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid, phone="+79990000809")
    await billing.cancel_draft(main.pool(), d, uid)
    assert (await publish(bot, d))["claim"].code == "cancelled"
    d = await mk(uid, phone="+79990000810")
    await billing.reject_draft(main.pool(), d)
    assert (await publish(bot, d))["claim"].code == "rejected"
    d = await mk(uid, phone="+79990000811")
    assert (await publish(bot, d))["status"] == "published"
    assert (await publish(bot, d))["claim"].code == "already_published"
    assert bot.channel_posts == 1


async def test_trip_date_in_past_blocks_publication():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid, date=past(1))
    res = await publish(bot, d)
    assert res["claim"].code == "trip_passed" and bot.channel_posts == 0
    assert await status_of(d) == "pending"


async def test_publish_handler_cancels_ad_with_past_date_and_tells_user():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid, date=past(2))
    cb = FakeCB(1, f"publish:{d}")
    await main.publish_draft(cb, bot)
    assert await status_of(d) == "cancelled"
    assert any("дата поездки" in m["text"].lower() for m in bot.to(uid))
    assert bot.channel_posts == 0


# ── Оплата ───────────────────────────────────────────────────────────────────

async def test_payment_credits_once_and_never_publishes():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid)
    p, created = await billing.create_payment(main.pool(), uid, 5, d)
    assert created and p["amount"] == 200
    again, created_again = await billing.create_payment(main.pool(), uid, 5, d)
    assert not created_again and again["id"] == p["id"]
    assert (await billing.confirm_payment(main.pool(), p["id"], 1))["result"] == "invalid"
    assert await billing.attach_receipt(main.pool(), p["id"], uid, "photo", "F", 1, 1)
    assert await billing.get_balance(main.pool(), uid) == (0, 0)
    first = await billing.confirm_payment(main.pool(), p["id"], 1)
    assert first["result"] == "credited" and first["balance"] == 5
    assert (await billing.confirm_payment(main.pool(), p["id"], 2))["result"] == "already"
    assert await billing.get_balance(main.pool(), uid) == (5, 0)
    assert await status_of(d) == "pending" and bot.channel_posts == 0


async def test_concurrent_confirmation_credits_once():
    uid = new_uid()
    p, _ = await billing.create_payment(main.pool(), uid, 2)
    await billing.attach_receipt(main.pool(), p["id"], uid, "photo", "F", 1, 1)
    res = await asyncio.gather(*[billing.confirm_payment(main.pool(), p["id"], a) for a in range(1, 9)])
    assert [r["result"] for r in res].count("credited") == 1
    assert await billing.get_balance(main.pool(), uid) == (2, 0)
    assert await main.pool().fetchval("SELECT count(*) FROM balance_ledger WHERE op_key=$1", f"pay:{p['id']}") == 1


async def test_rejected_receipt_can_be_resent():
    uid = new_uid()
    p, _ = await billing.create_payment(main.pool(), uid, 2)
    await billing.attach_receipt(main.pool(), p["id"], uid, "document", "D", 1, 2)
    status, _ = await billing.reject_payment(main.pool(), p["id"], 1)
    assert status == "rejected" and await billing.get_balance(main.pool(), uid) == (0, 0)
    assert (await billing.confirm_payment(main.pool(), p["id"], 1))["result"] == "invalid"
    assert await billing.reopen_payment(main.pool(), p["id"], uid)
    assert (await billing.find_receipt_target(main.pool(), uid))["status"] == "awaiting_receipt"
    await billing.attach_receipt(main.pool(), p["id"], uid, "photo", "F2", 1, 3)
    assert (await billing.confirm_payment(main.pool(), p["id"], 1))["result"] == "credited"


async def test_cancel_payment_closes_request():
    uid = new_uid()
    p, _ = await billing.create_payment(main.pool(), uid, 2)
    code, row = await billing.cancel_payment(main.pool(), p["id"], uid)
    assert code == "cancelled" and row["status"] == "cancelled"
    assert await billing.find_receipt_target(main.pool(), uid) is None
    assert await billing.attach_receipt(main.pool(), p["id"], uid, "photo", "F", 1, 1) is None
    assert (await billing.confirm_payment(main.pool(), p["id"], 1))["result"] == "invalid"
    assert (await billing.cancel_payment(main.pool(), p["id"], uid))[0] == "already"
    assert (await billing.cancel_payment(main.pool(), p["id"], uid + 1))[0] == "not_found"
    p2, created = await billing.create_payment(main.pool(), uid, 2)
    assert created and p2["id"] != p["id"]
    await billing.attach_receipt(main.pool(), p2["id"], uid, "photo", "F", 1, 2)
    assert (await billing.cancel_payment(main.pool(), p2["id"], uid))[0] == "under_review"
    await billing.reject_payment(main.pool(), p2["id"], 1)
    assert (await billing.cancel_payment(main.pool(), p2["id"], uid))[0] == "cancelled"


async def test_rejection_after_payment_keeps_balance():
    uid = new_uid()
    d = await mk(uid)
    p, _ = await billing.create_payment(main.pool(), uid, 2, d)
    await billing.attach_receipt(main.pool(), p["id"], uid, "photo", "F", 1, 1)
    await billing.confirm_payment(main.pool(), p["id"], 1)
    assert await billing.reject_draft(main.pool(), d)
    assert await billing.get_balance(main.pool(), uid) == (2, 0)
    assert (await billing.confirm_payment(main.pool(), p["id"], 1))["result"] == "already"
    assert await billing.get_balance(main.pool(), uid) == (2, 0)


async def test_cancel_before_publication_keeps_balance():
    uid = new_uid()
    await credit(uid, 2)
    d = await mk(uid)
    await billing.cancel_draft(main.pool(), d, uid)
    assert await billing.get_balance(main.pool(), uid) == (2, 0)


async def test_publishing_and_published_ads_cannot_be_rejected_or_cancelled():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid)
    assert (await billing.claim_publication(main.pool(), d)).ok
    assert await billing.reject_draft(main.pool(), d) is None
    assert await billing.cancel_draft(main.pool(), d, uid) is None
    await billing.release_publication(main.pool(), d, "x")
    await publish(bot, d)
    assert await billing.reject_draft(main.pool(), d) is None


async def test_repeated_reject_is_ignored():
    uid = new_uid()
    d = await mk(uid)
    assert await billing.reject_draft(main.pool(), d)
    assert await billing.reject_draft(main.pool(), d) is None


# ── Объявление без доступных публикаций (hold) ───────────────────────────────

async def test_hold_is_not_in_admin_queue_and_queue_lists_pending_and_errors():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    held = await mk(uid, phone="+79990001001", force_pending=False)
    assert await status_of(held) == "hold"
    await credit(uid, 1)
    pending = await mk(uid, phone="+79990001002")
    ids = [int(r["id"]) for r in await billing.publication_queue(main.pool(), 1000)]
    assert pending in ids and held not in ids
    bot.fail = True
    await publish(bot, pending)
    assert pending in [int(r["id"]) for r in await billing.publication_queue(main.pool(), 1000)]
    await billing.reject_draft(main.pool(), pending)
    assert pending not in [int(r["id"]) for r in await billing.publication_queue(main.pool(), 1000)]


async def test_holds_are_released_only_after_purchase_in_order():
    uid = new_uid()
    await occupy_free(uid)
    first = await mk(uid, phone="+79990001101", force_pending=False)
    second = await mk(uid, phone="+79990001102", force_pending=False)
    third = await mk(uid, phone="+79990001103", force_pending=False)
    assert await billing.release_holds(main.pool(), uid) == []
    await credit(uid, 2)
    released = await billing.release_holds(main.pool(), uid)
    assert [int(r["id"]) for r in released] == [first, second]
    assert all(r["status"] == "pending" and r["funding"] == "paid" for r in released)
    assert await status_of(third) == "hold"
    assert await billing.release_holds(main.pool(), uid) == []


async def test_balance_is_shared_between_open_ads():
    uid = new_uid()
    await occupy_free(uid)
    await credit(uid, 1)
    d1 = await mk(uid, phone="+79990001201", force_pending=False)
    d2 = await mk(uid, phone="+79990001202", force_pending=False)
    assert await status_of(d1) == "pending" and await status_of(d2) == "hold"


async def test_identical_hold_is_detected():
    uid = new_uid()
    await occupy_free(uid)
    d = await mk(uid, phone="+79990001301", force_pending=False)
    with pytest.raises(billing.PendingDuplicate) as ei:
        await mk(uid, phone="+79990001301", force_pending=False)
    assert ei.value.draft_id == d


async def test_cancelled_hold_is_closed():
    uid = new_uid()
    await occupy_free(uid)
    d = await mk(uid, phone="+79990001401", force_pending=False)
    assert await billing.cancel_draft(main.pool(), d, uid)
    assert await status_of(d) == "cancelled"


# ── Баланс, сохранность, ручные операции ─────────────────────────────────────

async def test_state_survives_pool_restart():
    uid = new_uid()
    await credit(uid, 4)
    p, _ = await billing.create_payment(main.pool(), uid, 5)
    d = await mk(uid)
    await main.pool().close()
    await main.init_db()
    assert await billing.get_balance(main.pool(), uid) == (4, 0)
    assert (await billing.get_payment(main.pool(), p["id"]))["status"] == "awaiting_receipt"
    assert await status_of(d) == "pending"


async def test_manual_operations_are_logged_and_need_a_reason():
    uid = new_uid()
    assert await billing.manual_adjust(main.pool(), 7, uid, 3, "компенсация", "manual_credit") == 3
    assert await billing.manual_adjust(main.pool(), 7, uid, -1, "ошибка начисления") == 2
    with pytest.raises(ValueError):
        await billing.manual_adjust(main.pool(), 7, uid, 1, "   ")
    with pytest.raises(ValueError):
        await billing.manual_adjust(main.pool(), 7, uid, -5, "слишком много")
    credits = await billing.ledger_history(main.pool(), uid, credits=True)
    debits = await billing.ledger_history(main.pool(), uid, credits=False)
    assert len(credits) == 1 and credits[0]["admin_id"] == 7 and credits[0]["kind"] == "manual_credit"
    assert credits[0]["reason"] == "компенсация"
    assert len(debits) == 1 and debits[0]["delta"] == -1 and debits[0]["created_at"] is not None
    assert await billing.get_balance(main.pool(), uid) == (2, 0)


async def test_concurrent_debits_never_make_balance_negative():
    uid = new_uid()
    await credit(uid, 3)
    res = await asyncio.gather(*[billing.manual_adjust(main.pool(), 1, uid, -1, "r") for _ in range(10)],
                               return_exceptions=True)
    assert sum(isinstance(r, int) for r in res) == 3
    assert await billing.get_balance(main.pool(), uid) == (0, 0)


# ── Планировщик ──────────────────────────────────────────────────────────────

async def test_scheduled_pending_ad_is_picked_up_when_due_and_published():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid)
    await main.set_scheduled(d, (main.now_local() + timedelta(days=1)).strftime("%d.%m.%Y %H:%M"))
    assert d not in [int(r["id"]) for r in await main.get_scheduled_ready()]
    await main.set_scheduled(d, (main.now_local() - timedelta(minutes=1)).strftime("%d.%m.%Y %H:%M"))
    assert d in [int(r["id"]) for r in await main.get_scheduled_ready()]
    assert (await publish(bot, d))["status"] == "published"
    assert d not in [int(r["id"]) for r in await main.get_scheduled_ready()]


async def test_hold_cannot_be_scheduled():
    uid = new_uid()
    await occupy_free(uid)
    d = await mk(uid, phone="+79990001501", force_pending=False)
    cb = FakeCB(1, f"sched:{d}")
    await main.sched_show_dates(cb)
    assert cb.answers and cb.answers[0][1] is True


# ── Хендлеры ─────────────────────────────────────────────────────────────────

async def test_form_without_publications_is_saved_but_not_sent_to_admin():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    bot.log.clear()
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form()), bot)
    assert bot.to(1) == []
    user_msgs = bot.to(uid)
    assert len(user_msgs) == 1 and "доступных публикаций нет" in user_msgs[0]["text"]
    assert "режим" not in user_msgs[0]["text"].lower()
    assert set(bot.buttons(user_msgs[0])) >= {"buy:menu"}
    row = await main.pool().fetchrow("SELECT * FROM drafts WHERE user_id=$1 ORDER BY id DESC LIMIT 1", uid)
    assert row["status"] == "hold" and row["pending_msg_id"] is None


async def test_duplicate_screens_are_not_dead_ends():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form()), bot)
    bot.log.clear()
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form()), bot)
    shown = bot.to(uid)[0]
    assert "уже сохранено" in shown["text"]
    assert set(bot.buttons(shown)) >= {"mystatus", "create_ad", "nav:back"}

    other, bot2 = new_uid(), FakeBot()
    await main.confirm_yes(FakeCB(other, "confirm:yes"), FakeState(form(phone="+79990000002")), bot2)
    bot2.log.clear()
    await main.confirm_yes(FakeCB(other, "confirm:yes"), FakeState(form(phone="+79990000002")), bot2)
    shown = bot2.to(other)[0]
    assert shown["text"] == "Такое объявление уже находится на проверке."
    assert set(bot2.buttons(shown)) >= {"mystatus", "create_ad", "nav:back"}


async def test_form_with_free_slot_reaches_admin_with_publish_buttons_only():
    uid, bot = new_uid(), FakeBot()
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form(phone="+79990000003")), bot)
    admin_msgs = bot.to(1)
    assert len(admin_msgs) == 1 and "бесплатная" in admin_msgs[0]["text"]
    d = await last_draft_id(uid)
    assert bot.buttons(admin_msgs[0]) == [f"publish:{d}", f"sched:{d}", f"reject:{d}"]
    assert "Режим" not in admin_msgs[0]["text"]


async def test_payment_screen_is_short_and_has_paid_and_cancel_buttons():
    uid, bot = new_uid(), FakeBot()
    main.CARD_NUMBER = "1111 2222 3333 4444"
    cb = FakeCB(uid, "buy:pkg:5")
    await main.buy_pkg_cb(cb, bot)
    screen = bot.to(uid)[0]
    assert len(screen["text"]) < 220 and "1111 2222 3333 4444" in screen["text"] and "200" in screen["text"]
    assert [b.split(":")[0] for b in bot.buttons(screen)] == ["payd", "paycancel"]
    assert any("новая заявка" in m["text"].lower() for m in bot.to(1))
    pid = int(bot.buttons(screen)[0].split(":")[1])

    bot.log.clear()
    await main.pay_paid_cb(FakeCB(uid, f"payd:{pid}"), bot)
    assert "Отправьте фото или файл чека" in bot.to(uid)[0]["text"]

    bot.log.clear()
    await main.pay_cancel_cb(FakeCB(uid, f"paycancel:{pid}"), bot)
    assert (await billing.get_payment(main.pool(), pid))["status"] == "cancelled"
    assert "отменена" in bot.to(uid)[0]["text"] and any("отменил заявку" in m["text"] for m in bot.to(1))
    repeat = FakeCB(uid, f"paycancel:{pid}")
    await main.pay_cancel_cb(repeat, bot)
    assert repeat.answers[0][1] is True


async def test_buy_for_ad_with_past_date_offers_no_payment():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    d = await mk(uid, date=past(2), phone="+79990001601", force_pending=False)
    bot.log.clear()
    await main.buy_pkg_cb(FakeCB(uid, "buy:pkg:2"), bot)
    assert await status_of(d) == "cancelled"
    assert await billing.find_receipt_target(main.pool(), uid) is None
    msgs = [m["text"] for m in bot.to(uid)]
    assert len(msgs) == 1 and "Дата поездки" in msgs[0]


async def test_purchase_warning_about_dates_is_gone():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    await mk(uid, date=future(1), phone="+79990001701", force_pending=False)
    main.CARD_NUMBER = "1111 2222 3333 4444"
    await main.buy_pkg_cb(FakeCB(uid, "buy:pkg:5"), bot)
    text = " ".join(m["text"] for m in bot.to(uid))
    assert "24" not in text and "не успеете" not in text and "Заявка #" in text


async def test_confirmed_payment_sends_held_ad_to_moderation_automatically():
    uid, bot = new_uid(), FakeBot()
    main.CARD_NUMBER = "1111 2222 3333 4444"
    await occupy_free(uid)
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form(phone="+79990001801")), bot)
    held = await last_draft_id(uid)
    await main.buy_pkg_cb(FakeCB(uid, "buy:pkg:2"), bot)
    payment = await billing.find_receipt_target(main.pool(), uid)
    assert payment["draft_id"] == held
    await billing.attach_receipt(main.pool(), payment["id"], uid, "photo", "F", 1, 1)
    bot.log.clear()
    admin_cb = FakeCB(1, f"padm:ok:{payment['id']}")
    await main.payment_admin_cb(admin_cb, bot)
    assert await status_of(held) == "pending"
    moderation = [m for m in bot.to(1) if "Новое объявление" in m["text"]]
    assert len(moderation) == 1
    assert bot.buttons(moderation[0]) == [f"publish:{held}", f"sched:{held}", f"reject:{held}"]
    user_texts = " ".join(m["text"] for m in bot.to(uid))
    assert "подтверждена" in user_texts and "отправлено на публикацию" in user_texts
    assert "режим" not in user_texts.lower()
    assert await bot_has_pending_msg(held)
    # повторное подтверждение: ни начисления, ни второго уведомления админу
    bot.log.clear()
    await main.payment_admin_cb(FakeCB(1, f"padm:ok:{payment['id']}"), bot)
    assert [m for m in bot.to(1) if "Новое объявление" in m["text"]] == []
    assert await billing.get_balance(main.pool(), uid) == (2, 0)


async def bot_has_pending_msg(draft_id):
    return (await main.get_draft(draft_id))["pending_msg_id"] is not None


async def test_payment_without_held_ad_only_credits_balance():
    uid, bot = new_uid(), FakeBot()
    p, _ = await billing.create_payment(main.pool(), uid, 2)
    await billing.attach_receipt(main.pool(), p["id"], uid, "photo", "F", 1, 1)
    await main.payment_admin_cb(FakeCB(1, f"padm:ok:{p['id']}"), bot)
    assert await billing.get_balance(main.pool(), uid) == (2, 0)
    assert [m for m in bot.to(1) if "Новое объявление" in m["text"]] == []
    assert bot.channel_posts == 0


async def test_status_screen_shows_hold_with_buy_and_cancel_buttons():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form(phone="+79990001901")), bot)
    text, kb = await main._status_screen(uid, "ru")
    buttons = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "ждёт покупки публикаций" in text
    assert "buy:menu" in buttons and any(b.startswith("mycancel:") for b in buttons)
