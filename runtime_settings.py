"""Persistent user-adjustable monitor thresholds."""

import json
import sys
import time
from decimal import Decimal, InvalidOperation


SETTING_LABELS = {
    "max_lot_rub": "максимальная стоимость",
    "min_floor_rub": "минимальный floor",
    "upgrade_min_floor_rub": "минимальный floor коллекции для улучшений",
}

CURRENCIES = {"rub", "ton"}
SOURCE_LABELS = {
    "tonnel": "Tonnel",
    "portals": "Portals",
    "mrkt": "MRKT",
}
FEATURE_LABELS = {
    "collection_basis": "Floor коллекции",
    "model_basis": "Floor модели",
    "backdrop_basis": "Цвет фона",
    "beautiful_number": "Красивый номер",
    "monochrome": "Монохром",
    "dark_upgrades": "Улучшения выбранных фонов",
    "valuable_upgrades": "Улучшения дорогих коллекций",
    "daily_report": "Ежедневный отчёт",
}

UPGRADE_BACKDROP_NAMES = (
    "Black", "Onyx Black", "Gunmetal", "Battleship Grey", "Feldgrau",
    "Steel Grey", "Roman Silver", "Platinum", "Ivory White", "Desert Sand",
    "Cappuccino", "Seal Brown", "Chocolate", "Chestnut", "Rosewood",
    "Burgundy", "Carmine", "Fire Engine", "Tomato", "Burnt Sienna", "Copper",
    "Carrot Juice", "Persimmon", "Orange", "Caramel", "Old Gold", "Satin Gold",
    "Pure Gold", "Amber", "Mustard", "Light Olive", "Khaki Green",
    "Lemongrass", "Pistachio", "Camo Green", "Ranger Green", "Rifle Green",
    "Dark Green", "Hunter Green", "Malachite", "Shamrock Green", "Mint Green",
    "Emerald", "Pacific Green", "Jade Green", "Pine Green", "Gunship Green",
    "Tactical Pine", "Deep Cyan", "Turquoise", "Aquamarine", "Pacific Cyan",
    "Moonstone", "Silver Blue", "Indigo Dye", "Midnight Blue", "Marine Blue",
    "French Blue", "Sapphire", "Sky Blue", "Azure Blue", "Celtic Blue",
    "Navy Blue", "Cobalt Blue", "Neon Blue", "Cyberpunk", "Electric Indigo",
    "Lavender", "Grape", "English Violet", "French Violet", "Electric Purple",
    "Purple", "Dark Lilac", "Fandango", "Mexican Pink", "Raspberry",
    "Mystic Pearl", "Strawberry", "Coral Red",
)
DEFAULT_UPGRADE_BACKDROPS = ("Black", "Onyx Black")
NUMBER_SETTING_LABELS = {
    "beautiful_number_max_distinct_digits": "максимум разных цифр в номере",
    "monochrome_min_score": "минимальное совпадение монохрома",
    "monochrome_max_floor_premium_percent": "допустимое превышение floor для монохрома",
    "daily_report_hour": "час ежедневного отчёта",
}
NUMBER_SETTING_RANGES = {
    "beautiful_number_max_distinct_digits": (1, 10),
    "monochrome_min_score": (0, 100),
    "monochrome_max_floor_premium_percent": (0, 100),
    "daily_report_hour": (0, 23),
}

FILTER_SCOPES = {
    "collections": "name",
    "models": "model",
    "backdrops": "backdrop",
}
FILTER_MODES = {"allow", "deny"}

_ACTIVE_SETTINGS_DB = None
_ACTIVE_OWNER_CHAT_ID = None
_ACTIVE_REMOTE = None
_LAST_REMOTE_SYNC = 0.0
DEFAULT_PROFILE = "__default__"


def activate_settings(db, owner_chat_id=None, remote=None):
    """Set the dedicated settings connection for this monitor process."""
    global _ACTIVE_SETTINGS_DB, _ACTIVE_OWNER_CHAT_ID, _ACTIVE_REMOTE
    global _LAST_REMOTE_SYNC
    _ACTIVE_SETTINGS_DB = db
    _ACTIVE_OWNER_CHAT_ID = str(owner_chat_id) if owner_chat_id is not None else None
    _ACTIVE_REMOTE = remote
    _LAST_REMOTE_SYNC = 0.0
    if db is not None:
        ensure_settings_table(db, _ACTIVE_OWNER_CHAT_ID)
        if remote and _ACTIVE_OWNER_CHAT_ID is not None:
            from history_store import register_settings_profile
            register_settings_profile(remote, _ACTIVE_OWNER_CHAT_ID)
            sync_remote_settings(db, remote, force=True)


