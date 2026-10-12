"""Read-only Tonnel deal monitor with Telegram notifications."""
import argparse
import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    from curl_cffi import requests
except ImportError:
    raise SystemExit("Не установлен curl_cffi. Запусти start_watch.command.")

from bot import ROOT, telegram

TONNEL_URL = "https://gifts2.tonnel.network/api/pageGifts"
RATE_URL = "https://api.coingecko.com/api/v3/simple/price"
HEADERS = {
    "accept": "*/*",
    "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
    "content-type": "application/json",
    "origin": "https://market.tonnel.network",
    "referer": "https://market.tonnel.network/",
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
        results.append(collection_result)

    variant_config = dict(config)
    variant_config["min_comparables"] = config["variant_min_comparables"]
    variant_config["floor_cluster_max_spread_fraction"] = config[
        "variant_floor_cluster_max_spread_fraction"
    ]
    labels = {"model": "модель", "backdrop": "фон"}
    for scope, label in labels.items():
        trait = item.get(scope)
        book = variant_books.get((scope, trait))
        if not book:
            continue
        result = score(item, book, rate, variant_config)
        if result is not None:
            result["basis"] = f"{label}: {trait}"
            results.append(result)
    if not results:
        return None
    return max(results, key=lambda result: (result["profit_rub"], result["discount"]))


def slug(item):
    base = re.sub(r"[^A-Za-z0-9]", "", str(item.get("name", ""))).lower()
    number = item.get("gift_num")
    return f"{base}-{number}" if base and isinstance(number, int) and number > 0 else ""


def format_alert(item, result, config):
    nft_slug = slug(item)
    nft_link = f"https://t.me/nft/{nft_slug}" if nft_slug else ""
    listing_link = f"https://t.me/tonnel_network_bot/gift?startapp={item['gift_id']}"
    return (
        "🔎 Возможный дешёвый лот на Tonnel\n"
        f"{item.get('name', 'Подарок')} #{item.get('gift_num', '?')}\n"
        f"Цена: {result['price']:.3f} TON ≈ {result['price'] * result['rate']:.0f} ₽\n"
        f"Основание: {result.get('basis', 'вся коллекция')}\n"
        f"Модель: {item.get('model', 'не указана')}\n"
        f"Фон: {item.get('backdrop', 'не указан')}\n"
        f"Следующий устойчивый floor: {result['next_floor']:.3f} TON\n"
        f"Разница: {result['discount'] * 100:.1f}%\n"
        f"Сценарий быстрой продажи: {result['resale']:.3f} TON\n"
        f"Расчётный остаток: {result['profit_ton']:.3f} TON ≈ {result['profit_rub']:.0f} ₽\n"
        f"В расчёте заложен резерв расходов {decimal(config['tonnel_round_trip_cost_fraction']) * 100:.1f}%.\n"
        "Это сигнал для ручной проверки, прибыль и ликвидность не гарантированы. "
        "Перед покупкой сверяй цену и доступность в Tonnel.\n"
        f"ID Tonnel: {item['gift_id']}\n"
        + (nft_link + "\n" if nft_link else "")
        + "Открыть объявление: " + listing_link
    )


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
            floor_book = fetch_collection(collection, {"price": 1, "gift_id": -1}, int(config["floor_limit"]))
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
                except (ValueError, KeyError, TypeError):
                    continue
                if result is None:
                    continue
                candidates += 1
                key = str(item["gift_id"])
                old = db.execute("SELECT price FROM tonnel_alerts WHERE id=?", (key,)).fetchone()
                if old and result["price"] >= decimal(old[0]):
                    continue
                text = format_alert(item, result, config)
                if send:
                    telegram(secrets, text)
                    db.execute(
                        "INSERT OR REPLACE INTO tonnel_alerts VALUES (?, ?, ?)",
                        (key, str(result["price"]), time.time()),
                    )
                    db.commit()
                    delivered += 1
                else:
                    print(text + "\n")
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
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text())
    secrets = json.loads((ROOT / "secrets.json").read_text())
    interval = max(60, int(config["poll_seconds"]))
    with sqlite3.connect(ROOT / "state.sqlite3") as db:
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
