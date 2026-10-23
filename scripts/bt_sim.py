"""Портфельный симулятор для исследования стратегий (лонг, без плеча, остаток в LQDT).

Стратегия вызывается на закрытии дня с View — срезом истории до этого дня; заглянуть вперёд нечем.
Заявки исполняются на open следующего торгового дня (fill="open") или по его close (fill="next_close"),
целыми лотами, с комиссией и проскальзыванием на каждую сторону. Дивиденды приходят в кэш на дату выплаты
за позиции, державшиеся на закрытии перед ex-датой, за вычетом 13 % удержания у источника в обоих режимах. Налог (tax_rate=None — ИИС): 13% с прибыли года по FIFO,
перенос убытка, ЛДВ > 3 лет; уплата в первый торговый день следующего года, при нехватке кэша — продажей
пропорционально по всем позициям. В конце окна — ликвидация по close.

Результат simulate: equity (оценка на закрытии каждого дня; ликвидация и итоговый налог в неё не входят — только
в final), trades, final, costs, taxes, dividends, capital, start, end, in_stocks и
cash_min — минимальный кэш, наблюдавшийся после налогового шага и после исполнения заявок (должен быть >= 0).
"""
import bisect
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import max_drawdown  # noqa: E402
from bt_data import Series  # noqa: E402
from common import add_years  # noqa: E402
from finmath import moments, sharpe, xirr  # noqa: E402

COST, SLIP, TAX = 0.0005, 0.001, 0.13
REBALANCE_THRESHOLD = 0.02  # доля капитала: меньшие отклонения от цели не торгуются
ACTIVE_GAP_DAYS = 7  # бумага без бара дольше этого срока считается не торгующейся
DIVIDEND_TAX = 0.13  # налог на дивиденды удерживается у источника на любом счёте (ИИС не освобождает)
Result = Dict[str, Any]


class View:
    """Срез всех серий до даты today включительно."""

    def __init__(self, series: Dict[str, Series], today: date, universe: Sequence[str], calendar: str = "IMOEX"):
        self._s = series
        self.today = today
        self.universe = list(universe)
        self.calendar = calendar
        self._end: Dict[str, int] = {}

    def _e(self, t: str) -> int:
        e = self._end.get(t)
        if e is None:
            e = bisect.bisect_right(self._s[t]["dates"], self.today)
            self._end[t] = e
        return e

    def n(self, t: str) -> int:
        return self._e(t) if t in self._s else 0

    def has(self, t: str, k: int) -> bool:
        return self.n(t) >= k

    def active(self, t: str, max_gap_days: int = ACTIVE_GAP_DAYS) -> bool:
        """Бумага торгуется: есть хотя бы один бар, и последний бар не старше max_gap_days от today."""
        e = self._e(t) if t in self._s else 0
        return e > 0 and (self.today - self._s[t]["dates"][e - 1]).days <= max_gap_days

    def last(self, t: str, field: str, k: int) -> List[float]:
        e = self._e(t)
        return self._s[t][field][max(0, e - k):e]

    def at(self, t: str, field: str, offset: int = 0) -> float:
        i = self._e(t) - 1 - offset
        if i < 0:
            raise IndexError(f"{t}: нет бара со смещением {offset} на {self.today}")
        return self._s[t][field][i]

    def asof(self, t: str, field: str, d: date) -> Optional[float]:
        """Значение поля на последнем баре не позже min(d, today); None — тикера или бара нет. Для рядов-уровней
        (КБД, ключевая ставка), у которых свой календарь."""
        s = self._s.get(t)
        if s is None:
            return None
        i = bisect.bisect_right(s["dates"], min(d, self.today)) - 1
        return s[field][i] if i >= 0 else None

    def bars_until(self, d: date) -> int:
        """Сколько баров торгового календаря строго после today и не позже d. Биржевой календарь известен заранее —
        единственное, что View знает о будущем."""
        cal = self._s[self.calendar]["dates"]
        return bisect.bisect_right(cal, d) - bisect.bisect_right(cal, self.today)

    def upcoming_dividends(self, t: str) -> List[Dict[str, Any]]:
        """Дивиденды с ex-датой после today, уже объявленные к today (declared <= today; без поля declared — известны)."""
        return [dv for dv in self._s[t].get("divs", []) if dv["ex"] > self.today
                and (dv.get("declared") is None or dv["declared"] <= self.today)]


