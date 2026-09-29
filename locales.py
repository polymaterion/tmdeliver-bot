"""
Тексты интерфейса бота на разных языках.

ПРАВИЛА ПЕРЕВОДА (важно соблюдать при добавлении нового текста):
- Интерфейс бота (меню, шаги формы, ошибки валидации, кнопки, НАЗВАНИЯ ГОРОДОВ
  на кнопках) — переводится, показывается на языке пользователя.
- ПРЕВЬЮ объявления (экран "Проверьте объявление" перед подтверждением,
  который видит только автор) — тоже переводится на язык автора, включая
  переведённое название города в маршруте.
- ИТОГОВЫЙ ПОСТ в канале/группах — НЕ переводится, ВСЕГДА на русском,
  включая названия городов. Его читают все подписчики канала, а не только
  автор, поэтому язык должен быть единым для всех.
- Тексты АДМИН-ПАНЕЛИ (уведомления о заявках, /stats, /broadcast и т.д.)
  тоже НЕ переводятся и всегда на русском.

Города хранятся и передаются между функциями как канонический русский текст
(ключ) — это то, что уходит в БД и в итоговый пост. Для отображения на кнопках
и в превью используется ANY_CITY_NAMES[lang][канонiчeский_город] через city_name().
Если для города нет перевода на нужный язык — используется русский оригинал.

Как добавить новый язык:
  1. Скопируй блок "ru" целиком в TEXTS, переведи все строки.
  2. Скопируй блок "ru" в ANY_CITY_NAMES, переведи названия городов.
  3. Добавь новый языковой код в LANGUAGES (например "en").
  4. Добавь кнопку выбора в kb_language() в main.py.

Как использовать в коде:
  from locales import t, city_name
  text = t(lang, "start_welcome")                  # без параметров
  text = t(lang, "date_range_hint", start=x, end=y) # с параметрами (str.format)
  label = city_name(lang, "Ашхабад")                # переведённое название города
"""

from typing import Optional

# Языки, доступные для выбора пользователем. Ключ — код языка, значение — название
# на самом этом языке (показывается в кнопке выбора).
LANGUAGES: dict[str, str] = {
    "ru": "Русский",
    "tk": "Türkmençe",
}

DEFAULT_LANG = "ru"


# ═══════════════════════════════════════════════════════════════════════════
# Названия городов — ключ везде канонический русский (как в CITIES в main.py,
# как в БД, как в итоговом посте канала). Значение — как показать на языке lang.
# ═══════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
# Справочник городов — загружается из cities.json (редактируемый вручную файл
# рядом с этим скриптом). Формат файла и инструкция — в самом cities.json
# (ключ "_readme"). Правка применяется только при следующем запуске бота
# (docker compose restart bot), файл не перечитывается на лету.
#
# ANY_CITY_NAMES: dict[lang][канонический_русский_город] = локализованное имя.
# Используется и для 10 кнопок быстрого выбора (popular=true в JSON, первые
# 10 по порядку появления в файле — см. POPULAR_CITIES), и для ручного ввода
# ("Другой город" → нечёткий поиск по этому же словарю, см. find_city_match
# в main.py).
# ═══════════════════════════════════════════════════════════════════════════
import json as _json
import logging as _logging
import os as _os

_logger = _logging.getLogger(__name__)
_CITIES_JSON_PATH = _os.path.join(_os.path.dirname(__file__), "cities.json")


def _load_cities_from_json() -> tuple[dict[str, dict[str, str]], list[str]]:
    """
    Читает cities.json и строит (ANY_CITY_NAMES, POPULAR_CITIES).
    Если файл отсутствует или битый — бот не должен молча остаться без
    городов вообще, поэтому при ошибке логируем и поднимаем исключение:
    лучше явный сбой при старте, чем тихо неработающий выбор маршрута.
    """
    with open(_CITIES_JSON_PATH, encoding="utf-8") as f:
        data = _json.load(f)

    any_city_names: dict[str, dict[str, str]] = {"ru": {}, "tk": {}}
    popular: list[str] = []

    for country in data["countries"]:
        for city in country["cities"]:
            canonical = city["ru"]
            any_city_names["ru"][canonical] = canonical
            any_city_names["tk"][canonical] = city.get("tk") or canonical
            if city.get("popular"):
                popular.append(canonical)

    if not popular:
        _logger.warning(
            "cities.json: ни один город не помечен popular=true — "
            "кнопки быстрого выбора будут пустыми, доступен только "
            "ручной ввод города."
        )
    elif len(popular) > 10:
        _logger.warning(
            "cities.json: помечено popular=true городов больше 10 (%d) — "
            "используются только первые 10 по порядку в файле.",
            len(popular),
        )
        popular = popular[:10]

    return any_city_names, popular


