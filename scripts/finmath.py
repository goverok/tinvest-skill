"""Чистая математика без API: XIRR, доходность к погашению, дюрация, FIFO-лоты.

Все функции детерминированы и покрыты tests/test_calc.py.
"""
import math
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

Flow = Tuple[date, float]          # (дата, сумма со знаком: взнос < 0, вывод/итоговая стоимость > 0)
CashFlow = Tuple[float, float]     # (срок в годах, сумма)


# ---------- XIRR ----------

def xnpv(rate: float, flows: Sequence[Flow]) -> float:
    t0 = min(d for d, _ in flows)
    return sum(a / (1.0 + rate) ** ((d - t0).days / 365.0) for d, a in flows)


def _solve_rate(npv_fn, lo: float = -0.99, hi: float = 10.0, tol: float = 1e-9) -> Optional[float]:
    """Решает npv_fn(r) = 0: Ньютон от 10%, при неудаче — бисекция по скобке [lo, hi]."""
    r = 0.1
    for _ in range(100):
        f = npv_fn(r)
        h = 1e-6
        df = (npv_fn(r + h) - f) / h
        if df == 0:
            break
        nr = r - f / df
        if nr <= -0.999 or nr > 1e4 or nr != nr:
            break
        if abs(nr - r) < tol:
            r = nr
            if abs(npv_fn(r)) < 1e-6 * max(1.0, abs(npv_fn(0.0))):
                return r
            break
        r = nr
    f_lo, f_hi = npv_fn(lo), npv_fn(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        f_mid = npv_fn(mid)
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def xirr(flows: Sequence[Flow]) -> Optional[float]:
    """Годовая доходность по неравномерным потокам (как XIRR в Excel). None, если решения нет."""
    flows = [(d, float(a)) for d, a in flows if a]
    if len(flows) < 2:
        return None
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    span_days = (max(d for d, _ in flows) - min(d for d, _ in flows)).days
    if span_days <= 0:
        return None
    return _solve_rate(lambda r: xnpv(r, flows))


def money_weighted_summary(flows: Sequence[Flow], terminal_value: float, as_of: date) -> Dict[str, Optional[float]]:
    """Сводка: взносы, выводы, итоговая стоимость, абсолютный результат, XIRR."""
    inflows = -sum(a for _, a in flows if a < 0)
    outflows = sum(a for _, a in flows if a > 0)
    all_flows = list(flows) + [(as_of, terminal_value)]
    return {
        "contributions": inflows,
        "withdrawals": outflows,
        "terminal_value": terminal_value,
        "net_result": terminal_value + outflows - inflows,
        "xirr": xirr(all_flows),
        "first_flow": min((d for d, _ in flows), default=as_of),
    }


# ---------- облигации ----------

def ytm(dirty_price: float, cashflows: Sequence[CashFlow]) -> Optional[float]:
    """Эффективная годовая доходность к погашению: Σ cf/(1+y)^t = грязная цена."""
    cfs = [(t, a) for t, a in cashflows if t > 0 and a]
    if dirty_price <= 0 or not cfs:
        return None
    return _solve_rate(lambda y: sum(a / (1.0 + y) ** t for t, a in cfs) - dirty_price, lo=-0.95, hi=5.0)


def macaulay_duration(cashflows: Sequence[CashFlow], y: float) -> Optional[float]:
    cfs = [(t, a) for t, a in cashflows if t > 0 and a]
    if not cfs or y is None:
        return None
    pv = [(t, a / (1.0 + y) ** t) for t, a in cfs]
    total = sum(p for _, p in pv)
    if total <= 0:
        return None
    return sum(t * p for t, p in pv) / total


def modified_duration(mac: Optional[float], y: Optional[float]) -> Optional[float]:
    if mac is None or y is None:
        return None
    return mac / (1.0 + y)


# ---------- FIFO-лоты ----------

def fifo(trades: Sequence[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """trades: [{date, side:'buy'|'sell', qty, unit_cost (для buy) | unit_price (для sell), note?}] по порядку.

    Возвращает (открытые лоты, реализованные сделки). Стоимость лота уже включает комиссию покупки,
    цена продажи — за вычетом комиссии продажи.
    """
    lots: List[Dict] = []
    realized: List[Dict] = []
    for t in sorted(trades, key=lambda x: x["date"]):
        q = float(t["qty"])
        if q <= 0:
            continue
        if t["side"] == "buy":
            lots.append({"date": t["date"], "qty": q, "left": q, "unit_cost": float(t.get("unit_cost", 0.0)),
                         "note": t.get("note", "")})
            continue
        price = float(t.get("unit_price", 0.0))
        while q > 1e-9 and lots:
            lot = lots[0]
            take = min(lot["left"], q)
            realized.append({
                "date": t["date"], "buy_date": lot["date"], "qty": take,
                "proceeds": take * price, "cost": take * lot["unit_cost"],
                "pnl": take * (price - lot["unit_cost"]),
                "hold_days": (t["date"] - lot["date"]).days,
            })
            lot["left"] -= take
            q -= take
            if lot["left"] <= 1e-9:
                lots.pop(0)
        if q > 1e-9:  # продано больше, чем есть лотов (перевод, сплит, неполная история)
            realized.append({"date": t["date"], "buy_date": None, "qty": q, "proceeds": q * price,
                             "cost": 0.0, "pnl": q * price, "hold_days": None, "note": "нет лота покупки"})
    return lots, realized


# ---------- размещение денег по целевым долям ----------

def allocate(amount: float, current: Dict[str, float], targets: Dict[str, float]) -> Dict[str, float]:
    """Распределяет amount по ключам так, чтобы приблизить доли к targets (в долях от 1), без продаж.

    Сначала закрывает дефициты пропорционально, остаток — пропорционально целевым весам.
    """
    if amount <= 0 or not targets:
        return {}
    total_after = sum(current.values()) + amount
    deficits = {k: max(0.0, targets[k] * total_after - current.get(k, 0.0)) for k in targets}
    s = sum(deficits.values())
    alloc: Dict[str, float] = {k: 0.0 for k in targets}
    if s > 0:
        scale = min(1.0, amount / s)
        for k in targets:
            alloc[k] = deficits[k] * scale
    rest = amount - sum(alloc.values())
    if rest > 1e-6:
        w = sum(targets.values()) or 1.0
        for k in targets:
            alloc[k] += rest * targets[k] / w
    return alloc


# ---------- Sharpe и поправка на перебор ----------

def moments(x: Sequence[float]) -> Tuple[float, float, float, float]:
    """(среднее, выборочное ст. отклонение, асимметрия, эксцесс; у нормального эксцесс 3)."""
    n = len(x)
    if n < 2:
        return (x[0] if x else 0.0), 0.0, 0.0, 3.0
    m = sum(x) / n
    d2 = sum((v - m) ** 2 for v in x)
    sd = math.sqrt(d2 / (n - 1))
    if d2 == 0:
        return m, 0.0, 0.0, 3.0
    pop_sd = math.sqrt(d2 / n)
    skew = sum((v - m) ** 3 for v in x) / n / pop_sd ** 3
    kurt = sum((v - m) ** 4 for v in x) / n / pop_sd ** 4
    return m, sd, skew, kurt


def sharpe(returns: Sequence[float], rf: Optional[Sequence[float]] = None, periods: int = 252) -> Optional[float]:
    """Годовой Sharpe: mean(r - rf) / std(r - rf) * sqrt(periods). None при < 2 наблюдениях или нулевой дисперсии."""
    ex = [r - (rf[i] if rf else 0.0) for i, r in enumerate(returns)]
    if len(ex) < 2:
        return None
    m, sd, _, _ = moments(ex)
    if sd == 0:
        return None
    return m / sd * math.sqrt(periods)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Квантиль стандартного нормального распределения бисекцией."""
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


EULER_GAMMA = 0.5772156649015329


def deflated_sharpe(sr: float, n_trials: int, n_obs: int, skew: float = 0.0, kurt: float = 3.0) -> Tuple[float, float]:
    """Deflated Sharpe Ratio (Bailey & López de Prado, 2014). sr — за период (не годовой).
    Возвращает (sr_star, p_value): ожидаемый максимум Sharpe среди n_trials попыток с нулевым истинным Sharpe
    и вероятность того, что истинный Sharpe стратегии не превышает этот порог."""
    if n_trials <= 1:
        sr_star = 0.0
    else:
        var_sr = 1.0 / n_obs  # дисперсия оценки Sharpe при нулевом истинном значении
        sr_star = math.sqrt(var_sr) * ((1 - EULER_GAMMA) * norm_ppf(1 - 1.0 / n_trials)
                                       + EULER_GAMMA * norm_ppf(1 - 1.0 / (n_trials * math.e)))
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4.0 * sr * sr))
    z = (sr - sr_star) * math.sqrt(n_obs - 1) / denom
    return sr_star, 1.0 - norm_cdf(z)
