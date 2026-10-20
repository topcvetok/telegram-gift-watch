"""Telegram controls for live Gift Watch thresholds."""

import argparse
import json
import sqlite3
import sys
import time

from bot import ROOT, request_json
from runtime_settings import (
    FEATURE_LABELS,
    NUMBER_SETTING_LABELS,
    SOURCE_LABELS,
    activate_settings,
    authorize_chat,
    is_authorized,
    revoke_chat,
    replace_filters,
    set_feature_enabled,
    set_number_setting,
    set_setting,
    set_source_enabled,
)


def api(secrets, method, payload=None):
    result = request_json(
        f"https://api.telegram.org/bot{secrets['bot_token']}/{method}",
        payload or {},
    )
    if not isinstance(result, dict) or result.get("ok") is not True:
        description = result.get("description") if isinstance(result, dict) else None
        detail = f": {description}" if description else ""
        raise RuntimeError(f"Telegram не подтвердил {method}{detail}")
    return result.get("result")


def ensure_view_table(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS settings_view "
        "(chat_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL)"
    )
    db.commit()


def remembered_settings_message(db, chat_id):
    ensure_view_table(db)
    row = db.execute(
        "SELECT message_id FROM settings_view WHERE chat_id=?", (str(chat_id),)
    ).fetchone()
    return row[0] if row else None


def remember_settings_message(db, chat_id, message_id):
    if message_id is None:
        return
    ensure_view_table(db)
    db.execute(
        "INSERT OR REPLACE INTO settings_view (chat_id, message_id) VALUES (?, ?)",
        (str(chat_id), int(message_id)),
    )
    db.commit()


def show_settings(secrets, config, db, *, message_id=None):
    """Compatibility entry point: settings now live only in the Mini App."""
    return send_app_launcher(secrets, config)


