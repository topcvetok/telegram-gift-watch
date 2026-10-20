"""Watch newly upgraded Telegram gifts for Black and Onyx Black backdrops."""

import argparse
import concurrent.futures
import html
import io
import json
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

from curl_cffi import requests
from PIL import Image

from bot import ROOT, telegram
from runtime_settings import activate_settings, active_profiles, feature_enabled, item_allowed


CATALOG_URL = "https://api.changes.tg/gifts"
FRAGMENT_IMAGE = "https://nft.fragment.com/gift/{slug}-{number}.small.jpg"
TELEGRAM_GIFT = "https://t.me/nft/{slug}-{number}"
SEED_PATH = ROOT / "gift_upgrade_heads.json"
TARGET_EDGES = {
    "Black": (14, 15, 15),
    "Onyx Black": (49, 54, 56),
}
_THREAD_LOCAL = threading.local()


def cdn_slug(name):
    return re.sub(r"[^a-z0-9]", "", str(name).casefold())


def telegram_slug(name):
    return re.sub(r"[^A-Za-z0-9]", "", str(name))


def session():
    current = getattr(_THREAD_LOCAL, "session", None)
    if current is None:
        current = requests.Session(impersonate="chrome")
        _THREAD_LOCAL.session = current
    return current


def request(method, url, *, attempts=3):
    last_error = None
    for attempt in range(max(1, int(attempts))):
        try:
            response = session().request(
                method,
                url,
                headers={"accept": "*/*", "user-agent": "GiftWatch/1.0"},
                timeout=20,
            )
            if response.status_code in (200, 404):
                return response
            last_error = RuntimeError(f"HTTP {response.status_code}: {url}")
        except requests.RequestsError as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(1 + attempt)
    raise RuntimeError(f"Telegram upgrade source unavailable: {last_error}")


def gift_exists(name, number):
    url = FRAGMENT_IMAGE.format(slug=cdn_slug(name), number=int(number))
    for attempt in range(3):
        try:
            response = session().head(
                url,
                headers={"accept": "*/*", "user-agent": "GiftWatch/1.0"},
                timeout=20,
            )
            # Fragment returns HTTP 500 for a few existing images while later
            # collectible numbers remain available.
            if response.status_code in (200, 500):
                return True
            if response.status_code == 404:
                return False
        except requests.RequestsError:
            pass
        if attempt < 2:
            time.sleep(1 + attempt)
    raise RuntimeError(f"Telegram upgrade source unavailable: {url}")


def gift_image(name, number):
    url = FRAGMENT_IMAGE.format(slug=cdn_slug(name), number=int(number))
    try:
        response = request("GET", url)
    except RuntimeError:
        return None
    return response.content if response.status_code == 200 else None


def find_latest_number(name):
    """Find the last contiguous collectible number without marketplace data."""
    if not gift_exists(name, 1):
        return 0
    low, high = 1, 2
    while gift_exists(name, high):
        low, high = high, high * 2
        if high > 4_194_304:
            raise RuntimeError(f"Unexpected Telegram gift supply for {name}")
    while low + 1 < high:
        middle = (low + high) // 2
        if gift_exists(name, middle):
            low = middle
        else:
            high = middle
    return low


def classify_backdrop(image_bytes):
    if not image_bytes:
        return None
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
        width, height = image.size
        pixels = []
        for left, top in ((0, 0), (width - 4, 0), (0, height - 4), (width - 4, height - 4)):
            for x in range(max(0, left), min(width, left + 4)):
                for y in range(max(0, top), min(height, top + 4)):
                    pixels.append(image.getpixel((x, y)))
    average = tuple(sum(pixel[i] for pixel in pixels) / len(pixels) for i in range(3))
    matches = sorted(
        (
            sum((average[i] - target[i]) ** 2 for i in range(3)) ** 0.5,
            name,
        )
        for name, target in TARGET_EDGES.items()
    )
    distance, name = matches[0]
    return name if distance <= 10 else None


