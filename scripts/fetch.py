#!/usr/bin/env python3
"""Выгрузка всего нужного из T-Invest API в ~/.tinvest/data (сырые JSON, как отдал API).

Запуск:
  python fetch.py --check              # только проверить токен: счета и уровень доступа
  python fetch.py                      # полная выгрузка по всем открытым счетам
  python fetch.py --sandbox            # то же для песочницы (TINVEST_SANDBOX_TOKEN, ~/.tinvest/data-sandbox)
  python fetch.py --targets ~/.tinvest/targets.yaml   # дополнительно подтянуть бумаги из целевого портфеля

Ничего не покупает и не продаёт: только чтение. Токен нигде не печатает.
"""
import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (data_dir, load_env, parse_date, read_json, today, warn, write_json)  # noqa: E402
from tinvest_api import TInvest, TInvestError, client_from_env  # noqa: E402

DEFAULT_BENCH = "LQDT,SBMX"
# код валюты -> тикеры-кандидаты для курса к рублю
FX_TICKERS = {
    "usd": ["USD000UTSTOM", "USDRUB", "USDRUBF"],
    "eur": ["EUR_RUB__TOM", "EURRUB"],
    "cny": ["CNYRUB_TOM", "CNYRUB"],
    "hkd": ["HKDRUB_TOM", "HKDRUB"],
    "kzt": ["KZTRUB_TOM"],
    "try": ["TRYRUB_TOM"],
    "byn": ["BYNRUB_TOM"],
    "chf": ["CHFRUB_TOM"],
    "gbp": ["GBPRUB_TOM"],
}
ACCESS_READ = {"ACCOUNT_ACCESS_LEVEL_FULL_ACCESS", "ACCOUNT_ACCESS_LEVEL_READ_ONLY"}


def pick_exact(instruments: List[Dict[str, Any]], ticker: str, kinds: Optional[Set[str]] = None) -> Optional[Dict[str, Any]]:
    cands = [i for i in instruments if (i.get("ticker") or "").upper() == ticker.upper()]
    if kinds:
        cands = [i for i in cands if (i.get("instrumentType") or "").lower() in kinds] or cands
    pref = ["TQTF", "TQBR", "TQOB", "TQCB", "CETS", "SPBXM"]
    cands.sort(key=lambda i: pref.index(i.get("classCode")) if i.get("classCode") in pref else 99)
    return cands[0] if cands else None


def fetch_accounts(api: TInvest, dd: Path) -> List[Dict[str, Any]]:
    accounts = api.get_accounts()
    write_json(dd / "accounts.json", accounts)
    return accounts


