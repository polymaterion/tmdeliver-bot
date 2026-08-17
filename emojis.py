"""
Единый реестр всех эмодзи бота.

КАК ЭТО РАБОТАЕТ:
Каждый эмодзи описан одной записью в EMOJIS: обычный unicode-символ (fallback)
и опциональный ID премиум-эмодзи (premium_id). Пока premium_id = None — везде
используется обычный fallback-символ. Как только вписываешь сюда ID премиум-
эмодзи — бот автоматически начинает использовать его ВЕЗДЕ: и в личных
сообщениях бота (через HTML-тег <tg-emoji>), и в постах канала/групп
(через MessageEntity custom_emoji).

КАК ПОСТАВИТЬ ПРЕМИУМ-ЭМОДЗИ:
1. Получи custom_emoji_id нужного премиум-эмодзи.
2. Впиши этот ID в поле "premium_id" нужной записи ниже.
3. Готово — больше нигде ничего менять не нужно, ни в main.py, ни в locales.py.

ВАЖНО:
- Премиум-эмодзи отображаются как задумано только если у канала/группы
  (для постов) или у получателя (для личных сообщений) есть Telegram Premium
  либо соответствующий буст-уровень канала. Иначе Telegram сам покажет fallback.
- Для постов (каналы/группы) <tg-emoji> HTML-тег не работает — только
  MessageEntity, поэтому используется единый механизм ниже (см. e() и entity_emoji()).
"""

from typing import NamedTuple, Optional


class EmojiDef(NamedTuple):
    fallback: str
    premium_id: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# РЕЕСТР ЭМОДЗИ — правь тут. Ключ слева — имя, используемое в коде.
# Чтобы включить премиум-версию: вставь ID вторым аргументом, например:
#   "plane": EmojiDef("✈️", "5251661859500628328"),
# Чтобы вернуть обычный эмодзи — сотри ID, оставь None:
#   "plane": EmojiDef("✈️", None),
# ═══════════════════════════════════════════════════════════════════════════

EMOJIS: dict[str, EmojiDef] = {
    # ── Используются в постах (канал/группы) и в личке бота ──────────────
    "plane":          EmojiDef("✈️"),    # лечу / маршрут
    "arrow":          EmojiDef("➡️"),    # стрелка маршрута (в личке бота)
    "route_arrow":    EmojiDef("➜"),     # стрелка маршрута (в постах канала/групп)
    "phone":          EmojiDef("📞"),    # телефон (перевозчик)
    "mobile":         EmojiDef("📱"),    # телефон (ищу попутчика)
    "calendar":       EmojiDef("🗓"),    # дата (перевозчик)
    "calendar_alt":   EmojiDef("📅"),    # дата (ищу попутчика)
    "package":        EmojiDef("📦"),    # груз
    "telegram":       EmojiDef("💬"),    # telegram-контакт в объявлении
    "search":         EmojiDef("🔎"),    # ищу попутчика

    # ── Только в личных сообщениях бота (меню, статусы, кнопки) ──────────
    "wave":           EmojiDef("👋"),    # приветствие
    "globe":          EmojiDef("🌐"),    # выбор языка
    "check":          EmojiDef("✅"),    # успех / подтверждение
    "cross":          EmojiDef("❌"),    # ошибка / отклонение
    "warning":        EmojiDef("⚠️"),    # предупреждение
    "eyes":           EmojiDef("👀"),    # внимание / превью
    "party":          EmojiDef("🎉"),    # объявление опубликовано
    "inbox":          EmojiDef("📥"),    # новая заявка (админу)
    "outbox_tray":    EmojiDef("📭"),    # пусто (нет объявлений)
    "clipboard":      EmojiDef("📋"),    # список объявлений
    "sos":            EmojiDef("🆘"),    # помощь /help
    "megaphone":      EmojiDef("📢"),    # рассылка / публикация
    "memo":           EmojiDef("✏️"),    # создать / редактировать
    "clock":          EmojiDef("🕐"),    # отложенная публикация
    "person":         EmojiDef("👤"),    # пользователь
    "id_card":        EmojiDef("🆔"),    # ID пользователя
    "chart":          EmojiDef("📊"),    # статистика
    "trend_up":       EmojiDef("🔝"),    # топ направлений
    "group":          EmojiDef("👥"),    # групповые чаты / пользователи
    "outbox":         EmojiDef("📤"),    # рассылка началась

    # ── Флаги стран (для маршрутов) ───────────────────────────────────────
    "flag_russia":        EmojiDef("🇷🇺"),
    "flag_turkmenistan":  EmojiDef("🇹🇲"),
}


# ═══════════════════════════════════════════════════════════════════════════
# API для использования в коде
# ═══════════════════════════════════════════════════════════════════════════

def e(key: str) -> str:
    """
    Эмодзи для личных сообщений бота (HTML, parse_mode="HTML").
    Если задан premium_id — вернёт <tg-emoji> тег, иначе — обычный символ.
    Использование: f"{e('plane')} Лечу"
    """
    d = EMOJIS[key]
    if d.premium_id:
        return f'<tg-emoji emoji-id="{d.premium_id}">{d.fallback}</tg-emoji>'
    return d.fallback


def raw(key: str) -> str:
    """Обычный unicode-символ эмодзи без какой-либо разметки (для alt-текста, логов и т.п.)."""
    return EMOJIS[key].fallback


def entity_emoji(builder, key: str):
    """
    Добавляет эмодзи в EntityBuilder (для постов канала/групп).
    Если задан premium_id — добавится MessageEntity custom_emoji, иначе — просто текст.
    Использование: entity_emoji(b, "plane").t(" Лечу")
    """
    d = EMOJIS[key]
    if d.premium_id:
        return builder.emoji(d.fallback, d.premium_id)
    return builder.t(d.fallback)


_CITY_TO_FLAG_KEY: dict[str, str] = {
    "Москва":          "flag_russia",
    "Санкт-Петербург": "flag_russia",
    "Казань":          "flag_russia",
    "Ашхабад":         "flag_turkmenistan",
    "Туркменабад":     "flag_turkmenistan",
    "Дашогуз":         "flag_turkmenistan",
    "Мары":            "flag_turkmenistan",
    "Туркменбаши":     "flag_turkmenistan",
    "Балканабад":      "flag_turkmenistan",
}


def city_flag(city: str) -> str:
    """Флаг страны для указанного города (для HTML-текста в личке бота)."""
    key = _CITY_TO_FLAG_KEY.get(city)
    return e(key) if key else ""


def city_flag_entity(builder, city: str):
    """Флаг страны как MessageEntity (для постов канала/групп)."""
    key = _CITY_TO_FLAG_KEY.get(city)
    if key:
        return entity_emoji(builder, key)
    return builder
