"""Протокол проверки стратегий: окна, отбор по плато, валидация, устойчивость, журнал закрытого окна, вердикт.

Окна — константы: TRAIN (подбор), VALID (проверка выбора), HOLDOUT (открывается один раз для финалистов).
"""
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bt_data import OLDEST10, Series, synthetic_safe  # noqa: E402
from bt_sim import COST, SLIP, TAX, simulate, summarize  # noqa: E402
from bt_strategies import BENCHMARKS, FAMILIES, grid_variants, random_portfolio  # noqa: E402
from common import fmt_pct, home, read_json, today, write_json  # noqa: E402
from finmath import deflated_sharpe  # noqa: E402

Window = Tuple[date, Optional[date]]
TRAIN: Window = (date(2014, 1, 1), date(2021, 12, 31))
VALID: Window = (date(2022, 1, 1), date(2023, 12, 31))
HOLDOUT: Window = (date(2024, 1, 1), None)
N_RANDOM, K_RANDOM = 200, 5
RANDOM_PERCENTILE = 0.80
MDD_TOLERANCE = 1.5
DSR_ALPHA = 0.05


def _end(w: Window) -> date:
    return w[1] or today()


def run_variant(series: Dict[str, Series], family: str, params: dict, window: Window, *,
                universe: Optional[Sequence[str]] = None, tax_rate: Optional[float] = None, cost: float = COST,
                slip: float = SLIP, fill: str = "open", capital: float = 1_000_000.0) -> Dict[str, Any]:
    fam = FAMILIES[family]
    res = simulate(series, fam["fn"], params, start=window[0], end=_end(window), universe=universe, capital=capital,
                   cost=cost, slip=slip, tax_rate=tax_rate, fill=fill, rebalance=fam["rebalance"])
    s = summarize(res, series.get("LQDT"))
    s["result"] = res
    return s


