"""Telegram controls for live Gift Watch thresholds."""

import argparse
import json
import sqlite3
import sys
import time

from bot import ROOT, request_json
from runtime_settings import effective_config, max_purchase_limit, set_setting


MAX_PRICE_PRESETS = (1000, 2000, 3000, 4000)
MIN_FLOOR_PRESETS = (500, 1000, 1500, 2000)


def api(secrets, method, payload=None):
    result = request_json(
        f"https://api.telegram.org/bot{secrets['bot_token']}/{method}",
        payload or {},
    )
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError(f"Telegram не подтвердил {method}")
    return result.get("result")


def settings_text(config, db):
    current = effective_config(config, db)
    return (
        "<b>Настройки Gift Watch</b>\n\n"
        f"💰 Максимальная стоимость: <b>{format_rubles(current['max_lot_rub'])} ₽</b>\n"
        f"📊 Минимальный floor: <b>{format_rubles(current['min_floor_rub'])} ₽</b>\n\n"
        "Нажми нужное значение ниже — оно сразу применится ко всем площадкам.\n\n"
        "Точное значение: <code>/maxprice 2500</code> или "
        "<code>/minfloor 1200</code>."
    )


def format_rubles(value):
    return f"{int(value):,}".replace(",", " ")


def settings_keyboard(config, db):
    limit = max_purchase_limit(config)
    max_values = [value for value in MAX_PRICE_PRESETS if limit is None or value <= limit]
    current = effective_config(config, db)
    rows = []
    for value in max_values:
        marker = "✅" if value == current["max_lot_rub"] else "💰"
        rows.append([{
            "text": f"{marker} Максимальная цена — {format_rubles(value)} ₽",
            "callback_data": f"setting:max_lot_rub:{value}",
        }])
    for value in MIN_FLOOR_PRESETS:
        marker = "✅" if value == current["min_floor_rub"] else "📊"
        rows.append([{
            "text": f"{marker} Минимальный floor — {format_rubles(value)} ₽",
            "callback_data": f"setting:min_floor_rub:{value}",
        }])
    rows.append([{"text": "🔄 Обновить настройки", "callback_data": "settings:refresh"}])
    return {"inline_keyboard": rows}


def show_settings(secrets, config, db, *, message_id=None):
    payload = {
        "chat_id": secrets["chat_id"],
        "text": settings_text(config, db),
        "parse_mode": "HTML",
        "reply_markup": settings_keyboard(config, db),
        "link_preview_options": {"is_disabled": True},
    }
    method = "sendMessage"
    if message_id is not None:
        method = "editMessageText"
        payload["message_id"] = message_id
    return api(secrets, method, payload)


def send_text(secrets, text):
    return api(secrets, "sendMessage", {
        "chat_id": secrets["chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    })


def parse_command(text):
    parts = str(text or "").strip().split()
    if not parts:
        return "", []
    command = parts[0].split("@", 1)[0].casefold()
    return command, parts[1:]


def apply_command(text, config, db):
    command, args = parse_command(text)
    if command in ("/settings", "/start", "/help"):
        return "settings"
    mapping = {"/maxprice": "max_lot_rub", "/minfloor": "min_floor_rub"}
    key = mapping.get(command)
    if key is None:
        return None
    if len(args) != 1:
        example = "/maxprice 2500" if key == "max_lot_rub" else "/minfloor 1200"
        raise ValueError(f"Формат команды: {example}")
    maximum = max_purchase_limit(config) if key == "max_lot_rub" else None
    set_setting(db, key, args[0], maximum=maximum)
    return "settings"


def apply_callback(data, config, db):
    if data == "settings:refresh":
        return False
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != "setting":
        raise ValueError("Неизвестная кнопка")
    key, value = parts[1:]
    maximum = max_purchase_limit(config) if key == "max_lot_rub" else None
    set_setting(db, key, value, maximum=maximum)
    return True


def handle_update(update, secrets, config, db):
    expected_chat = str(secrets["chat_id"])
    message = update.get("message") if isinstance(update, dict) else None
    if isinstance(message, dict):
        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("id")) != expected_chat:
            return
        try:
            action = apply_command(message.get("text"), config, db)
            if action == "settings":
                show_settings(secrets, config, db)
        except ValueError as exc:
            send_text(secrets, str(exc))
        return

    callback = update.get("callback_query") if isinstance(update, dict) else None
    if not isinstance(callback, dict):
        return
    callback_message = callback.get("message")
    chat = callback_message.get("chat") if isinstance(callback_message, dict) else None
    if not isinstance(chat, dict) or str(chat.get("id")) != expected_chat:
        return
    callback_id = callback.get("id")
    try:
        changed = apply_callback(callback.get("data"), config, db)
        if callback_id:
            api(secrets, "answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": "Настройка сохранена" if changed else "Показываю актуальные настройки",
            })
        show_settings(
            secrets, config, db,
            message_id=callback_message.get("message_id") if changed else None,
        )
    except ValueError as exc:
        if callback_id:
            api(secrets, "answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": str(exc),
                "show_alert": True,
            })


def run(config, secrets, db, duration=3300):
    api(secrets, "setMyCommands", {"commands": [
        {"command": "settings", "description": "Цена покупки и минимальный floor"},
        {"command": "maxprice", "description": "Максимальная цена в рублях"},
        {"command": "minfloor", "description": "Минимальный floor в рублях"},
    ]})
    deadline = time.monotonic() + max(1, int(duration))
    offset = None
    while time.monotonic() < deadline:
        payload = {"timeout": 15, "limit": 20, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        try:
            updates = api(secrets, "getUpdates", payload)
            if not isinstance(updates, list):
                raise RuntimeError("Telegram вернул неверный список команд")
            for update in updates:
                update_id = update.get("update_id") if isinstance(update, dict) else None
                if isinstance(update_id, int):
                    offset = update_id + 1
                handle_update(update, secrets, config, db)
        except RuntimeError as exc:
            print(f"Команды Telegram: {exc}", file=sys.stderr, flush=True)
            time.sleep(min(3, max(0, deadline - time.monotonic())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=3300)
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text())
    secrets = json.loads((ROOT / "secrets.json").read_text())
    with sqlite3.connect(ROOT / "state.sqlite3", timeout=30) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        run(config, secrets, db, args.duration)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Не удалось запустить настройки Telegram: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Управление настройками остановлено.")
