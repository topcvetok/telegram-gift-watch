"""Telegram controls for live Gift Watch thresholds."""

import argparse
import json
import re
import sqlite3
import sys
import time
from decimal import Decimal

from bot import ROOT, request_json
from runtime_settings import (
    FEATURE_LABELS,
    NUMBER_SETTING_LABELS,
    SETTING_LABELS,
    SOURCE_LABELS,
    activate_settings,
    authorize_chat,
    effective_config,
    feature_enabled,
    is_authorized,
    revoke_chat,
    set_feature_enabled,
    set_number_setting,
    set_setting,
    set_source_enabled,
    source_enabled,
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


def settings_text(config, db, chat_id=None):
    current = effective_config(config, db, chat_id)
    sources = "\n".join(
        f"{'✅' if source_enabled(current, source) else '❌'} {label}"
        for source, label in SOURCE_LABELS.items()
    )
    reasons = "\n".join((
        f"{'✅' if feature_enabled(current, 'collection_basis') else '❌'} Floor коллекции",
        f"{'✅' if feature_enabled(current, 'model_basis') else '❌'} Floor модели",
        f"{'✅' if feature_enabled(current, 'backdrop_basis') else '❌'} Цвет фона",
        f"{'✅' if feature_enabled(current, 'beautiful_number') else '❌'} "
        f"Красивый номер: ≤ <b>{current.get('beautiful_number_max_distinct_digits', 3)}</b> разных цифр",
        f"{'✅' if feature_enabled(current, 'monochrome') else '❌'} "
        f"Монохром: от <b>{current.get('monochrome_min_score', 80)}%</b>, "
        f"цена до <b>+{current.get('monochrome_max_floor_premium_percent', 15)}%</b> к floor модели",
    ))
    upgrades = (
        f"{'✅' if feature_enabled(current, 'dark_upgrades') else '❌'} "
        "Новые улучшения с Black / Onyx Black"
    )
    return (
        "<b>Настройки Gift Watch</b>\n\n"
        f"💰 Максимальная стоимость: <b>{format_threshold(current, 'max_lot')}</b>\n"
        f"📊 Минимальный floor: <b>{format_threshold(current, 'min_floor')}</b>\n\n"
        f"<b>Основания:</b>\n{reasons}\n\n"
        f"<b>Другие уведомления:</b>\n{upgrades}\n\n"
        f"<b>Площадки:</b>\n{sources}\n\n"
        "Нажми кнопку и отправь новое значение. "
        "Цены и переключатели применяются сразу.\n\n"
        "<i>Каталог коллекций для улучшений: @GiftChangesAPI</i>"
    )


def format_rubles(value):
    return f"{int(value):,}".replace(",", " ")


def format_threshold(config, name):
    ton_key = f"{name}_ton"
    if ton_key in config:
        value = format(Decimal(str(config[ton_key])), "f")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        return f"{value or '0'} TON"
    return f"{format_rubles(config[f'{name}_rub'])} ₽"


def settings_keyboard(config, db, chat_id=None):
    current = effective_config(config, db, chat_id)
    source_rows = [[{
        "text": f"{'✅' if source_enabled(current, source) else '❌'} {label}",
        "callback_data": f"settings:toggle:{source}",
    }] for source, label in SOURCE_LABELS.items()]
    feature_rows = [
        [{
            "text": f"{'✅' if feature_enabled(current, 'collection_basis') else '❌'} Floor коллекции",
            "callback_data": "settings:feature:collection_basis",
        }],
        [{
            "text": f"{'✅' if feature_enabled(current, 'model_basis') else '❌'} Floor модели",
            "callback_data": "settings:feature:model_basis",
        }],
        [{
            "text": f"{'✅' if feature_enabled(current, 'backdrop_basis') else '❌'} Цвет фона",
            "callback_data": "settings:feature:backdrop_basis",
        }],
        [{
            "text": f"{'✅' if feature_enabled(current, 'beautiful_number') else '❌'} Красивый номер",
            "callback_data": "settings:feature:beautiful_number",
        }],
        [{
            "text": f"🔢 Разных цифр: {current.get('beautiful_number_max_distinct_digits', 3)}",
            "callback_data": "settings:number:beautiful_number_max_distinct_digits",
        }],
        [{
            "text": f"{'✅' if feature_enabled(current, 'monochrome') else '❌'} Монохром",
            "callback_data": "settings:feature:monochrome",
        }],
        [{
            "text": f"🎨 Совпадение: от {current.get('monochrome_min_score', 80)}%",
            "callback_data": "settings:number:monochrome_min_score",
        }],
        [{
            "text": f"📈 Монохром до +{current.get('monochrome_max_floor_premium_percent', 15)}% к floor",
            "callback_data": "settings:number:monochrome_max_floor_premium_percent",
        }],
        [{
            "text": (
                f"{'✅' if feature_enabled(current, 'dark_upgrades') else '❌'} "
                "Улучшения Black / Onyx"
            ),
            "callback_data": "settings:feature:dark_upgrades",
        }],
    ]
    return {"inline_keyboard": [
        [{
            "text": "💰 Ввести максимальную стоимость",
            "callback_data": "settings:input:max_lot_rub",
        }],
        [{
            "text": "📊 Ввести минимальный floor",
            "callback_data": "settings:input:min_floor_rub",
        }],
        *feature_rows,
        *source_rows,
        [{"text": "🔄 Обновить настройки", "callback_data": "settings:refresh"}],
    ]}


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
    if message_id is None:
        message_id = remembered_settings_message(db, secrets["chat_id"])
    payload = {
        "chat_id": secrets["chat_id"],
        "text": settings_text(config, db, secrets["chat_id"]),
        "parse_mode": "HTML",
        "reply_markup": settings_keyboard(config, db, secrets["chat_id"]),
        "link_preview_options": {"is_disabled": True},
    }
    method = "sendMessage"
    if message_id is not None:
        method = "editMessageText"
        payload["message_id"] = message_id
    try:
        result = api(secrets, method, payload)
    except RuntimeError as exc:
        if "message is not modified" in str(exc):
            return None
        if method != "editMessageText":
            raise
        payload.pop("message_id", None)
        result = api(secrets, "sendMessage", payload)
    returned_message_id = result.get("message_id") if isinstance(result, dict) else None
    remember_settings_message(
        db,
        secrets["chat_id"],
        returned_message_id if returned_message_id is not None else message_id,
    )
    return result


def send_text(secrets, text):
    return api(secrets, "sendMessage", {
        "chat_id": secrets["chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    })


def send_input_prompt(secrets, key, config, *, message_id=None, error=None):
    if key == "max_lot_rub":
        text = (
            "Введи максимальную стоимость подарка от 1 ₽. "
            "Например: <b>5000 ₽</b> или <b>25 TON</b>"
        )
    elif key == "min_floor_rub":
        text = "Введи минимальный floor. Например: <b>1200 ₽</b> или <b>6 TON</b>"
    elif key == "beautiful_number_max_distinct_digits":
        text = "Сколько разных цифр максимум может быть в красивом номере? Введи число от <b>1</b> до <b>10</b>."
    elif key == "monochrome_max_floor_premium_percent":
        text = "На сколько процентов цена монохрома может быть выше floor модели? Введи число от <b>0</b> до <b>100</b>. Сейчас рекомендуется <b>15</b>."
    else:
        text = "Введи минимальное совпадение модели и фона от <b>0</b> до <b>100</b> процентов. Для сильного монохрома рекомендуется <b>80</b>."
    if error:
        text = f"⚠️ {error}\n\n{text}"
    payload = {
        "chat_id": secrets["chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[{
                "text": "↩️ Отменить ввод",
                "callback_data": "settings:cancel",
            }]],
        },
        "link_preview_options": {"is_disabled": True},
    }
    method = "sendMessage"
    if message_id is not None:
        method = "editMessageText"
        payload["message_id"] = message_id
    return api(secrets, method, payload)


def ensure_input_table(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS settings_input "
        "(chat_id TEXT PRIMARY KEY, setting_key TEXT NOT NULL, message_id INTEGER)"
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(settings_input)")}
    if "message_id" not in columns:
        db.execute("ALTER TABLE settings_input ADD COLUMN message_id INTEGER")
    db.commit()


def begin_input(db, chat_id, key, message_id=None):
    if key not in SETTING_LABELS and key not in NUMBER_SETTING_LABELS:
        raise ValueError("Неизвестная настройка")
    ensure_input_table(db)
    db.execute(
        "INSERT OR REPLACE INTO settings_input "
        "(chat_id, setting_key, message_id) VALUES (?, ?, ?)",
        (str(chat_id), key, message_id),
    )
    db.commit()


def pending_input(db, chat_id):
    return pending_input_state(db, chat_id)[0]


def pending_input_state(db, chat_id):
    ensure_input_table(db)
    row = db.execute(
        "SELECT setting_key, message_id FROM settings_input WHERE chat_id=?",
        (str(chat_id),),
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


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


def normalize_price_text(text, db=None):
    value = str(text or "").strip().casefold().replace(" ", "")
    match = re.fullmatch(
        r"(\d+(?:[.,]\d+)?)(ton|тон|₽|rub|руб|рублей|р)?", value
    )
    if not match:
        raise ValueError("Введи цену, например: 2500 ₽ или 12.5 TON")
    number, currency = match.groups()
    if currency in ("ton", "тон"):
        amount = Decimal(number.replace(",", "."))
        return format(amount, "f"), "ton"
    if not number.isdecimal():
        raise ValueError("Сумму в рублях укажи целым числом")
    return number, "rub"


def normalize_integer_text(text):
    value = str(text or "").strip().replace("%", "").strip()
    if not value.isdecimal():
        raise ValueError("Введи целое число")
    return int(value)


def parse_command(text):
    parts = str(text or "").strip().split()
    if not parts:
        return "", []
    command = parts[0].split("@", 1)[0].casefold()
    return command, parts[1:]


def apply_command(text, config, db, chat_id=None):
    command, args = parse_command(text)
    if command in ("/settings", "/start", "/help"):
        return "settings"
    if command == "/cancel":
        return "cancel"
    mapping = {"/maxprice": "max_lot_rub", "/minfloor": "min_floor_rub"}
    key = mapping.get(command)
    if key is None:
        return None
    if not args:
        example = "/maxprice 2500" if key == "max_lot_rub" else "/minfloor 1200"
        raise ValueError(f"Формат команды: {example}")
    value, currency = normalize_price_text(" ".join(args))
    set_setting(db, key, value, currency=currency, chat_id=chat_id)
    return "settings"


def apply_callback(data, config, db, chat_id=None):
    if data == "settings:refresh":
        return "refresh", None
    if data == "settings:cancel":
        return "cancel", None
    parts = str(data or "").split(":")
    if len(parts) == 3 and parts[:2] == ["settings", "toggle"]:
        source = parts[2]
        current = effective_config(config, db, chat_id)
        enabled = set_source_enabled(
            db, source, not source_enabled(current, source), chat_id=chat_id
        )
        return ("enabled" if enabled else "disabled"), source
    if len(parts) == 3 and parts[:2] == ["settings", "feature"]:
        feature = parts[2]
        current = effective_config(config, db, chat_id)
        enabled = set_feature_enabled(
            db, feature, not feature_enabled(current, feature), chat_id=chat_id
        )
        return ("feature_enabled" if enabled else "feature_disabled"), feature
    if len(parts) == 3 and parts[:2] == ["settings", "number"]:
        key = parts[2]
        if key not in NUMBER_SETTING_LABELS:
            raise ValueError("Неизвестная настройка")
        return "input", key
    if len(parts) != 3 or parts[:2] != ["settings", "input"]:
        raise ValueError("Неизвестная кнопка")
    key = parts[2]
    if key not in SETTING_LABELS:
        raise ValueError("Неизвестная настройка")
    return "input", key


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
                    show_settings(chat_secrets, config, db)
                else:
                    send_text(chat_secrets, "Неверный код доступа.")
            return
        if command == "/leave" and chat_id != owner_chat:
            revoke_chat(db, chat_id)
            delete_message(chat_secrets, message.get("message_id"))
            send_text(chat_secrets, "Доступ отключён. Настройки сохранены для повторного входа.")
            return
        key, settings_message_id = pending_input_state(db, chat_id)
        try:
            if key is not None and not command.startswith("/"):
                if key in SETTING_LABELS:
                    value, currency = normalize_price_text(text)
                    set_setting(
                        db, key, value, currency=currency, chat_id=chat_id
                    )
                else:
                    set_number_setting(
                        db, key, normalize_integer_text(text), chat_id=chat_id
                    )
                clear_input(db, chat_id)
                delete_message(chat_secrets, message.get("message_id"))
                show_settings(
                    chat_secrets, config, db, message_id=settings_message_id
                )
                return
            action = apply_command(text, config, db, chat_id)
            if action == "settings":
                clear_input(db, chat_id)
                delete_message(chat_secrets, message.get("message_id"))
                show_settings(
                    chat_secrets, config, db, message_id=settings_message_id
                )
            elif action == "cancel":
                clear_input(db, chat_id)
                delete_message(chat_secrets, message.get("message_id"))
                show_settings(
                    chat_secrets, config, db, message_id=settings_message_id
                )
        except ValueError as exc:
            if key is not None and settings_message_id is not None:
                delete_message(chat_secrets, message.get("message_id"))
                send_input_prompt(
                    chat_secrets,
                    key,
                    config,
                    message_id=settings_message_id,
                    error=str(exc),
                )
            else:
                send_text(chat_secrets, f"{exc}\nПопробуй ещё раз или отправь /cancel.")
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
    settings_message_id = callback_message.get("message_id")
    try:
        action, key = apply_callback(callback.get("data"), config, db, chat_id)
        if callback_id:
            if action == "input":
                answer = "Теперь введи значение"
            elif action == "cancel":
                answer = "Ввод отменён"
            elif action in ("enabled", "disabled"):
                answer = f"{SOURCE_LABELS[key]} {'включён' if action == 'enabled' else 'отключён'}"
            elif action in ("feature_enabled", "feature_disabled"):
                answer = f"{FEATURE_LABELS[key]} {'включён' if action == 'feature_enabled' else 'отключён'}"
            else:
                answer = "Настройки обновлены"
            api(chat_secrets, "answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": answer,
            })
        if action == "input":
            begin_input(db, chat_id, key, settings_message_id)
            send_input_prompt(
                chat_secrets, key, config, message_id=settings_message_id
            )
        else:
            if action == "cancel":
                clear_input(db, chat_id)
            show_settings(
                chat_secrets, config, db, message_id=settings_message_id
            )
    except ValueError as exc:
        if callback_id:
            api(chat_secrets, "answerCallbackQuery", {
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
        {"command": "join", "description": "Войти по коду доступа"},
        {"command": "leave", "description": "Отключить доступ"},
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
