#!/usr/bin/env python3
"""Бэктест тренд-фильтра: индекс выше SMA(N) — фонд на индекс (TMOS), ниже — фонд ликвидности (LQDT).

  python backtest.py [--fund TMOS|SBMX] [--refresh] [--capital 1000000]

Сигнал — закрытие IMOEX против SMA(N) с полосой ±b (гистерезис); сделка — на открытии следующего торгового дня.
Издержки на каждую сторону: комиссия 0,05% и проскальзывание 0,1%. Налог: ИИС — нет; брокерский счёт — 13% раз в год
с чистой прибыли, перенос убытков, ЛДВ для лотов дольше 3 лет. Walk-forward: сетка N×b подбирается на одной половине
истории, проверяется на другой. Свечи кэшируются в ~/.tinvest/data/backtest/ (старше 4 дней — докачиваются), отчёт — ~/.tinvest/reports/backtest_trend.md.
Ничего не покупает и не продаёт.
"""
import argparse
import bisect
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (add_years, data_dir, fmt_money, fmt_pct, load_env, md_table, parse_date,  # noqa: E402
                    read_json, reports_dir, today, write_json)
from finmath import xirr  # noqa: E402

COST = 0.0005  # комиссия брокера, доля от суммы сделки
SLIP = 0.001  # проскальзывание: покупаем дороже open, продаём дешевле
TAX = 0.13
GRID_N = (50, 100, 150, 200, 250)
GRID_BAND = (0.0, 0.01, 0.02, 0.03)
BASE_N, BASE_BAND = 200, 0.0  # правило из плана, без подгонки
HISTORY_FROM = date(2018, 1, 1)
# ключ -> (тикер, площадка); TMOS@ и LQDT на SPBRU доступны для торговли через API
SERIES = {"IMOEX": ("IMOEX", "TQBR"), "TMOS": ("TMOS@", "SPBRU"), "SBMX": ("SBMX", "TQBR"), "LQDT": ("LQDT", "SPBRU")}


# ---------- сигнал ----------

def trend_states(closes: Sequence[float], n: int, band: float) -> List[Optional[bool]]:
    """Состояние после закрытия каждого дня: True — в индексе, False — в ликвидности, None — SMA ещё не готова.
    Вход — close выше SMA·(1+band), выход — close ниже SMA·(1−band), между ними состояние не меняется."""
    out: List[Optional[bool]] = []
    state: Optional[bool] = None
    acc = 0.0
    for i, c in enumerate(closes):
        acc += c
        if i >= n:
            acc -= closes[i - n]
        if i < n - 1:
            out.append(None)
            continue
        sma = acc / n
        if state is None:
            state = c > sma * (1 + band)
        elif state and c < sma * (1 - band):
            state = False
        elif not state and c > sma * (1 + band):
            state = True
        out.append(state)
    return out


def align_states(index_dates: Sequence[date], states: Sequence[Optional[bool]], bar_dates: Sequence[date]) -> List[bool]:
    """Для каждой даты бара — состояние на последнем закрытии индекса строго до неё; прогрев и пропуски — вне рынка."""
    out = []
    for d in bar_dates:
        i = bisect.bisect_left(index_dates, d) - 1
        out.append(bool(states[i]) if i >= 0 and states[i] is not None else False)
    return out


# ---------- симуляция ----------

def max_drawdown(values: Sequence[float]) -> float:
    peak, mdd = float("-inf"), 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return mdd


