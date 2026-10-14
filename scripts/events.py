"""Календарь на N дней: оферты, погашения, купоны, дивиденды по бумагам в портфеле."""
from datetime import timedelta
from typing import Any, Dict, List, Tuple

from bonds import EVENT_RU
from common import fmt_money, md_table, money, parse_date, today
from model import Store


def collect(store: Store, acc_id: str, days: int = 60) -> List[Dict[str, Any]]:
    t0 = today()
    t1 = t0 + timedelta(days=days)
    pos = {p["uid"]: p for p in store.valued_positions(acc_id)}
    items: List[Dict[str, Any]] = []

    bevents = store.bond_events(acc_id)
    coupons = store.coupons(acc_id)
    for uid, p in pos.items():
        if p["type"] != "bond":
            continue
        rate = store.rate(p["expo_currency"]) or 1.0
        seen = set()
        for e in bevents.get(uid, []) or []:
            d = parse_date(e.get("eventDate"))
            et = e.get("eventType", "")
            if not d or not (t0 <= d <= t1):
                continue
            per = money(e.get("payOneBond"))
            key = (d, et)
            seen.add(key)
            items.append({"date": d, "kind": EVENT_RU.get(et, et), "ticker": p["ticker"], "name": p["name"],
                          "per_unit": per, "total_rub": per * p["qty"] * rate if per else None,
                          "note": (e.get("note") or "")[:80], "severity": 2 if et == "EVENT_TYPE_CALL" else (1 if et == "EVENT_TYPE_MTY" else 0)})
        for c in coupons.get(uid, []) or []:  # если событий нет — купоны из отдельного метода
            d = parse_date(c.get("couponDate"))
            if d and t0 <= d <= t1 and (d, "EVENT_TYPE_CPN") not in seen:
                per = money(c.get("payOneBond"))
                items.append({"date": d, "kind": "купон", "ticker": p["ticker"], "name": p["name"], "per_unit": per,
                              "total_rub": per * p["qty"] * rate, "note": "", "severity": 0})
        b = store.bonds.get(uid, {})
        mat = parse_date(b.get("maturityDate"))
        if mat and t0 <= mat <= t1 and (mat, "EVENT_TYPE_MTY") not in seen:
            nom, _ = store.bond_nominal(uid)
            items.append({"date": mat, "kind": "погашение", "ticker": p["ticker"], "name": p["name"], "per_unit": nom,
                          "total_rub": nom * p["qty"] * rate, "note": "", "severity": 1})
        cd = parse_date(b.get("callDate"))
        if cd and t0 <= cd <= t1 and (cd, "EVENT_TYPE_CALL") not in seen:
            items.append({"date": cd, "kind": "оферта", "ticker": p["ticker"], "name": p["name"], "per_unit": None,
                          "total_rub": None, "note": "из справочника (callDate)", "severity": 2})

    divs = store.dividends(acc_id)
    for uid, p in pos.items():
        if p["type"] not in ("share", "etf"):
            continue
        rate = store.rate(p["price_currency"]) or 1.0
        for dv in divs.get(uid, []) or []:
            rec = parse_date(dv.get("recordDate"))
            last_buy = parse_date(dv.get("lastBuyDate"))
            pay = parse_date(dv.get("paymentDate"))
            d = rec or pay
            if not d or not (t0 - timedelta(days=3) <= d <= t1):
                continue
            per = money(dv.get("dividendNet"))
            note = []
            if last_buy:
                note.append(f"последний день покупки {last_buy.isoformat()}")
            if pay:
                note.append(f"выплата {pay.isoformat()}")
            items.append({"date": d, "kind": "дивиденд (отсечка)", "ticker": p["ticker"], "name": p["name"], "per_unit": per,
                          "total_rub": per * p["qty"] * rate if per else None, "note": ", ".join(note), "severity": 1})
    items.sort(key=lambda x: (x["date"], -x["severity"]))
    return items


def build(store: Store, acc_id: str, days: int = 60) -> Tuple[str, List[Dict[str, Any]]]:
    acc = store.account(acc_id)
    items = collect(store, acc_id, days)
    out = [f"## Календарь на {days} дней · {acc.get('name') or acc_id}"]
    calls = [i for i in items if i["kind"] == "оферта"]
    if calls:
        c = calls[0]
        out.append(f"**Ближайшая оферта: {c['ticker']} {c['date'].isoformat()} (через {(c['date'] - today()).days} дн.).** "
                   "Поручение по оферте подаётся брокеру заранее — проверь в приложении срок приёма заявок; "
                   "без поручения бумага остаётся на руках с новым купоном.")
    else:
        out.append("Оферт в окне нет.")
    if not items:
        out.append("Событий по бумагам портфеля не найдено.")
        return "\n".join(out), items
    rows = [[i["date"].isoformat(), i["kind"], i["ticker"], i["name"][:30],
             fmt_money(i["per_unit"], digits=2) if i["per_unit"] else "—", fmt_money(i["total_rub"]) if i["total_rub"] else "—", i["note"]]
            for i in items]
    out.append(md_table(["Дата", "Событие", "Тикер", "Название", "На 1 шт.", "Всего ≈", "Примечание"], rows))
    total_in = sum(i["total_rub"] or 0 for i in items if i["kind"] in ("купон", "погашение", "дивиденд (отсечка)"))
    out.append(f"\nОжидаемые поступления за период ≈ {fmt_money(total_in)} до налога.")
    return "\n".join(out), items