Strategy = Callable[[View, dict, dict], Dict[str, float]]


def _bar_index(s: Series, d: date) -> Optional[int]:
    i = bisect.bisect_left(s["dates"], d)
    return i if i < len(s["dates"]) and s["dates"][i] == d else None


def _last_close(s: Series, d: date) -> Optional[float]:
    i = bisect.bisect_right(s["dates"], d) - 1
    return s["close"][i] if i >= 0 else None


def _close_before(s: Series, d: date) -> Optional[float]:
    """Последний close строго до даты d (то, что известно на открытии дня d)."""
    i = bisect.bisect_left(s["dates"], d) - 1
    return s["close"][i] if i >= 0 else None


def simulate(series: Dict[str, Series], strategy: Strategy, params: dict, *, start: date, end: date,
             universe: Optional[Sequence[str]] = None, capital: float = 1_000_000.0, cost: float = COST,
             slip: float = SLIP, tax_rate: Optional[float] = None, rebalance_threshold: float = REBALANCE_THRESHOLD,
             fill: str = "open", rebalance: str = "daily", safe: str = "LQDT", calendar: str = "IMOEX",
             delay: int = 0) -> Result:
    """delay — сколько торговых дней календаря заявка ждёт сверх обычного одного (0 — исполнение на следующем баре;
    проверка устойчивости к запаздыванию исполнения). Заявка, чей бар исполнения выходит за окно, не исполняется."""
    if delay < 0:
        raise ValueError("delay < 0")
    cal = [d for d in series[calendar]["dates"] if start <= d <= end]
    if not cal:
        raise ValueError(f"пустой календарь: нет свечей {calendar} в окне")
    default_uni = [t for t in series if t not in ("IMOEX", calendar, safe, "TMOS")]
    uni = [t for t in (universe if universe is not None else default_uni) if t in series]  # [] — пустая вселенная, не «все»
    tradable = uni + [safe]
    cash = capital
    qty: Dict[str, float] = {}
    lots: Dict[str, List[List[Any]]] = {}        # ticker -> [[дата, кол-во, цена с издержками]]
    gains: Dict[int, float] = {}
    carry = 0.0
    pending_div: List[Tuple[date, float]] = []   # (дата выплаты, сумма)
    pending: List[Tuple[int, Dict[str, float]]] = []   # (индекс бара исполнения в cal, цель) по порядку выставления
    state: dict = {}
    st = {"costs": 0.0, "taxes": 0.0, "dividends": 0.0}
    trades: List[Dict[str, Any]] = []
    equity: List[Tuple[date, float]] = []
    in_stocks: List[float] = []
    cash_min = capital

    def value(d: date) -> float:
        return cash + sum(q * (_last_close(series[t], d) or 0.0) for t, q in qty.items())

    def buy(t: str, px: float, d: date, amount: float) -> None:
        nonlocal cash
        lot = series[t]["lot"]
        unit = px * (1 + slip) * (1 + cost)
        n_lots = int(min(amount, cash) // (unit * lot))
        if n_lots <= 0:
            return
        q = n_lots * lot
        cash -= q * unit
        st["costs"] += q * (unit - px)
        qty[t] = qty.get(t, 0.0) + q
        lots.setdefault(t, []).append([d, q, unit])
        trades.append({"date": d, "ticker": t, "side": "buy", "qty": q, "price": px, "cost": q * (unit - px)})

    def sell(t: str, px: float, d: date, q: float) -> None:
        nonlocal cash
        q = min(q, qty.get(t, 0.0))
        if q <= 0:
            return
        unit = px * (1 - slip) * (1 - cost)
        net = q * unit
        st["costs"] += q * (px - unit)
        left = q
        while left > 1e-9 and lots.get(t):
            lot = lots[t][0]
            part = min(left, lot[1])
            gain = part * (unit - lot[2])
            if tax_rate and not (gain > 0 and d >= add_years(lot[0], 3)):
                gains[d.year] = gains.get(d.year, 0.0) + gain
            lot[1] -= part
            left -= part
            if lot[1] <= 1e-9:
                lots[t].pop(0)
        cash += net
        qty[t] -= q
        if qty[t] <= 1e-9:
            del qty[t]
        trades.append({"date": d, "ticker": t, "side": "sell", "qty": q, "price": px, "cost": q * (px - unit)})

    def settle(year: int) -> float:
        nonlocal carry
        base = gains.pop(year, 0.0) - carry
        if base <= 0:
            carry = -base
            return 0.0
        carry = 0.0
        return tax_rate * base

    def price(t: str, d: date, field: str) -> Optional[float]:
        i = _bar_index(series[t], d)
        return series[t][field][i] if i is not None else None

    def execute(target: Dict[str, float], d: date, field: str) -> None:
        def mark(t: str) -> float:  # цена оценки на момент исполнения: цена заявки, иначе вчерашний close
            px = price(t, d, field)
            return px if px is not None else (_close_before(series[t], d) or 0.0)

        total = cash + sum(q * mark(t) for t, q in qty.items())
        want = {t: target.get(t, 0.0) for t in tradable}
        want[safe] = max(0.0, 1.0 - sum(v for t, v in want.items() if t != safe))
        orders = []
        for t in tradable:
            px = price(t, d, field)
            if px is None:
                continue  # бумага сегодня не торгуется — пропускаем
            cur = qty.get(t, 0.0) * mark(t)
            delta = want[t] * total - cur
            if abs(delta) > rebalance_threshold * total or (want[t] == 0 and cur > 0):
                orders.append((t, px, delta))
        for t, px, delta in sorted(orders, key=lambda o: o[2]):  # сначала продажи
            if delta < 0:
                lot = series[t]["lot"]
                q = qty.get(t, 0.0) if want[t] == 0 else math.floor(-delta / px / lot) * lot
                sell(t, px, d, q)
        for t, px, delta in orders:
            if delta > 0:
                buy(t, px, d, delta)

    prev_year = None
    prev_d: Optional[date] = None
    for i, d in enumerate(cal):
        # 1. право на дивиденд — по позиции на закрытии перед ex-датой (до сегодняшних сделок)
        for t in uni:
            for dv in series[t]["divs"]:
                hit = dv["ex"] == d if prev_d is None else prev_d < dv["ex"] <= d  # ex вне календаря IMOEX — на ближайший день
                if hit and qty.get(t, 0.0) > 0:
                    amt = qty[t] * dv["amount"] * (1 - DIVIDEND_TAX)
                    pending_div.append((dv["pay"], amt))
        # 2. выплаты, срок которых наступил
        due_now = [p for p in pending_div if p[0] <= d]
        for p in due_now:
            cash += p[1]
            st["dividends"] += p[1]
            pending_div.remove(p)
        # 3. налог за прошлый год
        if tax_rate and prev_year is not None and d.year != prev_year:
            due = sum(settle(y) for y in range(prev_year, d.year))
            if due > cash:
                # нехватка считается один раз: sell() меняет cash, и пересчёт в цикле недопродавал бы последние бумаги;
                # доли — только по бумагам с баром сегодня, чтобы бумага без торгов не оставляла свою часть непокрытой
                short = due - cash
                priced = {t: px for t in qty if (px := price(t, d, "open"))}
                total_pos = sum(qty[t] * px for t, px in priced.items())
                shares = {t: qty[t] * px / total_pos for t, px in priced.items()} if total_pos > 0 else {}
                for t, share in shares.items():
                    px, lot = priced[t], series[t]["lot"]
                    q = math.ceil(short * share / (px * (1 - slip) * (1 - cost)) / lot) * lot
                    sell(t, px, d, q)
            cash -= due
            st["taxes"] += due
        cash_min = min(cash_min, cash)
        prev_year = d.year
        prev_d = d
        # 4. заявки, выставленные вчера (или delay баров назад)
        while pending and pending[0][0] <= i:
            execute(pending.pop(0)[1], d, "open" if fill == "open" else "close")
            cash_min = min(cash_min, cash)
        # 5. оценка на закрытии
        eq = value(d)
        equity.append((d, eq))
        in_stocks.append(sum(q * (_last_close(series[t], d) or 0.0) for t, q in qty.items() if t != safe) / eq if eq else 0.0)
        # 6. сигнал на закрытии
        is_last = i == len(cal) - 1
        month_end = is_last or cal[i + 1].month != d.month
        if rebalance == "daily" or month_end:
            view = View(series, d, uni, calendar)
            target = strategy(view, params, state) or {}
            if sum(target.values()) > 1.0 + 1e-9:
                raise ValueError(f"сумма весов {sum(target.values()):.3f} > 1 на {d}")
            if i + 1 + delay < len(cal):
                pending.append((i + 1 + delay, target))
    # ликвидация
    last = cal[-1]
    for t in list(qty):
        px = _last_close(series[t], last)
        if px:
            sell(t, px, last, qty[t])
    for p in pending_div:  # невыплаченные дивиденды — считаем полученными
        cash += p[1]
        st["dividends"] += p[1]
    if tax_rate:
        due = settle(last.year)
        cash -= due
        st["taxes"] += due
    # equity[-1] остаётся оценкой на закрытии последнего дня: ликвидация и итоговый налог входят только в final,
    # иначе налог последнего года попадал бы в просадку и в доходность последнего года
    return {"equity": equity, "trades": trades, "final": cash, "costs": st["costs"], "taxes": st["taxes"],
            "dividends": st["dividends"], "capital": capital, "start": cal[0], "end": last, "in_stocks": in_stocks,
            "cash_min": cash_min}


def daily_returns(equity: Sequence[Tuple[date, float]]) -> List[float]:
    return [equity[i][1] / equity[i - 1][1] - 1 for i in range(1, len(equity)) if equity[i - 1][1] > 0]


def _safe_returns(safe_series: Series, dates: Sequence[date]) -> List[float]:
    closes = [_last_close(safe_series, d) or 1.0 for d in dates]
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]


