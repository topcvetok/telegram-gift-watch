"""Reliable per-user Mini App history delivery."""

import hashlib
import hmac
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


def access_token(secrets, chat_id):
    writer = str(secrets.get("history_writer_token") or "").strip()
    if not writer:
        return None
    return hmac.new(
        writer.encode(), str(chat_id).encode(), hashlib.sha256
    ).hexdigest()


def ensure_outbox(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS history_outbox ("
        "chat_id TEXT NOT NULL, kind TEXT NOT NULL, event_key TEXT NOT NULL, "
        "payload TEXT NOT NULL, queued REAL NOT NULL, "
        "PRIMARY KEY (chat_id, kind, event_key))"
    )
    db.commit()


def _post(secrets, payload):
    endpoint = str(secrets.get("history_api_url") or "").strip()
    writer = str(secrets.get("history_writer_token") or "").strip()
    if not endpoint or not writer:
        return False
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False, default=str).encode(),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-writer-token": writer,
            "user-agent": "GiftWatch/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"История Mini App временно недоступна: {exc}", file=sys.stderr, flush=True)
        return False


def flush_outbox(db, secrets, limit=20):
    ensure_outbox(db)
    rows = db.execute(
        "SELECT chat_id, kind, event_key, payload FROM history_outbox "
        "ORDER BY queued LIMIT ?", (max(1, int(limit)),)
    ).fetchall()
    delivered = 0
    for chat_id, kind, event_key, raw in rows:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, dict):
            db.execute(
                "DELETE FROM history_outbox WHERE chat_id=? AND kind=? AND event_key=?",
                (chat_id, kind, event_key),
            )
            continue
        if not _post(secrets, payload):
            break
        db.execute(
            "DELETE FROM history_outbox WHERE chat_id=? AND kind=? AND event_key=?",
            (chat_id, kind, event_key),
        )
        db.commit()
        delivered += 1
    db.commit()
    return delivered


def queue_history(db, secrets, chat_id, record):
    token = access_token(secrets, chat_id)
    endpoint = str(secrets.get("history_api_url") or "").strip()
    if not token or not endpoint:
        return False
    payload = dict(record)
    queue_key = str(payload.pop("_queue_key", payload["event_key"]))
    payload.update({"chat_id": str(chat_id), "access_token": token})
    payload.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    ensure_outbox(db)
    db.execute(
        "INSERT OR REPLACE INTO history_outbox VALUES (?, ?, ?, ?, ?)",
        (
            str(chat_id), str(payload["kind"]), queue_key,
            json.dumps(payload, ensure_ascii=False, default=str), time.time(),
        ),
    )
    db.commit()
    flush_outbox(db, secrets)
    return True


def mark_offer_sold(db, secrets, chat_id, market, gift_id):
    event_key = f"{market}:{gift_id}"
    return queue_history(db, secrets, chat_id, {
        "action": "sold",
        "kind": "offer",
        "event_key": event_key,
        "_queue_key": f"{event_key}:sold",
    })


def record_offer(db, secrets, chat_id, market, item, result, target_url, preview_url):
    floors = {
        str(row.get("market")): str(row.get("model_floor"))
        for row in (result.get("market_floors") or [])
        if row.get("market") and row.get("model_floor") is not None
    }
    return queue_history(db, secrets, chat_id, {
        "kind": "offer",
        "event_key": f"{market}:{item['gift_id']}",
        "title": f"{item.get('name') or 'Подарок'} #{item.get('gift_num') or '?'}",
        "market": str(market),
        "price_ton": str(result.get("price")) if result.get("price") is not None else None,
        "floor_ton": str(result.get("next_floor")) if result.get("next_floor") is not None else None,
        "model": item.get("model"),
        "backdrop": item.get("backdrop"),
        "basis": result.get("basis"),
        "target_url": target_url,
        "preview_url": preview_url,
        "extra": {"market_floors": floors},
    })


def record_upgrade(db, secrets, chat_id, candidate, reasons, target_url):
    floor = candidate.get("collection_floor_ton")
    username = str(candidate.get("owner_username") or "").strip().lstrip("@")
    gift_key = f"{candidate['collection']}:{int(candidate['number'])}"
    return queue_history(db, secrets, chat_id, {
        "kind": "upgrade",
        "event_key": gift_key,
        "title": f"{candidate['collection']} #{int(candidate['number'])}",
        "market": "Telegram",
        "floor_ton": str(floor) if floor is not None else None,
        "model": candidate.get("model"),
        "backdrop": candidate.get("backdrop"),
        "owner_username": username or None,
        "basis": html.unescape(re.sub(
            r"<[^>]+>", "", " · ".join(str(reason) for reason in reasons)
        )),
        "target_url": target_url,
        "preview_url": target_url,
        "extra": {
            "floor_market": candidate.get("collection_floor_market"),
            "symbol": candidate.get("symbol"),
        },
    })