def parse_telegram_details(page):
    page = str(page or "")
    result = {}
    match = re.search(
        r'<meta\s+name="twitter:description"\s+content="([^"]*)"',
        page,
        re.IGNORECASE,
    )
    if match:
        for line in html.unescape(match.group(1)).splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in {"Model", "Backdrop", "Symbol"}:
                result[key.strip().casefold()] = value.strip()

    owner_match = re.search(
        r"<tr>\s*<th>\s*Owner\s*</th>\s*<td>(.*?)</td>\s*</tr>",
        page,
        re.IGNORECASE | re.DOTALL,
    )
    if not owner_match:
        return result
    owner_html = owner_match.group(1)
    owner = re.sub(r"<[^>]+>", "", owner_html)
    owner = " ".join(html.unescape(owner).split())
    if owner:
        result["owner"] = owner
    username_match = re.search(
        r'href=["\']https?://t\.me/([A-Za-z0-9_]{4,32})(?:[/?#][^"\']*)?["\']',
        owner_html,
        re.IGNORECASE,
    )
    username = username_match.group(1) if username_match else None
    if not username_match and owner:
        owner_username_match = re.fullmatch(
            r"(?:@([A-Za-z0-9_]{4,32})|([A-Za-z0-9_]{4,32})\.t\.me)",
            owner,
            re.IGNORECASE,
        )
        if owner_username_match:
            username = owner_username_match.group(1) or owner_username_match.group(2)
    if username:
        result["owner_username"] = username
    return result


def telegram_details(name, number):
    slug = telegram_slug(name)
    response = request("GET", TELEGRAM_GIFT.format(slug=slug, number=int(number)))
    return parse_telegram_details(response.text) if response.status_code == 200 else {}