def send_text(secrets, text):
    return api(secrets, "sendMessage", {
        "chat_id": secrets["chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    })


def send_app_launcher(secrets, config):
    app_url = str(config.get("mini_app_url") or "").strip()
    if not app_url:
        raise ValueError("Mini App пока не настроен")
    return api(secrets, "sendMessage", {
        "chat_id": secrets["chat_id"],
        "text": "Открой Mini App кнопкой ниже. После сохранения изменения сразу попадут в бот.",
        "reply_markup": {
            "keyboard": [[{
                "text": "⚙️ Открыть Gift Watch",
                "web_app": {"url": app_url},
            }]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        },
        "link_preview_options": {"is_disabled": True},
    })


def ensure_input_table(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS settings_input "
        "(chat_id TEXT PRIMARY KEY, setting_key TEXT NOT NULL, message_id INTEGER)"
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(settings_input)")}
    if "message_id" not in columns:
        db.execute("ALTER TABLE settings_input ADD COLUMN message_id INTEGER")
    db.commit()


def clear_input(db, chat_id):
    ensure_input_table(db)
    db.execute("DELETE FROM settings_input WHERE chat_id=?", (str(chat_id),))
    db.commit()


def delete_message(secrets, message_id):
    if message_id is None:
        return
    try:
        api(secrets, "deleteMessage", {
            "chat_id": secrets["chat_id"],
            "message_id": message_id,
        })
    except RuntimeError:
        pass


def remove_legacy_settings_views(secrets, db):
    """Delete remembered chat settings panels and cancel old text-input flows."""
    ensure_view_table(db)
    rows = db.execute("SELECT chat_id, message_id FROM settings_view").fetchall()
    for chat_id, message_id in rows:
        delete_message(dict(secrets, chat_id=str(chat_id)), message_id)
    db.execute("DELETE FROM settings_view")
    ensure_input_table(db)
    db.execute("DELETE FROM settings_input")
    db.commit()


def parse_command(text):
    parts = str(text or "").strip().split()
    if not parts:
        return "", []
    command = parts[0].split("@", 1)[0].casefold()
    return command, parts[1:]


def apply_command(text, config, db, chat_id=None):
    command, _ = parse_command(text)
    if command in ("/app", "/settings", "/start", "/help"):
        return "app"
    return None


def apply_web_app_payload(data, db, chat_id=None):
    try:
        payload = json.loads(str(data or ""))
    except json.JSONDecodeError:
        raise ValueError("Mini App передал повреждённые настройки") from None
    if not isinstance(payload, dict) or payload.get("type") != "save_settings":
        raise ValueError("Неизвестная команда Mini App")
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("Mini App не передал настройки")
    prices = settings.get("prices") or {}
    for name, key in (("max_lot", "max_lot_rub"), ("min_floor", "min_floor_rub")):
        entry = prices.get(name)
        if isinstance(entry, dict):
            set_setting(
                db, key, entry.get("value"),
                currency=str(entry.get("currency") or "rub").casefold(),
                chat_id=chat_id,
            )
    for source, enabled in (settings.get("sources") or {}).items():
        if source in SOURCE_LABELS and isinstance(enabled, bool):
            set_source_enabled(db, source, enabled, chat_id=chat_id)
    for feature, enabled in (settings.get("features") or {}).items():
        if feature in FEATURE_LABELS and isinstance(enabled, bool):
            set_feature_enabled(db, feature, enabled, chat_id=chat_id)
    for key, value in (settings.get("numbers") or {}).items():
        if key in NUMBER_SETTING_LABELS:
            set_number_setting(db, key, value, chat_id=chat_id)
    if "filters" in settings:
        replace_filters(db, settings.get("filters") or {}, chat_id=chat_id)
    return True


def handle_update(update, secrets, config, db):
    owner_chat = str(secrets["chat_id"])
    message = update.get("message") if isinstance(update, dict) else None
    if isinstance(message, dict):
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("id") is None:
            return
        chat_id = str(chat["id"])
        chat_secrets = dict(secrets, chat_id=chat_id)
        text = message.get("text")
        command, args = parse_command(text)
        if not is_authorized(db, chat_id, owner_chat):
            if command == "/join" and args and secrets.get("access_code"):
                if " ".join(args) == str(secrets["access_code"]):
                    authorize_chat(db, chat_id)
                    delete_message(chat_secrets, message.get("message_id"))
                    send_app_launcher(chat_secrets, config)
                else:
                    send_text(chat_secrets, "Неверный код доступа.")
            return
        if command == "/leave" and chat_id != owner_chat:
            revoke_chat(db, chat_id)
            delete_message(chat_secrets, message.get("message_id"))
            send_text(chat_secrets, "Доступ отключён. Настройки сохранены для повторного входа.")
            return
        web_app_data = message.get("web_app_data")
        if isinstance(web_app_data, dict):
            try:
                apply_web_app_payload(web_app_data.get("data"), db, chat_id)
                delete_message(chat_secrets, message.get("message_id"))
            except ValueError as exc:
                send_text(chat_secrets, f"Не удалось сохранить настройки: {exc}")
            return
        try:
            action = apply_command(text, config, db, chat_id)
            if action == "app":
                clear_input(db, chat_id)
                delete_message(chat_secrets, message.get("message_id"))
                send_app_launcher(chat_secrets, config)
        except ValueError as exc:
            send_text(chat_secrets, str(exc))
        return

    callback = update.get("callback_query") if isinstance(update, dict) else None
    if not isinstance(callback, dict):
        return
    callback_message = callback.get("message")
    chat = callback_message.get("chat") if isinstance(callback_message, dict) else None
    if not isinstance(chat, dict) or chat.get("id") is None:
        return
    chat_id = str(chat["id"])
    if not is_authorized(db, chat_id, owner_chat):
        return
    chat_secrets = dict(secrets, chat_id=chat_id)
    callback_id = callback.get("id")
    if callback_id:
        api(chat_secrets, "answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "Настройки перенесены в Mini App",
        })
    send_app_launcher(chat_secrets, config)


def run(config, secrets, db, duration=3300):
    api(secrets, "deleteMyCommands")
    # Data-saving Mini Apps must be opened from a reply-keyboard Web App button.
    # Hide the command menu; manually entered /app sends the compatible launcher.
    api(secrets, "setChatMenuButton", {"menu_button": {"type": "default"}})
    remove_legacy_settings_views(secrets, db)
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
        activate_settings(db, secrets["chat_id"])
        run(config, secrets, db, args.duration)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Не удалось запустить настройки Telegram: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Управление настройками остановлено.")
