"""Read-only MRKT deal monitor with Telegram notifications."""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from decimal import Decimal

try:
    from curl_cffi import requests
except ImportError:
    raise SystemExit("Не установлен curl_cffi. Запусти start_watch.command.")

from bot import ROOT, telegram
from alert_tracking import mark_offer_sold, remember_alert
from alert_format import format_market_alert
from market_data import record_market_activity
from runtime_settings import activate_settings, active_config, source_enabled, threshold_ton
from special_traits import apply_special_bases
from tonnel_watch import apply_sales_evidence, best_score, decimal, load_rate, slug

API_URL = "https://api.tgmrkt.io/api/v1"
NANOTON = Decimal("1000000000")
BUYER_MARKUP = Decimal("1.02")
EVENT_TYPES = ("Sale", "Listing", "ChangePrice")


class MrktAuthError(RuntimeError):
    pass


class MrktRateLimited(RuntimeError):
    def __init__(self, retry_after=60):
        self.retry_after = max(15, int(retry_after or 60))
        super().__init__(f"MRKT ограничил запросы; повтор через {self.retry_after} сек.")


def headers(token):
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://cdn.tgmrkt.io",
        "referer": "https://cdn.tgmrkt.io/",
        "authorization": token,
        "cookie": f"access_token={token}",
    }