def ensure_tables(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS telegram_upgrade_heads ("
        "collection TEXT PRIMARY KEY, last_number INTEGER NOT NULL, checked REAL NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS telegram_upgrade_alerts ("
        "gift_slug TEXT NOT NULL, chat_id TEXT NOT NULL, message_id INTEGER, sent REAL NOT NULL, "
        "PRIMARY KEY (gift_slug, chat_id))"
    )
    db.commit()


def seed_heads(db):
    ensure_tables(db)
    if not SEED_PATH.exists():
        return 0
    seeds = json.loads(SEED_PATH.read_text())
    rows = [
        (str(name), int(number), time.time())
        for name, number in seeds.items()
        if int(number) >= 0
    ]
    db.executemany(
        "INSERT OR IGNORE INTO telegram_upgrade_heads VALUES (?, ?, ?)", rows
    )
    db.commit()
    return len(rows)


def catalog_names():
    response = request("GET", CATALOG_URL)
    if response.status_code != 200:
        raise RuntimeError("Telegram gift catalog is unavailable")
    payload = response.json()
    if not isinstance(payload, list) or not all(isinstance(name, str) for name in payload):
        raise RuntimeError("Telegram gift catalog changed its response")
    return payload


def sync_catalog(db):
    seed_heads(db)
    try:
        names = catalog_names()
    except RuntimeError as exc:
        print(f"Каталог улучшений: {exc}", file=sys.stderr, flush=True)
        return
    known = {
        row[0] for row in db.execute("SELECT collection FROM telegram_upgrade_heads")
    }
    missing = [name for name in names if name not in known]
    for name in missing:
        head = find_latest_number(name)
        db.execute(
            "INSERT OR IGNORE INTO telegram_upgrade_heads VALUES (?, ?, ?)",
            (name, head, time.time()),
        )
        db.commit()


def scan_collection(name, start, max_batch):
    candidates = []
    current = int(start)
    for number in range(current + 1, current + max(1, int(max_batch)) + 1):
        if not gift_exists(name, number):
            break
        image_bytes = gift_image(name, number)
        backdrop = classify_backdrop(image_bytes)
        details = {}
        if backdrop is not None or image_bytes is None:
            details = telegram_details(name, number)
        if image_bytes is None and not details:
            break
        current = number
        reported = details.get("backdrop")
        if backdrop is None and reported:
            backdrop = next(
                (target for target in TARGET_EDGES if reported.casefold() == target.casefold()),
                None,
            )
        if backdrop:
            if reported and reported.casefold() != backdrop.casefold():
                continue
            candidates.append({
                "collection": name,
                "number": number,
                "backdrop": backdrop,
                "model": details.get("model"),
                "symbol": details.get("symbol"),
                "owner": details.get("owner"),
                "owner_username": details.get("owner_username"),
            })
    return name, current, candidates


def gift_link(candidate):
    return TELEGRAM_GIFT.format(
        slug=telegram_slug(candidate["collection"]),
        number=int(candidate["number"]),
    )


def format_owner(candidate):
    username = str(candidate.get("owner_username") or "").strip().lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{4,32}", username):
        escaped = html.escape(username)
        return f'<a href="https://t.me/{escaped}">@{escaped}</a>'
    owner = str(candidate.get("owner") or "").strip()
    if owner and not re.fullmatch(r"[UE]Q[A-Za-z0-9_-]{40,60}", owner):
        return f"<b>{html.escape(owner)}</b>"
    return "<b>ник скрыт</b>"


def format_alert(candidate):
    lines = [
        "🖤 <b>НОВОЕ УЛУЧШЕНИЕ В TELEGRAM</b>",
        "",
        f"🎁 <b>{html.escape(candidate['collection'])} #{int(candidate['number'])}</b>",
        f"👤 Улучшил: {format_owner(candidate)}",
        f"🎨 Фон: <b>{html.escape(candidate['backdrop'])}</b>",
    ]
    if candidate.get("model"):
        lines.append(f"🧩 Модель: {html.escape(candidate['model'])}")
    if candidate.get("symbol"):
        lines.append(f"🔹 Символ: {html.escape(candidate['symbol'])}")
    lines.extend(("", "Подарок только что улучшен внутри Telegram."))
    return "\n".join(lines)


def send_candidate(candidate, config, secrets, db, send=True):
    delivered = 0
    link = gift_link(candidate)
    gift_id = f"{telegram_slug(candidate['collection'])}-{int(candidate['number'])}"
    for chat_id, user_config in active_profiles(config):
        if not feature_enabled(user_config, "dark_upgrades"):
            continue
        if not item_allowed({
            "name": candidate.get("collection"),
            "model": candidate.get("model"),
            "backdrop": candidate.get("backdrop"),
        }, user_config):
            continue
        recipient = str(chat_id if chat_id is not None else secrets["chat_id"])
        old = db.execute(
            "SELECT 1 FROM telegram_upgrade_alerts WHERE gift_slug=? AND chat_id=?",
            (gift_id, recipient),
        ).fetchone()
        if old:
            continue
        if not send:
            print(format_alert(candidate) + "\n" + link + "\n", flush=True)
            continue
        message_id = telegram(
            dict(secrets, chat_id=recipient),
            format_alert(candidate),
            preview_url=link,
            button_url=link,
            button_text="Открыть подарок",
        )
        db.execute(
            "INSERT OR REPLACE INTO telegram_upgrade_alerts VALUES (?, ?, ?, ?)",
            (gift_id, recipient, message_id, time.time()),
        )
        db.commit()
        delivered += 1
    return delivered


def run_cycle(config, secrets, db, send=True):
    ensure_tables(db)
    rows = db.execute(
        "SELECT collection, last_number FROM telegram_upgrade_heads ORDER BY collection"
    ).fetchall()
    if not rows:
        sync_catalog(db)
        rows = db.execute(
            "SELECT collection, last_number FROM telegram_upgrade_heads ORDER BY collection"
        ).fetchall()
    workers = max(1, int(config.get("dark_upgrade_workers", 12)))
    max_batch = max(1, int(config.get("dark_upgrade_max_batch", 250)))
    delivered = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(scan_collection, name, number, max_batch)
            for name, number in rows
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                name, current, candidates = future.result()
            except RuntimeError as exc:
                print(f"Проверка улучшений: {exc}", file=sys.stderr, flush=True)
                continue
            db.execute(
                "UPDATE telegram_upgrade_heads SET last_number=?, checked=? WHERE collection=?",
                (current, time.time(), name),
            )
            db.commit()
            for candidate in candidates:
                try:
                    delivered += send_candidate(candidate, config, secrets, db, send)
                except RuntimeError as exc:
                    print(f"Уведомление об улучшении: {exc}", file=sys.stderr, flush=True)
    print(
        f"Проверено {len(rows)} коллекций Telegram; отправлено {delivered} уведомлений.",
        flush=True,
    )
    return delivered


def run(config, secrets, db, *, send=True, duration=None):
    sync_catalog(db)
    deadline = None if duration is None else time.monotonic() + max(1, int(duration))
    while deadline is None or time.monotonic() < deadline:
        started = time.monotonic()
        run_cycle(config, secrets, db, send)
        if deadline is None:
            return
        interval = max(10, int(config.get("dark_upgrade_poll_seconds", 60)))
        wait = min(interval - (time.monotonic() - started), deadline - time.monotonic())
        if wait > 0:
            time.sleep(wait)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--duration", type=int, default=3300)
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text())
    secrets = json.loads((ROOT / "secrets.json").read_text())
    with sqlite3.connect(ROOT / "settings.sqlite3", timeout=5) as settings_db:
        settings_db.execute("PRAGMA journal_mode=WAL")
        settings_db.execute("PRAGMA busy_timeout=5000")
        activate_settings(settings_db, secrets["chat_id"])
        with sqlite3.connect(ROOT / "state.sqlite3", timeout=30) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=30000")
            run(
                config,
                secrets,
                db,
                send=args.send,
                duration=None if args.once else args.duration,
            )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"Не удалось запустить монитор улучшений: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Монитор улучшений остановлен.")
