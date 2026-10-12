"""Read-only Tonnel deal monitor with Telegram notifications."""
import argparse
import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    from curl_cffi import requests
except ImportError:
    raise SystemExit("Не установлен curl_cffi. Запусти start_watch.command.")

from bot import ROOT, telegram

TONNEL_URL = "https://gifts2.tonnel.network/api/pageGifts"
EVENTS_URL = "https://gifts.coffin.meme/api/marketplace/events"
EVENTS_WS_URL = "wss://gifts.coffin.meme/api/marketplace/ws"
RATE_URL = "https://api.coingecko.com/api/v3/simple/price"
HEADERS = {
    "accept": "*/*",
    "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
    "content-type": "application/json",
    "origin": "https://market.tonnel.network",
    "referer": "https://market.tonnel.network/",
}
ALLOWED_BACKDROP_NAMES = {
    "black",
    "onyx black",
    "gunmetal",
    "midnight blue",
}


class SourceRateLimited(RuntimeError):
    def __init__(self, retry_after):
        self.retry_after = max(60, int(retry_after or 300))
        super().__init__(f"Tonnel ограничил запросы; повтор через {self.retry_after} сек.")


def decimal(value, label="число"):
    if value is None or isinstance(value, bool):
        raise ValueError(f"Не задано: {label}")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"Неверное значение: {label}") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"Значение должно быть неотрицательным: {label}")
    return result


def allowed_backdrop(value):
    if not isinstance(value, str):
        return False
    name = re.sub(r"\s*\([^)]*%\)\s*$", "", value).strip().casefold()
    return name in ALLOWED_BACKDROP_NAMES


def request(method, url, *, body=None, params=None, attempts=3):
    last_status = None
    for attempt in range(attempts):
        try:
            response = requests.request(
                method, url, json=body, params=params, headers=HEADERS,
                impersonate="chrome", timeout=20,
            )
            last_status = response.status_code
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429:
                try:
                    retry_after = int(response.headers.get("retry-after", "300"))
                except (TypeError, ValueError):
                    retry_after = 300
                raise SourceRateLimited(retry_after)
            if response.status_code not in (403, 500, 502, 503, 504):
                break
        except SourceRateLimited:
            raise
        except (OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Источник временно недоступен (HTTP {last_status or 'network'})")


def fetch_collection(name, sort, limit, attributes=None):
    market_filter = {
        "price": {"$exists": True},
        "refunded": {"$ne": True},
        "buyer": {"$exists": False},
        "export_at": {"$exists": True},
        "gift_name": name,
        "asset": "TON",
    }
    market_filter.update(attributes or {})
    payload = {
        "page": 1,
        "limit": limit,
        "sort": json.dumps(sort, separators=(",", ":")),
        "filter": json.dumps(market_filter, separators=(",", ":")),
        "price_range": None,
        "user_auth": "",
    }
    result = request("POST", TONNEL_URL, body=payload)
    if not isinstance(result, list):
        raise RuntimeError("Tonnel изменил схему ответа")
    return result


def load_variant_books(db, collection, items, rate, config, query_budget):
    db.execute(
        "CREATE TABLE IF NOT EXISTS tonnel_variant_books ("
        "scope TEXT, collection TEXT, trait TEXT, payload TEXT, checked REAL, "
        "PRIMARY KEY (scope, collection, trait))"
    )
    budget = decimal(config["max_lot_rub"], "лимит на лот") / rate
    keys = []
    seen = set()
    for item in items:
        try:
            if not eligible(item) or decimal(item["price"]) > budget:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        for scope in ("model", "backdrop"):
            trait = item.get(scope)
            if scope == "backdrop" and not allowed_backdrop(trait):
                continue
            key = (scope, trait)
            if isinstance(trait, str) and trait.strip() and key not in seen:
                seen.add(key)
                keys.append(key)

    now = time.time()
    max_age = float(config["variant_cache_seconds"])
    books = {}
    missing = []
    for scope, trait in keys:
        row = db.execute(
            "SELECT payload, checked FROM tonnel_variant_books "
            "WHERE scope=? AND collection=? AND trait=?",
            (scope, collection, trait),
        ).fetchone()
        if row and now - float(row[1]) <= max_age:
            try:
                books[(scope, trait)] = json.loads(row[0])
                continue
            except (TypeError, ValueError):
                pass
        if query_budget[0] > 0:
            missing.append((scope, trait))
            query_budget[0] -= 1

    def fetch(key):
        scope, trait = key
        book = fetch_collection(
            collection,
            {"price": 1, "gift_id": -1},
            int(config["variant_floor_limit"]),
            {scope: trait},
        )
        return key, book

    workers = max(1, int(config["variant_workers"]))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, key): key for key in missing}
        for future in as_completed(futures):
            key = futures[future]
            try:
                _, book = future.result()
            except SourceRateLimited:
                for pending in futures:
                    pending.cancel()
                raise
            except RuntimeError as exc:
                print(f"{collection}, {key[0]}: {exc}", file=sys.stderr, flush=True)
                continue
            books[key] = book
            db.execute(
                "INSERT OR REPLACE INTO tonnel_variant_books VALUES (?, ?, ?, ?, ?)",
                (key[0], collection, key[1], json.dumps(book, ensure_ascii=False), now),
            )
    db.commit()
    return books


