"""Persistent user-adjustable monitor thresholds."""

from decimal import Decimal, InvalidOperation


SETTING_LABELS = {
    "max_lot_rub": "максимальная стоимость",
    "min_floor_rub": "минимальный floor",
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
}
NUMBER_SETTING_LABELS = {
    "beautiful_number_max_distinct_digits": "максимум разных цифр в номере",
    "monochrome_min_score": "минимальное совпадение монохрома",
    "monochrome_max_floor_premium_percent": "допустимое превышение floor для монохрома",
}
NUMBER_SETTING_RANGES = {
    "beautiful_number_max_distinct_digits": (1, 10),
    "monochrome_min_score": (0, 100),
    "monochrome_max_floor_premium_percent": (0, 100),
}

_ACTIVE_SETTINGS_DB = None


def activate_settings(db):
    """Set the dedicated settings connection for this monitor process."""
    global _ACTIVE_SETTINGS_DB
    _ACTIVE_SETTINGS_DB = db


def active_config(config):
    """Apply live settings when a production settings connection is active."""
    if _ACTIVE_SETTINGS_DB is None:
        return dict(config)
    return effective_config(config, _ACTIVE_SETTINGS_DB)


def ensure_settings_table(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS runtime_settings "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL)"
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


def effective_config(config, db):
    """Return a config copy with current Telegram-controlled thresholds."""
    ensure_settings_table(db)
    result = dict(config)
    keys = (
        tuple(SETTING_LABELS)
        + tuple(f"{key}:currency" for key in SETTING_LABELS)
        + tuple(f"{source}_enabled" for source in SOURCE_LABELS)
        + tuple(f"{feature}_enabled" for feature in FEATURE_LABELS)
        + tuple(NUMBER_SETTING_LABELS)
    )
    rows = db.execute(
        f"SELECT key, value FROM runtime_settings WHERE key IN ({','.join('?' * len(keys))})",
        keys,
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
    return result


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


def set_setting(db, key, value, *, currency="rub"):
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
    ensure_settings_table(db)
    db.execute(
        "INSERT OR REPLACE INTO runtime_settings (key, value, updated) "
        "VALUES (?, ?, unixepoch())",
        (key, str(amount)),
    )
    db.execute(
        "INSERT OR REPLACE INTO runtime_settings (key, value, updated) "
        "VALUES (?, ?, unixepoch())",
        (f"{key}:currency", currency),
    )
    db.commit()
    return amount


def set_source_enabled(db, source, enabled):
    if source not in SOURCE_LABELS:
        raise ValueError("Неизвестная площадка")
    ensure_settings_table(db)
    db.execute(
        "INSERT OR REPLACE INTO runtime_settings (key, value, updated) "
        "VALUES (?, ?, unixepoch())",
        (f"{source}_enabled", "1" if enabled else "0"),
    )
    db.commit()
    return bool(enabled)


def set_feature_enabled(db, feature, enabled):
    if feature not in FEATURE_LABELS:
        raise ValueError("Неизвестное основание")
    ensure_settings_table(db)
    db.execute(
        "INSERT OR REPLACE INTO runtime_settings (key, value, updated) "
        "VALUES (?, ?, unixepoch())",
        (f"{feature}_enabled", "1" if enabled else "0"),
    )
    db.commit()
    return bool(enabled)


def set_number_setting(db, key, value):
    if key not in NUMBER_SETTING_LABELS:
        raise ValueError("Неизвестная настройка")
    amount = _integer_setting(key, value, NUMBER_SETTING_LABELS[key])
    ensure_settings_table(db)
    db.execute(
        "INSERT OR REPLACE INTO runtime_settings (key, value, updated) "
        "VALUES (?, ?, unixepoch())",
        (key, str(amount)),
    )
    db.commit()
    return amount


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
