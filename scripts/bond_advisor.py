"""Советник по облигационной части: корзины по дюрации, отбор ОФЗ-кандидатов, фрагмент targets.yaml.

Чистые функции без API: справочник, цены, купоны и свечи собирает bond_research.py через tinvest_api (только чтение).
"""
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import parse_date  # noqa: E402
from finmath import macaulay_duration, modified_duration, ytm  # noqa: E402

BUCKET_EDGES = [(1.0, "OFZ1Y"), (3.0, "OFZ3Y"), (5.0, "OFZ5Y")]
BUCKET_RU = {"OFZ1Y": "до 1 года", "OFZ3Y": "1–3 года", "OFZ5Y": "3–5 лет", "OFZ10Y": "больше 5 лет", "LQDT": "LQDT"}
MIN_TURNOVER = 10_000_000.0   # ₽ в день: ниже — спред съедает часть доходности даже на взносе 100 тыс.
TURNOVER_DAYS = 20
TOP_N = 3
TRADING_NORMAL = "SECURITY_TRADING_STATUS_NORMAL_TRADING"
MATURING_DAYS = 365
GROUP_FIXED, GROUP_SHORT, GROUP_CASH = "ОФЗ с фикс. купоном", "Флоатеры и корпоративные", "Денежный рынок"
GROUP_BUCKETS = [(GROUP_FIXED, ["OFZ3Y", "OFZ5Y", "OFZ10Y"]), (GROUP_SHORT, ["OFZ1Y"]), (GROUP_CASH, ["LQDT"])]
GROUP_TO_BUCKET = {GROUP_FIXED: "OFZ10Y", GROUP_SHORT: "OFZ1Y", GROUP_CASH: "LQDT"}


def bucket_of(duration: Optional[float], floater: bool = False) -> Optional[str]:
    """Корзина по модифицированной дюрации (лет): ≤1, 1–3, 3–5, >5; флоатер — короткая корзина."""
    if floater:
        return "OFZ1Y"
    if duration is None:
        return None
    for edge, b in BUCKET_EDGES:
        if duration <= edge:
            return b
    return "OFZ10Y"


def bond_metrics(dirty: float, nominal: float, coupons: Sequence[Tuple[date, float]], maturity: date,
                 t0: date) -> Tuple[Optional[float], Optional[float]]:
    """Доходность к погашению и модифицированная дюрация по грязной цене 1 бумаги, будущим купонам и номиналу."""
    cfs = [((d - t0).days / 365.0, a) for d, a in coupons if t0 < d <= maturity]
    cfs.append(((maturity - t0).days / 365.0, nominal))
    y = ytm(dirty, cfs)
    if y is None:
        return None, None
    return y, modified_duration(macaulay_duration(cfs, y), y)


def is_candidate(b: Dict[str, Any], t0: date, min_days: int = 30) -> bool:
    """ОФЗ в рублях, для ИИС, без амортизации, не ОФЗ-ИН (SU52…), не бессрочная, доступна к покупке, в нормальных торгах
    (если статус указан), гасится позже min_days."""
    ticker = b.get("ticker") or ""
    mat = parse_date(b.get("maturityDate"))
    return (ticker.startswith("SU") and not ticker.startswith("SU52") and (b.get("currency") or "rub").lower() == "rub"
            and b.get("forIisFlag") is True and b.get("tradingStatus", TRADING_NORMAL) == TRADING_NORMAL
            and not b.get("amortizationFlag") and not b.get("perpetualFlag") and b.get("buyAvailableFlag", True) is not False
            and mat is not None and (mat - t0).days > min_days)


def turnover_rub(candles: Sequence[Dict[str, Any]], nominal: float, lot: int, days: int = TURNOVER_DAYS,
                 today: Optional[date] = None) -> float:
    """Средний дневной оборот в ₽ за последние days свечей: лоты × лот × цена (% номинала) × номинал.
    Свеча сегодняшнего дня неполная — не учитывается."""
    last = [c for c in candles if c.get("close") and not (today and parse_date(c.get("time")) == today)][-days:]
    if not last:
        return 0.0
    return sum(c["volume"] * lot * c["close"] / 100.0 * nominal for c in last) / len(last)


def pick(rows: Sequence[Dict[str, Any]], top: int = TOP_N,
         min_turnover: float = MIN_TURNOVER) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """Ликвидные выпуски: по корзинам — топ по доходности к погашению; флоатеры — топ по обороту."""
    liquid = [r for r in rows if r["turnover"] >= min_turnover]
    by: Dict[str, List[Dict[str, Any]]] = {}
    for r in liquid:
        b = bucket_of(r["duration"])
        if r["floater"] or r["ytm"] is None or b is None:
            continue
        by.setdefault(b, []).append(r)
    by = {b: sorted(v, key=lambda r: -r["ytm"])[:top] for b, v in by.items()}
    floaters = sorted((r for r in liquid if r["floater"]), key=lambda r: -r["turnover"])[:top]
    return by, floaters