def active_config(config):
    """Apply live settings when a production settings connection is active."""
    if _ACTIVE_SETTINGS_DB is None:
        return dict(config)
    if _ACTIVE_REMOTE:
        sync_remote_settings(_ACTIVE_SETTINGS_DB, _ACTIVE_REMOTE)
    return effective_config(config, _ACTIVE_SETTINGS_DB, _ACTIVE_OWNER_CHAT_ID)


def active_profiles(config):
    """Return (chat_id, config) pairs for every authorized recipient."""
    if _ACTIVE_SETTINGS_DB is None or _ACTIVE_OWNER_CHAT_ID is None:
        return [(None, dict(config))]
    if _ACTIVE_REMOTE:
        sync_remote_settings(_ACTIVE_SETTINGS_DB, _ACTIVE_REMOTE)
    return [
        (chat_id, effective_config(config, _ACTIVE_SETTINGS_DB, chat_id))
        for chat_id in recipient_chat_ids(_ACTIVE_SETTINGS_DB, _ACTIVE_OWNER_CHAT_ID)
    ]


def ensure_settings_table(db, owner_chat_id=None):
    db.execute(
        "CREATE TABLE IF NOT EXISTS runtime_settings "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS runtime_settings_by_chat ("
        "chat_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
        "updated REAL NOT NULL, PRIMARY KEY (chat_id, key))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS authorized_chats ("
        "chat_id TEXT PRIMARY KEY, joined REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS runtime_filters ("
        "chat_id TEXT NOT NULL, scope TEXT NOT NULL, mode TEXT NOT NULL, "
        "value TEXT NOT NULL, normalized TEXT NOT NULL, updated REAL NOT NULL, "
        "PRIMARY KEY (chat_id, scope, mode, normalized))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS remote_settings_versions ("
        "chat_id TEXT PRIMARY KEY, updated TEXT NOT NULL)"
    )
    if owner_chat_id is not None:
        db.execute(
            "INSERT OR IGNORE INTO runtime_settings_by_chat "
            "(chat_id, key, value, updated) "
            "SELECT ?, key, value, updated FROM runtime_settings",
            (str(owner_chat_id),),
        )
    # Release the initial schema write before any marketplace performs network I/O.
    db.commit()


def _rubles(value, label):
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{label}: нужно указать целое число рублей") from None
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise ValueError(f"{label}: нужно указать целое число рублей")
    amount = int(amount)
    if amount < 0:
        raise ValueError(f"{label}: значение не может быть отрицательным")
    return amount


def _profile(chat_id=None):
    if chat_id is not None:
        return str(chat_id)
    if _ACTIVE_OWNER_CHAT_ID is not None:
        return _ACTIVE_OWNER_CHAT_ID
    return DEFAULT_PROFILE


def effective_config(config, db, chat_id=None):
    """Return a config copy with current Telegram-controlled thresholds."""
    ensure_settings_table(db, _ACTIVE_OWNER_CHAT_ID)
    result = dict(config)
    keys = (
        tuple(SETTING_LABELS)
        + tuple(f"{key}:currency" for key in SETTING_LABELS)
        + tuple(f"{source}_enabled" for source in SOURCE_LABELS)
        + tuple(f"{feature}_enabled" for feature in FEATURE_LABELS)
        + tuple(NUMBER_SETTING_LABELS)
        + ("upgrade_backdrops",)
    )
    rows = db.execute(
        f"SELECT key, value FROM runtime_settings_by_chat "
        f"WHERE chat_id=? AND key IN ({','.join('?' * len(keys))})",
        (_profile(chat_id), *keys),
    ).fetchall()
    stored = dict(rows)
    for key, label in SETTING_LABELS.items():
        if key not in stored:
            continue
        currency = stored.get(f"{key}:currency", "rub")
        ton_key = key.removesuffix("_rub") + "_ton"
        if currency == "ton":
            result[ton_key] = _tons(stored[key], label)
        else:
            result[key] = _rubles(stored[key], label)
            result.pop(ton_key, None)
    for source in SOURCE_LABELS:
        key = f"{source}_enabled"
        result[key] = stored.get(key, str(config.get(key, True))).casefold() not in {
            "0", "false", "off", "no"
        }
    for feature in FEATURE_LABELS:
        key = f"{feature}_enabled"
        result[key] = stored.get(key, str(config.get(key, True))).casefold() not in {
            "0", "false", "off", "no"
        }
    for key, label in NUMBER_SETTING_LABELS.items():
        if key in stored:
            result[key] = _integer_setting(key, stored[key], label)
    if "upgrade_backdrops" in stored:
        try:
            values = json.loads(stored["upgrade_backdrops"])
            result["upgrade_backdrops"] = canonical_upgrade_backdrops(values)
        except (TypeError, ValueError, json.JSONDecodeError):
            values = list(DEFAULT_UPGRADE_BACKDROPS)
            result["upgrade_backdrops"] = values
    else:
        result["upgrade_backdrops"] = canonical_upgrade_backdrops(
            result.get("upgrade_backdrops", DEFAULT_UPGRADE_BACKDROPS)
        )
    result["filter_lists"] = filter_lists(db, chat_id)
    return result


def normalize_filter_value(value):
    return " ".join(str(value or "").strip().casefold().split())


def filter_lists(db, chat_id=None):
    ensure_settings_table(db, _ACTIVE_OWNER_CHAT_ID)
    result = {
        scope: {mode: [] for mode in sorted(FILTER_MODES)}
        for scope in FILTER_SCOPES
    }
    rows = db.execute(
        "SELECT scope, mode, value FROM runtime_filters "
        "WHERE chat_id=? ORDER BY scope, mode, normalized",
        (_profile(chat_id),),
    ).fetchall()
    for scope, mode, value in rows:
        if scope in result and mode in result[scope]:
            result[scope][mode].append(str(value))
    return result


def replace_filters(db, filters, chat_id=None):
    if not isinstance(filters, dict):
        raise ValueError("Фильтры должны быть объектом")
    rows = []
    for scope in FILTER_SCOPES:
        modes = filters.get(scope, {})
        if not isinstance(modes, dict):
            raise ValueError(f"Некорректный фильтр: {scope}")
        for mode in FILTER_MODES:
            values = modes.get(mode, [])
            if not isinstance(values, list):
                raise ValueError(f"Некорректный список: {scope}/{mode}")
            unique = {}
            for value in values:
                text = str(value or "").strip()
                normalized = normalize_filter_value(text)
                if normalized:
                    unique[normalized] = text[:120]
            if len(unique) > 200:
                raise ValueError("В одном списке может быть не больше 200 значений")
            rows.extend((scope, mode, value, normalized) for normalized, value in unique.items())
    ensure_settings_table(db, _ACTIVE_OWNER_CHAT_ID)
    profile = _profile(chat_id)
    db.execute("DELETE FROM runtime_filters WHERE chat_id=?", (profile,))
    db.executemany(
        "INSERT INTO runtime_filters "
        "(chat_id, scope, mode, value, normalized, updated) "
        "VALUES (?, ?, ?, ?, ?, unixepoch())",
        [(profile, *row) for row in rows],
    )
    db.commit()
    return filter_lists(db, chat_id)


def item_allowed(item, config):
    filters = config.get("filter_lists") or {}
    for scope, item_key in FILTER_SCOPES.items():
        groups = filters.get(scope) or {}
        value = normalize_filter_value(item.get(item_key))
        denied = {normalize_filter_value(entry) for entry in groups.get("deny", [])}
        allowed = {normalize_filter_value(entry) for entry in groups.get("allow", [])}
        if value and value in denied:
            return False
        if allowed and value not in allowed:
            return False
    return True


def _integer_setting(key, value, label):
    try:
        amount = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label}: нужно указать целое число") from None
    low, high = NUMBER_SETTING_RANGES[key]
    if not low <= amount <= high:
        raise ValueError(f"{label}: допустимо от {low} до {high}")
    return amount