def load_collection_book(db, collection, config):
    db.execute(
        "CREATE TABLE IF NOT EXISTS tonnel_collection_books ("
        "collection TEXT PRIMARY KEY, payload TEXT, checked REAL)"
    )
    row = db.execute(
        "SELECT payload, checked FROM tonnel_collection_books WHERE collection=?",
        (collection,),
    ).fetchone()
    now = time.time()
    if row and now - float(row[1]) <= float(config["collection_floor_cache_seconds"]):
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            pass
    book = fetch_collection(
        collection,
        {"price": 1, "gift_id": -1},
        int(config["floor_limit"]),
    )
    db.execute(
        "INSERT OR REPLACE INTO tonnel_collection_books VALUES (?, ?, ?)",
        (collection, json.dumps(book, ensure_ascii=False), now),
    )
    db.commit()
    return book


def eligible(item):
    return (
        isinstance(item, dict)
        and item.get("status") == "forsale"
        and item.get("asset") == "TON"
        and not item.get("auction")
        and not item.get("dutchAuctionData")
        and not item.get("premarketData")
        and item.get("underLoan") is False
        and item.get("gift_id") is not None
        and decimal(item.get("price"), "цена") > 0
    )


def score(item, floor_book, rate, config):
    if not eligible(item):
        return None
    price = decimal(item["price"], "цена покупки")
    max_lot_ton = decimal(config["max_lot_rub"], "лимит на лот") / rate
    if price > max_lot_ton:
        return None
    others = []
    for other in floor_book:
        try:
            if eligible(other) and str(other["gift_id"]) != str(item["gift_id"]):
                others.append(decimal(other["price"], "цена аналога"))
        except (ValueError, KeyError):
            continue
    others.sort()
    minimum = int(config["min_comparables"])
    if len(others) < minimum:
        return None
    cluster = others[:minimum]
    next_floor = cluster[0]
    max_spread = decimal(config["floor_cluster_max_spread_fraction"], "разброс floor")
    if cluster[-1] > next_floor * (1 + max_spread):
        return None
    min_floor_rub = decimal(config.get("min_floor_rub", 0), "минимальный floor")
    if next_floor * rate < min_floor_rub:
        return None
    discount = (next_floor - price) / next_floor
    if discount < decimal(config["min_discount_fraction"], "минимальная скидка"):
        return None
    undercut = decimal(config["undercut_ton"], "шаг снижения цены")
    resale = max(Decimal("0"), next_floor - undercut)
    cost_fraction = decimal(config["tonnel_round_trip_cost_fraction"], "резерв расходов")
    fixed_cost = decimal(config["fixed_cost_ton"], "фиксированные расходы")
    net_sale = resale * (1 - cost_fraction) - fixed_cost
    profit_ton = net_sale - price
    if profit_ton <= 0:
        return None
    profit_rub = profit_ton * rate
    roi = profit_ton / price
    if profit_rub < decimal(config["min_scenario_profit_rub"], "минимальная прибыль"):
        return None
    if roi < decimal(config["min_roi_fraction"], "минимальная доходность"):
        return None
    return {
        "price": price,
        "next_floor": next_floor,
        "resale": resale,
        "discount": discount,
        "profit_ton": profit_ton,
        "profit_rub": profit_rub,
        "roi": roi,
        "rate": rate,
    }


