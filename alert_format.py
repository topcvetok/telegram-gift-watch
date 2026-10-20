"""Shared Telegram alert rendering for marketplace monitors."""

from html import escape
from decimal import Decimal


def _ton(value):
    return f"{value:.3f} TON"


def best_resale_market(result, config=None):
    """Pick the highest exact-model floor after configured marketplace costs."""
    config = config or {}
    candidates = []
    for entry in result.get("market_floors") or []:
        market = str(entry.get("market") or "")
        floor = entry.get("model_floor")
        if not market or floor is None or not entry.get("model_verified", True):
            continue
        if not config.get(f"{market.casefold()}_enabled", True):
            continue
        cost = Decimal(str(config.get(
            f"{market.casefold()}_round_trip_cost_fraction", 0
        ) or 0))
        cost = min(max(cost, Decimal("0")), Decimal("0.99"))
        net = floor * (Decimal("1") - cost)
        candidates.append({
            "market": market,
            "floor": floor,
            "cost_fraction": cost,
            "net": net,
            "margin": net - result["price"],
        })
    return max(candidates, key=lambda row: row["net"]) if candidates else None


def format_market_alert(item, result, *, market, config=None):
    """Render the compact evidence-first alert requested by the user."""
    price = result["price"]
    rate = result["rate"]
    floors = list(result.get("market_floors") or [])
    if not floors:
        if result.get("basis_scope") == "model":
            floors = [{"market": market, "model_floor": result["next_floor"]}]

    floor_lines = []
    for entry in floors:
        market_name = escape(str(entry.get("market") or "?"))
        floor_lines.append(f"<b>{market_name}</b>")
        floor = entry.get("model_floor")
        if floor is None:
            floor_lines.append("  • модель: нет активных лотов")
            continue
        difference = floor - price
        percent = difference / price * 100 if price > 0 else 0
        floor_lines.append(
            f"  • модель: {_ton(floor)} "
            f"({difference:+.3f} TON / {percent:+.1f}%)"
        )
    if not floor_lines:
        floor_lines.append("Нет активных лотов этой модели")

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

    if result.get("monochrome_relaxed"):
        delta = result.get("monochrome_floor_delta_fraction", 0) * 100
        maximum = result.get("monochrome_max_floor_premium_fraction", 0) * 100
        scenario_line = (
            f"Цена относительно floor модели: {delta:+.1f}% "
            f"(допуск для монохрома до +{maximum:.0f}%)"
        )
    else:
        scenario_line = (
            f"Расчётная прибыль: {result['profit_rub']:.0f} ₽ "
            f"при быстрой продаже за {_ton(result['resale'])}"
        )

    resale = best_resale_market(result, config)
    resale_lines = []
    if resale is not None:
        resale_lines = [
            "",
            "<b>Где выгоднее перепродать:</b>",
            f"{escape(resale['market'])} — floor модели {_ton(resale['floor'])}",
            f"После расходов: ≈ {_ton(resale['net'])}; "
            f"запас к покупке: {resale['margin']:+.3f} TON",
        ]

    lines = [
        f"<b>оффер на {escape(market)}</b>",
        "",
        f"💲 <b>ЦЕНА:</b> {_ton(price)} ≈ {price * rate:.0f} ₽",
        "",
        f"<b>Основание:</b> {basis}",
        f"Модель: {model}",
        f"Фон: {backdrop}",
        "",
        "<b>Floor модели по площадкам:</b>",
        *floor_lines,
        "",
        f"<b>Ликвидность:</b> {liquidity} (активность за 24 ч: {activity})",
        activity_line,
        "",
        scenario_line,
        *resale_lines,
    ]
    return "\n".join(lines)