def _tons(value, label):
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{label}: укажи число в TON") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{label}: значение в TON не может быть отрицательным")
    return amount


def set_setting(db, key, value, *, currency="rub", chat_id=None):
    if key not in SETTING_LABELS:
        raise ValueError("Неизвестная настройка")
    if currency not in CURRENCIES:
        raise ValueError("Неизвестная валюта")
    amount = (
        _tons(value, SETTING_LABELS[key])
        if currency == "ton"
        else _rubles(value, SETTING_LABELS[key])
    )
    if key == "max_lot_rub" and amount <= 0:
        raise ValueError("Максимальная стоимость должна быть больше нуля")
    ensure_settings_table(db, _ACTIVE_OWNER_CHAT_ID)
    profile = _profile(chat_id)
    db.execute(
        "INSERT OR REPLACE INTO runtime_settings_by_chat "
        "(chat_id, key, value, updated) VALUES (?, ?, ?, unixepoch())",
        (profile, key, str(amount)),
    )
    db.execute(
        "INSERT OR REPLACE INTO runtime_settings_by_chat "
        "(chat_id, key, value, updated) VALUES (?, ?, ?, unixepoch())",
        (profile, f"{key}:currency", currency),
    )
    db.commit()
    return amount


def set_source_enabled(db, source, enabled, *, chat_id=None):
    if source not in SOURCE_LABELS:
        raise ValueError("Неизвестная площадка")
    ensure_settings_table(db, _ACTIVE_OWNER_CHAT_ID)
    db.execute(
        "INSERT OR REPLACE INTO runtime_settings_by_chat "
        "(chat_id, key, value, updated) VALUES (?, ?, ?, unixepoch())",
        (_profile(chat_id), f"{source}_enabled", "1" if enabled else "0"),
    )
    db.commit()
    return bool(enabled)