def best_score(item, collection_book, variant_books, rate, config):
    results = []
    collection_result = score(item, collection_book, rate, config)
    if collection_result is not None:
        collection_result["basis"] = "вся коллекция"
        collection_result["basis_scope"] = "collection"
        collection_result["basis_trait"] = item.get("name")
        results.append(collection_result)

    variant_config = dict(config)
    variant_config["min_comparables"] = config["variant_min_comparables"]
    variant_config["floor_cluster_max_spread_fraction"] = config[
        "variant_floor_cluster_max_spread_fraction"
    ]
    labels = {"model": "модель", "backdrop": "фон"}
    for scope, label in labels.items():
        trait = item.get(scope)
        if scope == "backdrop" and not allowed_backdrop(trait):
            continue
        book = variant_books.get((scope, trait))
        if not book:
            continue
        result = score(item, book, rate, variant_config)
        if result is not None:
            result["basis"] = f"{label}: {trait}"
            result["basis_scope"] = scope
            result["basis_trait"] = trait
            results.append(result)
    if not results:
        return None
    return max(results, key=lambda result: (result["profit_rub"], result["discount"]))


def slug(item):
    base = re.sub(r"[^A-Za-z0-9]", "", str(item.get("name", ""))).lower()
    number = item.get("gift_num")
    return f"{base}-{number}" if base and isinstance(number, int) and number > 0 else ""


def event_timestamp(value):
    if not isinstance(value, str) or not value:
        return time.time()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def record_sale(event, db):
    if not isinstance(event, dict) or event.get("type") != "sale.completed":
        return False
    data = event.get("data")
    gift = data.get("gift") if isinstance(data, dict) else None
    if not isinstance(gift, dict) or data.get("asset") != "TON":
        return False
    try:
        price = decimal(data.get("price"), "цена продажи")
    except ValueError:
        return False
    event_id = event.get("eventId")
    name = gift.get("gift_name")
    if not isinstance(event_id, str) or not event_id or not isinstance(name, str) or price <= 0:
        return False
    occurred = event_timestamp(event.get("occurredAt"))
    db.execute(
        "CREATE TABLE IF NOT EXISTS tonnel_sales ("
        "event_id TEXT PRIMARY KEY, gift_id TEXT, gift_name TEXT, model TEXT, "
        "backdrop TEXT, symbol TEXT, price TEXT, source TEXT, occurred REAL)"
    )
    db.execute(
        "INSERT OR IGNORE INTO tonnel_sales VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id, str(gift.get("gift_id", "")), name,
            gift.get("model"), gift.get("backdrop"), gift.get("symbol"),
            str(price), str(data.get("source", "")), occurred,
        ),
    )
    db.execute("DELETE FROM tonnel_sales WHERE occurred < ?", (time.time() - 2 * 86400,))
    return True