def simulate(bars: Sequence[Dict[str, Any]], want: Sequence[bool], capital: float = 1_000_000.0,
             cost: float = COST, slip: float = SLIP, tax_rate: Optional[float] = None) -> Dict[str, Any]:
    """bars: [{date, risk_open, risk_close, safe_open, safe_close}], want[i] — где быть на открытии бара i (True — risk).
    Весь капитал в одном активе. Налог (tax_rate=None — ИИС): прибыль года по FIFO с комиссиями, перенос убытков,
    лоты дольше 3 лет с прибылью освобождены (ЛДВ); платится в первый торговый день следующего года продажей части
    позиции, за последний год — после ликвидации. В конце всё продаётся по close последнего бара."""
    st = {"cash": capital, "held": None, "costs": 0.0, "taxes": 0.0, "carry": 0.0}
    lots: List[List[Any]] = []  # [дата покупки, кол-во, цена с комиссией]
    gains: Dict[int, float] = {}

    def buy(asset: str, px: float, d: date) -> None:
        qty = st["cash"] / (px * (1 + slip) * (1 + cost))
        st["costs"] += st["cash"] - qty * px
        lots.append([d, qty, st["cash"] / qty])
        st["cash"], st["held"] = 0.0, asset

    def sell(qty: float, px: float, d: date) -> None:
        net = qty * px * (1 - slip) * (1 - cost)
        st["costs"] += qty * px - net
        left = qty
        while left > 1e-12 and lots:
            lot = lots[0]
            part = min(left, lot[1])
            gain = net * part / qty - part * lot[2]
            if tax_rate and not (gain > 0 and d >= add_years(lot[0], 3)):
                gains[d.year] = gains.get(d.year, 0.0) + gain
            lot[1] -= part
            left -= part
            if lot[1] <= 1e-12:
                lots.pop(0)
        st["cash"] += net
        if not lots:
            st["held"] = None

    def settle(year: int) -> float:
        base = gains.pop(year, 0.0) - st["carry"]
        if base <= 0:
            st["carry"] = -base
            return 0.0
        st["carry"] = 0.0
        return tax_rate * base

    def holding_qty() -> float:
        return sum(lot[1] for lot in lots)

    switches = 0
    equity, in_market = [], []
    prev_year = None
    for b, w in zip(bars, want):
        d = b["date"]
        if tax_rate and prev_year is not None and d.year != prev_year:
            due = sum(settle(y) for y in range(prev_year, d.year))
            if due > st["cash"] and st["held"]:
                px = b[st["held"] + "_open"]
                sell(min(holding_qty(), (due - st["cash"]) / (px * (1 - slip) * (1 - cost))), px, d)
            st["cash"] -= due
            st["taxes"] += due
        prev_year = d.year
        target = "risk" if w else "safe"
        if st["held"] != target:
            if st["held"]:
                sell(holding_qty(), b[st["held"] + "_open"], d)
                switches += 1
            buy(target, b[target + "_open"], d)
        equity.append((d, st["cash"] + holding_qty() * b[st["held"] + "_close"]))
        in_market.append(st["held"] == "risk")
    last = bars[-1]
    sell(holding_qty(), last[st["held"] + "_close"], last["date"])
    if tax_rate:
        due = settle(last["date"].year)
        st["cash"] -= due
        st["taxes"] += due
    return {"final": st["cash"], "equity": equity, "in_market": in_market, "switches": switches,
            "costs": st["costs"], "taxes": st["taxes"], "capital": capital,
            "start": bars[0]["date"], "end": last["date"]}


def summarize(r: Dict[str, Any]) -> Dict[str, Any]:
    vals = [v for _, v in r["equity"]]
    rate = xirr([(r["start"], -r["capital"]), (r["end"], r["final"])])
    return {"final": r["final"], "xirr": rate, "mdd": max_drawdown(vals), "switches": r["switches"],
            "in_market": sum(r["in_market"]) / len(r["in_market"]), "costs": r["costs"], "taxes": r["taxes"]}


def yearly(r: Dict[str, Any]) -> Dict[int, float]:
    """Доходность по календарным годам по оценке на закрытие (без ликвидации)."""
    out, base, cur_year, last_v = {}, r["capital"], None, None
    for d, v in r["equity"]:
        if cur_year is not None and d.year != cur_year:
            out[cur_year] = last_v / base - 1
            base = last_v
        cur_year, last_v = d.year, v
    out[cur_year] = last_v / base - 1
    return out


# ---------- данные ----------

def load_series(key: str, refresh: bool) -> Dict[str, Any]:
    path = data_dir() / "backtest" / f"{key}.json"
    cached = read_json(path)
    fresh = cached and cached.get("candles") and \
        parse_date(cached["candles"][-1]["time"]) >= today() - timedelta(days=4)
    if fresh and not refresh:
        return cached
    from tinvest_api import client_from_env
    api = client_from_env()
    ticker, cls = SERIES[key]
    inst = next((i for i in api.find_instrument(ticker.rstrip("@"))
                 if i.get("ticker") == ticker and i.get("classCode") == cls), None)
    if not inst:
        raise SystemExit(f"Инструмент {ticker} ({cls}) не найден")
    candles = api.get_day_candles(inst["uid"], HISTORY_FROM, today())
    data = {"instrument": inst, "candles": candles}
    write_json(path, data)
    return data


def daily(series: Dict[str, Any]) -> Dict[date, Dict[str, float]]:
    return {parse_date(c["time"]): c for c in series["candles"] if c["open"] > 0 and c["close"] > 0}


