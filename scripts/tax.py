"""Налоговый разбор: FIFO-лоты, ЛДВ, нереализованный и реализованный результат, оценка НДФЛ.

Оценка, не расчёт брокера: не учитывает валютную переоценку по иностранным бумагам,
корпоративные действия и сальдирование с другими базами (фьючерсы, купоны). Облигации — по цене покупки/продажи с НКД.
"""
from datetime import date
from typing import Any, Dict, List, Tuple

from common import add_years, fmt_money, md_table, today
from finmath import fifo
from model import Store

NDFL_STEP = 2_400_000.0  # порог 13% → 15% для доходов от продажи ценных бумаг (шкала 2025+)
LDV_PER_YEAR = 3_000_000.0  # льгота на долгосрочное владение: 3 млн ₽ за каждый полный год владения


def ndfl(base: float) -> float:
    if base <= 0:
        return 0.0
    return 0.13 * min(base, NDFL_STEP) + 0.15 * max(0.0, base - NDFL_STEP)


def build(store: Store, acc_id: str) -> Tuple[str, Dict[str, Any]]:
    acc = store.account(acc_id)
    iis = store.is_iis(acc_id)
    pos = {p["uid"]: p for p in store.valued_positions(acc_id)}
    trades = store.trades(acc_id)
    t0 = today()
    out: List[str] = [f"## Налоги · {acc.get('name') or acc_id} ({'ИИС' if iis else 'брокерский'})"]
    if iis:
        out.append("На ИИС НДФЛ по сделкам считается брокером только при закрытии счёта, ЛДВ внутри ИИС не применяется "
                   "(у ИИС-3 — своё освобождение при соблюдении срока). Ниже — справочно: лоты, результат и что было бы на обычном счёте.")

    open_lots_rows: List[List[Any]] = []
    unrealized_total = 0.0
    loss_candidates: List[Tuple[str, float, date]] = []
    ldv_soon: List[Tuple[str, date, float]] = []
    mismatches: List[str] = []
    realized_year: List[Dict[str, Any]] = []
    realized_prev: Dict[int, float] = {}

    for uid, tr in trades.items():
        lots, realized = fifo(tr)
        p = pos.get(uid)
        ticker = store.ticker(uid)
        price_now = p["price"] + (p["nkd"] if p and p["type"] == "bond" else 0.0) if p else None
        rate = (store.rate(p["price_currency"]) if p else 1.0) or 1.0
        held = sum(l["left"] for l in lots)
        if p and abs(held - p["qty"]) > 1e-6:
            mismatches.append(f"{ticker}: по операциям {held:g} шт., в портфеле {p['qty']:g} — сплит/перевод/неполная история")
        for l in lots:
            if price_now is None:
                continue
            value = l["left"] * price_now * rate
            pnl = value - l["left"] * l["unit_cost"]
            unrealized_total += pnl
            ldv_date = add_years(l["date"], 3)
            days_to_ldv = (ldv_date - t0).days
            foreign = (p.get("country") or "RU") != "RU"
            if iis:
                status = f"держишь {(t0 - l['date']).days} дн."
            else:
                status = "ЛДВ ✓" if days_to_ldv <= 0 else f"ЛДВ через {days_to_ldv} дн."
                if foreign:
                    status += " (иностр. эмитент — проверь)"
            if days_to_ldv > 0 and days_to_ldv <= 90 and pnl > 0:
                ldv_soon.append((ticker, ldv_date, pnl))
            if pnl < 0:
                loss_candidates.append((ticker, pnl, l["date"]))
            open_lots_rows.append([ticker, l["date"].isoformat(), f"{l['left']:g}", fmt_money(l["left"] * l["unit_cost"]),
                                   fmt_money(value), fmt_money(pnl), status + (f"; {l['note']}" if l.get("note") else "")])
        for r in realized:
            y = r["date"].year
            if y == t0.year:
                realized_year.append(dict(r, ticker=ticker))
            else:
                realized_prev[y] = realized_prev.get(y, 0.0) + r["pnl"]

    if open_lots_rows:
        open_lots_rows.sort(key=lambda r: (r[0], r[1]))
        out.append("\n### Открытые лоты (FIFO)\n" + md_table(["Тикер", "Куплено", "Шт.", "Стоимость", "Оценка", "P/L", "Статус"], open_lots_rows[:60]))
        if len(open_lots_rows) > 60:
            out.append(f"… и ещё {len(open_lots_rows) - 60} лотов (полный список — в reports/tax_lots.md).")
        out.append(f"\nНереализованный результат по лотам: **{fmt_money(unrealized_total)}**.")

    base = sum(r["pnl"] for r in realized_year)
    if realized_year:
        by_t: Dict[str, float] = {}
        for r in realized_year:
            by_t[r["ticker"]] = by_t.get(r["ticker"], 0.0) + r["pnl"]
        rows = [[k, fmt_money(v)] for k, v in sorted(by_t.items(), key=lambda x: -abs(x[1]))]
        out.append(f"\n### Реализованный результат {t0.year}\n" + md_table(["Тикер", "P/L"], rows))
        out.append(f"\nБаза по сделкам {t0.year}: **{fmt_money(base)}** → " +
                   (f"НДФЛ к удержанию ≈ **{fmt_money(ndfl(base))}** (13% до 2,4 млн, 15% выше; дивиденды и купоны считаются отдельно)."
                    if not iis else "на ИИС сейчас не удерживается."))
        if base < 0:
            out.append(f"Убыток {fmt_money(-base)} можно перенести на будущие годы (декларация 3-НДФЛ), если не перекроется прибылью до конца года.")
    else:
        out.append(f"\nПродаж в {t0.year} году не было — налоговой базы по сделкам нет.")
    if realized_prev:
        out.append("Прошлые годы по операциям: " + ", ".join(f"{y}: {fmt_money(v)}" for y, v in sorted(realized_prev.items())) + ".")

    if not iis:
        if loss_candidates and base > 0:
            loss_sum = sum(x[1] for x in loss_candidates)
            use = min(-loss_sum, base)
            out.append(f"\n### Сальдирование\nУбыточных лотов на {fmt_money(loss_sum)}. Фиксация до конца года уменьшает базу на {fmt_money(use)} "
                       f"и НДФЛ на ≈ {fmt_money(ndfl(base) - ndfl(base - use))}. Откупать ту же бумагу можно сразу — правила wash sale в РФ нет; "
                       "комиссия и спред — твоя цена вопроса. Лоты с близкой ЛДВ лучше не трогать.")
            out.append(md_table(["Тикер", "Куплено", "Убыток"], [[t, d.isoformat(), fmt_money(v)] for t, v, d in sorted(loss_candidates, key=lambda x: x[1])[:15]]))
        elif loss_candidates:
            out.append(f"\nУбыточные лоты есть ({fmt_money(sum(x[1] for x in loss_candidates))}), но прибыли {t0.year} года для зачёта нет.")
        if ldv_soon:
            out.append("\n### ЛДВ в ближайшие 90 дней\nЭти прибыльные лоты станут безналоговыми — продавать до даты невыгодно:\n" +
                       md_table(["Тикер", "Дата ЛДВ", "Прибыль"], [[t, d.isoformat(), fmt_money(v)] for t, d, v in sorted(ldv_soon, key=lambda x: x[1])]))
        out.append(f"\nЛДВ: 3 года владения, лимит {LDV_PER_YEAR / 1e6:.0f} млн ₽ за каждый год владения; с 2025 года на бумаги иностранных эмитентов не распространяется.")
    if mismatches:
        out.append("\nРасхождения лотов с портфелем (учтите при чтении):\n" + "\n".join(f"- {m}" for m in mismatches[:10]))
    return "\n".join(out), {"unrealized": unrealized_total, "realized_base": base, "ndfl": ndfl(base) if not iis else 0.0,
                            "open_lots_rows": open_lots_rows, "loss_candidates": loss_candidates, "ldv_soon": ldv_soon}