def median_decimal(values):
    ordered = sorted(values)
    size = len(ordered)
    if not size:
        return None
    middle = size // 2
    if size % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def sales_metrics(item, result, db, now=None):
    db.execute(
        "CREATE TABLE IF NOT EXISTS tonnel_stream_state "
        "(key TEXT PRIMARY KEY, value TEXT)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS tonnel_sales ("
        "event_id TEXT PRIMARY KEY, gift_id TEXT, gift_name TEXT, model TEXT, "
        "backdrop TEXT, symbol TEXT, price TEXT, source TEXT, occurred REAL)"
    )
    moment = float(now if now is not None else time.time())
    cutoff = moment - 86400
    name = item.get("name")
    collection_rows = db.execute(
        "SELECT price FROM tonnel_sales WHERE gift_name=? AND occurred>=?",
        (name, cutoff),
    ).fetchall()
    scope = result.get("basis_scope", "collection")
    trait = result.get("basis_trait")
    if scope == "model":
        basis_rows = db.execute(
            "SELECT price FROM tonnel_sales WHERE gift_name=? AND model=? AND occurred>=?",
            (name, trait, cutoff),
        ).fetchall()
        basis_label = f"модель {trait}"
    elif scope == "backdrop":
        basis_rows = db.execute(
            "SELECT price FROM tonnel_sales WHERE gift_name=? AND backdrop=? AND occurred>=?",
            (name, trait, cutoff),
        ).fetchall()
        basis_label = f"фон {trait}"
    else:
        basis_rows = collection_rows
        basis_label = "вся коллекция"
    prices = []
    for row in basis_rows:
        try:
            prices.append(decimal(row[0], "цена продажи"))
        except ValueError:
            continue
    started = db.execute(
        "SELECT value FROM tonnel_stream_state WHERE key='sales_started_at'"
    ).fetchone()
    history_hours = 0.0
    if started:
        try:
            history_hours = min(24.0, max(0.0, (moment - float(started[0])) / 3600))
        except (TypeError, ValueError):
            pass
    return {
        "collection_sales_24h": len(collection_rows),
        "basis_sales_24h": len(prices),
        "median_sale": median_decimal(prices),
        "sales_basis_label": basis_label,
        "sales_history_hours": history_hours,
    }


def apply_sales_evidence(item, result, db, config, now=None):
    if result is None:
        return None
    enriched = dict(result)
    metrics = sales_metrics(item, enriched, db, now)
    enriched.update(metrics)
    basis_sales = metrics["basis_sales_24h"]
    collection_sales = metrics["collection_sales_24h"]
    minimum = int(config.get("sales_confirmation_min_count", 3))
    confirmed = basis_sales >= minimum and metrics["median_sale"] is not None
    enriched["sales_confirmed"] = confirmed

    if confirmed:
        resale = min(enriched["resale"], metrics["median_sale"])
        cost_fraction = decimal(config["tonnel_round_trip_cost_fraction"], "резерв расходов")
        fixed_cost = decimal(config["fixed_cost_ton"], "фиксированные расходы")
        profit_ton = resale * (1 - cost_fraction) - fixed_cost - enriched["price"]
        profit_rub = profit_ton * enriched["rate"]
        roi = profit_ton / enriched["price"] if enriched["price"] > 0 else Decimal("0")
        if profit_ton <= 0:
            return None
        if profit_rub < decimal(config["min_scenario_profit_rub"], "минимальная прибыль"):
            return None
        if roi < decimal(config["min_roi_fraction"], "минимальная доходность"):
            return None
        enriched.update({
            "resale": resale,
            "profit_ton": profit_ton,
            "profit_rub": profit_rub,
            "roi": roi,
        })

    medium = int(config.get("liquidity_medium_sales_24h", 3))
    high = int(config.get("liquidity_high_sales_24h", 8))
    if collection_sales >= high:
        enriched["liquidity"] = "высокая"
    elif collection_sales >= medium:
        enriched["liquidity"] = "средняя"
    else:
        enriched["liquidity"] = "низкая"

    strong_sales = int(config.get("strong_signal_min_basis_sales_24h", 5))
    strong_discount = decimal(config.get("strong_signal_min_discount_fraction", 0.25))
    strong_roi = decimal(config.get("strong_signal_min_roi_fraction", 0.25))
    if (
        confirmed
        and basis_sales >= strong_sales
        and collection_sales >= high
        and enriched["discount"] >= strong_discount
        and enriched["roi"] >= strong_roi
    ):
        enriched["quality"] = "сильный"
    elif confirmed:
        enriched["quality"] = "нормальный"
    else:
        enriched["quality"] = "рискованный"
    return enriched


