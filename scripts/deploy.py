"""План размещения новых денег по целевым долям (без продаж): targets.yaml + текущий портфель -> список покупок в лотах."""
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import fmt_money, md_table
from finmath import allocate
from model import Store


def load_targets(path: Path) -> Dict[str, Any]:
    import yaml  # type: ignore
    cfg = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8")) or {}
    groups = cfg.get("groups") or []
    if not groups:
        raise ValueError(f"В {path} нет groups")
    for g in groups:
        g["name"] = str(g.get("name") or "группа")
        g["weight"] = float(g.get("weight") or 0)
        g["items"] = [{"ticker": str(i.get("ticker", "")).strip().upper(), "weight": float(i.get("weight") or 0)} for i in (g.get("items") or [])]
    cfg["groups"] = groups
    cfg["cash_reserve"] = float(cfg.get("cash_reserve") or 0)
    return cfg


def instrument_price(store: Store, ticker: str, held: Dict[str, Dict[str, Any]]) -> Tuple[Optional[float], int, str, Optional[str]]:
    """(цена 1 шт. в ₽, лот, название, uid) по тикеру — из портфеля или из справочника целевых бумаг."""
    if ticker in held:
        p = held[ticker]
        r = store.rate(p["price_currency"]) or 1.0
        return p["price"] * r + (p["nkd"] * r if p["type"] == "bond" else 0.0), p["lot"], p["name"], p["uid"]
    inst = store.targets_instruments.get(ticker)
    if not inst:
        for u, i in store.instruments.items():
            if (i.get("ticker") or "").upper() == ticker:
                inst = dict(i, uid=u)
                break
    if not inst:
        return None, 1, ticker, None
    uid = inst.get("uid")
    lp = store.last_prices.get(uid or "")
    if lp is None:
        return None, int(inst.get("lot") or 1), inst.get("name") or ticker, uid
    cur = (inst.get("currency") or "rub").lower()
    r = store.rate(cur) or 1.0
    if (inst.get("instrumentType") or "").lower() == "bond":
        nom, ncur = store.bond_nominal(uid or "")
        return lp / 100.0 * nom * (store.rate(ncur) or 1.0), 1, inst.get("name") or ticker, uid
    return lp * r, int(inst.get("lot") or 1), inst.get("name") or ticker, uid


