"""Beautiful-number and monochrome purchase signals."""

import re
import time

try:
    from curl_cffi import requests
except ImportError:
    raise SystemExit("Не установлен curl_cffi. Запусти start_watch.command.")


MONOCHROME_URL = "https://app.clue.tg/api/mono/resolve"


def gift_slug(item):
    name = re.sub(r"[^A-Za-z0-9]", "", str(item.get("name", ""))).lower()
    try:
        number = int(item.get("gift_num"))
    except (TypeError, ValueError):
        return ""
    return f"{name}-{number}" if name and number > 0 else ""


def beautiful_number(item, config):
    """Return (number, distinct digit count) when the configured rule matches."""
    if not bool(config.get("beautiful_number_enabled", False)):
        return None
    try:
        number = int(item.get("gift_num"))
        maximum = int(config.get("beautiful_number_max_distinct_digits", 3))
    except (TypeError, ValueError):
        return None
    if number <= 0 or not 1 <= maximum <= 10:
        return None
    distinct = len(set(str(number)))
    return (number, distinct) if distinct <= maximum else None


def fetch_monochrome_score(slug):
    """Fetch the public Clue perceptual model/backdrop match score."""
    response = requests.get(
        MONOCHROME_URL,
        params={"url": slug},
        headers={"accept": "application/json"},
        impersonate="chrome",
        timeout=12,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Clue вернул HTTP {response.status_code}")
    payload = response.json()
    score = float(payload.get("score"))
    if not 0 <= score <= 100:
        raise ValueError("Clue вернул неверную оценку монохромности")
    return score


def cached_monochrome_score(item, db, config, fetch=fetch_monochrome_score):
    slug = gift_slug(item)
    if not slug:
        return None
    db.execute(
        "CREATE TABLE IF NOT EXISTS gift_special_traits ("
        "slug TEXT PRIMARY KEY, monochrome_score REAL, checked REAL NOT NULL)"
    )
    db.commit()
    row = db.execute(
        "SELECT monochrome_score, checked FROM gift_special_traits WHERE slug=?",
        (slug,),
    ).fetchone()
    now = time.time()
    if row:
        max_age = float(config.get(
            "monochrome_cache_seconds" if row[0] is not None else "monochrome_retry_seconds",
            2592000 if row[0] is not None else 3600,
        ))
        if now - float(row[1]) <= max_age:
            return float(row[0]) if row[0] is not None else None
    try:
        score = fetch(slug)
    except (OSError, RuntimeError, TypeError, ValueError):
        score = None
    db.execute(
        "INSERT OR REPLACE INTO gift_special_traits VALUES (?, ?, ?)",
        (slug, score, now),
    )
    db.commit()
    return score


def apply_special_bases(item, result, config, db, fetch=fetch_monochrome_score):
    """Attach enabled aesthetic reasons to an already profitable floor signal."""
    if result is None:
        return None
    labels = []
    enriched = dict(result)
    requires_special = bool(enriched.pop("_requires_special_basis", False))
    number_match = beautiful_number(item, config)
    if number_match:
        number, distinct = number_match
        enriched["serial_distinct_digits"] = distinct
        labels.append(f"красивый номер #{number} ({distinct} разных цифр)")

    if bool(config.get("monochrome_enabled", False)):
        score = item.get("monochrome_score")
        if score is None:
            score = cached_monochrome_score(item, db, config, fetch=fetch)
        try:
            score = float(score)
            minimum = float(config.get("monochrome_min_score", 80))
        except (TypeError, ValueError):
            score = None
        if score is not None:
            enriched["monochrome_score"] = score
            if score >= minimum:
                labels.append(f"монохром {score:g}%")

    if labels:
        floor_basis = str(result.get("basis") or "стабильный floor")
        enriched["basis"] = f"{', '.join(labels)}; floor: {floor_basis}"
        enriched["special_basis"] = labels
    elif requires_special:
        return None
    return enriched
