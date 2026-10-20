"""Send one per-user daily summary of Gift Watch activity."""

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from alert_tracking import ensure_table
from bot import ROOT, telegram
from runtime_settings import activate_settings, active_profiles, feature_enabled


def ensure_tables(db):
    ensure_table(db)
    db.execute(
        "CREATE TABLE IF NOT EXISTS daily_report_deliveries ("
        "chat_id TEXT NOT NULL, local_day TEXT NOT NULL, sent REAL NOT NULL, "
        "message_id INTEGER, PRIMARY KEY (chat_id, local_day))"
    )
    db.commit()


def _top(counter, empty="нет"):
    if not counter:
        return empty
    return ", ".join(
        f"{escape(str(name))} — {count}"
        for name, count in counter.most_common(3)
    )


def build_report(db, chat_id, *, now=None):
    moment = float(now if now is not None else time.time())
    since = moment - 86400
    rows = db.execute(
        "SELECT market, gift_name, basis, profit_rub, sold FROM signal_log "
        "WHERE chat_id=? AND sent>=?",
        (str(chat_id), since),
    ).fetchall()
    signals = len(rows)
    sold = sum(int(row[4] or 0) for row in rows)
    market_counts = Counter(row[0] or "?" for row in rows)
    collection_counts = Counter(row[1] or "?" for row in rows)
    basis_counts = Counter(row[2] or "не указано" for row in rows)
    potential = sum(float(row[3] or 0) for row in rows)

    activity = {str(kind): int(count) for kind, count in db.execute(
        "SELECT kind, COUNT(*) FROM market_activity "
        "WHERE occurred>=? GROUP BY kind", (since,)
    ).fetchall()} if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_activity'"
    ).fetchone() else {}

    upgrades = 0
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='telegram_upgrade_alerts'"
    ).fetchone():
        upgrades = int(db.execute(
            "SELECT COUNT(*) FROM telegram_upgrade_alerts "
            "WHERE chat_id=? AND sent>=?", (str(chat_id), since)
        ).fetchone()[0])

    return "\n".join((
        "📊 <b>ОТЧЁТ GIFT WATCH ЗА 24 ЧАСА</b>",
        "",
        f"Подходящих офферов: <b>{signals}</b>",
        f"Из них отмечены проданными: <b>{sold}</b>",
        f"Улучшений Telegram: <b>{upgrades}</b>",
        f"Расчётная прибыль всех сигналов: <b>{potential:.0f} ₽</b>",
        "",
        f"<b>Площадки:</b> {_top(market_counts)}",
        f"<b>Коллекции:</b> {_top(collection_counts)}",
        f"<b>Основания:</b> {_top(basis_counts)}",
        "",
        "<b>Общая активность рынка:</b>",
        f"Сделки: {activity.get('sale', 0)} · листинги: {activity.get('listing', 0)} · "
        f"изменения цены: {activity.get('price', 0)}",
    ))


def run_cycle(config, secrets, db, *, send=True, now=None):
    ensure_tables(db)
    zone = ZoneInfo(str(config.get("daily_report_timezone", "Europe/Moscow")))
    local = datetime.fromtimestamp(
        float(now if now is not None else time.time()), zone
    )
    delivered = 0
    for chat_id, user_config in active_profiles(config):
        if not feature_enabled(user_config, "daily_report"):
            continue
        hour = int(user_config.get("daily_report_hour", 21))
        if local.hour < hour:
            continue
        recipient = str(chat_id if chat_id is not None else secrets["chat_id"])
        day = local.date().isoformat()
        if db.execute(
            "SELECT 1 FROM daily_report_deliveries WHERE chat_id=? AND local_day=?",
            (recipient, day),
        ).fetchone():
            continue
        text = build_report(db, recipient, now=local.timestamp())
        if not send:
            print(text, flush=True)
            continue
        message_id = telegram(dict(secrets, chat_id=recipient), text)
        db.execute(
            "INSERT INTO daily_report_deliveries VALUES (?, ?, ?, ?)",
            (recipient, day, time.time(), message_id),
        )
        db.commit()
        delivered += 1
    return delivered


def run(config, secrets, db, *, send=True, duration=None):
    deadline = None if duration is None else time.monotonic() + max(1, int(duration))
    while deadline is None or time.monotonic() < deadline:
        run_cycle(config, secrets, db, send=send)
        if deadline is None:
            return
        wait = min(60, deadline - time.monotonic())
        if wait > 0:
            time.sleep(wait)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--duration", type=int, default=3300)
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text())
    secrets = json.loads((ROOT / "secrets.json").read_text())
    with sqlite3.connect(ROOT / "settings.sqlite3", timeout=5) as settings_db:
        settings_db.execute("PRAGMA journal_mode=WAL")
        settings_db.execute("PRAGMA busy_timeout=5000")
        activate_settings(settings_db, secrets["chat_id"])
        with sqlite3.connect(ROOT / "state.sqlite3", timeout=30) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=30000")
            run(
                config, secrets, db, send=args.send,
                duration=None if args.once else args.duration,
            )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"Не удалось запустить ежедневный отчёт: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Ежедневный отчёт остановлен.")