def format_alert(item, result, config):
    nft_slug = slug(item)
    nft_link = f"https://t.me/nft/{nft_slug}" if nft_slug else ""
    listing_link = f"https://t.me/tonnel_network_bot/gift?startapp={item['gift_id']}"
    quality = result.get("quality", "рискованный")
    quality_icons = {"сильный": "🔥", "нормальный": "✅", "рискованный": "⚠️"}
    sales_count = int(result.get("basis_sales_24h", 0))
    collection_sales = int(result.get("collection_sales_24h", 0))
    median_sale = result.get("median_sale")
    sales_line = (
        f"Продажи по основанию за 24 ч: {sales_count}"
        + (f", медиана {median_sale:.3f} TON" if median_sale is not None else "")
    )
    history_hours = float(result.get("sales_history_hours", 0))
    history_line = (
        f"История продаж пока накапливается: {history_hours:.1f} ч\n"
        if history_hours < 24 else ""
    )
    lines = [
        f"{quality_icons.get(quality, '⚠️')} {quality.capitalize()} оффер на Tonnel",
        f"{item.get('name', 'Подарок')} #{item.get('gift_num', '?')}",
        f"Цена: {result['price']:.3f} TON ≈ {result['price'] * result['rate']:.0f} ₽",
        f"Основание: {result.get('basis', 'вся коллекция')}",
        f"Модель: {item.get('model', 'не указана')}",
        f"Фон: {item.get('backdrop', 'не указан')}",
        f"Следующий устойчивый floor: {result['next_floor']:.3f} TON",
        f"Разница: {result['discount'] * 100:.1f}%",
        f"Ликвидность коллекции: {result.get('liquidity', 'низкая')} "
        f"({collection_sales} продаж за 24 ч)",
        sales_line,
    ]
    if history_line:
        lines.append(history_line.rstrip())
    lines.extend([
        f"Сценарий быстрой продажи: {result['resale']:.3f} TON",
        f"Расчётный остаток: {result['profit_ton']:.3f} TON ≈ {result['profit_rub']:.0f} ₽",
        f"В расчёте заложен резерв расходов "
        f"{decimal(config['tonnel_round_trip_cost_fraction']) * 100:.1f}%.",
        "Это сигнал для ручной проверки, прибыль и ликвидность не гарантированы. "
        "Перед покупкой сверяй цену и доступность в Tonnel.",
        f"ID Tonnel: {item['gift_id']}",
    ])
    if nft_link:
        lines.append(nft_link)
    lines.append("Открыть объявление: " + listing_link)
    return "\n".join(lines)


def send_if_new_price(item, result, config, secrets, db, send):
    key = str(item["gift_id"])
    old = db.execute("SELECT price FROM tonnel_alerts WHERE id=?", (key,)).fetchone()
    if old and result["price"] >= decimal(old[0]):
        return False
    text = format_alert(item, result, config)
    if not send:
        print(text + "\n")
        return False
    telegram(secrets, text)
    db.execute(
        "INSERT OR REPLACE INTO tonnel_alerts VALUES (?, ?, ?)",
        (key, str(result["price"]), time.time()),
    )
    db.commit()
    return True


def event_item(event):
    if not isinstance(event, dict) or event.get("type") not in {
        "listing.created", "listing.price_changed",
    }:
        return None
    data = event.get("data")
    if not isinstance(data, dict) or data.get("asset") != "TON":
        return None
    if event.get("type") == "listing.created" and data.get("sale_type") != "FIXED":
        return None
    gift = data.get("gift")
    if not isinstance(gift, dict) or gift.get("gift_id") is None:
        return None
    return {
        **gift,
        "name": gift.get("gift_name"),
        "price": data.get("price"),
        "asset": "TON",
        "status": "forsale",
        "auction": None,
        "dutchAuctionData": None,
        "premarketData": None,
        "underLoan": False,
    }


