"""Telegram controls for live Gift Watch thresholds."""

import argparse
import json
import sqlite3
import sys
import time

from bot import ROOT, request_json
from runtime_settings import SETTING_LABELS, effective_config, max_purchase_limit, set_setting


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
        "Нажми кнопку и отправь свою цену обычным сообщением. "
        "Новое значение сразу применится ко всем площадкам."
    )


def format_rubles(value):
    return f"{int(value):,}".replace(",", " ")


def settings_keyboard(config, db):
    effective_config(config, db)
    return {"inline_keyboard": [
        [{
            "text": "💰 Ввести максимальную стоимость",
            "callback_data": "settings:input:max_lot_rub",
        }],
        [{
            "text": "📊 Ввести минимальный floor",
            "callback_data": "settings:input:min_floor_rub",
        }],
        [{"text": "🔄 Обновить настройки", "callback_data": "settings:refresh"}],
    ]}


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


def send_input_prompt(secrets, key, config):
    if key == "max_lot_rub":
        limit = max_purchase_limit(config)
        limit_text = f" от 1 до {format_rubles(limit)} ₽" if limit is not None else ""
        text = f"Введи максимальную стоимость подарка{limit_text}. Например: <b>2500</b>"
    else:
        text = "Введи минимальный floor в рублях. Например: <b>1200</b>"
    return api(secrets, "sendMessage", {
        "chat_id": secrets["chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {
            "force_reply": True,
            "input_field_placeholder": "Введи сумму в рублях",
        },
        "link_preview_options": {"is_disabled": True},
    })


def ensure_input_table(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS settings_input "
        "(chat_id TEXT PRIMARY KEY, setting_key TEXT NOT NULL)"
    )
    db.commit()


def begin_input(db, chat_id, key):
    if key not in SETTING_LABELS:
        raise ValueError("Неизвестная настройка")
    ensure_input_table(db)
    db.execute(
        "INSERT OR REPLACE INTO settings_input (chat_id, setting_key) VALUES (?, ?)",
        (str(chat_id), key),
    )
    db.commit()


def pending_input(db, chat_id):
    ensure_input_table(db)
    row = db.execute(
        "SELECT setting_key FROM settings_input WHERE chat_id=?", (str(chat_id),)
    ).fetchone()
    return row[0] if row else None


def clear_input(db, chat_id):
    ensure_input_table(db)
    db.execute("DELETE FROM settings_input WHERE chat_id=?", (str(chat_id),))
    db.commit()


def normalize_price_text(text):
    value = str(text or "").strip().replace(" ", "").replace("₽", "")
    if not value or not value.isdecimal():
        raise ValueError("Введи цену целым числом в рублях, например: 2500")
    return value


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
    if command == "/cancel":
        return "cancel"
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
        return "refresh", None
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[:2] != ["settings", "input"]:
        raise ValueError("Неизвестная кнопка")
    key = parts[2]
    if key not in SETTING_LABELS:
        raise ValueError("Неизвестная настройка")
    return "input", key


def handle_update(update, secrets, config, db):
    expected_chat = str(secrets["chat_id"])
    message = update.get("message") if isinstance(update, dict) else None
    if isinstance(message, dict):
        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("id")) != expected_chat:
            return
        try:
            text = message.get("text")
            command, _ = parse_command(text)
            key = pending_input(db, expected_chat)
            if key is not None and not command.startswith("/"):
                value = normalize_price_text(text)
                maximum = max_purchase_limit(config) if key == "max_lot_rub" else None
                set_setting(db, key, value, maximum=maximum)
                clear_input(db, expected_chat)
                show_settings(secrets, config, db)
                return
            action = apply_command(text, config, db)
            if action == "settings":
                clear_input(db, expected_chat)
                show_settings(secrets, config, db)
            elif action == "cancel":
                clear_input(db, expected_chat)
                show_settings(secrets, config, db)
        except ValueError as exc:
            send_text(secrets, f"{exc}\nПопробуй ещё раз или отправь /cancel.")
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
        action, key = apply_callback(callback.get("data"), config, db)
        if callback_id:
            api(secrets, "answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": "Теперь введи цену" if action == "input" else "Настройки обновлены",
            })
        if action == "input":
            begin_input(db, expected_chat, key)
            send_input_prompt(secrets, key, config)
        else:
            show_settings(secrets, config, db)
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
        {"command": "cancel", "description": "Отменить ввод цены"},
    ]})
    deadline = time.monotonic() + max(1, int(duration))
    offset = None
    while time.monotonic() < deadline:
        payload = {"timeout": 15, "limit": 20}
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
    with sqlite3.connect(ROOT / "settings.sqlite3", timeout=5) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        run(config, secrets, db, args.duration)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Не удалось запустить настройки Telegram: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Управление настройками остановлено.")
