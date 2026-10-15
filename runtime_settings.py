"""Persistent user-adjustable monitor thresholds."""

from decimal import Decimal, InvalidOperation


SETTING_LABELS = {
    "max_lot_rub": "максимальная стоимость",
    "min_floor_rub": "минимальный floor",
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
    rows = db.execute(
        "SELECT key, value FROM runtime_settings WHERE key IN (?, ?)",
        tuple(SETTING_LABELS),
    ).fetchall()
    for key, value in rows:
        result[key] = _rubles(value, SETTING_LABELS[key])
    return result


def set_setting(db, key, value, *, maximum=None):
    if key not in SETTING_LABELS:
        raise ValueError("Неизвестная настройка")
    amount = _rubles(value, SETTING_LABELS[key])
    if key == "max_lot_rub" and amount < 1:
        raise ValueError("Максимальная стоимость должна быть не меньше 1 ₽")
    if maximum is not None and amount > maximum:
        raise ValueError(f"Максимальная стоимость не может превышать {maximum} ₽")
    ensure_settings_table(db)
    db.execute(
        "INSERT OR REPLACE INTO runtime_settings (key, value, updated) "
        "VALUES (?, ?, unixepoch())",
        (key, str(amount)),
    )
    db.commit()
    return amount


def max_purchase_limit(config):
    budget = _rubles(config.get("budget_rub", 0), "бюджет")
    reserve = _rubles(config.get("reserve_rub", 0), "резерв")
    return max(1, budget - reserve) if budget else None
