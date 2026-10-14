"""Shared Telegram alert rendering for marketplace monitors."""

from html import escape


RISK_LABELS = {
    "сильный": ("🟢", "НИЗКИЙ РИСК"),
    "нормальный": ("🟡", "СРЕДНИЙ РИСК"),
    "рискованный": ("🔴", "ВЫСОКИЙ РИСК"),
}


def _ton(value):
    return f"{value:.3f} TON"


def format_market_alert(item, result, *, market):
    """Render the compact evidence-first alert requested by the user."""
    icon, risk = RISK_LABELS.get(result.get("quality"), RISK_LABELS["рискованный"])
    price = result["price"]
    rate = result["rate"]
    floors = list(result.get("market_floors") or [])
    if not floors:
        floors = [{"market": market, "floor": result["next_floor"]}]

    floor_lines = []
    for entry in floors:
        floor = entry.get("floor")
        if floor is None:
            floor_lines.append(
                f"{escape(str(entry.get('market') or '?'))} — нет данных по этому основанию"
            )
            continue
        difference = floor - price
        percent = difference / price * 100 if price > 0 else 0
        note = "" if entry.get("verified", True) else " · внешний floor"
        floor_lines.append(
            f"{escape(str(entry.get('market') or '?'))} — {_ton(floor)} "
            f"({difference:+.3f} TON / {percent:+.1f}%{note})"
        )
    if not floor_lines:
        floor_lines.append("Нет подтверждённого устойчивого floor")

    basis = escape(str(result.get("basis") or "вся коллекция"))
    model = escape(str(item.get("model") or "не указана"))
    backdrop = escape(str(item.get("backdrop") or "не указан"))
    sales = int(result.get("activity_sales_24h", result.get("collection_sales_24h", 0)))
    listings = int(result.get("activity_listings_24h", 0))
    price_changes = int(result.get("activity_price_changes_24h", 0))
    other = int(result.get("activity_other_24h", 0))
    activity = int(result.get("activity_24h", sales + listings + price_changes))
    liquidity = escape(str(result.get("liquidity") or "низкая"))
    activity_line = (
        f"Сделки: {sales} · новые листинги: {listings} · изменения цены: {price_changes}"
    )
    if other:
        activity_line += f" · прочие события: {other}"

    lines = [
        f"<b>{icon} {risk}: оффер на {escape(market)}</b>",
        "",
        f"💲 <b>ЦЕНА:</b> {_ton(price)} ≈ {price * rate:.0f} ₽",
        "",
        f"<b>Основание:</b> {basis}",
        f"Модель: {model}",
        f"Фон: {backdrop}",
        "",
        "<b>Устойчивые floor:</b>",
        *floor_lines,
        "",
        f"<b>Ликвидность:</b> {liquidity} (активность за 24 ч: {activity})",
        activity_line,
        "",
        f"Расчётная прибыль: {result['profit_rub']:.0f} ₽ "
        f"при быстрой продаже за {_ton(result['resale'])}",
    ]
    return "\n".join(lines)