def run_benchmark(series: Dict[str, Series], name: str, window: Window, *, tax_rate: Optional[float] = None,
                  capital: float = 1_000_000.0, universe: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    fn, params, reb = BENCHMARKS[name]
    uni = ["TMOS"] if name == "TMOS" else universe
    res = simulate(series, fn, params, start=window[0], end=_end(window), universe=uni, capital=capital,
                   tax_rate=tax_rate, rebalance=reb)
    s = summarize(res, series.get("LQDT"))
    s["result"] = res
    return s


# ---------- отбор ----------

def neighbors(variant: dict, grid: Dict[str, list]) -> List[dict]:
    out = []
    for k, values in grid.items():
        i = values.index(variant[k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(values):
                out.append(dict(variant, **{k: values[j]}))
    return out


def _key(v: dict) -> str:
    return json.dumps(v, sort_keys=True)


def pick_plateau(scores: List[Tuple[dict, float]], grid: Dict[str, list]) -> Tuple[dict, float]:
    """Вариант с максимальным средним Sharpe по окрестности (сам + соседи по сетке, которые есть в scores)."""
    by_key = {_key(v): s for v, s in scores}
    best, best_score = None, float("-inf")
    for v, s in scores:
        vals = [s] + [by_key[_key(n)] for n in neighbors(v, grid) if _key(n) in by_key]
        avg = sum(vals) / len(vals)
        if avg > best_score:
            best, best_score = v, avg
    return best, best_score


def random_distribution(series: Dict[str, Series], window: Window, n_runs: int = N_RANDOM, k: int = K_RANDOM,
                        universe: Optional[Sequence[str]] = None, capital: float = 1_000_000.0) -> List[float]:
    out = []
    for seed in range(n_runs):
        res = simulate(series, random_portfolio, {"K": k, "seed": seed}, start=window[0], end=_end(window),
                       universe=universe, capital=capital, rebalance="monthly")
        out.append(summarize(res, series.get("LQDT"))["sharpe"] or 0.0)
    return out


def percentile(values: Sequence[float], x: float) -> float:
    return sum(1 for v in values if v < x) / len(values) if values else 0.0


def validate(train: dict, valid: dict, lqdt_valid: dict, rand_valid: Sequence[float]) -> Tuple[bool, List[str]]:
    why = []
    if (valid["xirr"] or -1) <= (lqdt_valid["xirr"] or -1):
        why.append(f"XIRR {fmt_pct(valid['xirr'])} не выше LQDT {fmt_pct(lqdt_valid['xirr'])}")
    if percentile(rand_valid, valid["sharpe"] or 0.0) < RANDOM_PERCENTILE:
        why.append(f"Sharpe {valid['sharpe'] or 0:.2f} ниже {RANDOM_PERCENTILE:.0%}-го перцентиля случайных портфелей")
    if valid["mdd"] < train["mdd"] * MDD_TOLERANCE:
        why.append(f"просадка {valid['mdd']:.1%} хуже {train['mdd']:.1%} × {MDD_TOLERANCE}")
    return not why, why


# ---------- устойчивость ----------

def robustness(series: Dict[str, Series], family: str, params: dict, window: Window,
               universe: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, Any]]:
    return {
        "cost_x2": run_variant(series, family, params, window, universe=universe, cost=COST * 2, slip=SLIP * 2),
        "next_close": run_variant(series, family, params, window, universe=universe, fill="next_close"),
        "oldest10": run_variant(series, family, params, window, universe=[t for t in OLDEST10 if t in series]),
    }


def years_beating(strategy_yearly: Dict[int, float], safe_yearly: Dict[int, float]) -> Tuple[int, int]:
    years = [y for y in strategy_yearly if y in safe_yearly]
    return sum(1 for y in years if strategy_yearly[y] > safe_yearly[y]), len(years)


def top2_share(strategy_yearly: Dict[int, float]) -> float:
    pos = sorted((v for v in strategy_yearly.values() if v > 0), reverse=True)
    return sum(pos[:2]) / sum(pos) if pos else 0.0


def dsr(summary: dict, n_trials: int) -> Tuple[float, float]:
    return deflated_sharpe(summary["sharpe_daily"], n_trials, summary["n_obs"], summary["skew"], summary["kurt"])


# ---------- закрытое окно ----------

def holdout_log_path() -> Path:
    return home() / "data" / "backtest" / "holdout_log.json"


def record_holdout(finalists: List[Tuple[str, dict]]) -> List[str]:
    """Дописывает журнал; возвращает нарушения: ранее на закрытом окне прогонялись другие варианты семейства."""
    log = read_json(holdout_log_path(), []) or []
    violations = []
    for fam, params in finalists:
        h = hashlib.sha1(f"{fam}:{_key(params)}".encode()).hexdigest()[:12]
        earlier = [e for e in log if e["family"] == fam and e["hash"] != h]
        if earlier:
            seen = ", ".join(sorted({e["params"] for e in earlier}))
            violations.append(f"{fam}: окно уже открывалось {len(earlier)} раз с другими параметрами ({seen})")
        log.append({"at": datetime.now(timezone.utc).isoformat(), "family": fam, "params": _key(params), "hash": h})
    write_json(holdout_log_path(), log)
    return violations


def verdict(hold_iis: dict, hold_brk: dict, lqdt_iis: dict, tmos_iis: dict, ew_iis: dict,
            valid_mdd: float, dsr_p: float) -> Tuple[int, str]:
    x, xb, lq = hold_iis["xirr"] or -1, hold_brk["xirr"] or -1, lqdt_iis["xirr"] or -1
    beats_lqdt = x > lq and xb > lq
    mdd_ok = hold_iis["mdd"] >= valid_mdd * MDD_TOLERANCE
    significant = dsr_p < DSR_ALPHA
    if beats_lqdt and mdd_ok and significant:
        return 1, "Кандидат в песочницу: выше LQDT в обоих налоговых режимах, просадка в допуске, эффект значим после поправки на перебор."
    beats_market = x > (tmos_iis["xirr"] or -1) and x > (ew_iis["xirr"] or -1)
    if significant and beats_market:
        return 2, "Эффект есть, но не бьёт LQDT при текущей ставке: выше TMOS и равных весов, значим после поправки, ниже LQDT или просадка вне допуска."
    return 3, "Ничего не найдено: результат на закрытом окне не отличим от перебора или не обыгрывает рынок."


def threshold_rate(series: Dict[str, Series], family: str, params: dict, window: Window,
                   universe: Optional[Sequence[str]] = None, lo: float = 0.02, hi: float = 0.40) -> Optional[float]:
    """Ставка безрискового актива, при которой XIRR стратегии равен XIRR безрискового актива: LQDT заменяется
    синтетикой с постоянной ставкой. Сравнение — по эффективной доходности (XIRR) безрискового пая за то же окно,
    потому что ежедневное начисление даёт эффективную доходность выше номинальной; возвращается номинальная ставка
    (её сравнивают с заявленной доходностью LQDT)."""
    cal = [d for d in series["IMOEX"]["dates"] if d <= _end(window)]

    def gap(rate: float) -> Optional[float]:
        syn = synthetic_safe(cal, [(date(1900, 1, 1), rate)], spread=0.0)
        fake = dict(series)
        fake["LQDT"] = {"ticker": "LQDT", "lot": 1, "dates": cal, "open": syn, "high": syn, "low": syn, "close": syn,
                        "volume": [0.0] * len(syn), "tr": syn, "divs": [], "warnings": []}
        x = run_variant(fake, family, params, window, universe=universe)["xirr"]
        if x is None:
            return None
        i0 = next((i for i, d in enumerate(cal) if d >= window[0]), None)
        if i0 is None or len(cal) - 1 <= i0:
            return None
        days = (cal[-1] - cal[i0]).days
        safe_x = (syn[-1] / syn[i0]) ** (365.0 / days) - 1.0
        return x - safe_x

    g_lo, g_hi = gap(lo), gap(hi)
    if g_lo is None or g_hi is None or g_lo * g_hi > 0:
        return None
    for _ in range(8):
        mid = (lo + hi) / 2
        g = gap(mid)
        if g is None:
            return None
        if g * g_lo > 0:
            lo, g_lo = mid, g
        else:
            hi = mid
    return (lo + hi) / 2


def parse_finalist(spec: str) -> Tuple[str, dict]:
    fam, _, rest = spec.partition(":")
    if fam not in FAMILIES:
        raise ValueError(f"неизвестное семейство {fam}; есть: {', '.join(FAMILIES)}")
    grid = FAMILIES[fam]["grid"]
    params = {}
    for kv in rest.split(","):
        k, _, v = kv.partition("=")
        if k not in grid:
            raise ValueError(f"{fam}: нет параметра {k}; есть: {', '.join(grid)}")
        val = type(grid[k][0])(float(v)) if isinstance(grid[k][0], (int, float)) else v
        if val not in grid[k] or (isinstance(grid[k][0], (int, float)) and float(v) != val):
            raise ValueError(f"{fam}: значение {k}={v} вне сетки {grid[k]}")
        params[k] = val
    missing = [k for k in grid if k not in params]
    if missing:
        raise ValueError(f"{fam}: не заданы {', '.join(missing)}")
    return fam, params


def all_variants(family: str) -> List[dict]:
    return grid_variants(FAMILIES[family]["grid"])


TOTAL_TRIALS = sum(len(grid_variants(f["grid"])) for f in FAMILIES.values())
TAX_RATE = TAX