def collection_enabled(name, config):
    return bool(config.get("monitor_all_collections")) or name in config["tonnel_collections"]


def process_event(event, config, secrets, db, send=True):
    event_id = event.get("eventId") if isinstance(event, dict) else None
    if not isinstance(event_id, str) or not event_id:
        return False
    db.execute(
        "CREATE TABLE IF NOT EXISTS tonnel_events "
        "(id TEXT PRIMARY KEY, processed REAL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS tonnel_stream_state "
        "(key TEXT PRIMARY KEY, value TEXT)"
    )
    if db.execute("SELECT 1 FROM tonnel_events WHERE id=?", (event_id,)).fetchone():
        return False

    delivered = False
    record_sale(event, db)
    item = event_item(event)
    if item is not None and collection_enabled(item.get("name"), config):
        rate = load_rate(db)
        if decimal(item.get("price"), "цена") * rate <= decimal(
            config["max_lot_rub"], "лимит на лот"
        ):
            floor_book = load_collection_book(db, item["name"], config)
            variant_books = load_variant_books(
                db, item["name"], [item], rate, config,
                [int(config.get("variant_query_budget_per_event", 1))],
            )
            result = best_score(item, floor_book, variant_books, rate, config)
            result = apply_sales_evidence(item, result, db, config)
            if result is not None:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS tonnel_alerts "
                    "(id TEXT PRIMARY KEY, price TEXT, sent REAL)"
                )
                delivered = send_if_new_price(item, result, config, secrets, db, send)

    now = time.time()
    db.execute("INSERT INTO tonnel_events VALUES (?, ?)", (event_id, now))
    db.execute(
        "INSERT OR REPLACE INTO tonnel_stream_state VALUES ('cursor', ?)",
        (event_id,),
    )
    db.execute("DELETE FROM tonnel_events WHERE processed < ?", (now - 8 * 86400,))
    db.commit()
    return delivered


def replay_events(config, secrets, db, send=True):
    row = db.execute(
        "SELECT value FROM tonnel_stream_state WHERE key='cursor'"
    ).fetchone()
    if not row:
        return 0
    cursor = row[0]
    delivered = 0
    while cursor:
        payload = request(
            "GET", EVENTS_URL,
            params={"after": cursor, "limit": 100},
        )
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list) or not events:
            break
        for event in events:
            delivered += int(process_event(event, config, secrets, db, send))
        next_cursor = payload.get("nextAfter")
        if not isinstance(next_cursor, str) or next_cursor == cursor or len(events) < 100:
            break
        cursor = next_cursor
    return delivered


def stream_events(config, secrets, db, send=True, duration=14340):
    db.execute(
        "CREATE TABLE IF NOT EXISTS tonnel_stream_state "
        "(key TEXT PRIMARY KEY, value TEXT)"
    )
    db.execute(
        "INSERT OR IGNORE INTO tonnel_stream_state VALUES ('sales_started_at', ?)",
        (str(time.time()),),
    )
    db.commit()
    deadline = time.monotonic() + max(60, int(duration))
    delivered = 0
    while time.monotonic() < deadline:
        try:
            with requests.Session(impersonate="chrome") as session:
                socket = session.ws_connect(EVENTS_WS_URL, timeout=20)
                delivered += replay_events(config, secrets, db, send)
                print("Подключён поток новых лотов Tonnel.", flush=True)
                while time.monotonic() < deadline:
                    raw, _ = socket.recv()
                    event = json.loads(raw)
                    delivered += int(process_event(event, config, secrets, db, send))
                socket.close()
        except SourceRateLimited as exc:
            delay = min(exc.retry_after, 300)
            print(f"Поток ждёт лимит Tonnel: {delay} сек.", file=sys.stderr, flush=True)
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
        except Exception as exc:
            print(f"Переподключение к потоку Tonnel: {exc}", file=sys.stderr, flush=True)
            time.sleep(min(3, max(0, deadline - time.monotonic())))
    print(f"Поток завершён; отправлено {delivered} офферов.", flush=True)
    return delivered