def set_feature_enabled(db, feature, enabled, *, chat_id=None):
    if feature not in FEATURE_LABELS:
        raise ValueError("Неизвестное основание")
    ensure_settings_table(db, _ACTIVE_OWNER_CHAT_ID)
    db.execute(
        "INSERT OR REPLACE INTO runtime_settings_by_chat "
        "(chat_id, key, value, updated) VALUES (?, ?, ?, unixepoch())",
        (_profile(chat_id), f"{feature}_enabled", "1" if enabled else "0"),
    )
    db.commit()
    return bool(enabled)


def set_number_setting(db, key, value, *, chat_id=None):
    if key not in NUMBER_SETTING_LABELS:
        raise ValueError("Неизвестная настройка")
    amount = _integer_setting(key, value, NUMBER_SETTING_LABELS[key])
    ensure_settings_table(db, _ACTIVE_OWNER_CHAT_ID)
    db.execute(
        "INSERT OR REPLACE INTO runtime_settings_by_chat "
        "(chat_id, key, value, updated) VALUES (?, ?, ?, unixepoch())",
        (_profile(chat_id), key, str(amount)),
    )
    db.commit()
    return amount


def canonical_upgrade_backdrops(values):
    if not isinstance(values, (list, tuple)):
        raise ValueError("Цвета улучшений должны быть списком")
    canonical = {normalize_filter_value(name): name for name in UPGRADE_BACKDROP_NAMES}
    result = []
    seen = set()
    for value in values:
        normalized = normalize_filter_value(value)
        if normalized not in canonical:
            raise ValueError(f"Неизвестный цвет фона: {value}")
        if normalized not in seen:
            seen.add(normalized)
            result.append(canonical[normalized])
    return result


def set_upgrade_backdrops(db, values, *, chat_id=None):
    selected = canonical_upgrade_backdrops(values)
    ensure_settings_table(db, _ACTIVE_OWNER_CHAT_ID)
    db.execute(
        "INSERT OR REPLACE INTO runtime_settings_by_chat "
        "(chat_id, key, value, updated) VALUES (?, 'upgrade_backdrops', ?, unixepoch())",
        (_profile(chat_id), json.dumps(selected, ensure_ascii=False)),
    )
    db.commit()
    return selected


def apply_settings_dict(settings, db, chat_id=None):
    """Validate and persist one complete or partial Mini App settings object."""
    if not isinstance(settings, dict):
        raise ValueError("Mini App не передал настройки")
    prices = settings.get("prices") or {}
    if not isinstance(prices, dict):
        raise ValueError("Некорректные лимиты")
    for name, key in (
        ("max_lot", "max_lot_rub"),
        ("min_floor", "min_floor_rub"),
        ("upgrade_min_floor", "upgrade_min_floor_rub"),
    ):
        entry = prices.get(name)
        if isinstance(entry, dict):
            set_setting(
                db, key, entry.get("value"),
                currency=(
                    "ton" if name == "upgrade_min_floor"
                    else str(entry.get("currency") or "rub").casefold()
                ),
                chat_id=chat_id,
            )
    sources = settings.get("sources") or {}
    if not isinstance(sources, dict):
        raise ValueError("Некорректный список площадок")
    for source, enabled in sources.items():
        if source in SOURCE_LABELS and isinstance(enabled, bool):
            set_source_enabled(db, source, enabled, chat_id=chat_id)
    features = settings.get("features") or {}
    if not isinstance(features, dict):
        raise ValueError("Некорректный список оснований")
    for feature, enabled in features.items():
        if feature in FEATURE_LABELS and isinstance(enabled, bool):
            set_feature_enabled(db, feature, enabled, chat_id=chat_id)
    numbers = settings.get("numbers") or {}
    if not isinstance(numbers, dict):
        raise ValueError("Некорректные числовые настройки")
    for key, value in numbers.items():
        if key in NUMBER_SETTING_LABELS:
            set_number_setting(db, key, value, chat_id=chat_id)
    if "upgrade_backdrops" in settings:
        set_upgrade_backdrops(db, settings["upgrade_backdrops"], chat_id=chat_id)
    if "filters" in settings:
        replace_filters(db, settings.get("filters") or {}, chat_id=chat_id)
    return True