def build(store: Store, acc_id: str, amount: float, targets_path: Path) -> Tuple[str, Dict[str, Any]]:
    cfg = load_targets(targets_path)
    acc = store.account(acc_id)
    pos = store.valued_positions(acc_id)
    held = {p["ticker"].upper(): p for p in pos}
    cash_rub = sum(c[2] or 0 for c in store.cash(acc_id))
    total_now = sum(p["value_rub"] or 0 for p in pos) + cash_rub
    invest = max(0.0, amount - cfg["cash_reserve"])
    out: List[str] = [f"## Размещение {fmt_money(amount)} · {acc.get('name') or acc_id}"]

    groups = cfg["groups"]
    gw = sum(g["weight"] for g in groups) or 1.0
    g_targets = {g["name"]: g["weight"] / gw for g in groups}
    g_current: Dict[str, float] = {}
    assigned = set()
    for g in groups:
        v = 0.0
        for it in g["items"]:
            p = held.get(it["ticker"])
            if p:
                v += p["value_rub"] or 0
                assigned.add(it["ticker"])
        g_current[g["name"]] = v
    other = sum((p["value_rub"] or 0) for t, p in held.items() if t not in assigned)
    g_current["Прочее (вне целей)"] = other + cash_rub
    g_targets_full = dict(g_targets)
    g_targets_full["Прочее (вне целей)"] = 0.0

    g_alloc = allocate(invest, g_current, g_targets_full)
    rows: List[List[Any]] = []
    plan: List[Dict[str, Any]] = []
    spent = 0.0
    missing: List[str] = []
    for g in groups:
        g_amt = g_alloc.get(g["name"], 0.0)
        iw = sum(i["weight"] for i in g["items"]) or 1.0
        i_targets = {i["ticker"]: i["weight"] / iw for i in g["items"]}
        i_current = {i["ticker"]: (held[i["ticker"]]["value_rub"] or 0) if i["ticker"] in held else 0.0 for i in g["items"]}
        i_alloc = allocate(g_amt, i_current, i_targets)
        for ticker, a in i_alloc.items():
            price, lot, name, uid = instrument_price(store, ticker, held)
            if not price:
                missing.append(ticker)
                continue
            lot_cost = price * lot
            lots = int(math.floor(a / lot_cost)) if lot_cost > 0 else 0
            cost = lots * lot_cost
            spent += cost
            plan.append({"group": g["name"], "ticker": ticker, "name": name, "lots": lots, "units": lots * lot, "lot": lot,
                         "price": price, "cost": cost, "want": a, "uid": uid})
    # остаток — докупаем по одному лоту туда, где недобор больше всего
    rest = invest - spent
    for _ in range(200):
        best = None
        for p in plan:
            short = p["want"] - p["cost"]
            lot_cost = p["price"] * p["lot"]
            if lot_cost <= rest and short > 0 and (best is None or short > best[0]):
                best = (short, p)
        if not best:
            break
        p = best[1]
        p["lots"] += 1
        p["units"] += p["lot"]
        p["cost"] += p["price"] * p["lot"]
        rest -= p["price"] * p["lot"]
        spent += p["price"] * p["lot"]

    total_after = total_now + amount
    for p in plan:
        after = ((held[p["ticker"]]["value_rub"] or 0) if p["ticker"] in held else 0.0) + p["cost"]
        rows.append([p["group"], p["ticker"], p["name"][:30], p["lots"], f"{p['units']:g}", fmt_money(p["price"], digits=2),
                     fmt_money(p["cost"]), f"{after / total_after * 100:.1f}%"])
    rows = [r for r in rows if r[3] > 0] or rows
    out.append(f"Сейчас портфель {fmt_money(total_now)}, после взноса {fmt_money(total_after)}. К размещению {fmt_money(invest)}"
               + (f" (резерв {fmt_money(cfg['cash_reserve'])})" if cfg["cash_reserve"] else "") + ".")
    out.append("\n" + md_table(["Группа", "Тикер", "Название", "Лотов", "Шт.", "Цена", "Сумма", "Доля после"], rows))
    out.append(f"\nИтого покупок {fmt_money(spent)}, остаётся деньгами {fmt_money(amount - spent)}.")
    g_rows = []
    for g in groups:
        cur = g_current[g["name"]]
        add = sum(p["cost"] for p in plan if p["group"] == g["name"])
        g_rows.append([g["name"], f"{g['weight'] / gw * 100:.0f}%", f"{cur / total_now * 100:.1f}%" if total_now else "—",
                       f"{(cur + add) / total_after * 100:.1f}%", fmt_money(add)])
    g_rows.append(["Прочее (вне целей) + деньги", "0%", f"{g_current['Прочее (вне целей)'] / total_now * 100:.1f}%" if total_now else "—",
                   f"{(g_current['Прочее (вне целей)'] + amount - spent) / total_after * 100:.1f}%", "—"])
    out.append("\n### Доли по группам\n" + md_table(["Группа", "Цель", "Сейчас", "После", "Покупки"], g_rows))
    if missing:
        out.append(f"\nНет цены для: {', '.join(missing)} — запусти `fetch.py --targets ~/.tinvest/targets.yaml`, чтобы подтянуть эти бумаги.")
    out.append("\nЭто план, а не заявки: цены — последние известные, облигации — с НКД, комиссии не учтены. Выставлять заявки — вручную или на ступени 3 с подтверждением.")
    return "\n".join(out), {"plan": plan, "spent": spent, "rest": amount - spent}
