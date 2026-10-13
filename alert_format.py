"""Shared Telegram alert rendering for marketplace monitors."""

from html import escape


RISK_LABELS = {
    "сильный": ("🟢", "НИЗКИЙ РИСК"),
    "нормальный": ("🟡", "СРЕДНИЙ РИСК"),
    "рискованный": ("🔴", "ВЫСОКИЙ РИСК"),
}


def _ton(value):
    return f"{value:.3f} TON"


def format_market_alert(item, result, *, market, listing_link, gift_link):
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
            continue
        difference = floor - price
        percent = difference / price * 100 if price > 0 else 0
        floor_lines.append(
            f"{escape(str(entry.get('market') or '?'))} — {_ton(floor)} "
            f"({difference:+.3f} TON / {percent:+.1f}%)"
        )
    if not floor_lines:
        floor_lines.append("Нет подтверждённого устойчивого floor")

    recent = []
    for sale in list(result.get("recent_sales") or [])[:3]:
        source = escape(str(sale.get("source") or "рынок").capitalize())
        recent.append(f"{source} — {_ton(sale['price'])}")
    history = "; ".join(recent) if recent else "пока нет подтверждённых продаж"

    basis = escape(str(result.get("basis") or "вся коллекция"))
    model = escape(str(item.get("model") or "не указана"))
    backdrop = escape(str(item.get("backdrop") or "не указан"))
    collection_sales = int(result.get("collection_sales_24h", 0))
    basis_sales = int(result.get("basis_sales_24h", 0))
    liquidity = escape(str(result.get("liquidity") or "низкая"))

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
        f"<b>Ликвидность:</b> {liquidity} ({collection_sales} продаж за 24 ч)",
        f"<b>Продажи по основанию за 24 ч:</b> {basis_sales}",
        f"<b>История продаж:</b> {history}",
        "",
        f"Расчётная прибыль: {result['profit_rub']:.0f} ₽ "
        f"при быстрой продаже за {_ton(result['resale'])}",
    ]
    if gift_link:
        lines.extend(["", f'<a href="{escape(gift_link, quote=True)}">Подарок в Telegram</a>'])
    lines.append(
        f'<a href="{escape(listing_link, quote=True)}">Открыть оффер на {escape(market)}</a>'
    )
    return "\n".join(lines)
