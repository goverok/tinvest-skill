"""Чтение выгрузки fetch.py в удобные структуры: позиции с оценкой в рублях, внешние потоки, курсы."""
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import currency_of, money, parse_date, read_json

# ---- типы операций ----
EXTERNAL_IN = {"OPERATION_TYPE_INPUT", "OPERATION_TYPE_INP_MULTI", "OPERATION_TYPE_INPUT_SWIFT",
               "OPERATION_TYPE_INPUT_ACQUIRING"}
EXTERNAL_OUT = {"OPERATION_TYPE_OUTPUT", "OPERATION_TYPE_OUT_MULTI", "OPERATION_TYPE_OUTPUT_SWIFT",
                "OPERATION_TYPE_OUTPUT_ACQUIRING", "OPERATION_TYPE_OUTPUT_PENALTY"}
BUY_CARD = {"OPERATION_TYPE_BUY_CARD"}
IN_KIND = {"OPERATION_TYPE_INPUT_SECURITIES", "OPERATION_TYPE_OUTPUT_SECURITIES"}
TRADE_BUY = {"OPERATION_TYPE_BUY", "OPERATION_TYPE_BUY_CARD", "OPERATION_TYPE_BUY_MARGIN"}
TRADE_SELL = {"OPERATION_TYPE_SELL", "OPERATION_TYPE_SELL_MARGIN"}
REPAYMENT = {"OPERATION_TYPE_BOND_REPAYMENT_FULL", "OPERATION_TYPE_BOND_REPAYMENT"}
FEES = {"OPERATION_TYPE_BROKER_FEE", "OPERATION_TYPE_SERVICE_FEE", "OPERATION_TYPE_MARGIN_FEE",
        "OPERATION_TYPE_SUCCESS_FEE", "OPERATION_TYPE_BENEFIT_FEE", "OPERATION_TYPE_ADVICE_FEE",
        "OPERATION_TYPE_TRACK_MFEE", "OPERATION_TYPE_TRACK_PFEE", "OPERATION_TYPE_CASH_FEE", "OPERATION_TYPE_OUT_FEE"}
TAXES = {"OPERATION_TYPE_TAX", "OPERATION_TYPE_DIVIDEND_TAX", "OPERATION_TYPE_COUPON_TAX", "OPERATION_TYPE_TAX_CORRECTION",
         "OPERATION_TYPE_TAX_LUCRE", "OPERATION_TYPE_TAX_REPO", "OPERATION_TYPE_TAX_REPO_HOLD", "OPERATION_TYPE_TAX_REPO_REFUND",
         "OPERATION_TYPE_TAX_CORRECTION_COUPON", "OPERATION_TYPE_TAX_PROGRESSIVE", "OPERATION_TYPE_BOND_TAX_PROGRESSIVE",
         "OPERATION_TYPE_DIVIDEND_TAX_PROGRESSIVE", "OPERATION_TYPE_BENEFIT_TAX_PROGRESSIVE", "OPERATION_TYPE_CORP_TAX_PROGRESSIVE",
         "OPERATION_TYPE_TAX_CORRECTION_PROGRESSIVE", "OPERATION_TYPE_TAX_CORRECTION_COUPON_PROGRESSIVE"}
INCOME = {"OPERATION_TYPE_DIVIDEND", "OPERATION_TYPE_COUPON", "OPERATION_TYPE_OVERNIGHT", "OPERATION_TYPE_DIV_EXT",
          "OPERATION_TYPE_BENEFIT_INCOME"}

ACCOUNT_IIS = "ACCOUNT_TYPE_TINKOFF_IIS"
TYPE_RU = {"share": "акции", "bond": "облигации", "etf": "фонды", "currency": "валюта", "futures": "фьючерсы",
           "option": "опционы", "sp": "структурные", "cash": "деньги", "?": "прочее"}


def op_type(o: Dict[str, Any]) -> str:
    return str(o.get("type") or o.get("operationType") or "")


