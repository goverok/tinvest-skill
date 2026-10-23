"""Правила деления облигационной части между LQDT и корзинами ОФЗ по дюрации.

Контракт как у bt_strategies: (view, params, state) -> {тикер: вес}, сумма <= 1, остаток симулятор держит в LQDT.
Тикеры корзин — OFZ1Y/OFZ3Y/OFZ5Y/OFZ10Y (индексы RUGBITR*). Уровни КБД (ZCYC*) и ключевая ставка (KEYRATE) —
отдельные ряды; правило читает их через view.asof, которое не видит точки позже даты решения.
"""
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bt_data import SAFE_SPREAD  # noqa: E402
from bt_sim import View  # noqa: E402

BUCKETS = ["OFZ1Y", "OFZ3Y", "OFZ5Y", "OFZ10Y"]
UNIVERSE = ["RGBITR"] + BUCKETS
TERM = {"OFZ1Y": 1, "OFZ3Y": 3, "OFZ5Y": 5, "OFZ10Y": 10}
MONTH_DAYS = 30.44
KEYRATE_BARS = 400  # рабочих дней истории ставки, которых хватает на окно 12 мес.
CURRENT_NAME = "как сейчас"
CURRENT = {"OFZ10Y": 7 / 9, "OFZ1Y": 2 / 9}  # 35% длинных фиксов и 10% коротких из 45% облигационной части


def _months_ago(view: View, months: int) -> date:
    return view.today - timedelta(days=round(months * MONTH_DAYS))


def lqdt_yield(view: View) -> Optional[float]:
    """Оценка доходности LQDT на дату решения: ключевая ставка − SAFE_SPREAD."""
    kr = view.asof("KEYRATE", "close", view.today)
    return None if kr is None else kr - SAFE_SPREAD


def curve(view: View, p: dict, state: dict) -> Dict[str, float]:
    """Держать корзину, если доходность КБД на её срок выше доходности LQDT больше чем на s, иначе LQDT."""
    y = view.asof(f"ZCYC{TERM[p['bucket']]}", "close", view.today)
    safe = lqdt_yield(view)
    if y is None or safe is None:
        return {}
    return {p["bucket"]: 1.0} if y - safe > p["s"] else {}


def rate_cycle(view: View, p: dict, state: dict) -> Dict[str, float]:
    """Держать корзину, если ставка ниже своего максимума за M мес. на d или больше (цикл снижения), иначе LQDT.
    d = 0 — вырожденный вариант «держать всегда», оставлен нижней точкой сетки для оценки плато."""
    ds = view.last("KEYRATE", "dates", KEYRATE_BARS)
    vs = view.last("KEYRATE", "close", KEYRATE_BARS)
    if not vs:
        return {}
    since = _months_ago(view, p["M"])
    window = [v for d, v in zip(ds, vs) if d >= since] or [vs[-1]]
    seed = view.asof("KEYRATE", "close", since)  # ставка, действовавшая на начало окна (ряд может хранить только даты изменений)
    if seed is not None:
        window.append(seed)
    return {p["bucket"]: 1.0} if max(window) - vs[-1] >= p["d"] - 1e-12 else {}


def _ret(view: View, t: str, months: int) -> Optional[float]:
    past = view.asof(t, "close", _months_ago(view, months))
    now = view.asof(t, "close", view.today)
    return None if not past or not now else now / past - 1.0


def ts_momentum(view: View, p: dict, state: dict) -> Dict[str, float]:
    """Равными долями корзины, чья полная доходность за L мес. выше доходности LQDT за те же L мес.; нет таких — LQDT."""
    pool = BUCKETS if p["set"] == "all" else ["OFZ5Y", "OFZ10Y"]
    safe = _ret(view, "LQDT", p["L"])
    if safe is None:
        return {}
    good = [b for b in pool if (r := _ret(view, b, p["L"])) is not None and r > safe]
    return {b: 1.0 / len(good) for b in good}


def static(view: View, p: dict, state: dict) -> Dict[str, float]:
    return dict(p["weights"])


def random_bucket(view: View, p: dict, state: dict) -> Dict[str, float]:
    """Каждый месяц случайная корзина из пяти (LQDT и четыре ОФЗ); воспроизводимо по seed и дате."""
    pick = random.Random(f"{p.get('seed', 0)}-{view.today.isoformat()}").choice(["LQDT"] + BUCKETS)
    return {} if pick == "LQDT" else {pick: 1.0}


FAMILIES = {
    "curve": {"fn": curve, "grid": {"bucket": ["OFZ3Y", "OFZ5Y", "OFZ10Y"], "s": [-0.01, 0.0, 0.01, 0.02]}, "rebalance": "monthly"},
    "rate_cycle": {"fn": rate_cycle, "grid": {"M": [6, 12], "d": [0.0, 0.005, 0.01], "bucket": ["OFZ5Y", "OFZ10Y"]},
                   "rebalance": "monthly"},
    "ts_momentum": {"fn": ts_momentum, "grid": {"L": [3, 6, 12], "set": ["all", "long"]}, "rebalance": "monthly"},
}
BENCHMARKS = {
    "LQDT": (static, {"weights": {}}),
    "RGBITR": (static, {"weights": {"RGBITR": 1.0}}),
    CURRENT_NAME: (static, {"weights": CURRENT}),
    "равные доли": (static, {"weights": {b: 0.2 for b in BUCKETS}}),
}
