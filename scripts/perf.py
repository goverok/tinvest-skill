"""Честная доходность счёта: XIRR по внешним потокам и сравнение «те же взносы в бенчмарк»."""
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from common import fmt_money, fmt_pct, md_table, today
from finmath import money_weighted_summary, xirr
from model import Store, price_on


def benchmark_what_if(store: Store, flows: List[Tuple[date, float]], ticker: str) -> Optional[Dict[str, Any]]:
    """Те же потоки (взносы/выводы) в бенчмарк: сколько паёв накопилось и сколько они стоят сегодня."""
    b = store.benchmark(ticker)
    if not b or not b.get("candles"):
        return None
    candles = b["candles"]
    first = min((c["time"][:10] for c in candles if c.get("time")), default=None)
    first_d = date.fromisoformat(first) if first else None
    units = 0.0
    pending = 0.0
    late_start = False
    for d, a in sorted(flows):
        if first_d and d < first_d:
            pending += -a  # до появления фонда деньги «ждут» в кэше без дохода
            late_start = True
            continue
        if pending:
            p0 = price_on(candles, d, after=True)
            if p0:
                units += pending / p0
                pending = 0.0
        p = price_on(candles, d, after=True) or price_on(candles, d, after=False)
        if not p:
            continue
        units += (-a) / p  # взнос (a<0) покупает паи, вывод (a>0) продаёт
    last_p = candles[-1]["close"]
    if pending and last_p:
        units += pending / last_p
    terminal = units * last_p
    r = xirr(list(flows) + [(today(), terminal)])
    return {"ticker": ticker, "name": (b.get("instrument") or {}).get("name", ticker), "terminal": terminal,
            "xirr": r, "late_start": late_start, "first_candle": first}


def build(store: Store, acc_id: str) -> Tuple[str, Dict[str, Any]]:
    flows, counts, notes = store.external_flows(acc_id)
    totals = store.totals(acc_id)
    value = totals["portfolio"]
    acc = store.account(acc_id)
    out: List[str] = [f"### Доходность · {acc.get('name') or acc_id} ({'ИИС' if store.is_iis(acc_id) else 'брокерский'})"]
    if not flows:
        out.append("Внешних пополнений/выводов не найдено — XIRR не посчитать. Проверь, что история операций выгрузилась (fetch.py).")
        return "\n".join(out), {}
    s = money_weighted_summary(flows, value, today())
    years = max((today() - s["first_flow"]).days / 365.0, 0.01)
    out.append(
        f"Внесено {fmt_money(s['contributions'])}, выведено {fmt_money(s['withdrawals'])}, сейчас стоит {fmt_money(value)}: "
        f"результат **{fmt_money(s['net_result'])}**, XIRR **{fmt_pct(s['xirr'])} годовых** за {years:.1f} г. (с {s['first_flow']})."
    )
    rows = [["Твой счёт", fmt_money(value), fmt_money(s["net_result"]), fmt_pct(s["xirr"])]]
    bench_results = []
    for t in store.benchmarks():
        w = benchmark_what_if(store, flows, t)
        if not w:
            continue
        bench_results.append(w)
        rows.append([f"Те же взносы в {w['ticker']} ({w['name']})" + (" *" if w["late_start"] else ""),
                     fmt_money(w["terminal"]), fmt_money(w["terminal"] + s["withdrawals"] - s["contributions"]), fmt_pct(w["xirr"])])
    out.append("")
    out.append(md_table(["Вариант", "Стоимость сегодня", "Результат", "XIRR"], rows))
    if any(w["late_start"] for w in bench_results):
        out.append("\n\\* часть взносов была раньше первой свечи фонда — до неё деньги условно лежали без дохода.")
    out.append("\nКак читать: XIRR учитывает даты и размеры всех взносов, поэтому его можно сравнивать с депозитом. "
               "Строки бенчмарков — что было бы при тех же деньгах и датах в фонде денежного рынка (≈ ставка ЦБ) и в фонде на индекс.")
    # ИИС: вычет на взнос (тип А / ИИС-3) — 13% от взносов до 400 тыс. ₽ в год, приходит в следующем году
    ded_flows: List[Tuple[date, float]] = []
    ded_expected = 0.0
    if store.is_iis(acc_id):
        by_year: Dict[int, float] = {}
        for d, a in flows:
            if a < 0:
                by_year[d.year] = by_year.get(d.year, 0.0) + (-a)
        for y, c in sorted(by_year.items()):
            ded = 0.13 * min(400_000.0, c)
            when = date(y + 1, 6, 1)
            if when <= today():
                ded_flows.append((when, ded))
            else:
                ded_expected += ded
        if ded_flows or ded_expected:
            s2 = money_weighted_summary(list(flows) + ded_flows, value, today())
            got = sum(a for _, a in ded_flows)
            out.append(f"\nИИС: вычет 13% на взносы до 400 тыс. ₽ в год — получено {fmt_money(got)} за "
                       f"{', '.join(str(d.year - 1) for d, _ in ded_flows) or '—'}; с учётом вычетов XIRR **{fmt_pct(s2['xirr'])}**"
                       + (f", за {today().year} ожидается ещё ≈ {fmt_money(ded_expected)}" if ded_expected else "")
                       + ". Расчёт исходит из того, что вычет оформлен; если нет — это упущенные деньги.")
    if notes:
        out.append("\nЗамечания по данным:\n" + "\n".join(f"- {n}" for n in notes[:10]))
    ext = {k: v for k, v in counts.items() if any(x in k for x in ("INPUT", "OUTPUT", "INP_", "OUT_", "BUY_CARD"))}
    if ext:
        out.append("\nОперации, учтённые как внешние потоки: " + ", ".join(f"{k.replace('OPERATION_TYPE_', '')}×{v}" for k, v in sorted(ext.items())))
    return "\n".join(out), {"summary": s, "benchmarks": bench_results, "flows": flows}