def selected_accounts(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    wanted = {a.strip() for a in os.environ.get("TINVEST_ACCOUNT_ID", "").split(",") if a.strip()}
    out = []
    for a in accounts:
        if wanted and a.get("id") not in wanted:
            continue
        if not wanted and a.get("status") not in (None, "ACCOUNT_STATUS_OPEN"):
            continue
        if a.get("accessLevel") and a.get("accessLevel") not in ACCESS_READ:
            warn(f"Счёт {a.get('id')} ({a.get('name')}): нет доступа у токена ({a.get('accessLevel')}) — пропускаю")
            continue
        out.append(a)
    return out


def fetch_account(api: TInvest, dd: Path, acc: Dict[str, Any], cache: Dict[str, Dict[str, Any]], uids_all: Set[str]) -> Dict[str, Any]:
    acc_id = acc["id"]
    adir = dd / acc_id
    adir.mkdir(parents=True, exist_ok=True)

    portfolio = api.get_portfolio(acc_id, "RUB")
    write_json(adir / "portfolio.json", portfolio)
    positions = api.get_positions(acc_id)
    write_json(adir / "positions.json", positions)

    opened = parse_date(acc.get("openedDate")) or date(2015, 1, 1)
    ops = list(api.iter_operations(acc_id, opened - timedelta(days=1), datetime.now(timezone.utc) + timedelta(days=1)))
    write_json(adir / "operations.json", ops)

    uids: Set[str] = set()
    for p in portfolio.get("positions", []):
        if p.get("instrumentUid"):
            uids.add(p["instrumentUid"])
    for o in ops:
        if o.get("instrumentUid"):
            uids.add(o["instrumentUid"])
    uids_all |= uids

    # справочник по бумагам счёта
    instruments = cache["instruments"]
    bonds = cache["bonds"]
    assets = cache["assets"]
    for uid in sorted(uids):
        if uid not in instruments:
            try:
                instruments[uid] = api.get_instrument(uid)
            except TInvestError as e:
                warn(f"GetInstrumentBy {uid}: {e}")
                instruments[uid] = {"uid": uid, "name": "?", "instrumentType": "?"}
        inst = instruments[uid]
        itype = (inst.get("instrumentType") or "").lower()
        if itype == "bond" and uid not in bonds:
            try:
                bonds[uid] = api.get_bond(uid)
                for k in ("sector", "countryOfRisk", "issuer", "riskLevel"):
                    if bonds[uid].get(k) and not inst.get(k):
                        inst[k] = bonds[uid][k]
            except TInvestError as e:
                warn(f"BondBy {uid}: {e}")
                bonds[uid] = {}
        if itype == "share" and not inst.get("sector"):
            try:
                sh = api.get_share(uid)
                for k in ("sector", "countryOfRisk", "issueSize", "shareType"):
                    if sh.get(k):
                        inst[k] = sh[k]
            except TInvestError as e:
                warn(f"ShareBy {uid}: {e}")
        a_uid = inst.get("assetUid")
        if a_uid and a_uid not in assets:
            try:
                assets[a_uid] = api.get_asset(a_uid)
            except TInvestError as e:
                assets[a_uid] = {"uid": a_uid, "name": inst.get("name"), "_error": str(e)}

    # купоны, события, дивиденды — только по текущим позициям
    held = [p["instrumentUid"] for p in portfolio.get("positions", []) if p.get("instrumentUid")]
    t0 = today()
    coupons: Dict[str, Any] = {}
    events: Dict[str, Any] = {}
    dividends: Dict[str, Any] = {}
    for uid in held:
        inst = instruments.get(uid, {})
        itype = (inst.get("instrumentType") or "").lower()
        if itype == "bond":
            b = bonds.get(uid, {})
            maturity = parse_date(b.get("maturityDate")) or (t0 + timedelta(days=365 * 30))
            if b.get("perpetualFlag"):
                maturity = t0 + timedelta(days=365 * 30)
            if maturity < t0:  # погашена/дефолт — графика впереди нет
                coupons[uid], events[uid] = [], []
                continue
            try:
                coupons[uid] = api.get_bond_coupons(uid, inst.get("figi"), t0 - timedelta(days=1), maturity + timedelta(days=1))
            except TInvestError as e:
                warn(f"GetBondCoupons {inst.get('ticker')}: {e}")
                coupons[uid] = []
            try:
                events[uid] = api.get_bond_events(uid, t0 - timedelta(days=1), maturity + timedelta(days=1))
            except TInvestError as e:
                warn(f"GetBondEvents {inst.get('ticker')}: {e}")
                events[uid] = []
        elif itype in ("share", "etf"):
            try:
                dividends[uid] = api.get_dividends(uid, inst.get("figi"), t0 - timedelta(days=30), t0 + timedelta(days=180))
            except TInvestError as e:
                warn(f"GetDividends {inst.get('ticker')}: {e}")
                dividends[uid] = []
    write_json(adir / "bond_coupons.json", coupons)
    write_json(adir / "bond_events.json", events)
    write_json(adir / "dividends.json", dividends)

    first_flow: Optional[date] = None
    for o in ops:
        d = parse_date(o.get("date"))
        if d and (first_flow is None or d < first_flow):
            first_flow = d
    return {"id": acc_id, "name": acc.get("name"), "type": acc.get("type"), "positions": len(portfolio.get("positions", [])),
            "operations": len(ops), "first_operation": first_flow.isoformat() if first_flow else None}


def fetch_targets(api: TInvest, dd: Path, targets_path: Optional[str], cache: Dict[str, Dict[str, Any]], uids_all: Set[str]) -> None:
    """Бумаги из targets.yaml, которых ещё нет в портфеле: справочник + цена для плана покупок."""
    if not targets_path:
        return
    p = Path(targets_path).expanduser()
    if not p.exists():
        warn(f"targets не найден: {p}")
        return
    try:
        import yaml  # type: ignore
    except ImportError:
        warn("pyyaml не установлен: pip install pyyaml")
        return
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    by_ticker: Dict[str, Any] = read_json(dd / "targets_instruments.json", {}) or {}
    for g in cfg.get("groups", []):
        for it in g.get("items", []):
            ticker = str(it.get("ticker", "")).strip()
            if not ticker or ticker.upper() == "CASH" or ticker in by_ticker:
                continue
            try:
                found = api.find_instrument(ticker)
            except TInvestError as e:
                warn(f"FindInstrument {ticker}: {e}")
                continue
            inst = pick_exact(found, ticker)
            if not inst:
                warn(f"Тикер {ticker} из targets не найден в справочнике")
                continue
            by_ticker[ticker] = inst
            if inst.get("uid"):
                uids_all.add(inst["uid"])
                if inst["uid"] not in cache["instruments"]:
                    try:
                        cache["instruments"][inst["uid"]] = api.get_instrument(inst["uid"])
                    except TInvestError:
                        cache["instruments"][inst["uid"]] = inst
                if (inst.get("instrumentType") or "").lower() == "bond" and inst["uid"] not in cache["bonds"]:
                    try:
                        cache["bonds"][inst["uid"]] = api.get_bond(inst["uid"])
                    except TInvestError:
                        pass
    write_json(dd / "targets_instruments.json", by_ticker)


def fetch_benchmarks(api: TInvest, dd: Path, since: date) -> None:
    tickers = [t.strip() for t in os.environ.get("BENCH_TICKERS", DEFAULT_BENCH).split(",") if t.strip()]
    bdir = dd / "benchmarks"
    bdir.mkdir(parents=True, exist_ok=True)
    for t in tickers:
        try:
            found = [i for i in api.find_instrument(t) if (i.get("ticker") or "").upper() in (t.upper(), t.upper() + "@")
                     and (i.get("instrumentType") or "").lower() in ("etf", "share")]
            best, best_c = None, []
            for inst in found:  # берём доску, где свечи доходят до сегодня и история длиннее
                try:
                    c = api.get_day_candles(inst["uid"], since, today())
                except TInvestError:
                    continue
                if c and (not best_c or (c[-1]["time"], len(c)) > (best_c[-1]["time"], len(best_c))):
                    best, best_c = inst, c
            if not best:
                warn(f"Бенчмарк {t}: свечи не найдены")
                continue
            write_json(bdir / f"{t.upper()}.json", {"instrument": best, "candles": best_c})
        except TInvestError as e:
            warn(f"Бенчмарк {t}: {e}")


def fetch_fx(api: TInvest, dd: Path, currencies: Set[str], since: date) -> None:
    """Курсы валют к рублю: последняя цена + дневные свечи (для пересчёта старых операций)."""
    fx: Dict[str, float] = {"rub": 1.0}
    fdir = dd / "fx_candles"
    fdir.mkdir(parents=True, exist_ok=True)
    for cur in sorted(currencies):
        if cur in ("rub", ""):
            continue
        inst = None
        for t in FX_TICKERS.get(cur, []):
            try:
                inst = pick_exact(api.find_instrument(t), t, {"currency"})
            except TInvestError:
                inst = None
            if inst:
                break
        if not inst:
            try:  # запасной путь: поиск по коду валюты
                found = api.find_instrument(cur.upper(), kind="INSTRUMENT_TYPE_CURRENCY")
                inst = found[0] if found else None
            except TInvestError:
                inst = None
        if not inst:
            warn(f"Курс {cur}: инструмент не найден — укажи вручную в ~/.tinvest/fx.yaml (cur: курс)")
            continue
        try:
            lp = api.get_last_prices([inst["uid"]])
            if inst["uid"] in lp:
                fx[cur] = lp[inst["uid"]]
            candles = api.get_day_candles(inst["uid"], since, today())
            write_json(fdir / f"{cur}.json", {"instrument": inst, "candles": candles})
        except TInvestError as e:
            warn(f"Курс {cur}: {e}")
    manual = Path(os.environ.get("TINVEST_HOME", "~/.tinvest")).expanduser() / "fx.yaml"
    if manual.exists():
        try:
            import yaml  # type: ignore
            for k, v in (yaml.safe_load(manual.read_text(encoding="utf-8")) or {}).items():
                fx[str(k).lower()] = float(v)
        except Exception as e:  # noqa: BLE001
            warn(f"fx.yaml: {e}")
    write_json(dd / "fx.json", fx)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sandbox", action="store_true", help="работать с песочницей")
    ap.add_argument("--check", action="store_true", help="только проверить токен и показать счета")
    ap.add_argument("--targets", help="путь к targets.yaml, чтобы подтянуть бумаги из целевого портфеля")
    ap.add_argument("--no-benchmarks", action="store_true")
    args = ap.parse_args()

    load_env()
    try:
        api = client_from_env(args.sandbox)
    except ValueError as e:
        print(f"✗ {e}")
        return 2
    dd = data_dir(args.sandbox)

    try:
        accounts = fetch_accounts(api, dd)
    except TInvestError as e:
        print(f"✗ Не удалось получить счета: {e}")
        return 1
    print(f"Контур: {'ПЕСОЧНИЦА' if args.sandbox else 'боевой'} · {api.base}")
    print("Счета:")
    for a in accounts:
        print(f"  {a.get('id')} · {a.get('name') or '—'} · {a.get('type')} · {a.get('status')} · доступ: {a.get('accessLevel')}")
    if args.check:
        print("✓ Токен работает. Данные не выгружались (--check).")
        return 0

    accs = selected_accounts(accounts)
    if not accs:
        print("✗ Нет счетов для выгрузки (проверь TINVEST_ACCOUNT_ID и права токена)")
        return 1

    cache_dir = dd / "cache"
    cache = {
        "instruments": read_json(cache_dir / "instruments.json", {}) or {},
        "bonds": read_json(cache_dir / "bonds.json", {}) or {},
        "assets": read_json(cache_dir / "assets.json", {}) or {},
    }
    uids_all: Set[str] = set()
    summary = []
    for a in accs:
        print(f"→ {a.get('id')} {a.get('name') or ''}: портфель, операции, справочник…")
        try:
            summary.append(fetch_account(api, dd, a, cache, uids_all))
        except TInvestError as e:
            warn(f"Счёт {a.get('id')}: {e}")
    fetch_targets(api, dd, args.targets or os.environ.get("TINVEST_TARGETS"), cache, uids_all)

    for k, v in cache.items():
        write_json(cache_dir / f"{k}.json", v)

    # последние цены по всем известным бумагам
    try:
        lp = api.get_last_prices(sorted(uids_all))
        write_json(dd / "last_prices.json", lp)
    except TInvestError as e:
        warn(f"GetLastPrices: {e}")

    first = min([s["first_operation"] for s in summary if s.get("first_operation")] or [today().isoformat()])
    since = date.fromisoformat(first)
    currencies: Set[str] = set()
    for inst in cache["instruments"].values():
        currencies.add((inst.get("currency") or "").lower())
    for b in cache["bonds"].values():
        if b.get("nominal", {}).get("currency"):
            currencies.add(b["nominal"]["currency"].lower())
    for s in summary:
        for o in read_json(dd / s["id"] / "operations.json", []) or []:
            if o.get("payment", {}).get("currency"):
                currencies.add(o["payment"]["currency"].lower())
    fetch_fx(api, dd, currencies, since)
    if not args.no_benchmarks:
        fetch_benchmarks(api, dd, since)

    write_json(dd / "meta.json", {"fetched_at": datetime.now(timezone.utc).isoformat(), "base": api.base,
                                  "sandbox": args.sandbox, "accounts": summary, "api_calls": api.calls})
    print("Готово:")
    for s in summary:
        print(f"  {s['id']} · {s['name'] or '—'} · позиций {s['positions']} · операций {s['operations']} · первая операция {s['first_operation']}")
    print(f"  запросов к API: {api.calls} · данные: {dd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
