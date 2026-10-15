"""Семейства стратегий для исследования: функция (view, params, state) -> {ticker: вес}, сумма <= 1.

Все лонг; остаток капитала симулятор держит в LQDT. state — память между днями (позиции Donchian).
"""
import itertools
import math
import random
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bt_sim import View  # noqa: E402
from finmath import moments  # noqa: E402

BARS_PER_MONTH = 21
SMA_FILTER_N = 200


def _market_ok(view: View) -> bool:
    """IMOEX выше своей SMA200 (при нехватке истории — считаем, что нет)."""
    closes = view.last("IMOEX", "close", SMA_FILTER_N)
    return len(closes) == SMA_FILTER_N and closes[-1] > sum(closes) / SMA_FILTER_N


def momentum(view: View, p: dict, state: dict) -> Dict[str, float]:
    """Кросс-секционный моментум «L−1»: доходность за L месяцев без последнего, топ-K равными весами."""
    if p.get("market_filter") and not _market_ok(view):
        return {}
    need = p["L"] * BARS_PER_MONTH + 1
    scores = []
    for t in view.universe:
        if not view.has(t, need):
            continue
        tr = view.last(t, "tr", need)
        scores.append((tr[-1 - BARS_PER_MONTH] / tr[0] - 1, t))
    top = [t for _, t in sorted(scores, reverse=True)[:p["K"]]]
    return {t: 1.0 / p["K"] for t in top}


def _atr(view: View, t: str, n: int = 20) -> float:
    hi, lo, cl = view.last(t, "high", n + 1), view.last(t, "low", n + 1), view.last(t, "close", n + 1)
    trs = [max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])) for i in range(1, len(cl))]
    return sum(trs) / len(trs) if trs else 0.0


def donchian(view: View, p: dict, state: dict) -> Dict[str, float]:
    """Пробой: вход на close выше max(high, N) предыдущих дней, выход на close ниже min(low, M). K слотов."""
    held: Dict[str, bool] = state.setdefault("held", {})
    n, m, k = p["N"], p["M"], p["K"]
    for t in list(held):  # выходы
        if not view.has(t, m + 1):
            continue
        lows = view.last(t, "low", m + 1)[:-1]
        if view.at(t, "close") < min(lows):
            del held[t]
    candidates = []
    for t in view.universe:
        if t in held or not view.has(t, n + 1):
            continue
        highs = view.last(t, "high", n + 1)[:-1]
        c = view.at(t, "close")
        if c > max(highs):
            atr = _atr(view, t)
            candidates.append(((c - max(highs)) / atr if atr else 0.0, t))
    for _, t in sorted(candidates, reverse=True):
        if len(held) >= k:
            break
        held[t] = True
    return {t: 1.0 / k for t in held}


def trend_vol(view: View, p: dict, state: dict) -> Dict[str, float]:
    """Бумага в портфеле, если close > SMA(N); вес ∝ 1/σ20, масштаб под целевую годовую волатильность, сумма <= 1."""
    n, target = p["N"], p["target_vol"]
    inv = {}
    for t in view.universe:
        if not view.has(t, max(n, 22)):
            continue
        closes = view.last(t, "close", n)
        if closes[-1] <= sum(closes) / n:
            continue
        c20 = view.last(t, "close", 21)
        rets = [c20[i] / c20[i - 1] - 1 for i in range(1, len(c20))]
        _, sd, _, _ = moments(rets)
        if sd > 0:
            inv[t] = 1.0 / (sd * math.sqrt(252))
    if not inv:
        return {}
    total_inv = sum(inv.values())
    raw = {t: v / total_inv for t, v in inv.items()}           # веса ∝ 1/σ, сумма 1
    port_vol = sum(raw[t] / inv[t] for t in raw)                # σ портфеля без корреляций = Σ w_i σ_i
    scale = min(1.0, target / port_vol) if port_vol > 0 else 1.0
    return {t: w * scale for t, w in raw.items()}


def random_portfolio(view: View, p: dict, state: dict) -> Dict[str, float]:
    """K случайных бумаг равными весами; генератор — от seed и даты, чтобы прогон был воспроизводим."""
    avail = [t for t in view.universe if view.has(t, 1)]
    k = min(p["K"], len(avail))
    if k == 0:
        return {}
    rng = random.Random(f"{p.get('seed', 0)}-{view.today.isoformat()}")
    return {t: 1.0 / k for t in rng.sample(avail, k)}


def equal_weight(view: View, p: dict, state: dict) -> Dict[str, float]:
    avail = [t for t in view.universe if view.has(t, 1)]
    return {t: 1.0 / len(avail) for t in avail} if avail else {}


def hold(view: View, p: dict, state: dict) -> Dict[str, float]:
    return {p["ticker"]: 1.0} if view.has(p["ticker"], 1) else {}


def hold_safe(view: View, p: dict, state: dict) -> Dict[str, float]:
    return {}


def grid_variants(grid: Dict[str, list]) -> List[dict]:
    keys = list(grid)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]


FAMILIES = {
    "momentum": {"fn": momentum, "grid": {"L": [3, 6, 12], "K": [3, 5, 8], "market_filter": [0, 1]}, "rebalance": "monthly"},
    "donchian": {"fn": donchian, "grid": {"N": [20, 55, 100], "M": [10, 20], "K": [5, 10]}, "rebalance": "daily"},
    "trend_vol": {"fn": trend_vol, "grid": {"N": [50, 100, 200], "target_vol": [0.10, 0.15, 0.20]}, "rebalance": "daily"},
}
BENCHMARKS = {
    "LQDT": (hold_safe, {}, "monthly"),
    "TMOS": (hold, {"ticker": "TMOS"}, "monthly"),
    "equal_weight": (equal_weight, {}, "monthly"),
}