def sync_remote_settings(db, secrets, *, force=False, interval=5):
    """Pull Mini App settings from the durable backend into the live SQLite DB."""
    global _LAST_REMOTE_SYNC
    now = time.monotonic()
    if not force and now - _LAST_REMOTE_SYNC < max(1, int(interval)):
        return 0
    _LAST_REMOTE_SYNC = now
    from history_store import pull_settings_profiles

    owner = str(secrets.get("chat_id") or _ACTIVE_OWNER_CHAT_ID or "")
    if not owner:
        return 0
    profiles = pull_settings_profiles(secrets)
    if profiles is None:
        return 0
    ensure_settings_table(db, owner)
    allowed = set(recipient_chat_ids(db, owner))
    applied = 0
    for row in profiles:
        if not isinstance(row, dict):
            continue
        chat_id = str(row.get("chat_id") or "")
        settings = row.get("settings")
        updated = str(row.get("updated_at") or "")
        if chat_id not in allowed or not isinstance(settings, dict) or not settings:
            continue
        previous = db.execute(
            "SELECT updated FROM remote_settings_versions WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        if previous and str(previous[0]) == updated:
            continue
        try:
            apply_settings_dict(settings, db, chat_id)
        except ValueError as exc:
            print(
                f"Настройки Mini App для {chat_id} отклонены: {exc}",
                file=sys.stderr, flush=True,
            )
            continue
        db.execute(
            "INSERT OR REPLACE INTO remote_settings_versions (chat_id, updated) "
            "VALUES (?, ?)",
            (chat_id, updated),
        )
        db.commit()
        applied += 1
    return applied


def authorize_chat(db, chat_id):
    ensure_settings_table(db, _ACTIVE_OWNER_CHAT_ID)
    db.execute(
        "INSERT OR REPLACE INTO authorized_chats (chat_id, joined, active) "
        "VALUES (?, unixepoch(), 1)",
        (str(chat_id),),
    )
    db.commit()


def revoke_chat(db, chat_id):
    ensure_settings_table(db, _ACTIVE_OWNER_CHAT_ID)
    db.execute("DELETE FROM authorized_chats WHERE chat_id=?", (str(chat_id),))
    db.commit()


def is_authorized(db, chat_id, owner_chat_id):
    if str(chat_id) == str(owner_chat_id):
        return True
    ensure_settings_table(db, owner_chat_id)
    return db.execute(
        "SELECT 1 FROM authorized_chats WHERE chat_id=? AND active=1",
        (str(chat_id),),
    ).fetchone() is not None


def recipient_chat_ids(db, owner_chat_id):
    ensure_settings_table(db, owner_chat_id)
    owner = str(owner_chat_id)
    rows = db.execute(
        "SELECT chat_id FROM authorized_chats WHERE active=1 ORDER BY joined"
    ).fetchall()
    return [owner] + [str(row[0]) for row in rows if str(row[0]) != owner]


def source_enabled(config, source):
    if source not in SOURCE_LABELS:
        raise ValueError("Неизвестная площадка")
    return bool(config.get(f"{source}_enabled", True))


def feature_enabled(config, feature):
    if feature not in FEATURE_LABELS:
        raise ValueError("Неизвестное основание")
    return bool(config.get(f"{feature}_enabled", True))


def threshold_ton(config, name, rate):
    """Return a RUB- or TON-configured threshold in marketplace TON units."""
    ton_key = f"{name}_ton"
    if ton_key in config:
        return _tons(config[ton_key], name)
    return Decimal(str(config.get(f"{name}_rub", 0))) / Decimal(str(rate))
