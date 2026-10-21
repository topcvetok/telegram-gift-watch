"""Read-only gift watch prototype. Python 3.9+, standard library only."""
import argparse
import getpass
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent


def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError("Не задано обязательное число")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Неверное число") from None
    if not result.is_finite() or result < 0:
        raise ValueError("Число должно быть конечным и неотрицательным")
    return result


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Дата должна содержать часовой пояс")
    return result.timestamp()


def fresh(value, now, seconds):
    return value is not None and 0 <= now - timestamp(value) <= seconds


def request_json(url, body, headers=None):
    req = Request(url, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urlopen(req, timeout=20) as response:
            return json.load(response)
    except HTTPError as exc:
        # Never log request URLs: Telegram tokens occur in the URL.
        raise RuntimeError(f"HTTP {exc.code}; запрос не выполнен") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise RuntimeError("Источник недоступен или вернул неверный JSON") from None


def evaluate(item, rule, config, now):
    """No automatic valuation: compare against an explicit manual sale scenario."""
    if item.get("provider") not in ("tonnel", "mrkt"):
        return None
    if item.get("on_sale") is not True or item.get("premarket") is not False:
        return None
    if any(item.get(key) for key in ("on_auction", "staked", "lucky_buy")):
        return None
    # Missing restrictions are unknown, not proof of immediate transferability.
    for key in ("transferable_at", "resale_available_at"):
        if not item.get(key) or timestamp(item[key]) > now:
            return None
    if not fresh(config.get("rub_rate_checked_at"), now, 86400):
        return None
    if not fresh(rule.get("checked_at"), now, 86400):
        return None
    costs = config["costs"][item["provider"]]
    if not fresh(costs.get("checked_at"), now, 86400):
        return None
    rate = number(config["rub_per_ton"])
    price = number(item.get("price"))  # Swift buyer total, includes its markup.
    sale = number(rule.get("sale_scenario_ton"))
    sell_fee = number(costs.get("sell_fee_fraction"))
    if rate <= 0 or price <= 0 or sell_fee >= 1:
        return None
    buy_rub = price * rate + number(costs.get("extra_buy_rub"))
    extra = number(costs.get("transfer_and_withdraw_rub"))
    invested = buy_rub + extra
    available = number(config["budget_rub"]) - number(config["reserve_rub"])
    if invested > min(number(config["max_lot_rub"]), available):
        return None
    profit = sale * rate * (1 - sell_fee) - invested
    if profit < number(config["min_scenario_profit_rub"]):
        return None
    if profit / invested < number(config["min_roi_fraction"]):
        return None
    return {"buy_rub": buy_rub, "invested": invested, "profit": profit,
            "breakeven_ton": invested / rate / (1 - sell_fee), "sale": sale}


def fetch(rule, api_key, pages):
    rows = []
    seen = set()
    complete = False
    for page in range(pages):
        body = {"market": ["tonnel", "mrkt"], "name": rule["collection"],
                "sort_by": "price asc", "fee": 0}
        for key in ("model", "backdrop", "symbol"):
            if rule.get(key):
                body[key] = rule[key]
        result = request_json(
            f"https://partners.swiftgifts.tg/api/aggregator?page={page}",
            body, {"X-API-KEY": api_key})
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise RuntimeError("Схема источника изменилась")
        meta = result.get("meta") or {}
        if (meta.get("partial") or meta.get("providerErrors")
                or meta.get("providers_disabled")):
            raise RuntimeError("Источник сообщил неполное покрытие площадок")
        for item in result["items"]:
            identity = (item.get("provider"), item.get("giftId"))
            if identity[1] and identity not in seen:
                seen.add(identity)
                rows.append(item)
        more = result.get("has_more", result.get("hasMore"))
        if more is False:
            complete = True
            break
        if not result["items"]:
            break
        time.sleep(1)
    return rows, complete


def message(item, result, rule, complete):
    slug = item.get("slug", "")
    link = f"https://t.me/nft/{slug}" if re.fullmatch(r"[A-Za-z0-9]+-\d+", slug) else ""
    return (f"Кандидат для ручной проверки · {item['provider'].upper()}\n"
            f"{str(item.get('title', rule['collection']))[:100]} #{item.get('number', '?')}\n"
            f"Цена через источник: {item['price']} TON ≈ {result['buy_rub']:.0f} ₽\n"
            f"Безубыточная продажа: {result['breakeven_ton']:.3f} TON\n"
            f"Твой сценарий продажи: {result['sale']} TON\n"
            f"Результат при такой продаже: {result['profit']:.0f} ₽ до налогов\n"
            "Это расчёт по заданной цене, спрос и прибыль не подтверждены.\n"
            "Цена включает наценку источника; в самой площадке проверь итог.\n"
            + ("Просмотрена ограниченная выборка.\n" if not complete else "")
            + "Перед покупкой проверь доступность и ограничения.\n" + link)


def telegram(
    secrets, text, *, preview_url=None, button_url=None,
    button_text="Открыть оффер", callback_text=None, callback_data=None,
):
    preview_options = {"is_disabled": True}
    if preview_url:
        preview_options = {
            "is_disabled": False,
            "url": preview_url,
            "prefer_large_media": True,
            "show_above_text": True,
        }
    payload = {
        "chat_id": secrets["chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": preview_options,
    }
    buttons = []
    if button_url:
        buttons.append({"text": button_text, "url": button_url})
    if callback_text and callback_data:
        buttons.append({"text": callback_text, "callback_data": callback_data})
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [buttons]}
    result = request_json(
        f"https://api.telegram.org/bot{secrets['bot_token']}/sendMessage",
        payload)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Telegram не подтвердил доставку")
    message = result.get("result")
    message_id = message.get("message_id") if isinstance(message, dict) else None
    return message_id if isinstance(message_id, int) and message_id > 0 else None


def telegram_edit(secrets, message_id, text, *, preview_url=None):
    preview_options = {"is_disabled": True}
    if preview_url:
        preview_options = {
            "is_disabled": False,
            "url": preview_url,
            "prefer_large_media": True,
            "show_above_text": True,
        }
    payload = {
        "chat_id": secrets["chat_id"],
        "message_id": int(message_id),
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": preview_options,
        "reply_markup": {"inline_keyboard": []},
    }
    result = request_json(
        f"https://api.telegram.org/bot{secrets['bot_token']}/editMessageText",
        payload,
    )
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Telegram не подтвердил изменение сообщения")
    return True


def setup():
    path = ROOT / "secrets.json"
    if path.exists():
        raise ValueError("secrets.json уже существует; автоматическая перезапись отключена")
    values = {"bot_token": getpass.getpass("Токен BotFather (ввод скрыт): ").strip(),
              "chat_id": input("Твой числовой Telegram chat ID: ").strip(),
              "swift_api_key": getpass.getpass("Ключ Swift (можно оставить пустым): ").strip()}
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", values["bot_token"]):
        raise ValueError("Неверный формат токена")
    if not values["chat_id"].isdigit() or int(values["chat_id"]) <= 0:
        raise ValueError("Нужен ID личного чата — положительное число")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(values, stream)
    print("Настройки сохранены локально. Сообщения ещё не отправлялись.")


def demo():
    now = time.time()
    checked = datetime.now(timezone.utc).isoformat()
    config = {"budget_rub": 5000, "reserve_rub": 1000, "max_lot_rub": 1000,
              "min_scenario_profit_rub": 100, "min_roi_fraction": 0.15,
              "rub_per_ton": 100, "rub_rate_checked_at": checked,
              "costs": {"tonnel": {"checked_at": checked, "sell_fee_fraction": 0.02,
                                    "extra_buy_rub": 0, "transfer_and_withdraw_rub": 50}}}
    rule = {"collection": "Demo Gift", "sale_scenario_ton": 10, "checked_at": checked}
    item = {"provider": "tonnel", "giftId": "demo", "title": "ВЫМЫШЛЕННЫЙ ПОДАРОК",
            "number": 1, "price": 7, "on_sale": True, "premarket": False,
            "transferable_at": checked, "resale_available_at": checked}
    print("ДЕМО — все цены, курс и комиссии вымышлены; отправки и сети нет.\n")
    print(message(item, evaluate(item, rule, config, now + 1), rule, False))


def run(config, secrets, send, once):
    if not secrets.get("swift_api_key"):
        raise ValueError("Живой источник не подключён: нужен ключ Swift Partner API")
    if not config.get("watchlist"):
        raise ValueError("Сначала заполни watchlist в config.json")
    now = time.time()
    if not fresh(config.get("rub_rate_checked_at"), now, 86400):
        raise ValueError("Нужен курс TON/рубль, проверенный за последние 24 часа")
    if number(config.get("rub_per_ton")) <= 0:
        raise ValueError("Нужен положительный курс TON/рубль")
    for market in ("tonnel", "mrkt"):
        costs = config["costs"][market]
        if not fresh(costs.get("checked_at"), now, 86400):
            raise ValueError(f"Проверь и заполни комиссии {market} за последние 24 часа")
        for key in ("sell_fee_fraction", "extra_buy_rub", "transfer_and_withdraw_rub"):
            number(costs.get(key))
        if number(costs["sell_fee_fraction"]) >= 1:
            raise ValueError("Комиссия продажи должна быть долей от 0 до 1, не включая 1")
    for rule in config["watchlist"]:
        if not rule.get("collection") or not fresh(rule.get("checked_at"), now, 86400):
            raise ValueError("Нужны коллекция и свежая дата проверки сценария продажи")
        number(rule.get("sale_scenario_ton"))
    interval = max(30, float(number(config.get("poll_seconds", 60))))
    pages = max(1, min(10, int(config.get("max_pages", 2))))
    with sqlite3.connect(ROOT / "state.sqlite3") as db:
        db.execute("CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, price TEXT)")
        while True:
            try:
                for rule in config["watchlist"]:
                    rows, complete = fetch(rule, secrets["swift_api_key"], pages)
                    candidates = 0
                    for item in rows:
                        try:
                            result = evaluate(item, rule, config, time.time())
                        except (ValueError, TypeError, KeyError):
                            continue
                        if result is None:
                            continue
                        candidates += 1
                        key = f"{item['provider']}:{item['giftId']}"
                        old = db.execute("SELECT price FROM alerts WHERE id=?", (key,)).fetchone()
                        if old and number(item["price"]) >= number(old[0]):
                            continue
                        text = message(item, result, rule, complete)
                        if send:
                            telegram(secrets, text)
                            db.execute("INSERT OR REPLACE INTO alerts VALUES (?, ?)",
                                       (key, str(item["price"])))
                            db.commit()
                            time.sleep(1)
                        else:
                            print(text + "\n")
                    print(f"Прочитано: {len(rows)}; прошло фильтры: {candidates}; "
                          f"покрытие: {'полное по ответу источника' if complete else 'ограниченное'}")
            except RuntimeError as exc:
                print(f"Мониторинг прерван на этом цикле: {exc}")
                if once:
                    raise
                # Slow down on errors including rate limits; no rapid retry loop.
                time.sleep(max(interval, 120))
                continue
            if once:
                return
            time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["demo", "setup", "test-message", "watch"])
    parser.add_argument("--send", action="store_true", help="Разрешить отправку уведомлений")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.mode == "demo":
        demo()
        return
    if args.mode == "setup":
        setup()
        return
    secrets = json.loads((ROOT / "secrets.json").read_text())
    if args.mode == "test-message":
        telegram(secrets, "Gift Watch: проверка доставки. Живой мониторинг пока не подтверждён.")
        print("Telegram подтвердил доставку.")
        return
    config = json.loads((ROOT / "config.json").read_text())
    run(config, secrets, args.send, args.once)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, KeyError, OSError) as exc:
        # No traceback with request objects or secrets.
        print(f"Не удалось запустить: {exc}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Остановлено.")