def load_rate(db, max_age=3600):
    db.execute("CREATE TABLE IF NOT EXISTS rates (symbol TEXT PRIMARY KEY, value TEXT, checked REAL)")
    row = db.execute("SELECT value, checked FROM rates WHERE symbol='TONRUB'").fetchone()
    now = time.time()
    if row and now - float(row[1]) <= max_age:
        return decimal(row[0], "курс TON/рубль")
    result = request(
        "GET", RATE_URL,
        params={"ids": "the-open-network", "vs_currencies": "rub"},
    )
    try:
        rate = decimal(result["the-open-network"]["rub"], "курс TON/рубль")
    except (KeyError, TypeError):
        raise RuntimeError("Источник курса изменил схему ответа") from None
    if rate <= 0:
        raise RuntimeError("Источник курса вернул нулевое значение")
    db.execute("INSERT OR REPLACE INTO rates VALUES ('TONRUB', ?, ?)", (str(rate), now))
    db.commit()
    return rate


def run_cycle(config, secrets, db, send=False):
    rate = load_rate(db)
    db.execute("CREATE TABLE IF NOT EXISTS tonnel_alerts (id TEXT PRIMARY KEY, price TEXT, sent REAL)")
    total = candidates = delivered = 0
    variant_query_budget = [int(config["variant_query_budget_per_cycle"])]
    for index, collection in enumerate(config["tonnel_collections"]):
        try:
            floor_book = load_collection_book(db, collection, config)
            time.sleep(float(config["request_pause_seconds"]))
            newest = fetch_collection(
                collection,
                {"message_post_time": -1, "gift_id": -1},
                int(config["latest_limit"]),
            )
            variant_books = load_variant_books(
                db, collection, newest, rate, config, variant_query_budget
            )
            total += len(newest)
            for item in newest:
                try:
                    result = best_score(item, floor_book, variant_books, rate, config)
                    result = apply_sales_evidence(item, result, db, config)
                except (ValueError, KeyError, TypeError):
                    continue
                if result is None:
                    continue
                candidates += 1
                if send_if_new_price(item, result, config, secrets, db, send):
                    delivered += 1
        except SourceRateLimited:
            raise
        except RuntimeError as exc:
            print(f"{collection}: {exc}", file=sys.stderr, flush=True)
        if index + 1 < len(config["tonnel_collections"]):
            time.sleep(float(config["request_pause_seconds"]))
    print(
        f"Проверено {total} новых лотов в {len(config['tonnel_collections'])} коллекциях; "
        f"кандидатов {candidates}; отправлено {delivered}; курс {rate:.2f} ₽/TON.",
        flush=True,
    )
    return {"total": total, "candidates": candidates, "delivered": delivered, "rate": rate}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Один цикл и выход")
    parser.add_argument("--send", action="store_true", help="Отправлять подходящие сигналы")
    parser.add_argument(
        "--stream-seconds", type=int, default=0,
        help="Слушать поток новых лотов указанное число секунд",
    )
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text())
    secrets = json.loads((ROOT / "secrets.json").read_text())
    interval = max(60, int(config["poll_seconds"]))
    with sqlite3.connect(ROOT / "state.sqlite3") as db:
        if args.stream_seconds:
            stream_events(config, secrets, db, args.send, args.stream_seconds)
            return
        while True:
            delay = interval
            try:
                run_cycle(config, secrets, db, args.send)
            except SourceRateLimited as exc:
                delay = max(interval, min(exc.retry_after, 300))
                print(f"Цикл приостановлен: {exc}", file=sys.stderr, flush=True)
            except RuntimeError as exc:
                print(f"Цикл пропущен: {exc}", file=sys.stderr, flush=True)
            if args.once:
                return
            time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"Не удалось запустить: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Мониторинг остановлен.")