try:
    ANY_CITY_NAMES, POPULAR_CITIES = _load_cities_from_json()
except (FileNotFoundError, _json.JSONDecodeError, KeyError) as _e:
    _logger.critical("Не удалось загрузить cities.json: %s", _e)
    raise RuntimeError(
        f"cities.json отсутствует или повреждён ({_e}). "
        f"Проверь файл {_CITIES_JSON_PATH} — он должен лежать рядом с main.py."
    ) from _e


def city_name(lang: Optional[str], city: str) -> str:
    """
    Возвращает переведённое название города для отображения (кнопки, превью).
    lang=None — явный алиас "канонический русский" (используется когда нужен
    гарантированно русский текст, например для итогового поста в канале).
    ВАЖНО: сам город как значение для БД/итогового поста всегда остаётся
    каноническим русским — city_name() используется ТОЛЬКО для вывода на экран.
    Работает для любого города из ANY_CITY_NAMES, включая введённые вручную —
    если перевода на нужный язык нет, возвращается исходное написание.
    """
    if lang is None:
        return city
    return ANY_CITY_NAMES.get(lang, {}).get(city) or ANY_CITY_NAMES[DEFAULT_LANG].get(city, city)


TEXTS: dict[str, dict[str, str]] = {

    # ═══════════════════════════════════════════════════════════════════════
    "ru": {

        # ── Выбор языка ──────────────────────────────────────────────────
        "choose_language": "🌐 Выберите язык бота:",
        "language_changed": "✅ Язык изменён на русский.",

        # ── /start и подписка ────────────────────────────────────────────
        "start_welcome": (
            "👋 <b>Здравствуйте!</b>\n\n"
            "Здесь можно разместить объявление о поездке, передаче документов и посылок.\n\n"
            "Для размещения объявления подпишитесь на канал 👇"
        ),
        "btn_subscribe":       "Подписаться",
        "btn_subscribed":      "Я подписан ✅",
        "not_subscribed_yet":  "Похоже вы ещё не подписаны на канал 👀",
        "subscribe_first":     "Сначала подпишитесь на канал",

        # ── Выбор типа объявления ────────────────────────────────────────
        "choose_ad_type":      "Выберите тип объявления:",
        "btn_ad_carrier":      "✈️ Лечу — возьму посылки",
        "btn_ad_seeker":       "🔎 Ищу попутчика",

        # ── Перевозчик: маршрут ──────────────────────────────────────────
        "how_many_cities":     "Сколько городов в вашем маршруте?",
        "city_from":           "Из какого города вы выезжаете?",
        "city_to":             "Куда прибываете?",
        "city_step_n":         "Укажите город {step} из {total}:",
        "route_so_far":        "\n\nМаршрут: {route}",
        "city_already_used":   "⚠️ Этот город уже стоит перед этим. Выберите другой.",
        "route_confirmed":     "✅ Маршрут: <b>{route}</b>\n\n🗓 Укажите дату поездки.\n\nНапример:\n<code>{example}</code>",

        # ── Ручной ввод города ("Другой город") ─────────────────────────────
        "city_pick_hint":      "Нет нужного города среди кнопок? Напишите его название вручную.",
        "btn_other_city":      "✏️ Другой город",
        "ask_custom_city":     "Напишите название города:",
        "city_not_found":      "😕 Не нашёл такой город. Попробуйте написать иначе или выберите из списка кнопкой «Назад».",
        "city_suggestions":    "Возможно, вы имели в виду один из этих городов?",

        # ── Дата ──────────────────────────────────────────────────────────
        "date_bad_format":     "❌ Неверный формат. Напишите дату так:\n<code>{example}</code>",
        "date_invalid":        "❌ Неверная дата. Проверьте число и месяц.",
        "date_too_early":      "❌ Дата должна быть не раньше завтрашнего дня.",
        "date_out_of_range":   "❌ Дата должна быть в текущем или следующем месяце.\nДопустимо: с {start} по {end}.",

        # ── Груз ──────────────────────────────────────────────────────────
        "what_to_take":        "Что можете взять с собой?",
        "btn_cargo_docs":      "Только документы",
        "btn_cargo_parcels":   "Посылки и документы",
        "cargo_docs":          "только документы",
        "cargo_parcels":       "документы и посылки",

        # ── Телефон ───────────────────────────────────────────────────────
        "ask_phone": (
            "Укажите ваш номер телефона.\n\n"
            "Введите в международном формате:\n"
            "<code>+79991234567</code>  или  <code>+99361234567</code>"
        ),
        "phone_bad_format": (
            "❌ Неверный формат.\n\n"
            "Введите номер в международном формате:\n"
            "<code>+79991234567</code>  или  <code>+99361234567</code>"
        ),

        # ── Username ──────────────────────────────────────────────────────
        "ask_username":        "Отправьте ваш @username (необязательно)",
        "btn_skip":            "Пропустить ➡️",
        "username_invalid":    "❌ Неверный username. Допустимы латинские буквы, цифры и _ (3–32 символа).\n\nПопробуйте ещё раз или пропустите:",

        # ── Навигация ─────────────────────────────────────────────────────
        "btn_back":            "⬅️ Назад",

        # ── Ищу попутчика ─────────────────────────────────────────────────
        "seeker_city_from":    "Из какого города нужно передать?",
        "seeker_city_to":      "В какой город нужно доставить?",
        "seeker_from_confirmed": "✅ Откуда: <b>{city}</b>\n\nВ какой город нужно доставить?",
        "seeker_choose_other_city": "⚠️ Выберите другой город",
        "seeker_when":          "Когда нужно передать?",
        "days_10":               "В ближайшие 10 дней",
        "days_20":               "В ближайшие 20 дней",
        "days_30":               "В ближайшие 30 дней",
        "seeker_what_to_pass":   "Что нужно передать?",

        # ── Результаты поиска попутчиков ────────────────────────────────────
        "search_looking":        "🔍 Ищу подходящих попутчиков…",
        "search_found_header":   "🎉 Нашлись подходящие варианты!\n\nВот кто едет вашим маршрутом:",
        "search_none_found":     "😕 Пока никто не летит вашим маршрутом в ближайшее время.\n\nМожете разместить своё объявление — как только появится попутчик, вы окажетесь среди первых, кого он увидит.",
        "search_not_matched":    "Если предложенные варианты вам не подошли, можете разместить объявление.",
        "btn_place_ad":          "📢 Разместить объявление",

        # ── Превью объявления (текст-обёртка, сам пост не переводится) ────
        "preview_check_ad":      "👀 <b>Проверьте объявление:</b>\n\n",

        # ── Подписи внутри превью объявления (переводятся, само объявление в канале — нет) ──
        "preview_carrier_title": "Лечу",
        "preview_seeker_title":  "Ищу попутчика",
        "preview_take_docs":     "Возьму",
        "preview_deliver_docs":  "Нужно передать",

        # ── Подтверждение ────────────────────────────────────────────────
        "btn_confirm":          "✅ Подтвердить",
        "btn_restart":          "🔄 Начать заново",
        "data_lost_restart":    "Данные потерялись. Начните заново.",
        "ad_sent_for_review":   "✅ Объявление отправлено на публикацию.",
        "ad_rejected":          "❌ Объявление не прошло модерацию.",

        # ── Публикации, оплата, очередь (freemium) ──────────────────────────
        "btn_buy":              "💳 Дополнительные публикации",
        "btn_status":           "📋 Посмотреть статус",
        "btn_cancel_short":     "Отмена",
        "btn_close":            "Закрыть",
        "open_draft_block": (
            "⏳ У вас уже есть объявление, которое ожидает проверки или публикации.\n\n"
            "Дождитесь завершения текущей заявки, прежде чем отправлять новое объявление."
        ),
        "buy_menu":             "💳 Дополнительные публикации\n\nВыберите пакет:",
        "btn_pkg":              "{qty} {word} — {price} ₽",
        "btn_buy_pkg":          "Купить {qty} {word} — {price} ₽",
        "btn_other_pkg":        "Выбрать другой пакет",
        "buy_warning": (
            "⚠️ До вашей поездки осталось {days_text}.\n\n"
            "По правилам канала можно публиковать не более 1 объявления за 24 часа.\n\n"
            "Вы не успеете использовать все {qty} {word} до вылета.\n\n"
            "Вы можете купить пакет и сохранить оставшиеся публикации для будущих объявлений."
        ),
        "trip_passed_no_pay": (
            "⚠️ Дата поездки в вашем объявлении уже прошла, поэтому опубликовать его нельзя, "
            "и оплата для него не требуется.\n\n"
            "Создайте объявление с актуальной датой поездки."
        ),
        "pay_screen": (
            "💳 Оплата публикаций\n\n"
            "Заявка: #{id}\n"
            "Пакет: {qty} {word}\n"
            "Сумма: {price} ₽\n\n"
            "Переведите {price} ₽ на карту:\n\n"
            "<code>{card}</code>{holder}\n\n"
            "После перевода отправьте чек (фото или файл) в этот бот.\n\n"
            "После проверки оплаты администратором публикации будут начислены на ваш баланс."
        ),
        "pay_no_card":          "⚠️ Реквизиты для оплаты временно не настроены. Напишите в поддержку.",
        "pay_receipt_received": "🧾 Чек по заявке #{id} получен. Администратор проверит оплату — мы сообщим о результате.",
        "pay_confirmed_user": (
            "✅ Оплата по заявке #{id} подтверждена.\n\n"
            "На баланс начислено: {qty} {word}. Доступно платных публикаций: {balance}."
        ),
        "pay_rejected_user": (
            "❌ Чек по заявке #{id} отклонён. Публикации не начислены.\n\n"
            "Вы можете отправить чек повторно или создать новую заявку."
        ),
        "btn_resend_receipt":   "🧾 Отправить чек повторно",
        "btn_new_payment":      "💳 Новая заявка",
        "pay_resend_prompt":    "🧾 Отправьте чек по заявке #{id} (фото или файл) в этот чат.",
        "ad_approved_user":     "✅ Ваше объявление одобрено и готово к публикации. Администратор опубликует его в ближайшее время.",
        "ad_waiting_payment_user": (
            "💳 Объявление ожидает оплаты: бесплатная публикация сейчас занята, "
            "а платных публикаций на балансе нет.\n\nКупите пакет — объявление останется в очереди."
        ),
        "ad_published_paid_note": "Списана 1 платная публикация. Осталось на балансе: {balance}.",
        "btn_create_new":       "✏️ Создать объявление",
        "status_head":          "📋 Ваше объявление #{id}",
        "status_pending":       "Статус: на проверке у модератора",
        "status_approved":      "Статус: одобрено, ожидает публикации администратором",
        "status_publishing":    "Статус: публикуется…",
        "status_publish_error": "Статус: ошибка публикации — администратор повторит попытку",
        "status_waiting_payment": "Ожидает оплаты: нет свободной бесплатной публикации и платных на балансе.",
        "status_funding_free":  "Тип публикации: бесплатная",
        "status_funding_paid":  "Тип публикации: платная",
        "status_balance":       "Платных публикаций на балансе: {n}",
        "status_next_at":       "Следующая публикация доступна с: {dt}",
        "status_no_open":       "У вас нет объявлений в очереди.",
        "btn_cancel_ad":        "🗑 Отменить объявление",
        "btn_use_paid":         "💳 Использовать платную публикацию",
        "btn_use_free":         "🎁 Использовать бесплатную публикацию",
        "cancel_done":          "🗑 Объявление отменено. Публикации на балансе не затронуты.",
        "cancel_failed":        "Это объявление уже нельзя отменить.",
        "funding_changed":      "✅ Тип публикации изменён.",
        "funding_free_taken":   "Бесплатная публикация сейчас занята активным объявлением.",
        "funding_no_balance":   "На балансе нет платных публикаций.",
        "balance_text": (
            "💼 Ваши публикации\n\n"
            "Платных на балансе: {balance}\n"
            "Бесплатная публикация: {free}"
        ),
        "balance_free_yes":     "доступна",
        "balance_free_no":      "занята активным объявлением до {dt}",
        "manual_credit_user":   "🎁 Администратор изменил ваш баланс публикаций: {delta:+d}. Доступно: {balance}.",
        "ad_rejected_paid_note": "Ваши купленные публикации сохранены на балансе.",
        "unit_day_1": "день", "unit_day_2": "дня", "unit_day_5": "дней",
        "unit_pub_1": "публикация", "unit_pub_2": "публикации", "unit_pub_5": "публикаций",
        "today_word":           "сегодня",

        # ── Защита от повторных объявлений ──────────────────────────────────
        "duplicate_found": (
            "🔁 У вас уже есть точно такое же активное объявление — тот же "
            "маршрут, дата и контакты.\n\n"
            "Вместо создания дубликата можно просто повторить (переопубликовать) "
            "уже существующее объявление — оно снова окажется свежим в канале."
        ),
        "btn_repeat_ad":        "🔁 Повторить объявление",
        "ad_repeated":          "✅ Объявление повторно опубликовано.",
        "ad_repeat_failed":     "❌ Не получилось повторить объявление. Попробуйте ещё раз позже.",
        "ad_published_thanks": (
            "🎉 Ваше объявление опубликовано.\n\n"
            "Хотите оставить отзыв? Может есть предложения или замечания? "
            "Будем рады вашему отзыву."
        ),

        # ── Отзыв ─────────────────────────────────────────────────────────
        "btn_leave_feedback":   "💬 Оставить отзыв",
        "ask_feedback": (
            "Напишите одним сообщением ваш отзыв о боте или канале.\n\n"
            "Нам будет полезно узнать, что вам понравилось, что можно улучшить "
            "и каких функций не хватает.\n\n"
            "Мы читаем все отзывы и используем их для развития проекта."
        ),
        "feedback_thanks":      "Ваш отзыв получен! Спасибо за обратную связь 😊",

        # ── Общие кнопки ──────────────────────────────────────────────────
        "btn_create_ad":         "✏️ Создать объявление",

        # ── /posts ────────────────────────────────────────────────────────
        "no_active_ads":         "📭 У вас нет активных объявлений.",
        "your_active_ads":       "📋 <b>Ваши активные объявления ({count}):</b>\n",

        # ── /help ─────────────────────────────────────────────────────────
        "help_text": (
            "🆘 <b>Помощь</b>\n\n"
            "По всем вопросам обращайтесь к @{username}\n\n"
            "<b>Команды:</b>\n"
            "/start — перезапустить бота\n"
            "/posts — мои активные объявления\n"
            "/language — сменить язык\n"
            "/help — эта справка"
        ),
    },

    # ═══════════════════════════════════════════════════════════════════════
    "tk": {

        "choose_language": "🌐 Botuň dilini saýlaň:",
        "language_changed": "✅ Dil türkmen diline üýtgedildi.",

        "start_welcome": (
            "👋 <b>Salam!</b>\n\n" 
            "Bu ýerde ýükleri we resminamalary ugratmak barada "
            "bildiriş ýerleşdirip bilersiňiz.\n\n"
            "Bildiriş ýerleşdirmek üçin kanala agza boluň 👇"
        ),
        "btn_subscribe":       "Agza bol",
        "btn_subscribed":      "Men agza ✅",
        "not_subscribed_yet":  "Siz entäk kanala agza bolmadyňyz 👀",
        "subscribe_first":     "Ilki kanala agza boluň",

        "choose_ad_type":      "Bildiriş görnüşini saýlaň:",
        "btn_ad_carrier":      "✈️ Özüm gidip barýaryn",
        "btn_ad_seeker":       "🔎 Gidip barýan gözleýärin",

        "how_many_cities":     "Ugruňyzda näçe şäher bar?",
        "city_from":           "Haýsy şäherden ugraýarsyňyz?",
        "city_to":             "Haýsy şähere baryarsyňyz?",
        "city_step_n":         "{total}-den {step}-nji şäheri görkeziň:",
        "route_so_far":        "\n\nUgur: {route}",
        "city_already_used":   "⚠️ Bu şäher eýýäm ondan öň bar. Başga saýlaň.",
        "route_confirmed":     "✅ Ugur: <b>{route}</b>\n\n🗓 Ugraýan seneňizi görkeziň.\n\nMysal üçin:\n<code>{example}</code>",

        # ── Şäheri elden ýazmak ("Başga şäher") ─────────────────────────────
        "city_pick_hint":      "Gerekli şäher düwmeleriň arasynda ýokmy? Adyny elde ýazyň.",
        "btn_other_city":      "✏️ Başga şäher",
        "ask_custom_city":     "Şäheriň adyny ýazyň:",
        "city_not_found":      "😕 Beýle şäher tapylmady. Başgaça ýazyp görüň ýa-da «Yza» düwmesi bilen sanawdan saýlaň.",
        "city_suggestions":    "Belki, şu şäherleriň birini göz öňünde tutdunyz?",

        "date_bad_format":     "❌ Nädogry format. Senäni şeýle ýazyň:\n<code>{example}</code>",
        "date_invalid":        "❌ Nädogry sene. Güni we aýy barlaň.",
        "date_too_early":      "❌ Sene ertirden ir bolmaly däl.",
        "date_out_of_range":   "❌ Sene şu aýda ýa-da indiki aýda bolmaly.\nRugsat berilýär: {start} — {end}.",

        "what_to_take":        "Näme alyp bilersiňiz?",
        "btn_cargo_docs":      "Diňe resminamalar",
        "btn_cargo_parcels":   "Ýük we resminamalar",
        "cargo_docs":          "diňe resminamalar",
        "cargo_parcels":       "resminamalar we ýük",

        "ask_phone": (
            "Telefon belgiňizi görkeziň.\n\n"
            "Halkara formatda ýazyň:\n"
            "<code>+79991234567</code>  ýa-da  <code>+99361234567</code>"
        ),
        "phone_bad_format": (
            "❌ Nädogry format.\n\n"
            "Belgini halkara formatda ýazyň:\n"
            "<code>+79991234567</code>  ýa-da  <code>+99361234567</code>"
        ),

        "ask_username":        "Öz @username-ňizi iberiň (hökman däl)",
        "btn_skip":            "Geç ➡️",
        "username_invalid":    "❌ Nädogry username. Diňe latyn harplary, sanlar we _ rugsat berilýär (3–32 nyşan).\n\nGaýtadan synanyşyň ýa-da geçiň:",

        # ── Nawigasiýa ────────────────────────────────────────────────────
        "btn_back":            "⬅️ Yza",

        "seeker_city_from":    "Haýsy şäherden ibermeli?",
        "seeker_city_to":      "Haýsy şähere eltmeli?",
        "seeker_from_confirmed": "✅ Nireden: <b>{city}</b>\n\nHaýsy şähere eltmeli?",
        "seeker_choose_other_city": "⚠️ Başga şäher saýlaň",
        "seeker_when":          "Haçan ibermeli?",
        "days_10":               "Indiki 10 günde",
        "days_20":               "Indiki 20 günde",
        "days_30":               "Indiki 30 günde",
        "seeker_what_to_pass":   "Näme ibermeli?",

        # ── Ýoldaş gözleg netijeleri ─────────────────────────────────────
        "search_looking":        "🔍 Laýyk gelýän ýoldaşlar gözlenýär…",
        "search_found_header":   "🎉 Laýyk gelýän wariantlar tapyldy!\n\nSiziň marşrutyňyz bilen gidýänler:",
        "search_none_found":     "😕 Häzirlikçe siziň marşrutyňyz bilen hiç kim uçmaýar.\n\nÖz yglanyňyzy ýerleşdirip bilersiňiz — ýoldaş tapylan badyna siz ilkinjileriň hatarynda görersiňiz.",
        "search_not_matched":    "Teklip edilen wariantlar size laýyk gelmedik bolsa, yglanyňyzy ýerleşdirip bilersiňiz.",
        "btn_place_ad":          "📢 Yglan ýerleşdir",

        "preview_check_ad":      "👀 <b>Bildirişi barlaň:</b>\n\n",

        "preview_carrier_title": "Özüm gidip barýaryn",
        "preview_seeker_title":  "Gidip barýan gözleýärin",
        "preview_take_docs":     "Alyp bilýän",
        "preview_deliver_docs":  "Ibermeli",

        "btn_confirm":          "✅ Tassykla",
        "btn_restart":          "🔄 Täzeden başla",
        "data_lost_restart":    "Maglumatlar ýitdi. Täzeden başlaň.",
        "ad_sent_for_review":   "✅ Bildiriş barlaga iberildi.",
        "ad_rejected":          "❌ Bildiriş barlagdan geçmedi.",

        # ── Çap etmeler, töleg, nobat (freemium) ────────────────────────────
        "btn_buy":              "💳 Goşmaça çap etmeler",
        "btn_status":           "📋 Ýagdaýy görmek",
        "btn_cancel_short":     "Ýatyr",
        "btn_close":            "Ýap",
        "open_draft_block": (
            "⏳ Siziň barlaga ýa-da çap edilmegine garaşýan bildirişiňiz bar.\n\n"
            "Täze bildiriş iberilmezden öň şu arzanyň tamamlanmagyna garaşyň."
        ),
        "buy_menu":             "💳 Goşmaça çap etmeler\n\nPaket saýlaň:",
        "btn_pkg":              "{qty} {word} — {price} ₽",
        "btn_buy_pkg":          "Satyn al: {qty} {word} — {price} ₽",
        "btn_other_pkg":        "Başga paket saýla",
        "buy_warning": (
            "⚠️ Sapara çenli {days_text} galdy.\n\n"
            "Kanalyň düzgünlerine görä 24 sagatda 1 bildirişden köp çap edip bolmaýar.\n\n"
            "Uçuşa çenli {qty} {word} hemmesini ulanyp ýetişmersiňiz.\n\n"
            "Paketi satyn alyp, galanlaryny indiki bildirişler üçin saklap bilersiňiz."
        ),
        "trip_passed_no_pay": (
            "⚠️ Bildirişdäki saparyň senesi geçdi, şonuň üçin ony çap edip bolmaýar we töleg gerek däl.\n\n"
            "Täze senesi bilen bildiriş dörediň."
        ),
        "pay_screen": (
            "💳 Çap etmeleriň tölegi\n\n"
            "Arza: #{id}\n"
            "Paket: {qty} {word}\n"
            "Mukdar: {price} ₽\n\n"
            "{price} ₽ şu karta geçiriň:\n\n"
            "<code>{card}</code>{holder}\n\n"
            "Geçirenden soň çegi (surat ýa-da faýl) şu bota iberiň.\n\n"
            "Administrator tölegi barlanyndan soň çap etmeler balansyňyza goşular."
        ),
        "pay_no_card":          "⚠️ Töleg maglumatlary wagtlaýyn sazlanmady. Goldawa ýazyň.",
        "pay_receipt_received": "🧾 #{id} arza boýunça çek alyndy. Administrator barlar — netijäni habar bereris.",
        "pay_confirmed_user": (
            "✅ #{id} arza boýunça töleg tassyklandy.\n\n"
            "Balansa goşuldy: {qty} {word}. Elýeterli tölegli çap etmeler: {balance}."
        ),
        "pay_rejected_user": (
            "❌ #{id} arza boýunça çek ret edildi. Çap etmeler goşulmady.\n\n"
            "Çegi täzeden iberip ýa-da täze arza döredip bilersiňiz."
        ),
        "btn_resend_receipt":   "🧾 Çegi täzeden ibermek",
        "btn_new_payment":      "💳 Täze arza",
        "pay_resend_prompt":    "🧾 #{id} arza boýunça çegi (surat ýa-da faýl) şu çata iberiň.",
        "ad_approved_user":     "✅ Bildirişiňiz makullandy we çap etmäge taýýar. Administrator ýakynda çap eder.",
        "ad_waiting_payment_user": (
            "💳 Bildiriş tölegi garaşýar: mugt çap etmek hazir eýelenen, balansda tölegli çap etme ýok.\n\n"
            "Paket satyn alyň — bildiriş nobatda galar."
        ),
        "ad_published_paid_note": "1 tölegli çap etme hasapdan çykaryldy. Balansda galdy: {balance}.",
        "btn_create_new":       "✏️ Bildiriş döret",
        "status_head":          "📋 Siziň #{id} bildirişiňiz",
        "status_pending":       "Ýagdaý: moderatoryň barlagynda",
        "status_approved":      "Ýagdaý: makullandy, administratoryň çap etmegine garaşýar",
        "status_publishing":    "Ýagdaý: çap edilýär…",
        "status_publish_error": "Ýagdaý: çap etme säwligi — administrator täzeden synanyşar",
        "status_waiting_payment": "Töleg garaşylýar: mugt çap etme eýelenen, balansda tölegli ýok.",
        "status_funding_free":  "Çap etme görnüşi: mugt",
        "status_funding_paid":  "Çap etme görnüşi: tölegli",
        "status_balance":       "Balansdaky tölegli çap etmeler: {n}",
        "status_next_at":       "Indiki çap etme şundan başlap elýeterli: {dt}",
        "status_no_open":       "Nobatda bildirişiňiz ýok.",
        "btn_cancel_ad":        "🗑 Bildirişi ýatyr",
        "btn_use_paid":         "💳 Tölegli çap etmäni ulan",
        "btn_use_free":         "🎁 Mugt çap etmäni ulan",
        "cancel_done":          "🗑 Bildiriş ýatyryldy. Balansdaky çap etmelere täsir edilmedi.",
        "cancel_failed":        "Bu bildirişi indi ýatyryp bolmaýar.",
        "funding_changed":      "✅ Çap etme görnüşi üýtgedildi.",
        "funding_free_taken":   "Mugt çap etme hazir işjeň bildiriş bilen eýelenen.",
        "funding_no_balance":   "Balansda tölegli çap etme ýok.",
        "balance_text": (
            "💼 Siziň çap etmeleriňiz\n\n"
            "Balansda tölegli: {balance}\n"
            "Mugt çap etme: {free}"
        ),
        "balance_free_yes":     "elýeterli",
        "balance_free_no":      "{dt} çenli işjeň bildiriş bilen eýelenen",
        "manual_credit_user":   "🎁 Administrator çap etme balansyňyzy üýtgetdi: {delta:+d}. Elýeterli: {balance}.",
        "ad_rejected_paid_note": "Satyn alan çap etmeleriňiz balansda saklandy.",
        "unit_day_1": "gün", "unit_day_2": "gün", "unit_day_5": "gün",
        "unit_pub_1": "çap etme", "unit_pub_2": "çap etme", "unit_pub_5": "çap etme",
        "today_word":           "şu gün",

        # ── Gaýtalanýan bildirişlerden goragy ────────────────────────────────
        "duplicate_found": (
            "🔁 Sizde eýýäm şeýle bildiriş bar — şol bir ugur, sene we habarlaşmak üçin maglumatlar.\n\n"
            "Täzeden döretmegiň deregine, bar bolan bildirişi täzeden çap edip bilersiňiz — "
            "ol ýene-de kanalda täze bolar."
        ),
        "btn_repeat_ad":        "🔁 Bildirişi gaýtala",
        "ad_repeated":          "✅ Bildiriş täzeden çap edildi.",
        "ad_repeat_failed":     "❌ Bildirişi gaýtalamak başartmady. Birazdan gaýtadan synanyşyň.",
        "ad_published_thanks": (
            "🎉 Bildirişiňiz kanala goýuldy.\n\n"
            "Pikir bildirmek isleýärsiňizmi? Teklip ýa-da bellik bar bolsa, "
            "pikiriňize begenerdik."
        ),

        "btn_leave_feedback":   "💬 Pikir bildir",
        "ask_feedback": (
            "Bot ýa-da kanal barada pikiriňizi bir gezekde ýazyň.\n\n"
            "Näme halanyňyzy, nämäni gowulandyrmalydygyny we "
            "haýsy funksiýalaryň ýetmeýändigini bilmek bize peýdaly bolar.\n\n"
            "Biz ähli pikirleri okaýarys we kanaly we boty gowylandyrmak üçin ulanýarys."
        ),
        "feedback_thanks":      "Pikiriňiz kabul edildi! Sag boluň 😊",

        "btn_create_ad":         "✏️ Bildiriş döret",

        "no_active_ads":         "📭 Işjeň bildirişiňiz ýok.",
        "your_active_ads":       "📋 <b>Işjeň bildirişleriňiz ({count}):</b>\n",

        "help_text": (
            "🆘 <b>Kömek</b>\n\n"
            "Ähli soraglar üçin @{username} bilen habarlaşyň\n\n"
            "<b>Buýruklar:</b>\n"
            "/start — boty täzeden başlat\n"
            "/posts — işjeň bildirişlerim\n"
            "/language — dili üýtget\n"
            "/help — kömek"
        ),
    },
}


def t(lang: Optional[str], key: str, **kwargs) -> str:
    """
    Возвращает текст по ключу на нужном языке.
    Если языка или ключа нет — берёт из DEFAULT_LANG.
    kwargs подставляются через str.format().
    """
    lang = lang if lang in TEXTS else DEFAULT_LANG
    text = TEXTS[lang].get(key) or TEXTS[DEFAULT_LANG].get(key, f"[[{key}]]")
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text