def make_bars(risk: Dict[date, Dict[str, float]], safe: Dict[date, Dict[str, float]]) -> List[Dict[str, Any]]:
    return [{"date": d, "risk_open": risk[d]["open"], "risk_close": risk[d]["close"],
             "safe_open": safe[d]["open"], "safe_close": safe[d]["close"]} for d in sorted(set(risk) & set(safe))]


# ---------- отчёт ----------

def run(bars, idx_dates, idx_closes, n, band, capital, tax_rate=None):
    want = align_states(idx_dates, trend_states(idx_closes, n, band), [b["date"] for b in bars])
    return simulate(bars, want, capital, tax_rate=tax_rate)


def hold(bars, risk: bool, capital, tax_rate=None):
    return simulate(bars, [risk] * len(bars), capital, tax_rate=tax_rate)


def row(name: str, s: Dict[str, Any]) -> List[str]:
    return [name, fmt_money(s["final"]), fmt_pct(s["xirr"]), fmt_pct(s["mdd"]), str(s["switches"]),
            fmt_pct(s["in_market"], 0), fmt_money(s["costs"]), fmt_money(s["taxes"])]


HEAD = ["Вариант", "Итог", "Доходность, год.", "Макс. просадка", "Переключений", "В индексе", "Издержки", "Налог"]


def compare(bars, idx_dates, idx_closes, fund: str, n: int, band: float, capital: float, tax_rate=None) -> str:
    rows = [row(f"Тренд-фильтр SMA{n}" + (f" ±{band:.0%}" if band else ""),
                summarize(run(bars, idx_dates, idx_closes, n, band, capital, tax_rate))),
            row(f"Купил и держишь {fund}", summarize(hold(bars, True, capital, tax_rate))),
            row("Купил и держишь LQDT", summarize(hold(bars, False, capital, tax_rate)))]
    return md_table(HEAD, rows)


def fit(bars, idx_dates, idx_closes, capital):
    best = None
    for n in GRID_N:
        for band in GRID_BAND:
            s = summarize(run(bars, idx_dates, idx_closes, n, band, capital))
            if best is None or (s["xirr"] or -1) > (best[2]["xirr"] or -1):
                best = (n, band, s)
    return best


def walk_forward(bars, idx_dates, idx_closes, fund, capital) -> str:
    mid = len(bars) // 2
    halves = [("первой", bars[:mid], "второй", bars[mid:]), ("второй", bars[mid:], "первой", bars[:mid])]
    rows = []
    for fit_name, fit_bars, test_name, test_bars in halves:
        n, band, s_in = fit(fit_bars, idx_dates, idx_closes, capital)
        s_out = summarize(run(test_bars, idx_dates, idx_closes, n, band, capital))
        s_base = summarize(run(test_bars, idx_dates, idx_closes, BASE_N, BASE_BAND, capital))
        s_fund = summarize(hold(test_bars, True, capital))
        s_safe = summarize(hold(test_bars, False, capital))
        period = f"{test_bars[0]['date']} — {test_bars[-1]['date']}"
        rows.append([f"подбор на {fit_name}, проверка на {test_name} ({period})", f"SMA{n} ±{band:.0%}",
                     fmt_pct(s_in["xirr"]), fmt_pct(s_out["xirr"]), fmt_pct(s_out["mdd"]),
                     fmt_pct(s_base["xirr"]), fmt_pct(s_fund["xirr"]), fmt_pct(s_fund["mdd"]), fmt_pct(s_safe["xirr"])])
    return md_table(["Проход", "Подобрано", "Доходн. на подборе", "Доходн. на проверке", "Просадка на проверке",
                     f"SMA{BASE_N} на проверке", f"{fund} на проверке", f"Просадка {fund}", "LQDT на проверке"], rows)


def sensitivity(bars, idx_dates, idx_closes, capital) -> str:
    rows = []
    for n in GRID_N:
        cells = [f"SMA{n}"]
        for band in GRID_BAND:
            s = summarize(run(bars, idx_dates, idx_closes, n, band, capital))
            cells.append(f"{fmt_pct(s['xirr'])} / {fmt_pct(s['mdd'], 0)} / {s['switches']}")
        rows.append(cells)
    return md_table(["N \\ полоса"] + [f"±{b:.0%}" for b in GRID_BAND], rows)


