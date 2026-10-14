"""Облигации в портфеле: доходность к погашению/оферте, дюрация, ближайшие события."""
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from common import fmt_money, fmt_pct, md_table, money, parse_date, today
from finmath import macaulay_duration, modified_duration, ytm
from model import Store

EVENT_RU = {"EVENT_TYPE_CPN": "купон", "EVENT_TYPE_CALL": "оферта", "EVENT_TYPE_MTY": "погашение", "EVENT_TYPE_CONV": "конвертация"}
RISK_RU = {"RISK_LEVEL_LOW": "низкий", "RISK_LEVEL_MODERATE": "средний", "RISK_LEVEL_HIGH": "высокий", "RISK_LEVEL_UNSPECIFIED": "—"}


def coupon_schedule(store: Store, acc_id: str, uid: str) -> List[Tuple[date, float]]:
    """Будущие купоны (дата, сумма на 1 бумагу в валюте номинала) из GetBondCoupons, запасной путь — GetBondEvents."""
    out = []
    for c in store.coupons(acc_id).get(uid, []) or []:
        d = parse_date(c.get("couponDate") or c.get("eventDate"))
        amt = money(c.get("payOneBond"))
        if d and d >= today():
            out.append((d, amt))
    if not out:
        for e in store.bond_events(acc_id).get(uid, []) or []:
            if e.get("eventType") == "EVENT_TYPE_CPN":
                d = parse_date(e.get("eventDate"))
                if d and d >= today():
                    out.append((d, money(e.get("payOneBond"))))
    return sorted(out)


def principal_schedule(store: Store, acc_id: str, uid: str) -> List[Tuple[date, float]]:
    """Будущие выплаты номинала (погашение и амортизация) из GetBondEvents (EVENT_TYPE_MTY)."""
    out = []
    for e in store.bond_events(acc_id).get(uid, []) or []:
        if e.get("eventType") == "EVENT_TYPE_MTY":
            d = parse_date(e.get("eventDate"))
            if d and d >= today():
                out.append((d, money(e.get("payOneBond"))))
    return sorted(out)


def next_call(store: Store, acc_id: str, uid: str) -> Optional[date]:
    """Ближайшая оферта: из GetBondEvents (CALL) или callDate справочника."""
    dates = []
    for e in store.bond_events(acc_id).get(uid, []) or []:
        if e.get("eventType") == "EVENT_TYPE_CALL":
            d = parse_date(e.get("eventDate"))
            if d and d >= today():
                dates.append(d)
    cd = parse_date(store.bonds.get(uid, {}).get("callDate"))
    if cd and cd >= today():
        dates.append(cd)
    return min(dates) if dates else None


