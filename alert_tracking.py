"""Persist Telegram alert messages and replace them with a compact sold notice."""

import time
import re
from decimal import Decimal, InvalidOperation
from html import escape

from bot import telegram_edit


def ensure_table(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS offer_messages ("
        "market TEXT NOT NULL, gift_id TEXT NOT NULL, message_id INTEGER NOT NULL, "
        "offer_price TEXT NOT NULL, gift_name TEXT, gift_num TEXT, model TEXT, "
        "sent REAL NOT NULL, sold INTEGER NOT NULL DEFAULT 0, "
        "checked REAL NOT NULL DEFAULT 0, sold_at REAL, "
        "PRIMARY KEY (market, gift_id))"
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(offer_messages)")}
    if "preview_url" not in columns:
        db.execute("ALTER TABLE offer_messages ADD COLUMN preview_url TEXT")
    db.execute(
        "CREATE TABLE IF NOT EXISTS offer_messages_by_chat ("
        "market TEXT NOT NULL, gift_id TEXT NOT NULL, chat_id TEXT NOT NULL, "
        "message_id INTEGER NOT NULL, offer_price TEXT NOT NULL, gift_name TEXT, "
        "gift_num TEXT, model TEXT, sent REAL NOT NULL, "
        "sold INTEGER NOT NULL DEFAULT 0, checked REAL NOT NULL DEFAULT 0, "
        "sold_at REAL, preview_url TEXT, "
        "PRIMARY KEY (market, gift_id, chat_id))"
    )
    db.commit()


def _preview_url(name, number, stored=None):
    if isinstance(stored, str) and stored.startswith("https://t.me/nft/"):
        return stored
    base = re.sub(r"[^A-Za-z0-9]", "", str(name or "")).lower()
    number = str(number or "")
    return f"https://t.me/nft/{base}-{number}" if base and number.isdigit() else None


def remember_alert(
    db, market, item, result, message_id, *, preview_url=None, chat_id=None
):
    if not isinstance(message_id, int) or message_id <= 0:
        return False
    ensure_table(db)
    db.execute(
        "INSERT OR REPLACE INTO offer_messages_by_chat "
        "(market, gift_id, chat_id, message_id, offer_price, gift_name, gift_num, "
        "model, sent, sold, checked, sold_at, preview_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, NULL, ?)",
        (
            str(market), str(item["gift_id"]), str(chat_id or "__default__"),
            message_id, str(result["price"]), item.get("name"),
            str(item.get("gift_num") or ""), item.get("model"),
            time.time(), _preview_url(
                item.get("name"), item.get("gift_num"), preview_url
            ),
        ),
    )
    db.commit()
    return True


def pending_offer_ids(db, market, *, interval=30, limit=5, now=None):
    ensure_table(db)
    moment = float(now if now is not None else time.time())
    rows = db.execute(
        "SELECT gift_id FROM ("
        "SELECT gift_id, MIN(checked) AS checked FROM offer_messages_by_chat "
        "WHERE market=? AND sold=0 GROUP BY gift_id UNION ALL "
        "SELECT gift_id, checked FROM offer_messages WHERE market=? AND sold=0) "
        "WHERE checked<=? GROUP BY gift_id ORDER BY MIN(checked) ASC LIMIT ?",
        (
            str(market), str(market), moment - max(1, int(interval)),
            max(1, int(limit)),
        ),
    ).fetchall()
    ids = [str(row[0]) for row in rows]
    if ids:
        db.executemany(
            "UPDATE offer_messages_by_chat SET checked=? "
            "WHERE market=? AND gift_id=?",
            [(moment, str(market), gift_id) for gift_id in ids],
        )
        db.executemany(
            "UPDATE offer_messages SET checked=? WHERE market=? AND gift_id=?",
            [(moment, str(market), gift_id) for gift_id in ids],
        )
        db.commit()
    return ids


def _ton(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return f"{number:.3f} TON" if number.is_finite() and number >= 0 else None


def _sold_text(row, sale_price=None):
    _, _, _, offer_price, name, number, model, stored_preview_url = row
    preview_url = _preview_url(name, number, stored_preview_url)
    title = escape(str(name or "Telegram Gift"))
    if number:
        title += f" #{escape(str(number))}"
    lines = [
        "🔴 <b>ПРОДАНО</b>",
        "",
        f"<b>{title}</b>",
        f"Площадка: {escape(str(row[0]))}",
        f"Модель: {escape(str(model or 'не указана'))}",
        f"Цена оффера: {_ton(offer_price) or 'не указана'}",
    ]
    sold_for = _ton(sale_price)
    if sold_for and sold_for != _ton(offer_price):
        lines.append(f"Цена продажи: {sold_for}")
    if preview_url:
        lines.extend([
            "",
            f'🔎 <a href="{escape(preview_url, quote=True)}">Предпросмотр в Telegram</a>',
        ])
    return "\n".join(lines)


def mark_offer_sold(db, market, gift_id, secrets, *, sale_price=None, send=True):
    ensure_table(db)
    rows = db.execute(
        "SELECT market, gift_id, message_id, offer_price, gift_name, gift_num, model, "
        "preview_url, chat_id FROM offer_messages_by_chat "
        "WHERE market=? AND gift_id=? AND sold=0",
        (str(market), str(gift_id)),
    ).fetchall()
    legacy = db.execute(
        "SELECT market, gift_id, message_id, offer_price, gift_name, gift_num, model, "
        "preview_url "
        "FROM offer_messages WHERE market=? AND gift_id=? AND sold=0",
        (str(market), str(gift_id)),
    ).fetchone()
    if not rows and legacy is None:
        return False
    if send:
        seen_messages = set()
        for row in rows:
            chat_secrets = dict(secrets, chat_id=row[8])
            telegram_edit(
                chat_secrets, int(row[2]), _sold_text(row[:8], sale_price),
                preview_url=_preview_url(row[4], row[5], row[7]),
            )
            seen_messages.add((str(row[8]), int(row[2])))
        if legacy is not None:
            legacy_key = (str(secrets.get("chat_id", "__default__")), int(legacy[2]))
            if legacy_key not in seen_messages:
                telegram_edit(
                    secrets, int(legacy[2]), _sold_text(legacy, sale_price),
                    preview_url=_preview_url(legacy[4], legacy[5], legacy[7]),
                )
    db.execute(
        "UPDATE offer_messages_by_chat SET sold=1, sold_at=? "
        "WHERE market=? AND gift_id=?",
        (time.time(), str(market), str(gift_id)),
    )
    db.execute(
        "UPDATE offer_messages SET sold=1, sold_at=? WHERE market=? AND gift_id=?",
        (time.time(), str(market), str(gift_id)),
    )
    db.commit()
    return True
