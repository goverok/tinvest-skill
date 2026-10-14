"""Карта рисков счёта: структура, концентрация, валюты, сверка с итогами брокера."""
from typing import Any, Dict, List, Tuple

import bonds as bonds_mod
from common import fmt_money, fmt_pct, md_table
from model import TYPE_RU, Store

SECTOR_RU = {
    "financial": "финансы", "energy": "нефть и газ", "materials": "материалы", "industrials": "промышленность",
    "consumer": "потребительский", "health_care": "здравоохранение", "it": "технологии", "telecom": "телеком",
    "utilities": "энергетика/ЖКХ", "real_estate": "недвижимость", "government": "государство (ОФЗ)", "municipal": "муниципальные",
    "other": "прочее", "": "не указан", "gold": "золото (фонд)", "equity_index": "фонд на индекс акций",
    "money_market": "денежный рынок (фонд)", "corporate": "корпоративные облигации",
}


def _share(x: float, total: float) -> str:
    return f"{x / total * 100:.1f}%" if total else "—"


def build(store: Store, acc_id: str) -> Tuple[str, Dict[str, Any]]:
    acc = store.account(acc_id)
    pos = store.valued_positions(acc_id)
    cash = store.cash(acc_id)
    totals = store.totals(acc_id)
    cash_rub = sum(c[2] or 0 for c in cash)
    total = sum(p["value_rub"] or 0 for p in pos) + cash_rub
    out: List[str] = [f"## Аудит · {acc.get('name') or acc_id} ({'ИИС' if store.is_iis(acc_id) else 'брокерский'})"]
    if not pos and not cash_rub:
        out.append("Позиций нет.")
        return "\n".join(out), {}
    out.append(f"Оценка **{fmt_money(total)}** (брокер: {fmt_money(totals['portfolio'])}), "
               f"позиций {len(pos)}, деньги {fmt_money(cash_rub)} ({_share(cash_rub, total)}).")

    # сверка с итогами брокера по классам
    by_type: Dict[str, float] = {}
    for p in pos:
        by_type[p["type"]] = by_type.get(p["type"], 0.0) + (p["value_rub"] or 0)
    checks = []
    for key, name in (("share", "shares"), ("bond", "bonds"), ("etf", "etf")):
        mine, broker = by_type.get(key, 0.0), totals.get(name, 0.0)
        if broker or mine:
            diff = (mine - broker) / broker if broker else 1.0
            flag = "" if abs(diff) <= 0.02 else " ⚠"
            checks.append([TYPE_RU[key], fmt_money(mine), fmt_money(broker), f"{diff * 100:+.1f}%{flag}"])
    if checks:
        out.append("\nСверка с итогами брокера (расхождение > 2% = проверить курсы/цены):\n" + md_table(["Класс", "Мой расчёт", "Брокер", "Δ"], checks))

    # структура по классам
    rows = [[TYPE_RU.get(k, k), fmt_money(v), _share(v, total)] for k, v in sorted(by_type.items(), key=lambda x: -x[1])]
    rows.append(["деньги", fmt_money(cash_rub), _share(cash_rub, total)])
    out.append("\n### Структура\n" + md_table(["Класс", "Оценка", "Доля"], rows))

    # топ позиций
    top = sorted(pos, key=lambda p: -(p["value_rub"] or 0))
    top5 = sum(p["value_rub"] or 0 for p in top[:5])
    rows = [[p["ticker"], p["name"][:36], p["type_ru"], fmt_money(p["value_rub"]), _share(p["value_rub"] or 0, total),
             fmt_pct(p["expected_yield"] / (p["value_rub"] - p["expected_yield"]) if p["value_rub"] and p["value_rub"] != p["expected_yield"] else None)]
            for p in top[:15]]
    out.append(f"\n### Крупнейшие позиции (топ-5 = {_share(top5, total)} портфеля)\n" + md_table(["Тикер", "Название", "Класс", "Оценка", "Доля", "P/L%"], rows))

    # эмитенты
    by_issuer: Dict[str, float] = {}
    for p in pos:
        if p["type"] in ("share", "bond"):
            by_issuer[p["issuer"]] = by_issuer.get(p["issuer"], 0.0) + (p["value_rub"] or 0)
    if by_issuer:
        iss = sorted(by_issuer.items(), key=lambda x: -x[1])
        big = [f"{k} {_share(v, total)}" for k, v in iss[:5]]
        flag = " ⚠ один эмитент больше 25% портфеля" if iss and iss[0][1] / total > 0.25 else ""
        out.append(f"\n### Эмитенты (акции + облигации)\nКрупнейшие: {', '.join(big)}.{flag}")

    # секторы, страны, валюты
    by_sector: Dict[str, float] = {}
    by_country: Dict[str, float] = {}
    by_cur: Dict[str, float] = {}
    for p in pos:
        v = p["value_rub"] or 0
        by_sector[SECTOR_RU.get(p["sector"], p["sector"])] = by_sector.get(SECTOR_RU.get(p["sector"], p["sector"]), 0.0) + v
        by_country[p["country"] or "—"] = by_country.get(p["country"] or "—", 0.0) + v
        by_cur[p["expo_currency"]] = by_cur.get(p["expo_currency"], 0.0) + v
    for c, amt, rub in cash:
        by_cur[c] = by_cur.get(c, 0.0) + (rub or 0)
    sec_rows = [[k, fmt_money(v), _share(v, total)] for k, v in sorted(by_sector.items(), key=lambda x: -x[1])[:12] if v > 0]
    cur_rows = [[k.upper(), fmt_money(v), _share(v, total)] for k, v in sorted(by_cur.items(), key=lambda x: -x[1])]
    out.append("\n### Секторы\n" + md_table(["Сектор", "Оценка", "Доля"], sec_rows))
    out.append("\n### Валютная экспозиция\n" + md_table(["Валюта", "Оценка в ₽", "Доля"], cur_rows))
    non_ru = sum(v for k, v in by_country.items() if k not in ("RU", "—"))
    if non_ru:
        out.append(f"Бумаги с иностранной страной риска: {_share(non_ru, total)} (проверь блокировки и ЛДВ).")

    # облигации
    btext, bdata = bonds_mod.build(store, acc_id)
    if btext:
        out.append("\n" + btext)

    blocked = [p["ticker"] for p in pos if p["blocked"]]
    if blocked:
        out.append(f"\nЗаблокированные позиции: {', '.join(blocked)}.")
    if store.warnings:
        out.append("\nЗамечания:\n" + "\n".join(f"- {w}" for w in sorted(set(store.warnings))[:10]))
    return "\n".join(out), {"total": total, "positions": pos, "top5_share": top5 / total if total else 0,
                            "issuers": by_issuer, "by_currency": by_cur, "bonds": bdata}