def request(session, token, method, path, *, body=None, attempts=3):
    last_status = None
    for attempt in range(attempts):
        try:
            response = session.request(
                method, API_URL + path, headers=headers(token), json=body, timeout=20
            )
            last_status = response.status_code
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429:
                try:
                    delay = int(response.headers.get("retry-after", "60"))
                except (TypeError, ValueError):
                    delay = 60
                raise MrktRateLimited(delay)
            if response.status_code in (401, 403, 404):
                raise MrktAuthError("MRKT отклонил токен пользовательской сессии")
            if response.status_code not in (500, 502, 503, 504):
                break
        except (MrktAuthError, MrktRateLimited):
            raise
        except (OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(attempt + 1)
    raise RuntimeError(f"MRKT временно недоступен (HTTP {last_status or 'network'})")


def nano_to_ton(value):
    return decimal(value, "цена MRKT") / NANOTON


def listing_query(*, collection=None, model=None, backdrop=None, count=20):
    return {
        "count": min(20, max(1, int(count))),
        "cursor": "",
        "collectionNames": [collection] if collection else [],
        "modelNames": [model] if model else [],
        "backdropNames": [backdrop] if backdrop else [],
        "symbolNames": [],
        "minPrice": None,
        "maxPrice": None,
        "number": None,
        "isPremarket": False,
        "isNew": None,
        "luckyBuy": False,
        "giftType": None,
        "craftable": None,
        "isCrafted": None,
        "tgCanBeCraftedFrom": None,
        "removeSelfSales": True,
        "isTransferable": True,
        "availableForStaking": None,
        "forGame": None,
        "ordering": "Price",
        "lowToHigh": True,
        "query": None,
    }


def feed_query(count=20):
    return {
        "count": min(20, max(1, int(count))),
        "cursor": "",
        "collectionNames": [],
        "modelNames": [],
        "backdropNames": [],
        "number": None,
        "type": list(EVENT_TYPES),
        "minPrice": None,
        "maxPrice": None,
        "ordering": "Latest",
        "lowToHigh": False,
        "query": None,
    }


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def mrkt_item(raw, event=None):
    if not isinstance(raw, dict):
        return None
    price_raw = raw.get("salePrice")
    if isinstance(event, dict) and event.get("amount") is not None:
        price_raw = event["amount"]
    try:
        price = nano_to_ton(price_raw)
    except ValueError:
        return None
    gift_id = raw.get("id")
    if not gift_id or price <= 0:
        return None
    if raw.get("isOnAuction") is True or raw.get("isOnSale") is False or raw.get("isMine") is True:
        return None
    if raw.get("premarketStatus") not in (None, "", "None"):
        return None
    unlock = parse_time(raw.get("nextResaleDate") or raw.get("unlockDate") or raw.get("returnLockedUntil"))
    if raw.get("isLocked") or raw.get("isLockedForSale"):
        return None
    if unlock is not None and unlock > time.time():
        return None
    return {
        "gift_id": str(gift_id),
        "gift_num": raw.get("number"),
        "name": raw.get("collectionName") or raw.get("collectionTitle") or raw.get("title"),
        "model": raw.get("modelName"),
        "backdrop": raw.get("backdropName"),
        "symbol": raw.get("symbolName"),
        "price": price,
        "status": "forsale",
        "asset": "TON",
        "auction": False,
        "dutchAuctionData": None,
        "premarketData": None,
        "underLoan": False,
        "listing_event_id": str(event.get("id")) if isinstance(event, dict) and event.get("id") else None,
    }


def fetch_book(session, token, *, collection, model=None, backdrop=None, count=20):
    payload = request(
        session, token, "POST", "/gifts/saling",
        body=listing_query(collection=collection, model=model, backdrop=backdrop, count=count),
    )
    rows = payload.get("gifts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("MRKT изменил схему каталога")
    return [item for raw in rows if (item := mrkt_item(raw)) is not None]


def cached_book(db, session, token, *, collection, config, model=None, backdrop=None):
    db.execute(
        "CREATE TABLE IF NOT EXISTS mrkt_books ("
        "collection TEXT, model TEXT, backdrop TEXT, payload TEXT, checked REAL, "
        "PRIMARY KEY (collection, model, backdrop))"
    )
    db.commit()
    model_key = model or ""
    backdrop_key = backdrop or ""
    row = db.execute(
        "SELECT payload, checked FROM mrkt_books WHERE collection=? AND model=? AND backdrop=?",
        (collection, model_key, backdrop_key),
    ).fetchone()
    ttl = int(config.get("mrkt_book_cache_seconds", 20))
    if row and time.time() - float(row[1]) < ttl:
        try:
            payload = json.loads(row[0])
            if isinstance(payload, list):
                return payload
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    count = max(int(config.get("floor_limit", 12)), int(config.get("variant_floor_limit", 10)))
    book = fetch_book(
        session, token, collection=collection, model=model, backdrop=backdrop, count=count
    )
    db.execute(
        "INSERT OR REPLACE INTO mrkt_books VALUES (?, ?, ?, ?, ?)",
        (collection, model_key, backdrop_key, json.dumps(book, default=str), time.time()),
    )
    db.commit()
    return book


def mrkt_scoring_config(config):
    result = dict(config)
    result["tonnel_round_trip_cost_fraction"] = config.get(
        "mrkt_round_trip_cost_fraction", str(Decimal("1") - Decimal("1") / BUYER_MARKUP)
    )
    return result


def record_sale(event, db):
    if not isinstance(event, dict) or str(event.get("type", "")).casefold() != "sale":
        return False
    raw = event.get("gift")
    if not isinstance(raw, dict) or not event.get("id"):
        return False
    try:
        price = nano_to_ton(event.get("amount") or raw.get("salePrice"))
    except ValueError:
        return False
    happened = parse_time(event.get("date") or event.get("createdAt")) or time.time()
    db.execute(
        "CREATE TABLE IF NOT EXISTS tonnel_sales ("
        "event_id TEXT PRIMARY KEY, gift_id TEXT, gift_name TEXT, model TEXT, "
        "backdrop TEXT, symbol TEXT, price TEXT, source TEXT, occurred REAL)"
    )
    before = db.total_changes
    db.execute(
        "INSERT OR IGNORE INTO tonnel_sales VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(event["id"]), str(raw.get("id", "")), raw.get("collectionName"),
            raw.get("modelName"), raw.get("backdropName"), raw.get("symbolName"),
            str(price), "mrkt", happened,
        ),
    )
    db.commit()
    return db.total_changes > before


def listing_link(item):
    event_id = item.get("listing_event_id")
    if event_id:
        start_param = "feed" + str(event_id).replace("-", "")
    else:
        start_param = "gift" + str(item["gift_id"]).replace("-", "")
    return f"https://t.me/mrkt/app?startapp={start_param}"


def format_alert(item, result, config):
    return format_market_alert(item, result, market="MRKT")


def attach_cross_market_floors(
        item, result, config, secrets, db, *, session=None, token=None):
    if result is None:
        return None
    from portals_watch import add_portals_floor

    return add_portals_floor(
        item, result, config, secrets, db, base_market="MRKT",
        mrkt_session=session, mrkt_token=token,
    )


def send_if_new_price(item, result, config, secrets, db, send=True):
    db.execute(
        "CREATE TABLE IF NOT EXISTS mrkt_alerts "
        "(id TEXT PRIMARY KEY, price TEXT, sent REAL)"
    )
    key = str(item["gift_id"])
    old = db.execute("SELECT price FROM mrkt_alerts WHERE id=?", (key,)).fetchone()
    if old and result["price"] >= decimal(old[0]):
        return False
    text = format_alert(item, result, config)
    if not send:
        print(text + "\n")
        return False
    nft_slug = slug(item)
    preview_url = f"https://t.me/nft/{nft_slug}" if nft_slug else None
    message_id = telegram(
        secrets, text, preview_url=preview_url, button_url=listing_link(item)
    )
    remember_alert(
        db, "MRKT", item, result, message_id, preview_url=preview_url
    )
    db.execute(
        "INSERT OR REPLACE INTO mrkt_alerts VALUES (?, ?, ?)",
        (key, str(result["price"]), time.time()),
    )
    db.commit()
    return True


def process_event(event, config, secrets, db, session, token, send=True):
    if not isinstance(event, dict) or not event.get("id"):
        return False
    event_id = str(event["id"])
    db.execute(
        "CREATE TABLE IF NOT EXISTS mrkt_events "
        "(id TEXT PRIMARY KEY, processed REAL)"
    )
    if db.execute("SELECT 1 FROM mrkt_events WHERE id=?", (event_id,)).fetchone():
        return False
    raw = event.get("gift")
    event_type = str(event.get("type", "")).casefold()
    if event_type == "sale":
        kind = "sale"
    elif event_type == "changeprice":
        kind = "price"
    else:
        kind = "listing"
    if isinstance(raw, dict):
        record_market_activity(
            db, f"mrkt:{event_id}", "mrkt", raw.get("collectionName"), kind,
            parse_time(event.get("date") or event.get("createdAt")) or time.time(),
        )
    record_sale(event, db)
    if event_type == "sale" and isinstance(raw, dict) and raw.get("id") is not None:
        sale_price = None
        try:
            sale_price = nano_to_ton(event.get("amount") or raw.get("salePrice"))
        except ValueError:
            pass
        mark_offer_sold(
            db, "MRKT", raw["id"], secrets, sale_price=sale_price, send=send
        )
    # Never keep the activity transaction open during floor-book network requests.
    db.commit()
    delivered = False
    if event_type in ("listing", "changeprice"):
        item = mrkt_item(event.get("gift"), event)
        if item is not None:
            rate = load_rate(db)
            if item["price"] <= threshold_ton(config, "max_lot", rate):
                collection = item.get("name")
                collection_book = cached_book(
                    db, session, token, collection=collection, config=config
                )
                variant_books = {}
                if item.get("model"):
                    variant_books[("model", item["model"])] = cached_book(
                        db, session, token, collection=collection,
                        model=item["model"], config=config,
                    )
                if item.get("backdrop"):
                    variant_books[("backdrop", item["backdrop"])] = cached_book(
                        db, session, token, collection=collection,
                        backdrop=item["backdrop"], config=config,
                    )
                score_config = mrkt_scoring_config(config)
                result = best_score(item, collection_book, variant_books, rate, score_config)
                result = apply_special_bases(item, result, score_config, db)
                result = apply_sales_evidence(item, result, db, score_config)
                result = attach_cross_market_floors(
                    item, result, config, secrets, db, session=session, token=token
                )
                if result is not None:
                    delivered = send_if_new_price(item, result, config, secrets, db, send)
    now = time.time()
    db.execute("INSERT OR REPLACE INTO mrkt_events VALUES (?, ?)", (event_id, now))
    db.execute("DELETE FROM mrkt_events WHERE processed < ?", (now - 8 * 86400,))
    db.commit()
    return delivered


def fetch_feed(session, token, count=20):
    payload = request(session, token, "POST", "/feed", body=feed_query(count))
    rows = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("MRKT изменил схему ленты")
    return rows


def run_cycle(config, secrets, db, session, token, send=True):
    config = active_config(config)
    if not source_enabled(config, "mrkt"):
        return 0
    events = fetch_feed(session, token, int(config.get("mrkt_feed_limit", 20)))
    delivered = 0
    for event in reversed(events):
        delivered += int(process_event(event, config, secrets, db, session, token, send))
    print(f"MRKT: проверено {len(events)} событий; отправлено {delivered} офферов.", flush=True)
    return delivered


def run(config, secrets, db, *, send=True, duration=3300, fail_fast=False):
    token = str(secrets.get("mrkt_token", "")).strip()
    if not token:
        if fail_fast:
            raise RuntimeError("MRKT_TOKEN не настроен")
        print("MRKT: токен ещё не настроен; монитор Tonnel продолжает работу.", flush=True)
        return 0
    db.execute(
        "CREATE TABLE IF NOT EXISTS tonnel_stream_state "
        "(key TEXT PRIMARY KEY, value TEXT)"
    )
    db.execute(
        "INSERT OR IGNORE INTO tonnel_stream_state VALUES ('sales_started_at', ?)",
        (str(time.time()),),
    )
    db.commit()
    deadline = time.monotonic() + max(1, int(duration))
    interval = max(3, int(config.get("mrkt_poll_seconds", 5)))
    delivered = 0
    with requests.Session(impersonate="chrome") as session:
        while time.monotonic() < deadline:
            delay = interval
            try:
                delivered += run_cycle(config, secrets, db, session, token, send)
            except MrktRateLimited as exc:
                if fail_fast:
                    raise RuntimeError(str(exc)) from exc
                delay = min(exc.retry_after, 300)
                print(str(exc), file=sys.stderr, flush=True)
            except MrktAuthError as exc:
                if fail_fast:
                    raise RuntimeError(str(exc)) from exc
                print(f"MRKT остановлен: {exc}", file=sys.stderr, flush=True)
                return delivered
            except RuntimeError as exc:
                if fail_fast:
                    raise
                print(f"MRKT: цикл пропущен: {exc}", file=sys.stderr, flush=True)
            except sqlite3.OperationalError as exc:
                db.rollback()
                if fail_fast:
                    raise
                print(f"MRKT: база занята, цикл будет повторён: {exc}", file=sys.stderr, flush=True)
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
    return delivered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--duration", type=int, default=3300)
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text())
    secrets = json.loads((ROOT / "secrets.json").read_text())
    duration = 1 if args.once else args.duration
    with sqlite3.connect(ROOT / "settings.sqlite3", timeout=5) as settings_db:
        settings_db.execute("PRAGMA busy_timeout=5000")
        settings_db.execute("PRAGMA journal_mode=WAL")
        activate_settings(settings_db)
        with sqlite3.connect(ROOT / "state.sqlite3", timeout=30) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=30000")
            run(
                config, secrets, db, send=args.send, duration=duration,
                fail_fast=args.fail_fast,
            )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"Не удалось запустить MRKT: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Мониторинг MRKT остановлен.")