def yearly(result: Result) -> Dict[int, float]:
    out, base, cur_year, last_v = {}, result["capital"], None, None
    for d, v in result["equity"]:
        if cur_year is not None and d.year != cur_year:
            out[cur_year] = last_v / base - 1
            base = last_v
        cur_year, last_v = d.year, v
    if cur_year is not None:
        out[cur_year] = last_v / base - 1
    return out


def summarize(result: Result, safe_series: Optional[Series] = None) -> Dict[str, Any]:
    eq = result["equity"]
    rets = daily_returns(eq)
    rf = _safe_returns(safe_series, [d for d, _ in eq]) if safe_series else None
    ex = [r - (rf[i] if rf else 0.0) for i, r in enumerate(rets)]
    m, sd, sk, ku = moments(ex) if len(ex) > 1 else (0.0, 0.0, 0.0, 3.0)
    years = max((result["end"] - result["start"]).days / 365.25, 1e-9)
    turnover = sum(t["qty"] * t["price"] for t in result["trades"]) / result["capital"] / years
    return {"final": result["final"], "xirr": xirr([(result["start"], -result["capital"]), (result["end"], result["final"])]),
            "mdd": max_drawdown([v for _, v in eq]), "sharpe": sharpe(rets, rf), "sharpe_daily": (m / sd) if sd else 0.0,
            "n_obs": len(rets), "skew": sk, "kurt": ku, "trades": len(result["trades"]), "turnover": turnover,
            "in_market": sum(result["in_stocks"]) / len(result["in_stocks"]) if result["in_stocks"] else 0.0,
            "costs": result["costs"], "taxes": result["taxes"], "dividends": result["dividends"], "yearly": yearly(result)}
