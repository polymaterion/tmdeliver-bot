"""
Интеграционные тесты биллинга на НАСТОЯЩЕМ PostgreSQL (pgserver) — без Telegram.
Запуск:  pip install pytest pytest-asyncio pgserver && pytest -q tests
Нумерация сценариев (S1…S30) соответствует разделу 16 ТЗ.
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
    DATABASE_URL=_pg.get_uri().replace("postgresql://", "postgresql://", 1),
)
import main  # noqa: E402
import billing  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="module")


class Msg:
    def __init__(self, i): self.message_id = i


class FakeBot:
    def __init__(self):
        self.fail = False
        self.sent = []      # (chat_id, text)
        self.log = []       # dict(kind, chat_id, text, markup)
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
        self.sent.append((chat_id, text))
        self.log.append(dict(kind="send", chat_id=chat_id, text=text, markup=kw.get("reply_markup")))
        return Msg(self._id)

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.log.append(dict(kind="edit", chat_id=chat_id, text=text, markup=kw.get("reply_markup")))
        return Msg(message_id)

    async def delete_message(self, *a, **k): return True

    def to(self, chat_id):
        """Все тексты, отправленные/изменённые в чате chat_id."""
        return [m for m in self.log if m["chat_id"] == chat_id]

    @staticmethod
    def buttons(m):
        mk = m["markup"]
        return [b.callback_data for row in (mk.inline_keyboard if mk else []) for b in row]


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


async def mk(uid, date=None, ad_type="carrier", approve=True, funding=None, keep_hold=False):
    """Создаёт объявление. Если у пользователя нет доступных публикаций, create_draft кладёт его
    в 'hold' (админу не уходит). Для тестов claim-уровня (проверки «нет баланса» и т.п.) такое
    объявление принудительно переводится в pending, если keep_hold=False."""
    d = await billing.create_draft(main.pool(), dict(
        user_id=uid, full_name="U", cities=["Москва", "Ашхабад"], travel_date=date or future(10),
        cargo="docs", phone="+79990000000", ad_type=ad_type))
    if funding:
        await main.pool().execute("UPDATE drafts SET funding=$2 WHERE id=$1", d, funding)
    if not keep_hold:
        await main.pool().execute("UPDATE drafts SET status='pending' WHERE id=$1 AND status='hold'", d)
    if approve and not keep_hold:
        assert await billing.approve_draft(main.pool(), d)
    return d


async def credit(uid, n, reason="test"):
    return await billing.manual_adjust(main.pool(), 1, uid, n, reason)


async def age_publications(uid, hours=25):
    await main.pool().execute(
        "UPDATE drafts SET published_at=published_at - $2::interval WHERE user_id=$1 AND status='published'",
        uid, timedelta(hours=hours))


async def publish(bot, d):
    return await main._execute_publication(bot, d)


# ── S1–S5: бесплатное место ──────────────────────────────────────────────────
async def test_S1_new_user_has_free_slot():
    ov = await billing.get_overview(main.pool(), new_uid())
    assert ov["free_available"] and ov["available"] == 0


async def test_S2_S3_free_slot_taken_while_active_and_S13_balance_untouched():
    uid, bot = new_uid(), FakeBot()
    await credit(uid, 3)
    d = await mk(uid)
    assert (await main.get_draft(d))["funding"] == "free"
    r = await publish(bot, d)
    assert r["status"] == "published" and r["fin"]["funding"] == "free"
    assert await billing.get_balance(main.pool(), uid) == (3, 0)          # S13
    ov = await billing.get_overview(main.pool(), uid)
    assert not ov["free_available"]                                         # S2/S3
    d2 = await mk(uid)                                                      # новая заявка → платная
    assert (await main.get_draft(d2))["funding"] == "paid"


async def test_S3_cannot_force_free_while_slot_busy():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid)
    await publish(bot, d)
    await age_publications(uid)
    d2 = await mk(uid)
    ok, code = await billing.set_funding(main.pool(), d2, uid, "free")
    assert not ok and code == "free_taken"


async def test_S4_free_slot_returns_after_expiry():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid, date=future(1))
    await publish(bot, d)
    assert not (await billing.get_overview(main.pool(), uid))["free_available"]
    # срок поездки истёк
    await main.pool().execute("UPDATE drafts SET travel_date=$2 WHERE id=$1", d, past(1))
    assert (await billing.get_overview(main.pool(), uid))["free_available"]
    await main.pool().execute("UPDATE drafts SET travel_date=$2, expired=TRUE WHERE id=$1", d, future(1))
    assert (await billing.get_overview(main.pool(), uid))["free_available"]  # флаг expired тоже освобождает
    d2 = await mk(uid)
    assert (await main.get_draft(d2))["funding"] == "free"


async def test_S5_S27_restart_does_not_regrant_free():
    uid, bot = new_uid(), FakeBot()
    await publish(bot, await mk(uid))
    await billing.init_schema(main.pool())      # «перезапуск»: повторная миграция
    await billing.init_schema(main.pool())
    assert not (await billing.get_overview(main.pool(), uid))["free_available"]


async def test_migration_legacy_published_occupies_free_slot():
    uid = new_uid()
    # эмулируем строку из старой схемы: без status/funding
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


# ── S6–S8: очередь и защита от повторной отправки ────────────────────────────
async def test_S6_S7_multi_drafts_and_only_pending_identical_blocked():
    uid = new_uid()
    d1 = await mk(uid, approve=False)
    # Другое объявление того же пользователя не блокируется.
    d2 = await billing.create_draft(main.pool(), dict(
        user_id=uid, full_name="U", cities=["Москва", "Казань"], travel_date=future(10),
        cargo="docs", phone="+79990000000", ad_type="carrier"))
    assert d2 != d1
    # Идентичное объявление блокируется только пока первое pending.
    with pytest.raises(billing.PendingDuplicate) as ei:
        await mk(uid, approve=False)
    assert ei.value.draft_id == d1
    assert await billing.approve_draft(main.pool(), d1)
    # После завершения модерации идентичная новая заявка разрешена.
    d3 = await billing.create_draft(main.pool(), dict(
        user_id=uid, full_name="U", cities=["Москва", "Ашхабад"], travel_date=future(10),
        cargo="docs", phone="+79990000000", ad_type="carrier"))
    assert d3 != d1
    # Параллельные одинаковые pending-заявки: создаётся ровно одна.
    uid2 = new_uid()
    res = await asyncio.gather(*[mk(uid2, approve=False) for _ in range(8)], return_exceptions=True)
    assert sum(isinstance(x, int) for x in res) == 1
    assert sum(isinstance(x, billing.PendingDuplicate) for x in res) == 7


async def test_rejected_and_cancelled_do_not_block():
    uid = new_uid()
    d = await mk(uid, approve=False)
    assert await billing.reject_draft(main.pool(), d)
    d2 = await mk(uid, approve=False)
    assert await billing.cancel_draft(main.pool(), d2, uid)
    await mk(uid, approve=False)
    # чужой пользователь не может отменить
    assert await billing.cancel_draft(main.pool(), d2, uid + 999) is None


async def test_rejected_cancelled_do_not_take_free_slot():
    uid = new_uid()
    d = await mk(uid, approve=False)
    await billing.reject_draft(main.pool(), d)
    assert (await billing.get_overview(main.pool(), uid))["free_available"]


# ── S8/S10: одобрение и оплата НЕ публикуют ──────────────────────────────────
async def test_S8_approve_does_not_publish():
    uid = new_uid()
    d = await mk(uid)
    row = await main.get_draft(d)
    assert row["status"] == "approved" and not row["published"] and row["channel_msg_id"] is None


async def test_unapproved_cannot_be_published():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid, approve=False)
    r = await publish(bot, d)
    assert r["status"] == "claim_failed" and r["claim"].code == "not_approved"
    assert bot.channel_posts == 0


# ── S9–S11: платежи ──────────────────────────────────────────────────────────
async def test_S9_S10_S11_payment_flow_no_double_credit_no_autopublish():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid)                                        # объявление одобрено и ждёт
    p, created = await billing.create_payment(main.pool(), uid, 5, d)
    assert created and p["amount"] == 200
    p2, created2 = await billing.create_payment(main.pool(), uid, 5, d)
    assert not created2 and p2["id"] == p["id"]              # повторный клик — та же заявка
    assert (await billing.confirm_payment(main.pool(), p["id"], 1))["result"] == "invalid"   # без чека нельзя
    assert await billing.get_balance(main.pool(), uid) == (0, 0)                              # S9
    await billing.attach_receipt(main.pool(), p["id"], uid, "photo", "F", 1, 1)
    assert await billing.get_balance(main.pool(), uid) == (0, 0)
    r1 = await billing.confirm_payment(main.pool(), p["id"], 1)
    assert r1["result"] == "credited" and r1["balance"] == 5
    row = await main.get_draft(d)
    assert row["status"] == "approved" and not row["published"]                               # S10
    assert bot.channel_posts == 0
    r2 = await billing.confirm_payment(main.pool(), p["id"], 2)                               # S11
    assert r2["result"] == "already"
    assert await billing.get_balance(main.pool(), uid) == (5, 0)


async def test_S11_concurrent_confirm_by_two_admins():
    uid = new_uid()
    p, _ = await billing.create_payment(main.pool(), uid, 2)
    await billing.attach_receipt(main.pool(), p["id"], uid, "photo", "F", 1, 1)
    res = await asyncio.gather(*[billing.confirm_payment(main.pool(), p["id"], a) for a in range(1, 9)])
    assert sorted(r["result"] for r in res).count("credited") == 1
    assert await billing.get_balance(main.pool(), uid) == (2, 0)
    n = await main.pool().fetchval("SELECT count(*) FROM balance_ledger WHERE op_key=$1", f"pay:{p['id']}")
    assert n == 1


async def test_payment_reject_then_retry_receipt():
    uid = new_uid()
    p, _ = await billing.create_payment(main.pool(), uid, 2)
    await billing.attach_receipt(main.pool(), p["id"], uid, "document", "D", 1, 2)
    st, _ = await billing.reject_payment(main.pool(), p["id"], 1)
    assert st == "rejected" and await billing.get_balance(main.pool(), uid) == (0, 0)
    assert (await billing.confirm_payment(main.pool(), p["id"], 1))["result"] == "invalid"   # отклонённый не подтвердить
    assert await billing.reopen_payment(main.pool(), p["id"], uid)
    tgt = await billing.find_receipt_target(main.pool(), uid)
    assert tgt["id"] == p["id"] and tgt["status"] == "awaiting_receipt"
    await billing.attach_receipt(main.pool(), p["id"], uid, "photo", "F2", 1, 3)
    assert (await billing.confirm_payment(main.pool(), p["id"], 1))["result"] == "credited"
    assert await main.pool().fetchval("SELECT count(*) FROM payment_receipts WHERE payment_id=$1", p["id"]) == 2


# ── S12–S16: списание и 24 часа ──────────────────────────────────────────────
async def test_S12_paid_publication_charges_exactly_one():
    uid, bot = new_uid(), FakeBot()
    await publish(bot, await mk(uid))            # занимаем бесплатное
    await age_publications(uid)
    await credit(uid, 5)
    d = await mk(uid)
    assert (await main.get_draft(d))["funding"] == "paid"
    r = await publish(bot, d)
    assert r["status"] == "published" and r["fin"]["balance"] == 4
    assert await billing.get_balance(main.pool(), uid) == (4, 0)
    spends = await main.pool().fetch("SELECT * FROM balance_ledger WHERE user_id=$1 AND kind='spend'", uid)
    assert len(spends) == 1 and spends[0]["delta"] == -1 and spends[0]["draft_id"] == d


async def test_S14_S15_identical_cooldown_but_different_ads_are_immediate():
    uid, bot = new_uid(), FakeBot()
    await publish(bot, await mk(uid))
    await age_publications(uid)
    await credit(uid, 5)
    d1 = await mk(uid)
    assert (await publish(bot, d1))["status"] == "published"
    posts = bot.channel_posts

    # Идентичное объявление блокируется на 24 часа.
    d2 = await mk(uid)
    r = await publish(bot, d2)
    assert r["status"] == "claim_failed" and r["claim"].code == "cooldown"
    assert r["claim"].next_at - r["claim"].last_at == timedelta(hours=24)
    assert bot.channel_posts == posts and await billing.get_balance(main.pool(), uid) == (4, 0)

    # Другое объявление того же пользователя не зависит от cooldown d1.
    d3 = await billing.create_draft(main.pool(), dict(
        user_id=uid, full_name="U", cities=["Москва", "Казань"], travel_date=future(10),
        cargo="docs", phone="+79990000000", ad_type="carrier"))
    await billing.approve_draft(main.pool(), d3)
    assert (await publish(bot, d3))["status"] == "published"

    await main.pool().execute(
        "UPDATE drafts SET published_at=now()-interval '23 hours' WHERE id=$1", d1)
    assert (await publish(bot, d2))["claim"].code == "cooldown"
    await main.pool().execute(
        "UPDATE drafts SET published_at=now()-interval '24 hours 1 second' WHERE id=$1", d1)
    assert (await publish(bot, d2))["status"] == "published"
    assert await billing.get_balance(main.pool(), uid) == (2, 0)


async def test_cooldown_is_per_fingerprint_and_other_users_unaffected():
    a, b, bot = new_uid(), new_uid(), FakeBot()
    await publish(bot, await mk(a))
    assert (await publish(bot, await mk(b)))["status"] == "published"
    await credit(a, 1)
    da = await mk(a)
    assert (await publish(bot, da))["claim"].code == "cooldown"




async def test_different_fingerprints_can_publish_without_user_cooldown():
    uid, bot = new_uid(), FakeBot()
    await credit(uid, 2)
    d1 = await mk(uid, funding="paid")
    d2 = await billing.create_draft(main.pool(), dict(
        user_id=uid, full_name="U", cities=["Москва", "Казань"], travel_date=future(10),
        cargo="docs", phone="+79990000000", ad_type="carrier"))
    await billing.approve_draft(main.pool(), d2)
    await main.pool().execute("UPDATE drafts SET funding='paid' WHERE id=$1", d2)
    assert (await publish(bot, d1))["status"] == "published"
    assert (await publish(bot, d2))["status"] == "published"

# ── S17–S19: ошибки Telegram и гонки ─────────────────────────────────────────
async def test_S17_telegram_error_no_charge_and_retry():
    uid, bot = new_uid(), FakeBot()
    await publish(bot, await mk(uid))
    await age_publications(uid)
    await credit(uid, 2)
    d = await mk(uid)
    bot.fail = True
    r = await publish(bot, d)
    assert r["status"] == "telegram_error"
    row = await main.get_draft(d)
    assert row["status"] == "publish_error" and not row["published"] and row["published_at"] is None
    assert await billing.get_balance(main.pool(), uid) == (2, 0)                              # ни списания, ни резерва
    bot.fail = False
    r = await publish(bot, d)
    assert r["status"] == "published" and await billing.get_balance(main.pool(), uid) == (1, 0)


async def test_S18_S19_double_click_and_two_admins_single_post_single_charge():
    uid, bot = new_uid(), FakeBot()
    await publish(bot, await mk(uid))
    await age_publications(uid)
    await credit(uid, 3)
    d = await mk(uid)
    bot.delay = 0.3                                      # окно гонки: публикация «висит» в Telegram
    before = bot.channel_posts
    res = await asyncio.gather(*[publish(bot, d) for _ in range(6)])
    assert [r["status"] for r in res].count("published") == 1
    assert bot.channel_posts == before + 1
    codes = {r["claim"].code for r in res if r["status"] == "claim_failed"}
    assert codes <= {"in_progress", "already_published"}
    assert await billing.get_balance(main.pool(), uid) == (2, 0)
    assert await main.pool().fetchval("SELECT count(*) FROM balance_ledger WHERE op_key=$1", f"spend:{d}") == 1


async def test_reserve_protects_balance_during_publishing():
    """Пока идёт отправка, резерв не даёт потратить ту же публикацию другим объявлением."""
    uid = new_uid()
    await credit(uid, 1)
    await main.pool().execute("UPDATE user_balances SET reserved=0 WHERE user_id=$1", uid)
    d = await mk(uid, funding="paid")
    c = await billing.claim_publication(main.pool(), d)
    assert c.ok and await billing.get_balance(main.pool(), uid) == (1, 1)
    with pytest.raises(ValueError):
        await billing.manual_adjust(main.pool(), 1, uid, -1, "нельзя уйти ниже резерва")
    await billing.release_publication(main.pool(), d, "x")
    assert await billing.get_balance(main.pool(), uid) == (1, 0)


async def test_S13_S17_restart_during_publishing_recovers_without_charge():
    uid = new_uid()
    await credit(uid, 1)
    d = await mk(uid, funding="paid")
    assert (await billing.claim_publication(main.pool(), d)).ok      # «бот упал» после claim
    assert await billing.recover_stuck_publications(main.pool()) == [d]
    row = await main.get_draft(d)
    assert row["status"] == "publish_error" and not row["published"]
    assert await billing.get_balance(main.pool(), uid) == (1, 0)


# ── Проверки состояния объявления ────────────────────────────────────────────
async def test_paid_unpaid_cannot_publish():
    uid, bot = new_uid(), FakeBot()
    await publish(bot, await mk(uid))
    await age_publications(uid)
    d = await mk(uid)                                    # платное, баланс 0
    r = await publish(bot, d)
    assert r["claim"].code == "no_balance" and (await main.get_draft(d))["status"] == "approved"


async def test_cancelled_rejected_published_cannot_publish():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid)
    await billing.cancel_draft(main.pool(), d, uid)
    assert (await publish(bot, d))["claim"].code == "cancelled"
    d = await mk(uid)
    await billing.reject_draft(main.pool(), d)
    assert (await publish(bot, d))["claim"].code == "rejected"
    d = await mk(uid)
    assert (await publish(bot, d))["status"] == "published"
    assert (await publish(bot, d))["claim"].code == "already_published"
    assert bot.channel_posts == 1


async def test_S30_trip_in_past_blocks_publication():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid, date=past(1))
    r = await publish(bot, d)
    assert r["claim"].code == "trip_passed" and bot.channel_posts == 0
    assert (await main.get_draft(d))["status"] == "approved"


# ── S20–S21: расчёт до вылета ────────────────────────────────────────────────
def _calc(days, qty, free=True, avail=0, last=None):
    now = billing.utcnow()
    dl = billing.to_local(now).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days + 1)
    return billing.calc_usable_publications(now=now, last_pub_at=last, deadline_local=dl,
                                            free_available=free, balance_available=avail, package_qty=qty)


async def test_S20_S21_warning_when_not_enough_time_but_purchase_allowed():
    c = _calc(days=3, qty=5, free=False)
    assert not c.fits and c.usable_from_package < 5 and c.unused_from_package > 0
    assert _calc(days=30, qty=5).fits
    assert _calc(days=30, qty=2).fits
    # недавняя публикация сдвигает первый слот
    c2 = _calc(days=1, qty=2, free=False, last=billing.utcnow() - timedelta(hours=1))
    assert c2.usable_from_package < 2
    # дата в прошлом → слотов нет
    assert _calc(days=-2, qty=2).usable_total == 0


async def test_S20_warning_text_before_payment_via_handler():
    uid = new_uid()
    await publish(FakeBot(), await mk(uid))         # свободное занято
    await age_publications(uid)
    await mk(uid, date=future(1), approve=False, keep_hold=True)   # поездка завтра, публикаций нет → hold
    w, passed = await main._package_warning(FakeBot(), uid, "ru", 5)
    assert not passed and w
    # правильная формулировка: про ОДИНАКОВОЕ объявление, а не «не более 1 объявления за 24 часа»
    assert "1 объявления за 24 часа" not in w and "По правилам канала" not in w
    assert "Одинаковое объявление" in w and "не чаще 1 раза в 24 ч" in w
    assert "из 5 публикаций" in w and "осталось: 1 день" in w
    w2, _ = await main._package_warning(FakeBot(), uid, "ru", 2)
    assert w2 is None                                  # 2 публикации до завтрашнего вечера успевают — предупреждения нет


async def test_trip_passed_no_payment_offer_and_draft_cancelled():
    uid = new_uid()
    d = await mk(uid, date=past(2), approve=False)
    bot = FakeBot()
    w, passed = await main._package_warning(bot, uid, "ru", 2)
    assert passed and w is None
    assert (await main.get_draft(d))["status"] == "cancelled"
    assert await billing.get_open_draft(main.pool(), uid) is None    # можно создавать новое


# ── S22–S23: отклонение сохраняет баланс ─────────────────────────────────────
async def test_S22_S23_rejected_after_payment_keeps_balance():
    uid = new_uid()
    d = await mk(uid, funding="paid")
    p, _ = await billing.create_payment(main.pool(), uid, 2, d)
    await billing.attach_receipt(main.pool(), p["id"], uid, "photo", "F", 1, 1)
    await billing.confirm_payment(main.pool(), p["id"], 1)
    assert await billing.reject_draft(main.pool(), d)
    assert await billing.get_balance(main.pool(), uid) == (2, 0)
    # повторного начисления за платёж нет
    assert (await billing.confirm_payment(main.pool(), p["id"], 1))["result"] == "already"
    assert await billing.get_balance(main.pool(), uid) == (2, 0)


async def test_cancel_before_publish_keeps_balance():
    uid = new_uid()
    await credit(uid, 2)
    d = await mk(uid, funding="paid")
    await billing.cancel_draft(main.pool(), d, uid)
    assert await billing.get_balance(main.pool(), uid) == (2, 0)


async def test_cannot_reject_or_cancel_while_publishing_or_published():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid)
    assert (await billing.claim_publication(main.pool(), d)).ok
    assert await billing.reject_draft(main.pool(), d) is None
    assert await billing.cancel_draft(main.pool(), d, uid) is None
    await billing.release_publication(main.pool(), d, "x")
    await publish(bot, d)
    assert await billing.reject_draft(main.pool(), d) is None


# ── S24–S26: сохранность после «перезапуска» ─────────────────────────────────
async def test_S24_S25_S26_state_survives_pool_restart():
    uid = new_uid()
    await credit(uid, 4)
    p, _ = await billing.create_payment(main.pool(), uid, 5)
    d = await mk(uid)
    await main.pool().close()
    await main.init_db()                                  # новый пул + повторная миграция
    assert await billing.get_balance(main.pool(), uid) == (4, 0)
    assert (await billing.get_payment(main.pool(), p["id"]))["status"] == "awaiting_receipt"
    assert (await main.get_draft(d))["status"] == "approved"


# ── S28: повторные callback-и ────────────────────────────────────────────────
async def test_S28_repeated_callbacks_are_idempotent():
    uid = new_uid()
    d = await mk(uid, approve=False)
    assert await billing.approve_draft(main.pool(), d)
    assert await billing.approve_draft(main.pool(), d) is None      # повторное «Одобрить»
    assert await billing.reject_draft(main.pool(), d)
    assert await billing.reject_draft(main.pool(), d) is None       # повторное «Отклонить»


# ── S29: ручные операции в истории ───────────────────────────────────────────
async def test_S29_manual_ops_logged_and_reason_required():
    uid = new_uid()
    assert await billing.manual_adjust(main.pool(), 7, uid, 3, "компенсация", "manual_credit") == 3
    assert await billing.manual_adjust(main.pool(), 7, uid, -1, "ошибка начисления") == 2
    with pytest.raises(ValueError):
        await billing.manual_adjust(main.pool(), 7, uid, 1, "   ")
    with pytest.raises(ValueError):
        await billing.manual_adjust(main.pool(), 7, uid, -5, "слишком много")
    cr = await billing.ledger_history(main.pool(), uid, credits=True)
    db = await billing.ledger_history(main.pool(), uid, credits=False)
    assert len(cr) == 1 and cr[0]["admin_id"] == 7 and cr[0]["reason"] == "компенсация" and cr[0]["kind"] == "manual_credit"
    assert len(db) == 1 and db[0]["delta"] == -1 and db[0]["created_at"] is not None
    assert await billing.get_balance(main.pool(), uid) == (2, 0)


async def test_concurrent_manual_debit_never_goes_negative():
    uid = new_uid()
    await credit(uid, 3)
    res = await asyncio.gather(*[billing.manual_adjust(main.pool(), 1, uid, -1, "r") for _ in range(10)],
                               return_exceptions=True)
    assert sum(isinstance(r, int) for r in res) == 3
    assert await billing.get_balance(main.pool(), uid) == (0, 0)


# ── Прочее ───────────────────────────────────────────────────────────────────
async def test_seeker_deadline_and_free_slot():
    uid, bot = new_uid(), FakeBot()
    d = await mk(uid, date="В ближайшие 10 дней", ad_type="seeker")
    assert (await publish(bot, d))["status"] == "published"
    assert not (await billing.get_overview(main.pool(), uid))["free_available"]
    await main.pool().execute("UPDATE drafts SET published_at=now()-interval '11 days' WHERE id=$1", d)
    assert (await billing.get_overview(main.pool(), uid))["free_available"]


async def test_admin_queue_lists_only_ready():
    uid = new_uid()
    d1 = await mk(uid)
    ids = [int(r["id"]) for r in await billing.queue_ready(main.pool(), 500)]
    assert d1 in ids
    await billing.reject_draft(main.pool(), d1)
    assert d1 not in [int(r["id"]) for r in await billing.queue_ready(main.pool(), 500)]


async def test_queue_pending_lists_unapproved():
    uid = new_uid()
    d = await mk(uid, approve=False)
    assert d in [int(r["id"]) for r in await billing.queue_pending(main.pool(), 500)]
    await billing.approve_draft(main.pool(), d)
    assert d not in [int(r["id"]) for r in await billing.queue_pending(main.pool(), 500)]


# ═══ Правки: hold, серии автопубликаций, возвраты, оплата, режимы ═══════════════

def dt_of(r):
    from datetime import datetime
    return datetime.strptime(r["scheduled_at"], "%d.%m.%Y %H:%M")


async def occupy_free(uid):
    """Пользователь уже использовал бесплатное место (активная бесплатная публикация)."""
    await publish(FakeBot(), await mk(uid, date=future(30)))
    await age_publications(uid)


async def test_hold_when_no_publications_not_sent_to_moderation():
    uid = new_uid()
    await occupy_free(uid)
    d = await mk(uid, keep_hold=True)
    row = await main.get_draft(d)
    assert row["status"] == "hold" and row["funding"] == "paid"
    assert d not in [int(r["id"]) for r in await billing.queue_pending(main.pool(), 1000)]
    # нельзя ни одобрить, ни опубликовать
    assert await billing.approve_draft(main.pool(), d) is None
    assert (await publish(FakeBot(), d))["claim"].code == "not_approved"


async def test_hold_released_only_after_purchase_and_only_once():
    uid = new_uid()
    await occupy_free(uid)
    d = await mk(uid, keep_hold=True)
    assert await billing.release_hold(main.pool(), d, uid) is None          # публикаций нет
    await credit(uid, 2)
    r = await billing.release_hold(main.pool(), d, uid)
    assert r and r["status"] == "pending" and r["funding"] == "paid"
    assert await billing.release_hold(main.pool(), d, uid) is None          # повторный клик
    assert d in [int(x["id"]) for x in await billing.queue_pending(main.pool(), 1000)]


async def test_balance_is_allocated_across_open_drafts():
    """Баланс 1: первое объявление уходит админу, второе (другое) — hold, а не второй раз «платное»."""
    uid = new_uid()
    await occupy_free(uid)
    await credit(uid, 1)
    d1 = await mk(uid, keep_hold=True)
    assert (await main.get_draft(d1))["status"] == "pending"
    d2 = await billing.create_draft(main.pool(), dict(
        user_id=uid, full_name="U", cities=["Москва", "Казань"], travel_date=future(10),
        cargo="docs", phone="+79990000000", ad_type="carrier"))
    assert (await main.get_draft(d2))["status"] == "hold"


async def test_hold_duplicate_detected():
    uid = new_uid()
    await occupy_free(uid)
    d = await mk(uid, keep_hold=True)
    with pytest.raises(billing.PendingDuplicate) as ei:
        await mk(uid, keep_hold=True)
    assert ei.value.draft_id == d


async def test_auto_distribute_evenly_all_identical_all_debited_at_once():
    uid = new_uid()
    await occupy_free(uid)
    await credit(uid, 3)
    d = await mk(uid, date=future(10), funding="paid", approve=True)
    res = await billing.auto_distribute(main.pool(), d)
    assert res["result"] == "scheduled" and len(res["ids"]) == 3 and res["debited"] == 3
    assert await billing.get_balance(main.pool(), uid) == (0, 0)             # списано сразу все
    rows = await main.pool().fetch("SELECT * FROM drafts WHERE series_id=$1 ORDER BY id", d)
    assert len(rows) == 3 and all(r["status"] == "approved" and r["funding"] == "prepaid" for r in rows)
    assert len({r["fingerprint"] for r in rows}) == 1                         # все одинаковые
    assert len({(r["route"], r["travel_date"], r["cargo"], r["phone"]) for r in rows}) == 1
    t = [dt_of(r) for r in rows]
    gaps = [(t[i + 1] - t[i]).total_seconds() / 3600 for i in range(2)]
    assert all(g >= 24 for g in gaps) and abs(gaps[0] - gaps[1]) < 0.1        # равномерно
    now_l = billing.to_local(billing.utcnow())
    assert (t[0] - now_l).total_seconds() < 120                               # первая — сразу
    trip_end = __import__("datetime").datetime.strptime(future(10), "%d.%m.%Y") + timedelta(days=1)
    assert t[-1] < trip_end                                                    # все до вылета
    ledger = await main.pool().fetch("SELECT * FROM balance_ledger WHERE user_id=$1 AND kind='auto_spend'", uid)
    assert len(ledger) == 1 and ledger[0]["delta"] == -3
    # повторный вызов ничего не делает
    assert (await billing.auto_distribute(main.pool(), d))["result"] == "skip"
    assert await billing.get_balance(main.pool(), uid) == (0, 0)


async def test_auto_distribute_free_plus_paid():
    uid = new_uid()
    await credit(uid, 2)
    d = await mk(uid, date=future(10), approve=True)          # свободное место есть → funding=free
    assert (await main.get_draft(d))["funding"] == "free"
    res = await billing.auto_distribute(main.pool(), d)
    assert len(res["ids"]) == 3 and res["debited"] == 2
    assert await billing.get_balance(main.pool(), uid) == (0, 0)


async def test_auto_distribute_limited_by_identical_interval_rest_stays_on_balance():
    uid = new_uid()
    await occupy_free(uid)
    await credit(uid, 5)
    d = await mk(uid, date=future(1), funding="paid", approve=True)
    res = await billing.auto_distribute(main.pool(), d)
    n = len(res["ids"])
    assert res["result"] == "scheduled" and 1 <= n < 5 and res["debited"] == n
    assert await billing.get_balance(main.pool(), uid) == (5 - n, 0)          # остаток сохранён
    rows = await main.pool().fetch("SELECT * FROM drafts WHERE series_id=$1 ORDER BY id", d)
    t = [dt_of(r) for r in rows]
    assert all((t[i + 1] - t[i]).total_seconds() >= 24 * 3600 for i in range(len(t) - 1))


async def test_series_publishes_without_extra_charge_and_cancel_refunds_rest():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    await credit(uid, 3)
    d = await mk(uid, date=future(10), funding="paid", approve=True)
    res = await billing.auto_distribute(main.pool(), d)
    ids = res["ids"]
    r1 = await publish(bot, ids[0])
    assert r1["status"] == "published" and r1["fin"]["funding"] == "prepaid"
    assert await billing.get_balance(main.pool(), uid) == (0, 0)              # повторного списания нет
    # вторая публикация серии раньше интервала — не выходит
    assert (await publish(bot, ids[1]))["claim"].code == "cooldown"
    # пользователь отменяет оставшиеся → 2 публикации возвращаются, вышедшая — нет
    row = await billing.cancel_draft(main.pool(), ids[1], uid)
    assert row["status"] == "cancelled"
    assert await billing.get_balance(main.pool(), uid) == (2, 0)
    st = {int(r["id"]): r["status"] for r in await main.pool().fetch("SELECT id,status FROM drafts WHERE series_id=$1", d)}
    assert st[ids[0]] == "published" and st[ids[1]] == "cancelled" and st[ids[2]] == "cancelled"
    assert await billing.cancel_draft(main.pool(), ids[1], uid) is None       # повтор — без двойного возврата
    assert await billing.get_balance(main.pool(), uid) == (2, 0)


async def test_admin_reject_of_series_refunds_everything():
    uid = new_uid()
    await occupy_free(uid)
    await credit(uid, 3)
    d = await mk(uid, date=future(10), funding="paid", approve=True)
    await billing.auto_distribute(main.pool(), d)
    assert await billing.get_balance(main.pool(), uid) == (0, 0)
    assert await billing.reject_draft(main.pool(), d)
    assert await billing.get_balance(main.pool(), uid) == (3, 0)


async def test_cancel_payment_closes_request_correctly():
    uid = new_uid()
    p, _ = await billing.create_payment(main.pool(), uid, 2)
    code, row = await billing.cancel_payment(main.pool(), p["id"], uid)
    assert code == "cancelled" and row["status"] == "cancelled"
    assert await billing.find_receipt_target(main.pool(), uid) is None         # чеки больше не принимаются
    assert await billing.attach_receipt(main.pool(), p["id"], uid, "photo", "F", 1, 1) is None
    assert (await billing.confirm_payment(main.pool(), p["id"], 1))["result"] == "invalid"
    assert (await billing.cancel_payment(main.pool(), p["id"], uid))[0] == "already"
    assert (await billing.cancel_payment(main.pool(), p["id"], uid + 1))[0] == "not_found"   # чужую нельзя
    p2, created = await billing.create_payment(main.pool(), uid, 2)            # можно создать новую
    assert created and p2["id"] != p["id"]
    # после отправки чека отменить нельзя
    await billing.attach_receipt(main.pool(), p2["id"], uid, "photo", "F", 1, 2)
    assert (await billing.cancel_payment(main.pool(), p2["id"], uid))[0] == "under_review"
    # отклонённую — можно
    await billing.reject_payment(main.pool(), p2["id"], 1)
    assert (await billing.cancel_payment(main.pool(), p2["id"], uid))[0] == "cancelled"


async def test_cooldown_can_be_disabled(monkeypatch):
    monkeypatch.setattr(billing, "COOLDOWN", timedelta(0))
    uid, bot = new_uid(), FakeBot()
    d1 = await mk(uid, date=future(1))
    assert (await publish(bot, d1))["status"] == "published"
    await credit(uid, 3)
    d2 = await mk(uid, date=future(1), approve=True)       # такое же объявление (свободное занято → платное)
    await main.pool().execute("UPDATE drafts SET status='approved' WHERE id=$1", d2)
    assert (await publish(bot, d2))["status"] == "published"      # без ограничения
    t = billing.plan_series(now=billing.utcnow(), last_pub_at=None, deadline_local=None, total=3)
    assert len(t) == 3
    ctx_user = new_uid()
    await mk(ctx_user, date=future(1), approve=False, keep_hold=True)
    assert await main._package_warning(FakeBot(), ctx_user, "ru", 5) == (None, False)


# ── Хендлеры: имитация Telegram ──────────────────────────────────────────────

class FakeUser:
    def __init__(self, uid, name="Тест", username="tester"):
        self.id, self.full_name, self.username = uid, name, username


class FakeChat:
    def __init__(self, i): self.id = i


class FakeMsg:
    def __init__(self, uid, mid=500):
        self.chat, self.message_id, self.replies = FakeChat(uid), mid, []

    async def edit_reply_markup(self, **kw): return True

    async def reply(self, text, **kw):
        self.replies.append(text)


class FakeCB:
    def __init__(self, uid, data, mid=500, admin=False):
        self.data, self.from_user = data, FakeUser(uid)
        self.message, self.answers = FakeMsg(uid if not admin else uid, mid), []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeState:
    def __init__(self, data=None): self.d = dict(data or {})
    async def get_data(self): return dict(self.d)
    async def update_data(self, **kw): self.d.update(kw)
    async def clear(self): self.d.clear()
    async def set_state(self, *a): pass
    async def get_state(self): return None


def form(**over):
    d = dict(ad_type="carrier", cities=["Москва", "Ашхабад"], travel_date=future(10),
             cargo="docs", phone="+79990000001", custom_username=None, bot_msg_id=500)
    d.update(over)
    return d


async def test_H1_form_without_publications_is_not_sent_to_admin():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    bot.log.clear()
    cb, st = FakeCB(uid, "confirm:yes"), FakeState(form())
    await main.confirm_yes(cb, st, bot)
    assert bot.to(1) == []                                          # админу ничего не ушло
    user_msgs = bot.to(uid)
    assert len(user_msgs) == 1 and "доступных публикаций нет" in user_msgs[0]["text"]
    assert "buy:menu" in bot.buttons(user_msgs[0])
    row = await main.pool().fetchrow("SELECT * FROM drafts WHERE user_id=$1 ORDER BY id DESC LIMIT 1", uid)
    assert row["status"] == "hold" and row["pending_msg_id"] is None


async def test_H2_duplicate_screen_has_exit_buttons():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form()), bot)
    bot.log.clear()
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form()), bot)       # то же самое второй раз
    m = bot.to(uid)[0]
    assert "уже сохранено" in m["text"]
    assert set(bot.buttons(m)) >= {"mystatus", "create_ad", "nav:back"}              # не тупик
    # и «на проверке»
    uid2 = new_uid()
    await main.confirm_yes(FakeCB(uid2, "confirm:yes"), FakeState(form(phone="+79990000002")), bot)
    bot.log.clear()
    await main.confirm_yes(FakeCB(uid2, "confirm:yes"), FakeState(form(phone="+79990000002")), bot)
    m = bot.to(uid2)[0]
    assert m["text"] == "Такое объявление уже находится на проверке."
    assert set(bot.buttons(m)) >= {"mystatus", "create_ad", "nav:back"}


async def test_H3_form_with_free_slot_goes_to_admin_with_mode():
    uid, bot = new_uid(), FakeBot()
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form(phone="+79990000003")), bot)
    adm = bot.to(1)
    assert len(adm) == 1 and "Режим: ручной" in adm[0]["text"] and "бесплатная" in adm[0]["text"]
    assert any(b.startswith("approve:") for b in bot.buttons(adm[0]))


async def test_H4_payment_screen_short_with_paid_and_cancel_buttons():
    uid, bot = new_uid(), FakeBot()
    await main.pool().execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", uid)
    main.CARD_NUMBER = "1111 2222 3333 4444"
    await main._start_payment(bot, FakeUser(uid), "ru", 5, uid, 500)
    scr = bot.to(uid)[0]
    assert len(scr["text"]) < 220 and "1111 2222 3333 4444" in scr["text"] and "200" in scr["text"]
    assert "Переведите" not in scr["text"] and "будут начислены" not in scr["text"]
    b = bot.buttons(scr)
    assert [x.split(":")[0] for x in b] == ["payd", "paycancel"]                    # вместо «Назад»
    assert any("новая заявка" in m["text"].lower() for m in bot.to(1))
    pid = int(b[0].split(":")[1])
    # «Я оплатил» просит прислать чек
    bot.log.clear()
    await main.pay_paid_cb(FakeCB(uid, f"payd:{pid}"), bot)
    assert "Отправьте фото или файл чека" in bot.to(uid)[0]["text"]
    # «Отмена» закрывает заявку
    bot.log.clear()
    cb = FakeCB(uid, f"paycancel:{pid}")
    await main.pay_cancel_cb(cb, bot)
    assert (await billing.get_payment(main.pool(), pid))["status"] == "cancelled"
    assert "отменена" in bot.to(uid)[0]["text"] and any("отменил заявку" in m["text"] for m in bot.to(1))
    cb2 = FakeCB(uid, f"paycancel:{pid}")
    await main.pay_cancel_cb(cb2, bot)                                              # повтор — без побочных эффектов
    assert cb2.answers and cb2.answers[0][1] is True


async def test_H5_mode_choice_notifies_admin_and_sends_held_draft_to_moderation():
    uid, bot = new_uid(), FakeBot()
    main.CARD_NUMBER = "1111 2222 3333 4444"
    await occupy_free(uid)
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form(phone="+79990000004")), bot)   # hold
    held = await main.pool().fetchrow("SELECT * FROM drafts WHERE user_id=$1 AND status='hold'", uid)
    await main._start_payment(bot, FakeUser(uid), "ru", 2, uid, 500)
    p = await billing.find_receipt_target(main.pool(), uid)
    assert p["draft_id"] == held["id"]                                                  # платёж привязан к объявлению
    await billing.attach_receipt(main.pool(), p["id"], uid, "photo", "F", 1, 1)
    await billing.confirm_payment(main.pool(), p["id"], 1)
    bot.log.clear()
    await main.publication_mode_cb(FakeCB(uid, f"pubmode:auto:{p['id']}", mid=600), bot)
    adm = [m["text"] for m in bot.to(1)]
    assert any("Выбран режим публикации: 🤖 автоматический" in x for x in adm)          # админ видит режим
    mod = [m for m in bot.to(1) if "Новое объявление" in m["text"]]
    assert len(mod) == 1 and "Режим: автоматический" in mod[0]["text"]
    assert (await main.get_draft(held["id"]))["status"] == "pending"                   # ушло на проверку без повторной формы
    assert "отправлено на проверку" in " ".join(m["text"] for m in bot.to(uid))
    # повторное нажатие: ничего не дублируется
    n = len(bot.to(1))
    await main.publication_mode_cb(FakeCB(uid, f"pubmode:auto:{p['id']}", mid=601), bot)
    assert len([m for m in bot.to(1) if "Новое объявление" in m["text"]]) == 1


async def test_H6_approve_in_auto_mode_schedules_series_and_notifies_user():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    await credit(uid, 3)
    await billing.set_publication_mode(main.pool(), uid, "auto")
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form(phone="+79990000005")), bot)
    d = (await main.pool().fetchrow("SELECT id FROM drafts WHERE user_id=$1 ORDER BY id DESC LIMIT 1", uid))["id"]
    bot.log.clear()
    cb = FakeCB(1, f"approve:{d}")
    await main.approve_draft_cb(cb, bot)
    assert "Запланировано одинаковых публикаций: 3" in cb.message.replies[0]
    assert any("Запланировано публикаций: 3" in m["text"] for m in bot.to(uid))
    assert await billing.get_balance(main.pool(), uid) == (0, 0)
    # новую форму без публикаций — снова hold
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form(phone="+79990000006")), bot)
    assert (await main.pool().fetchrow("SELECT status FROM drafts WHERE user_id=$1 ORDER BY id DESC LIMIT 1", uid))["status"] == "hold"


async def test_H7_manual_approve_does_not_schedule_or_charge():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    await credit(uid, 2)
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form(phone="+79990000007")), bot)
    d = (await main.pool().fetchrow("SELECT id FROM drafts WHERE user_id=$1 ORDER BY id DESC LIMIT 1", uid))["id"]
    cb = FakeCB(1, f"approve:{d}")
    await main.approve_draft_cb(cb, bot)
    assert "режим: ручной" in cb.message.replies[0]
    row = await main.get_draft(d)
    assert row["scheduled_at"] is None and row["series_id"] is None and row["status"] == "approved"
    assert await billing.get_balance(main.pool(), uid) == (2, 0)           # ничего не списано до публикации
    assert (await publish(bot, d))["status"] == "published"
    assert await billing.get_balance(main.pool(), uid) == (1, 0)           # списано ровно 1 при выходе



async def test_H8_status_screen_groups_series_and_shows_hold():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    await credit(uid, 3)
    d = await mk(uid, date=future(10), funding="paid", approve=True)
    await billing.auto_distribute(main.pool(), d)
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form(phone="+79990000009")), bot)   # → hold
    text, kb = await main._status_screen(bot, uid, "ru")
    assert "серия: вышло 0 из 3" in text and "ждёт покупки публикаций" in text
    cds = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "buy:menu" in cds and sum(c.startswith("mycancel:") for c in cds) == 2   # серия — одной кнопкой + hold


async def test_H9_auto_partial_note_sent_to_user_when_not_all_fit():
    uid, bot = new_uid(), FakeBot()
    await occupy_free(uid)
    await credit(uid, 5)
    await billing.set_publication_mode(main.pool(), uid, "auto")
    await main.confirm_yes(FakeCB(uid, "confirm:yes"), FakeState(form(travel_date=future(1), phone="+79990000010")), bot)
    d = (await main.pool().fetchrow("SELECT id FROM drafts WHERE user_id=$1 ORDER BY id DESC LIMIT 1", uid))["id"]
    bot.log.clear()
    await main.approve_draft_cb(FakeCB(1, f"approve:{d}"), bot)
    msg = " ".join(m["text"] for m in bot.to(uid))
    assert "успеют выйти" in msg and "останутся на балансе" in msg
    bal, _ = await billing.get_balance(main.pool(), uid)
    assert 0 < bal < 5