def op_qty(o: Dict[str, Any]) -> float:
    for k in ("quantityDone", "quantity"):
        v = o.get(k)
        if v not in (None, "", "0", 0):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def price_on(candles: List[Dict[str, Any]], d: date, after: bool = True) -> Optional[float]:
    """Цена закрытия в ближайший торговый день на дату d или после (after=True) / до."""
    best = None
    for c in candles:
        cd = parse_date(c.get("time"))
        if not cd:
            continue
        if after and cd >= d:
            if best is None or cd < best[0]:
                best = (cd, c["close"])
        elif not after and cd <= d:
            if best is None or cd > best[0]:
                best = (cd, c["close"])
    return best[1] if best else None


class Store:
    """Все данные одной выгрузки."""

    def __init__(self, dd: Path):
        self.dd = dd
        self.meta = read_json(dd / "meta.json", {}) or {}
        self.accounts: List[Dict[str, Any]] = read_json(dd / "accounts.json", []) or []
        self.instruments: Dict[str, Dict[str, Any]] = read_json(dd / "cache" / "instruments.json", {}) or {}
        self.bonds: Dict[str, Dict[str, Any]] = read_json(dd / "cache" / "bonds.json", {}) or {}
        self.assets: Dict[str, Dict[str, Any]] = read_json(dd / "cache" / "assets.json", {}) or {}
        self.last_prices: Dict[str, float] = read_json(dd / "last_prices.json", {}) or {}
        self.fx: Dict[str, float] = read_json(dd / "fx.json", {"rub": 1.0}) or {"rub": 1.0}
        self.targets_instruments: Dict[str, Dict[str, Any]] = read_json(dd / "targets_instruments.json", {}) or {}
        # дробления акций: {uid или тикер: [[дата, коэффициент], ...]} — сделки до даты пересчитываются в новые штуки
        self.splits: Dict[str, List[List[Any]]] = read_json(dd / "cache" / "splits.json", {}) or {}
        self._fx_candles: Dict[str, List[Dict[str, Any]]] = {}
        self.warnings: List[str] = []

    # ---- доступ к счетам ----
    def account_ids(self) -> List[str]:
        return [a["id"] for a in self.meta.get("accounts", []) if (self.dd / a["id"] / "portfolio.json").exists()] or \
               [p.name for p in self.dd.iterdir() if p.is_dir() and (p / "portfolio.json").exists()]

    def account(self, acc_id: str) -> Dict[str, Any]:
        for a in self.accounts:
            if a.get("id") == acc_id:
                return a
        return {"id": acc_id, "name": acc_id, "type": "?"}

    def is_iis(self, acc_id: str) -> bool:
        return self.account(acc_id).get("type") == ACCOUNT_IIS

    def portfolio(self, acc_id: str) -> Dict[str, Any]:
        return read_json(self.dd / acc_id / "portfolio.json", {}) or {}

    def positions_raw(self, acc_id: str) -> Dict[str, Any]:
        return read_json(self.dd / acc_id / "positions.json", {}) or {}

    def operations(self, acc_id: str) -> List[Dict[str, Any]]:
        ops = read_json(self.dd / acc_id / "operations.json", []) or []
        ops.sort(key=lambda o: o.get("date") or "")
        return ops

    def coupons(self, acc_id: str) -> Dict[str, List[Dict[str, Any]]]:
        return read_json(self.dd / acc_id / "bond_coupons.json", {}) or {}

    def bond_events(self, acc_id: str) -> Dict[str, List[Dict[str, Any]]]:
        return read_json(self.dd / acc_id / "bond_events.json", {}) or {}

    def dividends(self, acc_id: str) -> Dict[str, List[Dict[str, Any]]]:
        return read_json(self.dd / acc_id / "dividends.json", {}) or {}

    def benchmark(self, ticker: str) -> Optional[Dict[str, Any]]:
        return read_json(self.dd / "benchmarks" / f"{ticker.upper()}.json", None)

    def benchmarks(self) -> List[str]:
        bdir = self.dd / "benchmarks"
        return sorted(p.stem for p in bdir.glob("*.json")) if bdir.exists() else []

    # ---- справочник ----
    def inst(self, uid: str) -> Dict[str, Any]:
        return self.instruments.get(uid, {}) or {}

    def ticker(self, uid: str) -> str:
        return self.inst(uid).get("ticker") or uid[:8]

    def name(self, uid: str) -> str:
        return self.inst(uid).get("name") or self.ticker(uid)

    def issuer(self, uid: str) -> str:
        inst = self.inst(uid)
        if (inst.get("ticker") or "").startswith("SU") and (inst.get("instrumentType") or "") == "bond":
            return "Минфин России"
        if inst.get("issuer"):
            return inst["issuer"]
        a = self.assets.get(inst.get("assetUid") or "", {})
        for b in a.get("brands", []) or []:
            if b.get("company"):
                return b["company"]
            if b.get("name"):
                return b["name"]
        if (inst.get("instrumentType") or "") == "share" and inst.get("name"):
            return inst["name"]
        if a.get("name") and len(a["name"]) <= 40:
            return a["name"]
        # запасной вариант: первое слово названия
        n = inst.get("name") or ""
        m = re.match(r"[^\s,]+(?:\s[^\s,]+)?", n)
        return m.group(0) if m else n or uid[:8]

    def bond_nominal(self, uid: str) -> Tuple[float, str]:
        b = self.bonds.get(uid, {})
        nom = b.get("nominal") or {}
        return money(nom) or 1000.0, currency_of(nom, self.inst(uid).get("currency") or "rub")

    def rate(self, cur: str) -> Optional[float]:
        cur = (cur or "rub").lower()
        if cur == "rub":
            return 1.0
        return self.fx.get(cur)

    def fx_candles(self, cur: str) -> List[Dict[str, Any]]:
        cur = cur.lower()
        if cur not in self._fx_candles:
            j = read_json(self.dd / "fx_candles" / f"{cur}.json", {}) or {}
            self._fx_candles[cur] = j.get("candles", [])
        return self._fx_candles[cur]

    def to_rub(self, amount: float, cur: str, d: Optional[date] = None) -> Optional[float]:
        cur = (cur or "rub").lower()
        if cur == "rub":
            return amount
        if d:
            p = price_on(self.fx_candles(cur), d, after=False) or price_on(self.fx_candles(cur), d, after=True)
            if p:
                return amount * p
        r = self.rate(cur)
        return amount * r if r else None

    # ---- позиции с оценкой ----
    def bond_price_mode(self, acc_id: str) -> str:
        """'abs' — цена облигации в портфеле уже в валюте, 'pct' — в процентах от номинала. Выбираем по сумме."""
        pf = self.portfolio(acc_id)
        total_bonds = money(pf.get("totalAmountBonds"))
        s_abs = s_pct = 0.0
        for p in pf.get("positions", []):
            if (p.get("instrumentType") or "").lower() != "bond":
                continue
            uid = p.get("instrumentUid", "")
            q = money(p.get("quantity"))
            price = money(p.get("currentPrice"))
            nkd = money(p.get("currentNkd"))
            nom, ncur = self.bond_nominal(uid)
            r = self.rate(currency_of(p.get("currentPrice"), ncur)) or 1.0
            s_abs += (q * price + q * nkd) * r
            s_pct += (q * price / 100.0 * nom + q * nkd) * r
        if total_bonds <= 0 or (s_abs == 0 and s_pct == 0):
            return "abs"
        return "abs" if abs(s_abs - total_bonds) <= abs(s_pct - total_bonds) else "pct"

    def valued_positions(self, acc_id: str) -> List[Dict[str, Any]]:
        pf = self.portfolio(acc_id)
        mode = self.bond_price_mode(acc_id)
        out: List[Dict[str, Any]] = []
        for p in pf.get("positions", []):
            uid = p.get("instrumentUid", "")
            inst = self.inst(uid)
            itype = (p.get("instrumentType") or inst.get("instrumentType") or "?").lower()
            q = money(p.get("quantity"))
            if q == 0:
                continue
            if itype == "currency" and ((inst.get("ticker") or p.get("ticker") or "").upper().startswith("RUB") or inst.get("isoCurrencyName") == "rub"):
                continue  # рубли уже учтены как деньги счёта
            price = money(p.get("currentPrice"))
            pcur = currency_of(p.get("currentPrice"), inst.get("currency") or "rub")
            nkd = money(p.get("currentNkd"))
            nom, ncur = (self.bond_nominal(uid) if itype == "bond" else (0.0, pcur))
            price_abs = price / 100.0 * nom if (itype == "bond" and mode == "pct") else price
            avg = money(p.get("averagePositionPrice"))
            avg_abs = avg / 100.0 * nom if (itype == "bond" and mode == "pct") else avg
            value_cur = q * price_abs + (q * nkd if itype == "bond" else 0.0)
            rate = self.rate(pcur)
            value_rub = value_cur * rate if rate else None
            if value_rub is None:
                self.warnings.append(f"Нет курса {pcur} для {self.ticker(uid)}: позиция без рублёвой оценки")
            expo_cur = pcur
            if itype == "currency":  # сама валюта: экспозиция — на неё
                expo_cur = (inst.get("nominal", {}) or {}).get("currency") or re.sub(r"[^A-Za-z]", "", (inst.get("ticker") or "")[:3]).lower() or "fx"
            elif itype == "bond":
                expo_cur = ncur
            out.append({
                "uid": uid, "ticker": self.ticker(uid), "name": self.name(uid), "type": itype,
                "type_ru": TYPE_RU.get(itype, itype), "qty": q, "lot": int(inst.get("lot") or 1),
                "price": price_abs, "price_currency": pcur, "nkd": nkd, "avg_price": avg_abs,
                "value_cur": value_cur, "value_rub": value_rub, "expo_currency": expo_cur,
                "expected_yield": money(p.get("expectedYield")), "issuer": self.issuer(uid),
                "sector": inst.get("sector") or ("government" if (inst.get("ticker") or "").startswith("SU") and itype == "bond" else "") or "",
                "country": inst.get("countryOfRisk") or "", "blocked": bool(p.get("blocked")),
            })
        return out

    def cash(self, acc_id: str) -> List[Tuple[str, float, Optional[float]]]:
        """Деньги на счёте по валютам: (валюта, сумма, сумма в ₽)."""
        raw = self.positions_raw(acc_id)
        out = []
        for m in raw.get("money", []) or []:
            cur = currency_of(m)
            amt = money(m)
            out.append((cur, amt, self.to_rub(amt, cur)))
        if not out:
            pf = self.portfolio(acc_id)
            amt = money(pf.get("totalAmountCurrencies"))
            out.append(("rub", amt, amt))
        return out

    def totals(self, acc_id: str) -> Dict[str, float]:
        pf = self.portfolio(acc_id)
        return {
            "shares": money(pf.get("totalAmountShares")), "bonds": money(pf.get("totalAmountBonds")),
            "etf": money(pf.get("totalAmountEtf")), "currencies": money(pf.get("totalAmountCurrencies")),
            "futures": money(pf.get("totalAmountFutures")), "options": money(pf.get("totalAmountOptions")),
            "sp": money(pf.get("totalAmountSp")), "portfolio": money(pf.get("totalAmountPortfolio")),
            "expected_yield_pct": money(pf.get("expectedYield")),
        }

    # ---- внешние денежные потоки ----
    def external_flows(self, acc_id: str) -> Tuple[List[Tuple[date, float]], Dict[str, int], List[str]]:
        """Потоки «инвестор ↔ счёт» в рублях со знаком инвестора: взнос < 0, вывод > 0.

        Возвращает (потоки, счётчик по типам операций, замечания).
        """
        flows: List[Tuple[date, float]] = []
        counts: Dict[str, int] = {}
        notes: List[str] = []
        skipped_fx = 0.0
        for o in self.operations(acc_id):
            t = op_type(o)
            counts[t] = counts.get(t, 0) + 1
            d = parse_date(o.get("date"))
            if not d:
                continue
            pay = money(o.get("payment"))
            cur = currency_of(o.get("payment"))
            amount: Optional[float] = None
            if t in EXTERNAL_IN or t in EXTERNAL_OUT:
                amount = -pay  # пополнение: payment>0 -> инвестор отдал деньги (-)
            elif t in BUY_CARD:
                amount = pay   # покупка с карты: payment<0 -> деньги пришли извне и сразу потрачены
            elif t in IN_KIND:
                qty = op_qty(o)
                price = money(o.get("price"))
                val = abs(pay) if pay else qty * price
                if val:
                    amount = -val if t.endswith("INPUT_SECURITIES") else val
                    notes.append(f"{d}: перевод бумаг {self.ticker(o.get('instrumentUid') or '')} оценён в {val:,.0f} {cur} — проверь")
                else:
                    notes.append(f"{d}: перевод бумаг {self.ticker(o.get('instrumentUid') or '')} без оценки — не учтён в XIRR")
            if amount is None or amount == 0:
                continue
            rub = self.to_rub(amount, cur, d)
            if rub is None:
                skipped_fx += abs(amount)
                continue
            flows.append((d, rub))
        if skipped_fx:
            notes.append(f"Потоки в валюте без курса на {skipped_fx:,.0f} (в валюте) не учтены в XIRR")
        return flows, counts, notes

    def split_factor(self, uid: str, d: date) -> float:
        """Во сколько раз умножить количество в сделке на дату d с учётом последующих дроблений."""
        f = 1.0
        for key in (uid, self.ticker(uid)):
            for sd, ratio in self.splits.get(key, []) or []:
                if d < date.fromisoformat(str(sd)[:10]):
                    f *= float(ratio)
            if key in self.splits:
                break
        return f

    def trades(self, acc_id: str) -> Dict[str, List[Dict[str, Any]]]:
        """Сделки по бумагам для FIFO: {uid: [{date, side, qty, unit_cost|unit_price, note}]} в рублях."""
        ops = self.operations(acc_id)
        fees_by_parent: Dict[str, float] = {}
        for o in ops:
            if op_type(o) in FEES and o.get("parentOperationId"):
                fees_by_parent[o["parentOperationId"]] = fees_by_parent.get(o["parentOperationId"], 0.0) + abs(money(o.get("payment")))
        out: Dict[str, List[Dict[str, Any]]] = {}
        for o in ops:
            t = op_type(o)
            uid = o.get("instrumentUid") or ""
            if not uid:
                continue
            d = parse_date(o.get("date"))
            qty = op_qty(o)
            if not d or qty <= 0:
                continue
            qty *= self.split_factor(uid, d)  # после дробления старые штуки становятся новыми
            pay = money(o.get("payment"))
            cur = currency_of(o.get("payment"))
            fee = abs(money(o.get("commission"))) + fees_by_parent.get(o.get("id", ""), 0.0)
            if t in TRADE_BUY:
                cost = abs(pay) + fee
                rub = self.to_rub(cost, cur, d)
                if rub is None:
                    continue
                out.setdefault(uid, []).append({"date": d, "side": "buy", "qty": qty, "unit_cost": rub / qty, "note": ""})
            elif t in TRADE_SELL or t in REPAYMENT:
                proceeds = max(0.0, abs(pay) - fee)
                rub = self.to_rub(proceeds, cur, d)
                if rub is None:
                    continue
                out.setdefault(uid, []).append({"date": d, "side": "sell", "qty": qty, "unit_price": rub / qty,
                                                "note": "погашение" if t in REPAYMENT else ""})
            elif t == "OPERATION_TYPE_INPUT_SECURITIES":
                price = money(o.get("price"))
                rub = self.to_rub(price, cur, d) if price else 0.0
                out.setdefault(uid, []).append({"date": d, "side": "buy", "qty": qty, "unit_cost": rub or 0.0,
                                                "note": "перевод бумаг: уточни цену приобретения" if not rub else "перевод бумаг"})
            elif t == "OPERATION_TYPE_OUTPUT_SECURITIES":
                out.setdefault(uid, []).append({"date": d, "side": "sell", "qty": qty, "unit_price": 0.0, "note": "перевод бумаг на другой счёт"})
        return out