def fragment_groups(weights: Dict[str, float], sleeve: float, buy: Dict[str, List[str]],
                    hold_zero: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """Облигационные группы targets.yaml: weights — доли корзин внутри облигационной части (сумма 1), sleeve — её доля
    портфеля в %, buy — тикеры для покупки по корзинам, hold_zero — держим, но не докупаем (вес 0) по группам."""
    weights = dict(weights)
    for b in list(weights):  # доля корзины без ликвидного выпуска уходит в LQDT: deploy нормирует состав внутри группы
        if b != "LQDT" and weights[b] > 0 and not buy.get(b):
            weights["LQDT"] = weights.get("LQDT", 0.0) + weights[b]
            weights[b] = 0.0
    out = []
    bought = set()
    for name, buckets in GROUP_BUCKETS:
        gw = sum(weights.get(b, 0.0) for b in buckets)
        items: Dict[str, float] = {}
        for b in buckets:
            tickers = ["LQDT"] if b == "LQDT" else list(buy.get(b, []))
            if gw > 0 and weights.get(b, 0.0) > 0 and tickers:
                for t in tickers:
                    items[t] = items.get(t, 0.0) + weights[b] / gw / len(tickers) * 100.0
        bought |= {t for t, w in items.items() if w > 0}
        out.append({"name": name, "weight": gw * sleeve, "items": items})
    for g in out:  # держим, но не докупаем; выпуск, который покупается в другой группе, второй раз не пишем
        for t in hold_zero.get(g["name"], []):
            if t not in bought:
                g["items"].setdefault(t, 0.0)
        g["items"] = [{"ticker": t, "weight": w} for t, w in g["items"].items()]
    return out


def _num(x: float) -> str:
    r = round(float(x), 1)
    return str(int(r)) if r == int(r) else str(r)


def render_groups(groups: Sequence[Dict[str, Any]]) -> str:
    lines = ["groups:"]
    for g in groups:
        lines += [f"  - name: {g['name']}", f"    weight: {_num(g['weight'])}"]
        if not g["items"]:
            lines.append("    items: []")
            continue
        lines.append("    items:")
        lines += [f"      - {{ticker: {i['ticker']}, weight: {_num(i['weight'])}}}" for i in g["items"]]
    return "\n".join(lines) + "\n"


def current_weights(groups: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, float], float]:
    """Группы targets.yaml -> (доли корзин внутри облигационной части, облигационная часть в % портфеля)."""
    w: Dict[str, float] = {}
    for g in groups:
        b = GROUP_TO_BUCKET.get(g["name"])
        if b:
            w[b] = w.get(b, 0.0) + float(g["weight"])
    sleeve = sum(w.values())
    return ({b: v / sleeve for b, v in w.items()} if sleeve > 0 else {}), sleeve


def replace_maturing(groups: Sequence[Dict[str, Any]], maturing: Dict[str, str],
                     repl: Dict[str, List[str]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Облигационные группы targets.yaml, где гасящийся выпуск всегда заканчивает с весом 0: вес переходит первому
    подходящему кандидату его корзины (repl: корзина -> тикеры по убыванию предпочтения; кандидат, который сам гасится
    или уже стоит в другой группе, пропускается), а если такого нет — уходит в LQDT группы «Денежный рынок».
    Решения по всем выпускам группы принимаются от исходных весов и применяются одним проходом, поэтому порядок строк
    на результат не влияет. Остальные группы не возвращаются."""
    bond = [g for g in groups if g["name"] in GROUP_TO_BUCKET]
    st = {g["name"]: {"weight": float(g["weight"]), "items": {i["ticker"]: float(i["weight"]) for i in g["items"]}} for g in bond}
    holder: Dict[str, str] = {}
    for g in groups:
        for i in g["items"]:
            holder.setdefault(i["ticker"], g["name"])
    notes: List[str] = []
    to_cash = 0.0
    for name, s in st.items():
        gw0, iw0 = s["weight"], sum(s["items"].values())
        moves = []  # (гасящийся тикер, исходный вес, кандидат или None, причина пропуска первого кандидата)
        for t, w in s["items"].items():
            if t not in maturing or w <= 0:
                continue
            skipped, new = [], None
            for c in repl.get(maturing[t], []):
                if c in maturing:
                    skipped.append(f"замена {c} сама гасится → ")
                elif holder.get(c, name) != name:
                    skipped.append(f"замена {c} уже в группе {holder[c]} → ")
                else:
                    new = c
                    break
            if new:
                holder[new] = name
            moves.append((t, w, new, skipped[0] if skipped else ""))
        if not moves:
            continue
        for t, w, new, _ in moves:  # гасящийся выпуск кандидатом не бывает, поэтому исходный вес w — его единственный вес
            s["items"][t] = 0.0
            if new:
                s["items"][new] = s["items"].get(new, 0.0) + w
        kept = sum(s["items"].values())
        shift = gw0 * (iw0 - kept) / iw0 if iw0 > 0 else 0.0
        if shift < 1e-9:
            shift = 0.0
        s["weight"], to_cash = gw0 - shift, to_cash + shift
        if kept > 0:  # внутри группы с весом > 0 снова 100
            s["items"] = {t: w / kept * 100.0 for t, w in s["items"].items()}
        for t, w, new, why in moves:
            head = f"{t} гасится в ближайшие 12 месяцев — {why}"
            if new:
                notes.append(head + ("взят следующий кандидат " if why else "вес " + _num(w) + " передан ") + new)
            elif shift > 0:
                notes.append(head + "доля перенесена в LQDT: нет подходящей замены")
            else:
                notes.append(head + "вес обнулён: нет подходящей замены, а группа без веса — переносить нечего")
    if to_cash > 0:
        cash = st.setdefault(GROUP_CASH, {"weight": 0.0, "items": {}})
        iw = sum(cash["items"].values())
        ab = {t: (w / iw * cash["weight"] if iw > 0 else 0.0) for t, w in cash["items"].items()}
        ab["LQDT"] = ab.get("LQDT", 0.0) + to_cash
        cash["weight"] += to_cash
        cash["items"] = {t: a / cash["weight"] * 100.0 for t, a in ab.items()}
    out = [{"name": n, "weight": s["weight"], "items": [{"ticker": t, "weight": w} for t, w in s["items"].items()]} for n, s in st.items()]
    return out, notes