def analyze_bond(store: Store, acc_id: str, pos: Dict[str, Any]) -> Dict[str, Any]:
    uid = pos["uid"]
    b = store.bonds.get(uid, {})
    nom, ncur = store.bond_nominal(uid)
    maturity = parse_date(b.get("maturityDate"))
    floater = bool(b.get("floatingCouponFlag"))
    amort = bool(b.get("amortizationFlag"))
    perpetual = bool(b.get("perpetualFlag"))
    call = next_call(store, acc_id, uid)
    coupons = coupon_schedule(store, acc_id, uid)
    t0 = today()
    dirty = pos["price"] + pos["nkd"]  # грязная цена за 1 бумагу в валюте
    res: Dict[str, Any] = {
        "uid": uid, "ticker": pos["ticker"], "name": pos["name"], "value_rub": pos["value_rub"], "currency": ncur,
        "nominal": nom, "maturity": maturity, "call": call, "floater": floater, "amortization": amort, "perpetual": perpetual,
        "risk": RISK_RU.get(b.get("riskLevel") or "", "—"), "ofz": (pos["ticker"] or "").startswith("SU") or pos.get("sector") == "government",
        "ytm": None, "ytc": None, "duration_mac": None, "duration_mod": None, "next_coupon": coupons[0] if coupons else None,
        "notes": [],
    }
    if floater:
        res["notes"].append("флоатер: доходность зависит от ставки, дюрация ≈ срок до ближайшего купона")
        if coupons:
            res["duration_mod"] = (coupons[0][0] - t0).days / 365.0
        return res
    if perpetual:
        res["notes"].append("бессрочная: доходность только к оферте")
    if amort and not principal_schedule(store, acc_id, uid):
        res["notes"].append("амортизация: расчёт приближённый (номинал к погашению взят целиком)")
    if not coupons and not maturity:
        res["notes"].append("нет графика купонов — доходность не посчитана")
        return res

    principal = principal_schedule(store, acc_id, uid)

    def cashflows(end: Optional[date]) -> List[Tuple[float, float]]:
        cfs = [((d - t0).days / 365.0, a) for d, a in coupons if (end is None or d <= end)]
        if principal and (end is None or end == maturity):
            cfs += [((d - t0).days / 365.0, a) for d, a in principal if (end is None or d <= end)]
        elif end:
            cfs.append(((end - t0).days / 365.0, nom))
        return cfs

    if maturity and not perpetual and dirty > 0:
        cfs = cashflows(maturity)
        y = ytm(dirty, cfs)
        res["ytm"] = y
        if y is not None:
            mac = macaulay_duration(cfs, y)
            res["duration_mac"] = mac
            res["duration_mod"] = modified_duration(mac, y)
    if call and dirty > 0:
        res["ytc"] = ytm(dirty, cashflows(call))
    return res


def build(store: Store, acc_id: str) -> Tuple[str, List[Dict[str, Any]]]:
    pos = [p for p in store.valued_positions(acc_id) if p["type"] == "bond"]
    if not pos:
        return "", []
    rows_data = [analyze_bond(store, acc_id, p) for p in pos]
    total = sum(r["value_rub"] or 0 for r in rows_data) or 1.0
    rows = []
    for r in sorted(rows_data, key=lambda x: -(x["value_rub"] or 0)):
        rows.append([
            r["ticker"], r["name"][:34], fmt_money(r["value_rub"]), f"{(r['value_rub'] or 0) / total * 100:.0f}%",
            fmt_pct(r["ytm"]) if r["ytm"] is not None else ("флоатер" if r["floater"] else "—"),
            fmt_pct(r["ytc"]) if r["ytc"] is not None else "—",
            f"{r['duration_mod']:.1f}" if r["duration_mod"] is not None else "—",
            r["call"].isoformat() if r["call"] else "—",
            r["maturity"].isoformat() if r["maturity"] else ("бессрочная" if r["perpetual"] else "—"),
            r["risk"],
        ])
    out = ["### Облигации", md_table(["Тикер", "Название", "Оценка", "Доля обл.", "YTM", "К оферте", "Дюр., лет", "Оферта", "Погашение", "Риск"], rows)]
    fixed = [r for r in rows_data if r["duration_mod"] is not None and not r["floater"] and r["value_rub"]]
    if fixed:
        w = sum(r["value_rub"] for r in fixed)
        dur = sum(r["duration_mod"] * r["value_rub"] for r in fixed) / w
        y = sum((r["ytm"] or 0) * r["value_rub"] for r in fixed if r["ytm"] is not None) / w
        out.append(f"\nФиксированный купон: средняя модифицированная дюрация **{dur:.1f}** (рост ставок на 1 п.п. ≈ −{dur:.1f}% цены), средняя YTM {fmt_pct(y)} до налога.")
    fl = sum(r["value_rub"] or 0 for r in rows_data if r["floater"])
    ofz = sum(r["value_rub"] or 0 for r in rows_data if r["ofz"])
    out.append(f"Флоатеры: {fl / total * 100:.0f}% облигационной части · ОФЗ: {ofz / total * 100:.0f}%.")
    notes = sorted({f"{r['ticker']}: {n}" for r in rows_data for n in r["notes"]})
    if notes:
        out.append("\n" + "\n".join(f"- {n}" for n in notes[:12]))
    return "\n".join(out), rows_data