def switches_log(bars, idx_dates, idx_closes, n, band) -> str:
    want = align_states(idx_dates, trend_states(idx_closes, n, band), [b["date"] for b in bars])
    rows, prev = [], None
    for b, w in zip(bars, want):
        if w != prev:
            px = b["risk_open"] if w else b["safe_open"]
            rows.append([str(b["date"]), "→ индекс" if w else "→ LQDT", f"{px:g}"])
        prev = w
    return md_table(["Дата (open)", "Куда", "Цена покупки"], rows)


def build(fund: str, capital: float, refresh: bool) -> str:
    idx = daily(load_series("IMOEX", refresh))
    idx_dates = sorted(idx)
    idx_closes = [idx[d]["close"] for d in idx_dates]
    safe = daily(load_series("LQDT", refresh))
    bars = make_bars(daily(load_series(fund, refresh)), safe)
    years = (bars[-1]["date"] - bars[0]["date"]).days / 365.25

    r_iis = run(bars, idx_dates, idx_closes, BASE_N, BASE_BAND, capital)
    yr = {"Тренд-фильтр": yearly(r_iis), fund: yearly(hold(bars, True, capital)), "LQDT": yearly(hold(bars, False, capital))}
    year_rows = [[str(y)] + [fmt_pct(yr[k].get(y)) for k in yr] for y in sorted(yr["LQDT"])]

    out = [f"# Бэктест: тренд-фильтр IMOEX/SMA → {fund} / LQDT",
           "",
           f"Период: {bars[0]['date']} — {bars[-1]['date']} ({years:.1f} лет, {len(bars)} торговых дней), "
           f"капитал {fmt_money(capital)}. Сформировано {today()}.",
           "",
           "## Правила и допущения",
           f"- Сигнал: закрытие IMOEX против SMA(N); вход выше SMA·(1+b), выход ниже SMA·(1−b). "
           f"Базовое правило из плана — SMA{BASE_N} без полосы.",
           f"- Сделка — на открытии следующего торгового дня после сигнала; весь капитал в {fund} или в LQDT.",
           f"- Издержки на каждую сторону: комиссия {COST:.2%}, проскальзывание {SLIP:.1%}. Дробные паи.",
           f"- ИИС — без налога. Брокерский счёт — {TAX:.0%} раз в год с чистой прибыли (FIFO, с переносом убытков "
           "через декларацию), ЛДВ для лотов дольше 3 лет; лимит ЛДВ и ставка 15% не учитываются.",
           "- В конце периода всё продаётся по закрытию последнего дня. Доходность — годовая (XIRR одного взноса).",
           "- Просадка — по оценке на закрытие дня. Паузы торгов (февраль–март 2022) — как в данных: сделок нет.",
           "",
           "## Весь период, ИИС (без налога)",
           compare(bars, idx_dates, idx_closes, fund, BASE_N, BASE_BAND, capital),
           "",
           f"## Весь период, брокерский счёт ({TAX:.0%}, ЛДВ)",
           compare(bars, idx_dates, idx_closes, fund, BASE_N, BASE_BAND, capital, TAX),
           "",
           "## Walk-forward (ИИС)",
           f"Сетка: N ∈ {list(GRID_N)}, полоса ∈ {[f'{b:.0%}' for b in GRID_BAND]}; критерий подбора — доходность после издержек.",
           "",
           walk_forward(bars, idx_dates, idx_closes, fund, capital),
           "",
           "## Чувствительность к параметрам, весь период (ИИС)",
           "Ячейка: доходность год. / макс. просадка / переключений.",
           "",
           sensitivity(bars, idx_dates, idx_closes, capital),
           "",
           f"## По годам, ИИС (SMA{BASE_N}; первый и последний год неполные)",
           md_table(["Год"] + list(yr), year_rows),
           "",
           f"## Переключения SMA{BASE_N}",
           switches_log(bars, idx_dates, idx_closes, BASE_N, BASE_BAND)]

    if fund != "SBMX":
        sb = make_bars(daily(load_series("SBMX", refresh)), safe)
        out += ["", f"## Проверка на другом фонде: SBMX ({sb[0]['date']} — {sb[-1]['date']}, ИИС)",
                compare(sb, idx_dates, idx_closes, "SBMX", BASE_N, BASE_BAND, capital)]
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fund", default="TMOS", choices=["TMOS", "SBMX"])
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    ap.add_argument("--refresh", action="store_true", help="заново скачать свечи")
    args = ap.parse_args()
    load_env()
    text = build(args.fund, args.capital, args.refresh)
    path = reports_dir() / "backtest_trend.md"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"Сохранено: {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
