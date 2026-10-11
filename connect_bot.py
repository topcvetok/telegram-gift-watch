"""Local, hidden-token setup for the user's existing notification bot."""
import getpass
import json
import os
import re
import secrets
import time

from bot import ROOT, request_json, telegram

EXPECTED_BOT = "giftschatnickbot"


def api(token, method, body=None):
    result = request_json(f"https://api.telegram.org/bot{token}/{method}", body or {})
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Telegram не подтвердил запрос")
    return result["result"]


def matching_chat(updates, challenge):
    for update in updates:
        message = update.get("message", {})
        chat = message.get("chat", {})
        sender = message.get("from", {})
        if (message.get("text") == "/start " + challenge
                and chat.get("type") == "private"
                and chat.get("id") == sender.get("id")
                and isinstance(chat.get("id"), int) and chat["id"] > 0):
            return str(chat["id"])
    return None


def connect():
    path = ROOT / "secrets.json"
    if path.exists():
        raise RuntimeError("Настройки уже существуют. Перезапись отключена.")
    print(f"Подключение @{EXPECTED_BOT}. Токен вводится только здесь, не в чате.", flush=True)
    print("Он будет сохранён локально в secrets.json с доступом только владельцу файла.", flush=True)
    token = getpass.getpass("Вставь токен BotFather и нажми Enter (ввод не отображается): ").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise RuntimeError("Неверный формат токена; ничего не сохранено.")
    identity = api(token, "getMe")
    if identity.get("username", "").lower() != EXPECTED_BOT:
        raise RuntimeError("Этот токен принадлежит другому боту; ничего не сохранено.")
    if api(token, "getWebhookInfo").get("url"):
        raise RuntimeError("У бота уже подключён webhook. Он не изменён; нужна отдельная настройка chat ID.")
    challenge = "bind_" + secrets.token_hex(12)
    print(f"\nТокен проверен. Открой https://t.me/{EXPECTED_BOT}?start={challenge}", flush=True)
    print("Нажми Start. Или отправь этому боту следующую команду:", flush=True)
    print("/start " + challenge, flush=True)
    input("После отправки нажми Enter здесь: ")
    # No offset: do not acknowledge or discard pending updates belonging to another client.
    updates = api(token, "getUpdates", {"timeout": 0, "limit": 100})
    chat_id = matching_chat(updates, challenge)
    if chat_id is None:
        raise RuntimeError("Команда привязки не найдена. Возможно, другой процесс читает бота. Ничего не сохранено.")
    values = {"bot_token": token, "chat_id": chat_id, "swift_api_key": ""}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(values, stream)
    print("Бот привязан к твоему личному чату; настройки сохранены.", flush=True)
    input("Нажми Enter, чтобы отправить себе одно тестовое уведомление; Ctrl+C — отмена: ")
    telegram(values, "Gift Watch подключён. Это тест доставки; мониторинг Tonnel/MRKT ещё не запущен.")
    print("Telegram подтвердил доставку. Источники объявлений ещё требуют подключения.", flush=True)


if __name__ == "__main__":
    try:
        connect()
    except (ValueError, RuntimeError, KeyError, OSError):
        # Keep credentials, request URLs and remote payloads out of output.
        print("Подключение не завершено. Проверь токен, сеть и отсутствие другого обработчика бота.")
        raise SystemExit(1)
    except (KeyboardInterrupt, EOFError):
        print("Настройка остановлена пользователем.")
