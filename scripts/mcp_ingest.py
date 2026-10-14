#!/usr/bin/env python3
"""Сборка данных из ответов официального T-Invest MCP (формат {"currency","value"}) в раскладку, которую читают отчёты.

Используется, когда данные приходят не через fetch.py (REST), а через MCP-сервер в Claude Desktop / Claude Code:
ответы MCP сохраняются в файлы, а недостающее (цены, купоны, справочник) описывается в mcp_extra.json.

  python mcp_ingest.py <папка с mcp_extra.json и raw/>  [--home ~/.tinvest]

Структура mcp_extra.json — см. пример в tests/ или в документации скилла.
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import read_json, write_json  # noqa: E402


def mv(x, cur="rub"):
    return {"currency": cur, "value": f"{x:.6f}".rstrip("0").rstrip(".") if isinstance(x, float) else str(x)}


def qv(x):
    return {"value": f"{x:.6f}".rstrip("0").rstrip(".") if isinstance(x, float) else str(x)}


def iso(d: str) -> str:
    return d if "T" in d else f"{d}T00:00:00Z"


def gen_coupons(spec: dict, as_of: date):
    """Полугодовые купоны по спецификации: от первой даты шагом period дней до погашения включительно + номинал в погашение."""
    out = []
    d = date.fromisoformat(spec["first"])
    mat = date.fromisoformat(spec["maturity"])
    period = int(spec.get("period", 182))
    while d <= mat + timedelta(days=3):
        if d >= as_of:
            out.append({"eventDate": d.isoformat(), "eventType": "EVENT_TYPE_CPN", "payOneBond": mv(spec["amount"])})
        d = d + timedelta(days=period)
    out.append({"eventDate": mat.isoformat(), "eventType": "EVENT_TYPE_MTY", "payOneBond": mv(spec.get("nominal", 1000))})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="папка с mcp_extra.json и raw/")
    ap.add_argument("--home", default=None, help="куда писать data/ (по умолчанию — та же папка)")
    args = ap.parse_args()
    src = Path(args.src).expanduser()
    home = Path(args.home).expanduser() if args.home else src
    extra = read_json(src / "mcp_extra.json")
    dd = home / "data"
    acc = extra["account"]
    acc_id = acc["id"]
    as_of = date.fromisoformat(extra["as_of"])
    write_json(dd / "accounts.json", [acc])

    held = {p[0]: p for p in extra["positions"]}
    base_to_uid = {}
    for t in held:
        base_to_uid[t.rstrip("@")] = t

    def uid_of(ticker: str) -> str:
        return base_to_uid.get(ticker.rstrip("@"), ticker.rstrip("@") if ticker.endswith("@") and ticker.rstrip("@") in base_to_uid else ticker)

    # ---- справочник ----
    instruments, bonds = {}, {}
    for t, s in extra.get("shares", {}).items():
        instruments[t] = {"uid": t, "ticker": t, "name": s["name"], "instrumentType": "share", "currency": "rub", "lot": s.get("lot", 1),
                          "sector": s.get("sector", ""), "countryOfRisk": "RU", "issuer": s.get("issuer", s["name"])}
    for t, s in extra.get("etfs", {}).items():
        instruments[t] = {"uid": t, "ticker": t, "name": s["name"], "instrumentType": "etf", "currency": "rub", "lot": s.get("lot", 1),
                          "sector": s.get("sector", ""), "countryOfRisk": "RU", "issuer": s.get("issuer", s["name"])}
    for t, b in extra.get("bonds", {}).items():
        instruments[t] = {"uid": t, "ticker": t, "name": b["name"], "instrumentType": "bond", "currency": "rub", "lot": 1,
                          "sector": b.get("sector", ""), "countryOfRisk": "RU", "issuer": b.get("issuer", b["name"])}
        bonds[t] = {"uid": t, "ticker": t, "name": b["name"], "nominal": mv(b.get("nominal", 1000)), "aciValue": mv(b.get("aci", 0)),
                    "maturityDate": iso(b["maturity"]) if b.get("maturity") else None, "couponQuantityPerYear": b.get("cpy", 2),
                    "floatingCouponFlag": bool(b.get("floater")), "amortizationFlag": bool(b.get("amort")), "perpetualFlag": False,
                    "riskLevel": "RISK_LEVEL_LOW" if t.startswith("SU") else ("RISK_LEVEL_HIGH" if b.get("defaulted") else "RISK_LEVEL_MODERATE"),
                    "defaulted": bool(b.get("defaulted"))}
    # эмитент — через «актив» с брендом, как это делает model.issuer()
    assets = {}
    for t, inst in instruments.items():
        a_uid = f"asset-{t}"
        inst["assetUid"] = a_uid
        assets[a_uid] = {"uid": a_uid, "name": inst["issuer"], "brands": [{"company": inst["issuer"]}]}
    write_json(dd / "cache" / "instruments.json", instruments)
    write_json(dd / "cache" / "bonds.json", bonds)
    write_json(dd / "cache" / "assets.json", assets)
    write_json(dd / "cache" / "splits.json", extra.get("splits", {}))

    # ---- портфель в формате API ----
    positions = []
    for t, cls, qty, avg, exp in extra["positions"]:
        inst = instruments.get(t)
        if not inst:
            print(f"⚠ нет справочника для {t} — пропускаю", file=sys.stderr)
            continue
        itype = inst["instrumentType"]
        mv_price = extra.get("market_values", {}).get(t)
        binfo = extra["bonds"].get(t, {}) if itype == "bond" else {}
        if mv_price is not None and not binfo.get("defaulted"):
            price_abs = float(mv_price) / 100.0 * float(binfo.get("nominal", 1000)) if itype == "bond" else float(mv_price)
        else:
            price_abs = (qty * avg + exp) / qty if qty else 0.0   # как оценивает брокер: средняя FIFO + результат
        nkd = float(binfo.get("aci", 0)) if itype == "bond" else 0.0
        positions.append({"instrumentUid": t, "instrumentType": itype, "quantity": qv(float(qty)), "averagePositionPrice": mv(avg),
                          "currentPrice": mv(price_abs), "currentNkd": mv(nkd), "expectedYield": qv(float(exp)), "blocked": False,
                          "ticker": t, "classCode": cls})
    tot = extra["totals"]
    portfolio = {"totalAmountShares": mv(tot["shares"]), "totalAmountBonds": mv(tot["bonds"]), "totalAmountEtf": mv(tot["etf"]),
                 "totalAmountCurrencies": mv(tot["currencies"]), "totalAmountFutures": mv(tot.get("futures", 0)),
                 "totalAmountPortfolio": mv(tot["portfolio"]), "expectedYield": qv(float(tot.get("expectedYieldPct", 0))), "positions": positions}
    write_json(dd / acc_id / "portfolio.json", portfolio)
    write_json(dd / acc_id / "positions.json", {"money": [mv(extra["cash_rub"])], "securities": [], "blocked": []})

    # ---- операции ----
    ops_src = read_json(src / "raw" / "operations_mcp.json", {}) or {}
    ops = []
    for o in ops_src.get("items", []):
        o = dict(o)
        if o.get("ticker"):
            o["instrumentUid"] = uid_of(o["ticker"])
        ops.append(o)
    write_json(dd / acc_id / "operations.json", ops)

    # ---- купоны/события, дивиденды ----
    events = {}
    for t, spec in extra.get("coupon_specs", {}).items():
        events[t] = gen_coupons(spec, as_of)
    for t, lst in extra.get("explicit_events", {}).items():
        events[t] = [{"eventDate": d, "eventType": f"EVENT_TYPE_{k}", "payOneBond": mv(a)} for d, k, a in lst]
    write_json(dd / acc_id / "bond_events.json", events)
    write_json(dd / acc_id / "bond_coupons.json", {})
    divs = {}
    for t, lst in extra.get("dividends", {}).items():
        divs[t] = [{"dividendNet": mv(x["dividendNet"]), "recordDate": iso(x["recordDate"]), "lastBuyDate": iso(x.get("lastBuyDate", x["recordDate"])),
                    "paymentDate": iso(x.get("paymentDate", x["recordDate"]))} for x in lst]
    write_json(dd / acc_id / "dividends.json", divs)

    # ---- цены, курсы, бенчмарки ----
    lp = {}
    for t, v in extra.get("market_values", {}).items():
        lp[t] = float(v)
    write_json(dd / "last_prices.json", lp)
    write_json(dd / "fx.json", {"rub": 1.0})
    write_json(dd / "targets_instruments.json", {t: dict(instruments[t], uid=t) for t in instruments if t in lp})
    for t, rel in extra.get("benchmarks", {}).items():
        c = read_json(src / rel, {}) or {}
        candles = [{"time": x.get("time"), "close": float(x["close"]["value"]), "open": float(x["open"]["value"])} for x in c.get("candles", [])
                   if x.get("close", {}).get("value")]
        write_json(dd / "benchmarks" / f"{t.upper()}.json", {"instrument": instruments.get(t, {"ticker": t, "name": t}), "candles": candles})

    write_json(dd / "meta.json", {"fetched_at": datetime.now(timezone.utc).isoformat(), "base": "mcp", "sandbox": False,
                                  "accounts": [{"id": acc_id, "name": acc.get("name"), "type": acc.get("type"),
                                                "first_operation": min((o.get("date", "")[:10] for o in ops), default=None)}]})
    print(f"✓ данные собраны: {dd} · позиций {len(positions)} · операций {len(ops)} · облигаций с графиком {len(events)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
