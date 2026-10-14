#!/usr/bin/env python3
"""Синтетическая выгрузка в формате T-Invest API (без сети) для проверки скриптов.

  python tests/make_fixture.py /tmp/tinvest-fixture
Потом: TINVEST_HOME=/tmp/tinvest-fixture python scripts/report.py all
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from common import write_json  # noqa: E402

TODAY = date(2026, 10, 14)


def mv(x: float, cur: str = "rub"):
    units = int(x) if x >= 0 else -int(-x)
    nano = int(round((x - units) * 1e9))
    return {"currency": cur, "units": str(units), "nano": nano}


def q(x: float):
    d = mv(x)
    d.pop("currency")
    return d


def ts(d: date, t: str = "07:00:00") -> str:
    return f"{d.isoformat()}T{t}Z"


SBER = "sber-uid-0001"
OFZ = "ofz-uid-26238"
CORP = "corp-uid-0001"
LQDT = "lqdt-uid-0001"
USD = "usd-uid-0001"


def instruments():
    return {
        SBER: {"uid": SBER, "figi": "BBG004730N88", "ticker": "SBER", "classCode": "TQBR", "name": "Сбер Банк", "instrumentType": "share",
               "currency": "rub", "lot": 10, "sector": "financial", "countryOfRisk": "RU", "assetUid": "asset-sber", "exchange": "MOEX"},
        OFZ: {"uid": OFZ, "figi": "BBG00ZZZ0001", "ticker": "SU26238RMFS4", "classCode": "TQOB", "name": "ОФЗ 26238", "instrumentType": "bond",
              "currency": "rub", "lot": 1, "sector": "government", "countryOfRisk": "RU", "assetUid": "asset-ofz", "exchange": "MOEX"},
        CORP: {"uid": CORP, "figi": "BBG00ZZZ0002", "ticker": "RU000A10TEST", "classCode": "TQCB", "name": "Тест-Лизинг 001P-05", "instrumentType": "bond",
               "currency": "rub", "lot": 1, "sector": "financial", "countryOfRisk": "RU", "assetUid": "asset-corp", "exchange": "MOEX"},
        LQDT: {"uid": LQDT, "figi": "BBG00ZZZ0003", "ticker": "LQDT", "classCode": "TQTF", "name": "Ликвидность", "instrumentType": "etf",
               "currency": "rub", "lot": 1, "sector": "", "countryOfRisk": "RU", "assetUid": "asset-lqdt", "exchange": "MOEX"},
        USD: {"uid": USD, "figi": "BBG0013HGFT4", "ticker": "USD000UTSTOM", "classCode": "CETS", "name": "Доллар США", "instrumentType": "currency",
              "currency": "rub", "lot": 1000, "nominal": {"currency": "usd", "units": "1", "nano": 0}},
    }


def bonds():
    return {
        OFZ: {"uid": OFZ, "ticker": "SU26238RMFS4", "nominal": mv(1000), "maturityDate": ts(date(2041, 5, 15)), "couponQuantityPerYear": 2,
              "floatingCouponFlag": False, "amortizationFlag": False, "perpetualFlag": False, "riskLevel": "RISK_LEVEL_LOW"},
        CORP: {"uid": CORP, "ticker": "RU000A10TEST", "nominal": mv(1000), "maturityDate": ts(date(2029, 3, 1)), "couponQuantityPerYear": 12,
               "floatingCouponFlag": False, "amortizationFlag": False, "perpetualFlag": False, "riskLevel": "RISK_LEVEL_MODERATE",
               "callDate": ts(date(2026, 11, 20))},
    }


def assets():
    return {"asset-sber": {"uid": "asset-sber", "name": "Сбербанк", "brands": [{"company": "Сбербанк"}]},
            "asset-ofz": {"uid": "asset-ofz", "name": "ОФЗ", "brands": [{"company": "Минфин России"}]},
            "asset-corp": {"uid": "asset-corp", "name": "Тест-Лизинг", "brands": [{"company": "Тест-Лизинг"}]},
            "asset-lqdt": {"uid": "asset-lqdt", "name": "ВИМ Ликвидность", "brands": [{"company": "ВИМ Инвестиции"}]}}


def portfolio(mode: str = "pct"):
    ofz_price = 55.5 if mode == "pct" else 555.0
    corp_price = 98.2 if mode == "pct" else 982.0
    positions = [
        {"figi": "BBG004730N88", "instrumentType": "share", "quantity": q(100), "averagePositionPrice": mv(202), "expectedYield": q(9800),
         "currentNkd": mv(0), "currentPrice": mv(300), "instrumentUid": SBER, "quantityLots": q(10)},
        {"figi": "BBG00ZZZ0001", "instrumentType": "bond", "quantity": q(50), "averagePositionPrice": mv(60 if mode == "pct" else 600), "expectedYield": q(-2250),
         "currentNkd": mv(20.3), "currentPrice": mv(ofz_price), "instrumentUid": OFZ, "quantityLots": q(50)},
        {"figi": "BBG00ZZZ0002", "instrumentType": "bond", "quantity": q(30), "averagePositionPrice": mv(100 if mode == "pct" else 1000), "expectedYield": q(-540),
         "currentNkd": mv(4.1), "currentPrice": mv(corp_price), "instrumentUid": CORP, "quantityLots": q(30)},
        {"figi": "BBG00ZZZ0003", "instrumentType": "etf", "quantity": q(1000), "averagePositionPrice": mv(1.5), "expectedYield": q(100),
         "currentNkd": mv(0), "currentPrice": mv(1.6), "instrumentUid": LQDT, "quantityLots": q(1000)},
    ]
    shares = 100 * 300
    bonds_v = 50 * (555 + 20.3) + 30 * (982 + 4.1)
    etf = 1000 * 1.6
    cash = 5000
    return {"totalAmountShares": mv(shares), "totalAmountBonds": mv(bonds_v), "totalAmountEtf": mv(etf), "totalAmountCurrencies": mv(cash),
            "totalAmountFutures": mv(0), "expectedYield": q(5.2), "positions": positions, "totalAmountPortfolio": mv(shares + bonds_v + etf + cash)}


def operations():
    ops = []
    n = [0]

    def op(d: date, typ: str, payment: float, uid: str = "", qty: int = 0, price: float = 0.0, commission: float = 0.0, parent: str = ""):
        n[0] += 1
        o = {"id": f"op{n[0]}", "cursor": f"c{n[0]}", "brokerAccountId": "acc1", "date": ts(d, "10:15:00"), "type": typ,
             "state": "OPERATION_STATE_EXECUTED", "payment": mv(payment), "price": mv(price), "commission": mv(commission),
             "quantity": str(qty), "quantityDone": str(qty), "quantityRest": "0", "instrumentUid": uid, "parentOperationId": parent,
             "name": typ.replace("OPERATION_TYPE_", "").title()}
        ops.append(o)
        return o["id"]

    op(date(2023, 1, 10), "OPERATION_TYPE_INPUT", 50000)
    b1 = op(date(2023, 1, 12), "OPERATION_TYPE_BUY", -9000, SBER, 60, 150)
    op(date(2023, 1, 12), "OPERATION_TYPE_BROKER_FEE", -27, SBER, parent=b1)
    op(date(2023, 2, 1), "OPERATION_TYPE_BUY", -30000, OFZ, 50, 600)   # с НКД
    op(date(2023, 2, 1), "OPERATION_TYPE_BROKER_FEE", -15, OFZ)
    op(date(2023, 8, 1), "OPERATION_TYPE_COUPON", 1775, OFZ)
    op(date(2024, 1, 15), "OPERATION_TYPE_INPUT", 10000)
    op(date(2024, 2, 1), "OPERATION_TYPE_BUY", -11200, SBER, 40, 280, commission=-33.6)
    op(date(2024, 12, 20), "OPERATION_TYPE_DIVIDEND", 3384, SBER)
    op(date(2024, 12, 20), "OPERATION_TYPE_DIVIDEND_TAX", -440, SBER)
    op(date(2025, 6, 1), "OPERATION_TYPE_BUY", -1500, LQDT, 1000, 1.5)
    op(date(2025, 6, 1), "OPERATION_TYPE_BUY_CARD", -30000, CORP, 30, 1000)
    op(date(2025, 9, 1), "OPERATION_TYPE_OUTPUT", -2000)
    op(date(2026, 1, 10), "OPERATION_TYPE_BUY", -5800, SBER, 20, 290, commission=-17.4)
    op(date(2026, 2, 10), "OPERATION_TYPE_SELL", 6200, SBER, 20, 310, commission=-18.6)
    op(date(2026, 3, 5), "OPERATION_TYPE_TAX", -120)
    return ops


def coupons():
    ofz = []
    d = date(2026, 11, 18)
    while d <= date(2041, 5, 15):
        ofz.append({"figi": "BBG00ZZZ0001", "couponDate": ts(d), "couponNumber": "7", "payOneBond": mv(35.4), "couponType": "COUPON_TYPE_CONSTANT",
                    "couponPeriod": 182})
        d = d + timedelta(days=182)
    corp = []
    d = date(2026, 10, 25)
    while d <= date(2029, 3, 1):
        corp.append({"figi": "BBG00ZZZ0002", "couponDate": ts(d), "payOneBond": mv(13.15), "couponType": "COUPON_TYPE_CONSTANT", "couponPeriod": 30})
        d = d + timedelta(days=30)
    return {OFZ: ofz, CORP: corp}


def bond_events():
    return {CORP: [{"instrumentId": CORP, "eventDate": ts(date(2026, 11, 20)), "eventType": "EVENT_TYPE_CALL", "payOneBond": mv(1000),
                    "note": "Оферта по требованию владельцев, заявки до 13.11.2026"},
                   {"instrumentId": CORP, "eventDate": ts(date(2026, 10, 25)), "eventType": "EVENT_TYPE_CPN", "payOneBond": mv(13.15)}],
            OFZ: [{"instrumentId": OFZ, "eventDate": ts(date(2026, 11, 18)), "eventType": "EVENT_TYPE_CPN", "payOneBond": mv(35.4)}]}


def dividends():
    return {SBER: [{"dividendNet": mv(34.84), "paymentDate": ts(date(2026, 11, 25)), "declaredDate": ts(date(2026, 10, 1)),
                    "lastBuyDate": ts(date(2026, 11, 7)), "recordDate": ts(date(2026, 11, 10)), "dividendType": "Regular", "yieldValue": q(11.6)}]}


def candles(start: date, growth_per_year: float, p0: float, wobble: float = 0.0):
    out = []
    d = start
    i = 0
    while d <= TODAY:
        years = (d - start).days / 365.0
        p = p0 * (1 + growth_per_year) ** years
        if wobble:
            p *= 1 + wobble * ((i % 7) - 3) / 10
        out.append({"time": ts(d, "04:00:00"), "open": p, "close": round(p, 4), "high": p, "low": p, "volume": 1000})
        d += timedelta(days=1)
        i += 1
    return out


def main(root: Path):
    dd = root / "data"
    accounts = [
        {"id": "acc1", "type": "ACCOUNT_TYPE_TINKOFF_IIS", "name": "ИИС", "status": "ACCOUNT_STATUS_OPEN", "openedDate": ts(date(2023, 1, 9)),
         "accessLevel": "ACCOUNT_ACCESS_LEVEL_READ_ONLY"},
        {"id": "acc2", "type": "ACCOUNT_TYPE_TINKOFF", "name": "Брокерский", "status": "ACCOUNT_STATUS_OPEN", "openedDate": ts(date(2023, 1, 9)),
         "accessLevel": "ACCOUNT_ACCESS_LEVEL_READ_ONLY"},
    ]
    write_json(dd / "accounts.json", accounts)
    write_json(dd / "cache" / "instruments.json", instruments())
    write_json(dd / "cache" / "bonds.json", bonds())
    write_json(dd / "cache" / "assets.json", assets())
    for acc, mode in (("acc1", "pct"), ("acc2", "abs")):
        write_json(dd / acc / "portfolio.json", portfolio(mode))
        write_json(dd / acc / "positions.json", {"money": [mv(5000)], "securities": []})
        ops = operations()
        for o in ops:
            o["brokerAccountId"] = acc
        write_json(dd / acc / "operations.json", ops)
        write_json(dd / acc / "bond_coupons.json", coupons())
        write_json(dd / acc / "bond_events.json", bond_events())
        write_json(dd / acc / "dividends.json", dividends())
    write_json(dd / "last_prices.json", {SBER: 300.0, OFZ: 55.5, CORP: 98.2, LQDT: 1.6, USD: 84.0})
    write_json(dd / "fx.json", {"rub": 1.0, "usd": 84.0})
    write_json(dd / "benchmarks" / "LQDT.json", {"instrument": instruments()[LQDT], "candles": candles(date(2022, 1, 3), 0.15, 1.0)})
    write_json(dd / "benchmarks" / "SBMX.json", {"instrument": {"ticker": "SBMX", "name": "Индекс Мосбиржи", "uid": "sbmx"},
                                                 "candles": candles(date(2024, 6, 1), 0.08, 15.0, wobble=0.02)})
    write_json(dd / "targets_instruments.json", {})
    write_json(dd / "meta.json", {"fetched_at": ts(TODAY, "06:00:00"), "base": "fixture", "sandbox": False,
                                  "accounts": [{"id": "acc1", "name": "ИИС", "first_operation": "2023-01-10"},
                                               {"id": "acc2", "name": "Брокерский", "first_operation": "2023-01-10"}]})
    (root / "targets.yaml").write_text(
        "cash_reserve: 1000\ngroups:\n  - name: Акции РФ\n    weight: 50\n    items:\n      - {ticker: SBER, weight: 100}\n"
        "  - name: Облигации\n    weight: 40\n    items:\n      - {ticker: SU26238RMFS4, weight: 70}\n      - {ticker: RU000A10TEST, weight: 30}\n"
        "  - name: Денежный рынок\n    weight: 10\n    items:\n      - {ticker: LQDT, weight: 100}\n", encoding="utf-8")
    print(f"fixture: {root}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/tinvest-fixture"))
