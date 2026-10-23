#!/usr/bin/env python3
"""Проверка математики и отчётов на синтетических данных. Запуск: python tests/test_calc.py"""
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from finmath import allocate, fifo, macaulay_duration, modified_duration, xirr, xnpv, ytm  # noqa: E402


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol


def test_xirr():
    flows = [(date(2024, 1, 1), -1000.0), (date(2024, 7, 1), -1000.0), (date(2025, 1, 1), 2200.0)]
    r = xirr(flows)
    assert r is not None and close(xnpv(r, flows), 0, 1e-4), r
    # ровно 365 дней, один поток: +10% (как XIRR в Excel, база 365)
    assert close(xirr([(date(2023, 1, 1), -100.0), (date(2024, 1, 1), 110.0)]), 0.10, 1e-4)
    # отрицательная доходность
    assert close(xirr([(date(2023, 1, 1), -100.0), (date(2024, 1, 1), 90.0)]), -0.10, 1e-4)
    assert xirr([(date(2024, 1, 1), -100.0)]) is None
    print("xirr ok", round(r, 4))


def test_ytm_duration():
    # 2-летняя облигация, купон 10% раз в год, цена = номинал -> ytm 10%, Маколей ≈ 1.909
    cfs = [(1.0, 10.0), (2.0, 110.0)]
    y = ytm(100.0, cfs)
    assert close(y, 0.10, 1e-5), y
    mac = macaulay_duration(cfs, y)
    assert close(mac, 1.9091, 1e-3), mac
    assert close(modified_duration(mac, y), 1.7355, 1e-3)
    # ОФЗ-подобная: цена 55.5% + НКД 2.03%, купон 3.54% x2, 14.6 лет -> ytm в районе 14-16%
    cfs = [((i + 1) * 0.5, 35.4) for i in range(29)] + [(14.6, 1000.0)]
    y2 = ytm(575.3, cfs)
    assert y2 is not None and 0.13 < y2 < 0.17, y2
    print("ytm ok", round(y, 4), round(y2, 4))


def test_fifo():
    trades = [
        {"date": date(2023, 1, 1), "side": "buy", "qty": 10, "unit_cost": 100.0},
        {"date": date(2023, 6, 1), "side": "buy", "qty": 10, "unit_cost": 120.0},
        {"date": date(2024, 1, 1), "side": "sell", "qty": 15, "unit_price": 130.0},
    ]
    lots, realized = fifo(trades)
    assert len(lots) == 1 and close(lots[0]["left"], 5) and close(lots[0]["unit_cost"], 120.0)
    assert close(sum(r["pnl"] for r in realized), 10 * 30 + 5 * 10)
    assert realized[0]["hold_days"] == 365
    print("fifo ok")


def test_allocate():
    cur = {"a": 60.0, "b": 20.0, "c": 0.0}
    tg = {"a": 0.5, "b": 0.3, "c": 0.2}
    al = allocate(40.0, cur, tg)
    assert close(sum(al.values()), 40.0, 1e-6), al
    after = {k: cur[k] + al[k] for k in cur}
    tot = sum(after.values())
    assert all(abs(after[k] / tot - tg[k]) < 1e-6 for k in tg), after  # 40 хватает ровно закрыть дефициты
    al2 = allocate(10.0, cur, tg)
    assert close(sum(al2.values()), 10.0) and al2["a"] == 0.0  # «a» перевешен — ничего не докупаем
    print("allocate ok")


def test_reports():
    import make_fixture
    tmp = Path(tempfile.mkdtemp(prefix="tinvest-fixture-"))
    make_fixture.main(tmp)
    os.environ["TINVEST_HOME"] = str(tmp)
    import common
    common.today = lambda: date(2026, 10, 14)  # фиксируем «сегодня» для теста
    import model, audit, perf, events, tax, deploy, bonds  # noqa: E402
    for m in (model, audit, perf, events, tax, deploy, bonds):
        m.today = common.today
    store = model.Store(tmp / "data")
    assert set(store.account_ids()) == {"acc1", "acc2"}

    # режим цены облигаций: acc1 — проценты от номинала, acc2 — рубли
    assert store.bond_price_mode("acc1") == "pct", store.bond_price_mode("acc1")
    assert store.bond_price_mode("acc2") == "abs"
    pos = {p["ticker"]: p for p in store.valued_positions("acc1")}
    assert close(pos["SBER"]["value_rub"], 30000)
    assert close(pos["SU26238RMFS4"]["value_rub"], 50 * (555 + 20.3), 1e-3), pos["SU26238RMFS4"]["value_rub"]
    assert pos["SBER"]["issuer"] == "Сбербанк"

    # аудит сходится с итогами брокера
    text, data = audit.build(store, "acc1")
    assert "⚠" not in text.split("### Структура")[0], text
    assert 0.9 < data["top5_share"] <= 1.0
    assert any(b["ticker"] == "RU000A10TEST" and b["call"] == date(2026, 11, 20) for b in data["bonds"])
    ofz = next(b for b in data["bonds"] if b["ticker"] == "SU26238RMFS4")
    assert ofz["ytm"] and 0.13 < ofz["ytm"] < 0.17 and 5 < ofz["duration_mod"] < 9, (ofz["ytm"], ofz["duration_mod"])

    # доходность: потоки = -50000, -10000, -30000 (BUY_CARD), +2000
    flows, counts, notes = store.external_flows("acc1")
    assert sorted(a for _, a in flows) == [-50000.0, -30000.0, -10000.0, 2000.0], flows
    text, data = perf.build(store, "acc1")
    s = data["summary"]
    assert close(s["contributions"], 90000) and close(s["withdrawals"], 2000)
    assert s["xirr"] is not None and -0.5 < s["xirr"] < 0.5
    assert len(data["benchmarks"]) == 2 and all(b["terminal"] > 0 for b in data["benchmarks"])
    assert any(b["late_start"] for b in data["benchmarks"])  # SBMX начинается позже первого взноса

    # календарь: оферта, купоны, дивиденд
    text, items = events.build(store, "acc1", 60)
    kinds = {(i["kind"], i["ticker"]) for i in items}
    assert ("оферта", "RU000A10TEST") in kinds and ("дивиденд (отсечка)", "SBER") in kinds and ("купон", "SU26238RMFS4") in kinds, kinds
    assert "Ближайшая оферта: RU000A10TEST 2026-11-20" in text

    # налоги: продано 20 SBER @310 (−18.6) против лота 20 @290 (+17.4): FIFO берёт самые старые лоты (150)!
    text, data = tax.build(store, "acc2")
    # FIFO: продажа 20 шт закрывает 20 из лота 2023 по 150.45 -> прибыль = 6181.4 - 20*150.45
    assert close(data["realized_base"], (6200 - 18.6) - 20 * (9000 + 27) / 60, 1e-6), data["realized_base"]
    assert close(data["ndfl"], 0.13 * data["realized_base"], 1e-6)
    # открытых лотов SBER: 40 (2023) + 40 (2024) + 20 (2026) = 100 = портфель -> без расхождений
    assert not any("SBER" in r for r in text.split("Расхождения")[1:]) if "Расхождения" in text else True
    text_iis, data_iis = tax.build(store, "acc1")
    assert data_iis["ndfl"] == 0.0 and "ИИС" in text_iis

    # размещение 100 000 по целям
    text, data = deploy.build(store, "acc1", 100000.0, tmp / "targets.yaml")
    assert data["spent"] <= 99000 and data["spent"] > 90000, data["spent"]
    assert all(p["units"] % p["lot"] == 0 for p in data["plan"])
    print("reports ok")


def _bar(d, ro, rc, so, sc):
    return {"date": d, "risk_open": ro, "risk_close": rc, "safe_open": so, "safe_close": sc}


def test_fetch_targets():
    """Бумаги из targets.yaml, уже лежащие в кэше справочника, всё равно попадают в запрос цен."""
    from fetch import fetch_targets
    from common import write_json

    class Api:
        def __init__(self):
            self.finds = []

        def find_instrument(self, q):
            self.finds.append(q)
            return [{"ticker": q, "classCode": "TQTF", "instrumentType": "etf", "uid": "u-" + q}]

        def get_instrument(self, uid):
            return {"uid": uid}

    d = Path(tempfile.mkdtemp(prefix="tinvest-targets-"))
    yml = ['groups:', '  - name: A', '    weight: 100', '    items:', '      - {ticker: SBMX, weight: 50}', '      - {ticker: NEW, weight: 50}']
    (d / "t.yaml").write_text(chr(10).join(yml) + chr(10), encoding="utf-8")
    write_json(d / "targets_instruments.json", {"SBMX": {"ticker": "SBMX", "uid": "u-SBMX", "instrumentType": "etf"}})
    api, uids = Api(), set()
    fetch_targets(api, d, str(d / "t.yaml"), {"instruments": {}, "bonds": {}}, uids)
    assert uids == {"u-SBMX", "u-NEW"}, uids
    assert api.finds == ["NEW"], api.finds          # кэш справочника не перезапрашивается
    print("fetch_targets ok")


def test_moex_iss():
    import moex_iss as mi
    page = {"history": {"columns": ["TRADEDATE", "CLOSE", "DURATION", "YIELD"],
                        "data": [["2014-01-08", 118.2, 160, 5.95], ["2014-01-09", None, 159, 5.98], ["2014-01-10", 118.3, 158, 6.0]]},
            "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[100, 3215, 100]]}}
    rows, nxt, total = mi.parse_index_page(page)
    assert [r["date"] for r in rows] == ["2014-01-08", "2014-01-10"] and nxt == 200 and total == 3215, (rows, nxt, total)
    assert close(rows[0]["yield"], 0.0595) and rows[0]["duration"] == 160.0
    z = {"yearyields": {"columns": ["tradedate", "period", "value"], "data": [["2014-01-31", 1.0, 6.543], ["2014-01-31", 10.0, 8.1]]}}
    d, c = mi.parse_zcyc(z)
    assert d == "2014-01-31" and close(c[1.0], 0.06543) and close(c[10.0], 0.081), (d, c)
    assert mi.parse_zcyc({"yearyields": {"columns": ["tradedate", "period", "value"], "data": []}}) == (None, {})
    xml = ('<KR diffgr:id="KR1"><DT>2026-10-15T00:00:00+03:00</DT><Rate>14.00</Rate></KR>'
           '<KR diffgr:id="KR2"><DT>2026-10-14T00:00:00+03:00</DT><Rate>16.50</Rate></KR>')
    kr = mi.parse_key_rate(xml)
    assert [d for d, _ in kr] == ["2026-10-14", "2026-10-15"] and close(kr[0][1], 0.165) and close(kr[1][1], 0.14), kr
    assert mi.month_ends(["2014-01-30", "2014-01-31", "2014-02-03", "2014-02-28", "2014-03-03"]) == ["2014-01-31", "2014-02-28", "2014-03-03"]
    merged = mi.merge_rows([{"date": "2014-01-08", "close": 1.0}, {"date": "2014-01-09", "close": 2.0}],
                           [{"date": "2014-01-09", "close": 2.5}, {"date": "2014-01-10", "close": 3.0}])
    assert [r["close"] for r in merged] == [1.0, 2.5, 3.0]
    # пагинация: страница из одних None-закрытий не обрывает загрузку, пустой сырой блок — обрывает
    pages = {0: {"history": {"columns": ["TRADEDATE", "CLOSE", "DURATION", "YIELD"],
                             "data": [["2014-01-08", None, 1, 1.0]]},
                 "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, 150, 100]]}},
             100: {"history": {"columns": ["TRADEDATE", "CLOSE", "DURATION", "YIELD"],
                               "data": [["2014-01-09", 5.0, 1, 1.0]]},
                   "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[100, 150, 100]]}}}
    saved_get = mi._get_json
    mi._get_json = lambda url, params: pages[params["start"]]
    try:
        assert [r["date"] for r in mi.fetch_index("RGBITR", date(2014, 1, 1))] == ["2014-01-09"]
    finally:
        mi._get_json = saved_get
    # сеть недоступна: кэш используется (с датой в предупреждении), без кэша — SystemExit
    import requests
    import shutil
    from common import read_json, write_json
    old_home = os.environ.get("TINVEST_HOME")
    saved = (mi.fetch_index, mi.fetch_zcyc, mi.fetch_key_rate, mi.warn, mi.today)
    warns = []

    def down(*a, **k):
        raise requests.ConnectionError("нет сети")

    def fresh_home():
        os.environ["TINVEST_HOME"] = tempfile.mkdtemp(prefix="tinvest-iss-")
        warns.clear()
        return mi.bonds_dir()

    def seed(bd, idx_dates, zcyc, kr_date):
        for ix in mi.INDICES:
            write_json(bd / f"{ix}.json", [{"date": d, "close": 100.0, "duration": 100.0, "yield": 0.06} for d in idx_dates])
        if zcyc is not None:
            write_json(bd / "zcyc.json", zcyc)
        write_json(bd / "key_rate.json", [[kr_date, 0.055]])

    homes = []
    try:
        mi.warn = warns.append
        mi.today = lambda: date(2014, 2, 5)
        mi.fetch_index = mi.fetch_zcyc = mi.fetch_key_rate = down
        bd = fresh_home()
        homes.append(os.environ["TINVEST_HOME"])
        try:
            mi.load()
            raise AssertionError("без кэша и сети должен быть SystemExit")
        except SystemExit:
            pass
        seed(bd, ["2014-01-31"], {"2014-01-31": {"1.0": 0.065, "10.0": 0.081}}, "2014-01-31")
        raw = mi.load()
        assert raw["indices"]["RGBITR"][0]["close"] == 100.0 and raw["zcyc"]["2014-01-31"][10.0] == 0.081
        assert raw["key_rate"] == [("2014-01-31", 0.055)]
        assert warns == [] or all("2014-01-31" in w for w in warns), warns  # кэш свежий (<= STALE_DAYS) — сети не касались
        # кэш устарел: предупреждения содержат дату последних данных
        mi.today = lambda: date(2014, 3, 20)
        warns.clear()
        raw = mi.load()
        assert len(warns) == 7 and all("2014-01-31" in w for w in warns), warns
        # refresh при упавшей сети кэш не трогает
        before = {p.name: p.read_bytes() for p in bd.iterdir()}
        warns.clear()
        raw = mi.load(refresh=True)
        assert {p.name: p.read_bytes() for p in bd.iterdir()} == before and raw["zcyc"]["2014-01-31"][1.0] == 0.065
        assert warns and all("2014-01-31" in w for w in warns), warns
        # нет кэша КБД при упавшей сети — SystemExit
        bd = fresh_home()
        homes.append(os.environ["TINVEST_HOME"])
        seed(bd, ["2014-01-31"], None, "2014-01-31")
        try:
            mi.load()
            raise AssertionError("без кэша КБД должен быть SystemExit")
        except SystemExit as e:
            assert "КБД" in str(e), e
        # устаревшая «последняя» точка заменяется, концы завершённых месяцев остаются, пустая свежая КБД не пишется
        mi.today = lambda: date(2026, 10, 3)
        mi.fetch_index = lambda ix, d_from: []
        mi.fetch_key_rate = lambda a, b: []
        calls = []

        def zc(d):
            calls.append(d)
            if d is None:
                return "2026-10-02", {1.0: 0.15}
            return (None, {}) if d == "2026-09-30" else (d, {1.0: 0.14})
        mi.fetch_zcyc = zc
        bd = fresh_home()
        homes.append(os.environ["TINVEST_HOME"])
        seed(bd, ["2026-08-31", "2026-09-30", "2026-10-02"], {"2026-08-31": {"1.0": 0.13}, "2026-09-25": {"1.0": 0.12}}, "2026-10-01")
        raw = mi.load()
        saved_z = read_json(bd / "zcyc.json", {})
        assert sorted(saved_z) == ["2026-08-31", "2026-10-02"], saved_z  # 09-25 вытеснена, пустая 09-30 не записана
        assert sorted(raw["zcyc"]) == ["2026-08-31", "2026-10-02"] and calls == ["2026-09-30", None], (raw["zcyc"], calls)
        assert warns == [], warns
    finally:
        mi.fetch_index, mi.fetch_zcyc, mi.fetch_key_rate, mi.warn, mi.today = saved
        for h in homes:
            shutil.rmtree(h, ignore_errors=True)
        if old_home is None:
            os.environ.pop("TINVEST_HOME", None)
        else:
            os.environ["TINVEST_HOME"] = old_home
    print("moex_iss ok")


def test_bond_series():
    import moex_iss as mi
    from bt_sim import View, simulate
    from datetime import timedelta
    d0 = date(2020, 1, 1)
    days = [d0 + timedelta(days=i) for i in range(40)]
    raw = {"indices": {ix: [{"date": d.isoformat(), "close": 100.0 + i * (k + 1), "duration": 100.0 * (k + 1), "yield": 0.06}
                            for i, d in enumerate(days)] for k, ix in enumerate(mi.INDICES)},
           "zcyc": {"2020-01-31": {1.0: 0.05, 3.0: 0.055, 5.0: 0.06, 10.0: 0.065}},
           "key_rate": [("2020-01-01", 0.0625), ("2020-01-20", 0.06)]}
    lq = mi._series("LQDT", days, [1.0 + i * 0.001 for i in range(40)])
    s = mi.build_series(raw, lq)
    assert set(s) >= {"RGBITR", "OFZ1Y", "OFZ10Y", "ZCYC1", "ZCYC10", "KEYRATE", "LQDT"}, set(s)
    assert close(s["OFZ10Y"]["duration"][0], 500.0 / 365.0) and close(s["OFZ10Y"]["yield"][0], 0.06)
    med = mi.bucket_durations(s)
    assert med["OFZ1Y"][0] < med["OFZ3Y"][0] < med["OFZ5Y"][0] < med["OFZ10Y"][0], med
    v = View(s, date(2020, 1, 25), ["OFZ1Y"], "RGBITR")
    assert v.asof("ZCYC10", "close", date(2020, 2, 28)) is None            # КБД за 31.01 на 25.01 ещё неизвестна
    assert close(v.asof("KEYRATE", "close", date(2020, 1, 25)), 0.06) and close(v.asof("KEYRATE", "close", date(2020, 1, 10)), 0.0625)
    assert v.asof("NOPE", "close", date(2020, 1, 25)) is None
    assert v.bars_until(date(2020, 1, 27)) == 2
    # календарь RGBITR и IMOEX на одинаковых датах дают один и тот же результат
    s2 = dict(s, IMOEX=s["RGBITR"])

    def half(view, p, st):
        return {"OFZ5Y": 0.5}

    a = simulate(s2, half, {}, start=d0, end=days[-1], universe=["OFZ5Y"], calendar="RGBITR", rebalance="monthly")
    b = simulate(s2, half, {}, start=d0, end=days[-1], universe=["OFZ5Y"], rebalance="monthly")
    assert close(a["final"], b["final"]) and a["trades"] == b["trades"] and a["trades"], (a["final"], b["final"])
    print("bond_series ok")


def test_bond_rules():
    import bond_rules as br
    import moex_iss as mi
    from bt_sim import View
    from bt_strategies import grid_variants
    from datetime import timedelta
    d0 = date(2020, 1, 1)
    days = [d0 + timedelta(days=i) for i in range(400)]

    def lvl(t, vals, ds=None):
        return mi._series(t, ds or days, vals)

    base = {"RGBITR": lvl("RGBITR", [100.0] * 400), "LQDT": lvl("LQDT", [1.0003 ** i for i in range(400)])}
    for b in br.BUCKETS:
        base[b] = lvl(b, [100.0] * 400)
    last = days[-1]
    # curve: ставка 10% -> LQDT 9.5%; КБД 10 лет 12% -> спред 2.5 п.п. > 2 п.п.
    s = dict(base, KEYRATE=lvl("KEYRATE", [0.10] * 400), ZCYC10=lvl("ZCYC10", [0.12], [days[100]]))
    assert br.curve(View(s, last, br.UNIVERSE, "RGBITR"), {"bucket": "OFZ10Y", "s": 0.02}, {}) == {"OFZ10Y": 1.0}
    s["ZCYC10"] = lvl("ZCYC10", [0.11], [days[100]])                     # спред 1.5 п.п.
    assert br.curve(View(s, last, br.UNIVERSE, "RGBITR"), {"bucket": "OFZ10Y", "s": 0.02}, {}) == {}
    s["ZCYC10"] = lvl("ZCYC10", [0.11, 0.30], [days[100], days[399]])   # будущая точка не видна
    assert br.curve(View(s, days[398], br.UNIVERSE, "RGBITR"), {"bucket": "OFZ10Y", "s": 0.02}, {}) == {}
    assert br.curve(View(dict(base, KEYRATE=s["KEYRATE"]), last, br.UNIVERSE, "RGBITR"), {"bucket": "OFZ3Y", "s": 0.0}, {}) == {}
    # rate_cycle: 16% до дня 300, потом 14%
    kr = lvl("KEYRATE", [0.16 if i < 300 else 0.14 for i in range(400)])
    v = View(dict(base, KEYRATE=kr), last, br.UNIVERSE, "RGBITR")
    assert br.rate_cycle(v, {"M": 6, "d": 0.01, "bucket": "OFZ10Y"}, {}) == {"OFZ10Y": 1.0}
    assert br.rate_cycle(v, {"M": 6, "d": 0.03, "bucket": "OFZ10Y"}, {}) == {}
    kr_old = lvl("KEYRATE", [0.16 if i < 100 else 0.14 for i in range(400)])   # снижение 300 дней назад
    v2 = View(dict(base, KEYRATE=kr_old), last, br.UNIVERSE, "RGBITR")
    assert br.rate_cycle(v2, {"M": 6, "d": 0.005, "bucket": "OFZ5Y"}, {}) == {}
    assert br.rate_cycle(v2, {"M": 12, "d": 0.005, "bucket": "OFZ5Y"}, {}) == {"OFZ5Y": 1.0}
    assert br.rate_cycle(v2, {"M": 6, "d": 0.0, "bucket": "OFZ5Y"}, {}) == {"OFZ5Y": 1.0}   # d=0 — держать всегда
    # ts_momentum: OFZ10Y быстрее LQDT, OFZ5Y падает, OFZ1Y/3Y стоят
    s = dict(base, KEYRATE=kr)
    s["OFZ10Y"] = lvl("OFZ10Y", [100.0 * 1.001 ** i for i in range(400)])
    s["OFZ5Y"] = lvl("OFZ5Y", [100.0 * 0.999 ** i for i in range(400)])
    v = View(s, last, br.UNIVERSE, "RGBITR")
    assert br.ts_momentum(v, {"L": 3, "set": "all"}, {}) == {"OFZ10Y": 1.0}
    assert br.ts_momentum(v, {"L": 3, "set": "long"}, {}) == {"OFZ10Y": 1.0}
    s["OFZ10Y"] = s["OFZ5Y"]
    assert br.ts_momentum(View(s, last, br.UNIVERSE, "RGBITR"), {"L": 6, "set": "long"}, {}) == {}
    assert br.ts_momentum(View(s, days[30], br.UNIVERSE, "RGBITR"), {"L": 12, "set": "all"}, {}) == {}   # мало истории
    # бенчмарки и случайный контроль
    fn, p = br.BENCHMARKS[br.CURRENT_NAME]
    w = fn(v, p, {})
    assert close(w["OFZ10Y"], 7 / 9) and close(w["OFZ1Y"], 2 / 9) and sum(w.values()) <= 1 + 1e-9
    assert br.BENCHMARKS["LQDT"][0](v, br.BENCHMARKS["LQDT"][1], {}) == {}
    assert br.BENCHMARKS["RGBITR"][0](v, br.BENCHMARKS["RGBITR"][1], {}) == {"RGBITR": 1.0}
    r1 = br.random_bucket(v, {"seed": 3}, {})
    assert r1 == br.random_bucket(v, {"seed": 3}, {}) and sum(r1.values()) <= 1.0
    assert [len(grid_variants(f["grid"])) for f in br.FAMILIES.values()] == [12, 12, 6]
    assert all(f["rebalance"] == "monthly" for f in br.FAMILIES.values())
    print("bond_rules ok")


def test_backtest():
    from backtest import align_states, max_drawdown, simulate, trend_states
    # SMA(3): прогрев -> None; 10 = SMA -> не выше -> вне рынка; 13 > 11 -> в рынке; 7 < 10 -> вне
    assert trend_states([10, 10, 10, 13, 7], 3, 0.0) == [None, None, False, True, False]
    # гистерезис: 11.5 == SMA 11.5 -> не ниже, остаёмся в рынке
    assert trend_states([10, 10, 10, 13, 11.5], 3, 0.0)[-1] is True
    # полоса 10%: вход только выше SMA·1.1, выход только ниже SMA·0.9
    assert trend_states([10, 10, 10, 11.5, 9.5], 3, 0.1) == [None, None, False, False, False]  # 11.5 < 10.5·1.1
    assert trend_states([10, 10, 10, 13, 9.9], 3, 0.1) == [None, None, False, True, True]  # 9.9 > 10.97·0.9
    assert trend_states([10, 10, 10, 13, 9.9], 3, 0.0)[-1] is False
    # сигнал берётся с последнего закрытия индекса строго ДО даты бара; прогрев = вне рынка
    idx_d = [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 4)]
    assert align_states(idx_d, [None, True, False], [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)])         == [False, True, True, False]
    assert close(max_drawdown([100, 120, 90, 130, 65]), -0.5)

    # без переключений, ИИС: покупка по open с проскальзыванием и комиссией, продажа в конце по close
    bars = [_bar(date(2024, 1, d), 10, 10, 1.0, 1.0) for d in (1, 2, 3)]
    r = simulate(bars, [False] * 3, capital=1000.0, cost=0.0005, slip=0.001)
    assert close(r["final"], 1000 * 0.999 * 0.9995 / (1.001 * 1.0005), 1e-9), r["final"]
    assert r["switches"] == 0 and r["taxes"] == 0.0

    # переключение без издержек: 1000 в safe @1 -> продали @1.1 = 1100 -> 110 risk @10 -> close 12 = 1320
    bars = [_bar(date(2024, 1, 1), 10, 10, 1.0, 1.0), _bar(date(2024, 1, 2), 10, 11, 1.1, 1.1),
            _bar(date(2024, 1, 3), 11, 12, 1.1, 1.1)]
    r = simulate(bars, [False, True, True], capital=1000.0, cost=0.0, slip=0.0)
    assert close(r["final"], 1320.0) and r["switches"] == 1
    assert close(r["equity"][1][1], 110 * 11) and r["in_market"] == [False, True, True]

    # брокерский счёт: прибыль 2020 = 200 -> налог 26 в первый день 2021 продажей 2.6 паёв @10
    bars = [_bar(date(2020, 1, 10), 10, 10, 1.0, 1.0), _bar(date(2020, 6, 1), 10, 10, 1.2, 1.2),
            _bar(date(2021, 1, 11), 10, 10, 1.2, 1.2), _bar(date(2021, 2, 1), 10, 10, 1.2, 1.2)]
    r = simulate(bars, [False, True, True, True], capital=1000.0, cost=0.0, slip=0.0, tax_rate=0.13)
    assert close(r["taxes"], 26.0) and close(r["final"], 1174.0), r

    # перенос убытка: 2020 −200, 2021 +400 -> налог 13% с 200
    bars = [_bar(date(2020, 1, 10), 10, 10, 1.0, 1.0), _bar(date(2020, 6, 1), 10, 10, 0.8, 0.8),
            _bar(date(2021, 3, 1), 10, 15, 0.8, 0.8)]
    r = simulate(bars, [False, True, True], capital=1000.0, cost=0.0, slip=0.0, tax_rate=0.13)
    assert close(r["taxes"], 26.0) and close(r["final"], 1174.0), r

    # ЛДВ: держали дольше 3 лет -> налога нет; чуть меньше 3 лет -> 13% с прибыли
    hold = lambda end: [_bar(date(2020, 1, 10), 10, 10, 1.0, 1.0), _bar(end, 10, 10, 2.0, 2.0)]  # noqa: E731
    r = simulate(hold(date(2023, 1, 10)), [False, False], capital=1000.0, cost=0.0, slip=0.0, tax_rate=0.13)
    assert close(r["final"], 2000.0) and r["taxes"] == 0.0, r
    r = simulate(hold(date(2022, 12, 30)), [False, False], capital=1000.0, cost=0.0, slip=0.0, tax_rate=0.13)
    assert close(r["final"], 1870.0), r
    print("backtest ok")


def test_sharpe():
    from finmath import deflated_sharpe, moments, norm_cdf, norm_ppf, sharpe
    # постоянная доходность 0.1% в день при rf 0 -> std 0 -> None
    assert sharpe([0.001] * 10) is None
    # чередование +1%/-1% со средним 0 -> Sharpe 0; +2%/0% -> mean 1%, std ~1% -> ~sqrt(252)
    assert close(sharpe([0.01, -0.01] * 50), 0.0, 1e-9)
    s = sharpe([0.02, 0.0] * 50)
    assert s is not None and 15.0 < s < 16.0, s
    # rf вычитается: доходности 2%/0% при rf 1% -> избыточные +1%/-1% -> 0
    assert close(sharpe([0.02, 0.0] * 5, [0.01] * 10), 0.0, 1e-9)
    # доходность равна rf -> избыточная нулевая дисперсия -> None
    assert sharpe([0.01] * 5, [0.01] * 5) is None
    # константа без rf: среднее одинаковых float неточно, но дисперсия считается нулевой
    assert sharpe([0.007] * 10) is None
    assert sharpe([0.003] * 3) is None
    # переменный rf, избыточная доходность 1% в каждом периоде -> нулевая дисперсия -> None
    assert sharpe([0.02, 0.0, 0.03, 0.01], [0.01, -0.01, 0.02, 0.0]) is None
    m, sd, sk, ku = moments([1.0, 2.0, 3.0, 4.0, 5.0])
    assert close(m, 3.0) and close(sd, 2.5 ** 0.5) and close(sk, 0.0) and 1.5 < ku < 2.0, (m, sd, sk, ku)
    assert close(norm_cdf(0.0), 0.5) and close(norm_cdf(1.96), 0.975, 1e-3)
    assert close(norm_ppf(0.975), 1.96, 1e-2) and close(norm_ppf(0.5), 0.0, 1e-6)
    # одна попытка -> порог 0, PSR обычный: sr=0.1/день, T=1000 -> p ~ 0.001
    sr_star, p1 = deflated_sharpe(0.1, 1, 1000)
    assert sr_star == 0.0 and p1 < 0.002, (sr_star, p1)
    # больше попыток -> порог выше, p больше
    s39, p39 = deflated_sharpe(0.1, 39, 1000)
    assert s39 > 0.0 and p39 > p1, (s39, p39)
    s100, p100 = deflated_sharpe(0.1, 100, 1000)
    assert s100 > s39 and p100 > p39
    # нулевой Sharpe при 39 попытках -> p > 0.5
    assert deflated_sharpe(0.0, 39, 1000)[1] > 0.5
    print("sharpe ok")


def _candles(start, closes, opens=None):
    from datetime import timedelta
    out = []
    for i, c in enumerate(closes):
        d = start + timedelta(days=i)
        o = opens[i] if opens else c
        out.append({"time": d.isoformat() + "T00:00:00Z", "open": o, "close": c, "high": max(o, c), "low": min(o, c), "volume": 100})
    return out


def test_bt_data():
    from bt_data import check_quality, make_series, splice_safe, synthetic_safe
    # сплит 1:10 на 4-й день: цены до него делятся на 10, объёмы умножаются
    s = make_series("X", _candles(date(2024, 1, 1), [100.0, 110.0, 120.0, 12.0]), splits=[("2024-01-04", 10)])
    assert s["close"] == [10.0, 11.0, 12.0, 12.0] and s["volume"][0] == 1000 and s["volume"][3] == 100, s["close"]
    # дивиденд 10 на старую акцию: lastBuyDate 2024-01-02 -> ex 2024-01-03 (до сплита) -> 1.0 на новую;
    # tr на ex = (12 + 1) / 11 от предыдущего
    div = [{"dividendNet": {"units": "10", "nano": 0}, "lastBuyDate": "2024-01-02T00:00:00Z",
            "recordDate": "2024-01-03T00:00:00Z", "paymentDate": "2024-01-10T00:00:00Z"}]
    s = make_series("X", _candles(date(2024, 1, 1), [100.0, 110.0, 120.0, 12.0]), splits=[("2024-01-04", 10)], dividends=div)
    assert len(s["divs"]) == 1 and s["divs"][0]["ex"] == date(2024, 1, 3) and close(s["divs"][0]["amount"], 1.0), s["divs"]
    assert close(s["tr"][1], 1.1) and close(s["tr"][2], 1.1 * 13 / 11) and close(s["tr"][3], s["tr"][2])
    # до перехода на T+1 (2023-07-31) ex-день — сам бар lastBuyDate: гэп 2020-01-02 с 110 до 100 при дивиденде 10 -> tr не меняется
    old = [{"dividendNet": {"units": "10", "nano": 0}, "lastBuyDate": "2020-01-02T00:00:00Z",
            "recordDate": "2020-01-03T00:00:00Z", "paymentDate": "2020-01-10T00:00:00Z"}]
    s = make_series("X", _candles(date(2020, 1, 1), [110.0, 100.0, 100.0]), dividends=old)
    assert s["divs"][0]["ex"] == date(2020, 1, 2) and close(s["tr"][1], 1.0) and close(s["tr"][2], 1.0), (s["divs"], s["tr"])
    # дата объявления: declaredDate из API; отсутствует или позже lastBuyDate -> lastBuyDate − 7 дней
    assert s["divs"][0]["declared"] == date(2019, 12, 26), s["divs"]
    s = make_series("X", _candles(date(2020, 1, 1), [110.0, 100.0, 100.0]), dividends=[dict(old[0], declaredDate="2019-12-01T00:00:00Z")])
    assert s["divs"][0]["declared"] == date(2019, 12, 1)
    s = make_series("X", _candles(date(2020, 1, 1), [110.0, 100.0, 100.0]), dividends=[dict(old[0], declaredDate="2020-01-03T00:00:00Z")])
    assert s["divs"][0]["declared"] == date(2019, 12, 26)
    # качество: разрыв > 7 дней, нулевой объём, скачок > 30% без сплита
    c = _candles(date(2024, 1, 1), [10.0, 10.0, 14.0])
    c[2]["time"] = "2024-01-20T00:00:00Z"
    c[1]["volume"] = 0
    w = check_quality(make_series("Y", c))
    assert any("разрыв" in x for x in w) and any("объём" in x for x in w) and any("скачок" in x for x in w), w
    # пауза 2022 не считается разрывом
    c = _candles(date(2022, 2, 24), [10.0, 10.0])
    c[1]["time"] = "2022-03-25T00:00:00Z"
    assert not any("разрыв" in x for x in check_quality(make_series("Z", c)))
    # скачок в окрестности обвала 2022 — не подсказка про сплит, а пометка «обвал/возобновление 2022»
    c = _candles(date(2022, 2, 23), [10.0, 5.0])
    w = check_quality(make_series("Z", c))
    assert any("скачок" in x and "2022)" in x for x in w) and not any("SPLITS" in x for x in w), w
    # синтетический LQDT: 10% годовых минус 0.5 п.п., ежедневная капитализация -> за 365 дней между 9.5% и 10%
    days = [date(2019, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(366)]
    v = synthetic_safe(days, [(date(2018, 1, 1), 0.10)])
    assert close(v[0], 1.0) and 1.095 < v[-1] < 1.10, v[-1]
    # склейка: синтетика до первой свечи реального, дальше реальный с непрерывным масштабом
    real = make_series("LQDT", _candles(date(2019, 6, 1), [1.0, 1.01, 1.02]))
    cal = [date(2019, 5, 30), date(2019, 5, 31), date(2019, 6, 1), date(2019, 6, 2), date(2019, 6, 3)]
    sp = splice_safe(cal, real, key_rate=[(date(2018, 1, 1), 0.10)])
    assert sp["dates"] == cal and len(sp["close"]) == 5
    assert close(sp["close"][3] / sp["close"][2], 1.01) and sp["close"][1] <= sp["close"][2]
    assert sp["tr"] == sp["close"] or close(sp["tr"][-1] / sp["tr"][0], sp["close"][-1] / sp["close"][0])
    # ставка берётся из ПОСЛЕДНЕЙ вступившей в силу записи, а не максимальной
    from datetime import timedelta
    d2019 = [date(2019, 1, 1) + timedelta(days=i) for i in range(365)]
    v = synthetic_safe(d2019, [(date(2018, 1, 1), 0.10), (date(2019, 7, 1), 0.06)])
    exp, hi, lo = 1.0, 1.0, 1.0
    for i in range(1, len(d2019)):
        r = (0.095 if d2019[i] < date(2019, 7, 1) else 0.055)
        exp *= 1 + r * 1 / 365
        hi *= 1 + 0.095 / 365
        lo *= 1 + 0.055 / 365
    assert close(v[-1], exp) and lo < v[-1] < hi, (v[-1], exp, lo, hi)
    # дивиденд в валюте (не RUB) пропускается с предупреждением и не влияет на tr
    usd = [{"dividendNet": {"units": "1", "nano": 0, "currency": "usd"}, "lastBuyDate": "2024-01-02T00:00:00Z",
            "paymentDate": "2024-01-10T00:00:00Z"}]
    s = make_series("U", _candles(date(2024, 1, 1), [100.0, 110.0, 120.0]), dividends=usd)
    assert s["divs"] == [] and close(s["tr"][2], 1.2) and any("usd" in x for x in s["warnings"]), s["warnings"]
    print("bt_data ok")


def _series(ticker, start, closes, opens=None, lot=1, divs=()):
    from bt_data import make_series
    s = make_series(ticker, _candles(start, closes, opens), lot=lot)
    s["divs"] = list(divs)
    return s


def _flat(ticker, start, n, price=1.0, lot=1):
    return _series(ticker, start, [price] * n, lot=lot)


def _live(r):
    """Сделки до ликвидации в последний день окна."""
    return [t for t in r["trades"] if t["date"] < r["end"]]


def test_bt_sim():
    from bt_sim import View, simulate, summarize
    d0 = date(2024, 1, 1)
    idx = _flat("IMOEX", d0, 6)
    safe = _flat("LQDT", d0, 6)
    a = _series("A", d0, [10, 10, 12, 12, 12, 12], opens=[10, 10, 10, 12, 12, 12])
    b = _flat("B", d0, 6, price=5.0, lot=10)
    series = {"IMOEX": idx, "LQDT": safe, "A": a, "B": b}
    kw = dict(start=d0, end=date(2024, 1, 6), capital=1000.0, cost=0.0, slip=0.0)

    # View не видит будущего
    v = View(series, date(2024, 1, 3), ["A", "B"])
    assert v.n("A") == 3 and v.last("A", "close", 100) == [10, 10, 12] and v.at("A", "close") == 12 and v.at("A", "close", 1) == 10
    try:
        v.at("A", "close", 3)
        assert False
    except IndexError:
        pass

    # 50/50 с первого дня без издержек: A растёт 10 -> 12 на 3-й день (open 10 -> покупка по 10)
    half = lambda view, p, st: {"A": 0.5, "B": 0.5}  # noqa: E731
    r = simulate(series, half, {}, **kw)
    # день 1: сигнал на закрытии, исполнение на open дня 2: 50 A @10, 10 лотов B = 100 шт @5 -> 500 + 500
    # A: 50 * 12 = 600 с дня 3 -> итог 1100
    assert close(r["final"], 1100.0), r["final"]
    assert r["trades"][0]["date"] == date(2024, 1, 2) and {t["ticker"] for t in r["trades"][:2]} == {"A", "B"}
    assert close(r["equity"][0][1], 1000.0) and close(r["equity"][-1][1], 1100.0)

    # порог ребалансировки: цель 0.5/0.5, после роста A доля 600/1100 = 0.545 -> отклонение 4.5% > 2% -> сделка;
    # с порогом 0.10 сделок после первых двух нет (ликвидационные в конце не считаем)
    r2 = simulate(series, half, {}, rebalance_threshold=0.10, **kw)
    assert len(_live(r2)) == 2, _live(r2)
    r3 = simulate(series, half, {}, rebalance_threshold=0.02, **kw)
    assert len(_live(r3)) > 2

    # лоты: B лот 10 по 5 = 50 за лот; капитал 1000 и вес 0.07 = 70 -> 1 лот (50), не 1.4
    r = simulate(series, lambda v, p, s: {"B": 0.07}, {}, **kw)
    assert r["trades"][0]["qty"] == 10

    # кэш не уходит в минус при издержках: вес 1.0 в A с комиссией 1% -> покупается 99 шт, кэш >= 0
    r = simulate(series, lambda v, p, s: {"A": 1.0}, {}, start=d0, end=date(2024, 1, 6), capital=1000.0, cost=0.01, slip=0.0)
    first_a = next(t for t in r["trades"] if t["ticker"] == "A")
    assert first_a["qty"] == 99 and min(e for _, e in r["equity"]) > 0 and r["cash_min"] >= 0, first_a

    # бумага без бара в этот день: C торгуется только 2 дня, оценка по последнему close, заявок по ней нет
    c = _series("C", d0, [3.0, 3.0])
    series_c = dict(series, C=c)
    r = simulate(series_c, lambda v, p, s: {"C": 0.5}, {}, universe=["A", "B", "C"], **kw)
    live = _live(r)
    assert {t["ticker"] for t in live} == {"C", "LQDT"} and all(t["date"] == date(2024, 1, 2) for t in live), live
    assert close(r["final"], 1000.0)  # C оценивается по последнему close 3.0 и так же ликвидируется

    # дивиденд: ex день 3, выплата день 5, бумага продана на день 4 -> деньги всё равно приходят
    a_div = _series("A", d0, [10] * 6, divs=[{"ex": date(2024, 1, 3), "pay": date(2024, 1, 5), "amount": 1.0}])
    s2 = dict(series, A=a_div)
    hold_then_sell = lambda v, p, st: {"A": 1.0} if v.today < date(2024, 1, 3) else {}  # noqa: E731
    r = simulate(s2, hold_then_sell, {}, **kw)
    assert close(r["dividends"], 87.0) and close(r["final"], 1087.0), (r["dividends"], r["final"])
    # дивиденд облагается у источника (13%) на любом счёте, ИИС не освобождает
    r = simulate(s2, hold_then_sell, {}, tax_rate=0.13, **kw)
    assert close(r["dividends"], 87.0), r["dividends"]

    # налог: прибыль 200 в 2024, продажа 03.01.2025 уже в новом году -> 26 удерживается ИТОГОВЫМ расчётом в конце окна
    d1 = date(2024, 12, 30)
    cal = [d1, date(2024, 12, 31), date(2025, 1, 3), date(2025, 1, 6)]
    def _s(t, closes, lot=1):
        from bt_data import make_series
        return make_series(t, [{"time": d.isoformat() + "T00:00:00Z", "open": c, "close": c, "high": c, "low": c, "volume": 1}
                               for d, c in zip(cal, closes)], lot=lot)
    # сигнал на закрытии 30.12 -> покупка по open 31.12 (10); сигнал {} 31.12 -> продажа по open 03.01 (12)
    s3 = {"IMOEX": _s("IMOEX", [1, 1, 1, 1]), "LQDT": _s("LQDT", [1, 1, 1, 1]), "A": _s("A", [10, 10, 12, 12])}
    sig = lambda v, p, st: {"A": 1.0} if v.today == d1 else {}  # noqa: E731
    r = simulate(s3, sig, {}, start=d1, end=cal[-1], capital=1000.0, cost=0.0, slip=0.0, tax_rate=0.13)
    assert close(r["taxes"], 26.0) and close(r["final"], 1174.0), (r["taxes"], r["final"])

    # налоги: общий помощник — серия open=close=цена на произвольном календаре
    def _sc(t, days, closes):
        from bt_data import make_series
        return make_series(t, [{"time": d.isoformat() + "T00:00:00Z", "open": c, "close": c, "high": c, "low": c, "volume": 1}
                               for d, c in zip(days, closes)])

    def _tax_run(days, a_closes, sig_fn, **extra):
        ss = {"IMOEX": _sc("IMOEX", days, [1] * len(days)), "LQDT": _sc("LQDT", days, [1] * len(days)),
              "A": _sc("A", days, a_closes)}
        return simulate(ss, sig_fn, {}, start=days[0], end=days[-1], capital=1000.0, cost=0.0, slip=0.0, tax_rate=0.13, **extra)

    # смена года: покупка A 30.12 по 10, продажа 31.12 по 12 (прибыль 200 в 2024), снова A -> 03.01 налог 26 платится
    # принудительной продажей (кэш ~0), а не итоговым расчётом
    ydays = [date(2024, 12, 27), date(2024, 12, 30), date(2024, 12, 31), date(2025, 1, 3), date(2025, 1, 6)]
    ysig = lambda v, p, st: {"A": 1.0} if v.today == ydays[0] else {}  # noqa: E731
    r = _tax_run(ydays, [10, 10, 12, 12, 12], ysig)
    assert close(r["taxes"], 26.0), r["taxes"]
    # цель не меняется на смене года (всё в LQDT), поэтому единственная сделка 03.01 — принудительная продажа на 26
    jan3 = [t for t in r["trades"] if t["date"] == date(2025, 1, 3)]
    assert len(jan3) == 1 and jan3[0]["side"] == "sell" and jan3[0]["ticker"] == "LQDT" and close(jan3[0]["qty"], 26.0), jan3
    assert r["cash_min"] >= 0 and min(e for _, e in r["equity"]) > 0 and close(r["final"], 1174.0), (r["cash_min"], r["final"])

    # смена года с ДВУМЯ позициями: 30.12 куплено 1000 A по 1; 31.12 по open 2 продано 500 A (прибыль 500) и куплено
    # 1000 B по 1, кэш 0. 03.01 налог 65 покрывается продажей пропорционально (A 17 шт по 2 + B 33 шт по 1 = 67),
    # кэш не уходит в минус. 2025: прибыль 17 (принудительная) + 483 (ликвидация) = 500 -> ещё 65. Итог 2 + 966 + 967 - 65.
    def _two_run(b_days):
        ss = {"IMOEX": _sc("IMOEX", ydays, [1] * 5), "LQDT": _sc("LQDT", ydays, [1] * 5),
              "A": _sc("A", ydays, [1, 1, 2, 2, 2]), "B": _sc("B", b_days, [1] * len(b_days))}
        sig_ab = lambda v, p, st: {"A": 1.0} if v.today == ydays[0] else {"A": 0.5, "B": 0.5}  # noqa: E731
        return simulate(ss, sig_ab, {}, start=ydays[0], end=ydays[-1], capital=1000.0, cost=0.0, slip=0.0, tax_rate=0.13)
    r = _two_run(ydays)
    assert r["cash_min"] >= 0 and close(r["taxes"], 130.0) and close(r["final"], 1870.0), (r["cash_min"], r["taxes"], r["final"])
    # B без бара 03.01: его доля перераспределяется на A (33 шт по 2 = 66), кэш не уходит в минус
    r = _two_run([d for d in ydays if d != date(2025, 1, 3)])
    jan3 = [t for t in r["trades"] if t["date"] == date(2025, 1, 3)]
    assert r["cash_min"] >= 0 and [(t["ticker"], t["qty"]) for t in jan3] == [("A", 33)], (r["cash_min"], jan3)

    # брокерский счёт, цены только растут: налог в конце окна входит в final, но не в equity и не в просадку
    gdays = [date(2024, 3, 1), date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6), date(2024, 3, 7)]
    r = _tax_run(gdays, [10, 10, 11, 12, 12], lambda v, p, st: {"A": 1.0})
    assert r["taxes"] > 0 and summarize(r)["mdd"] == 0.0 and r["final"] < r["equity"][-1][1], (r["taxes"], r["equity"][-1], r["final"])

    # перенос убытка: 2024 -200, 2025 +400 -> налог 0.13 * (400 - 200) = 26
    cdays = [date(2024, 12, 27), date(2024, 12, 30), date(2024, 12, 31), date(2025, 1, 3), date(2025, 1, 6),
             date(2025, 1, 7), date(2025, 1, 8)]
    csig = lambda v, p, st: {"A": 1.0} if v.today in (cdays[0], cdays[3]) else {}  # noqa: E731
    r = _tax_run(cdays, [10, 10, 8, 8, 8, 12, 12], csig)
    assert close(r["taxes"], 26.0) and close(r["final"], 1174.0), (r["taxes"], r["final"])

    # ЛДВ: лот куплен 2020-01-10, продан 2023-01-10 (ровно 3 года) с прибылью -> налог 0
    edays = [date(2020, 1, 9), date(2020, 1, 10), date(2023, 1, 9), date(2023, 1, 10), date(2023, 1, 11)]
    esig = lambda v, p, st: {} if v.today >= date(2023, 1, 9) else {"A": 1.0}  # noqa: E731
    r = _tax_run(edays, [10, 10, 15, 15, 15], esig)
    assert close(r["taxes"], 0.0) and close(r["final"], 1500.0), (r["taxes"], r["final"])

    # дивиденд с ex-датой вне календаря IMOEX (выходная сессия акции) всё равно начисляется
    odays = [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 6)]
    o_idx, o_safe = _sc("IMOEX", odays, [1] * 5), _sc("LQDT", odays, [1] * 5)
    o_a = _series("A", d0, [10] * 6, divs=[{"ex": date(2024, 1, 3), "pay": date(2024, 1, 5), "amount": 1.0}])
    r = simulate({"IMOEX": o_idx, "LQDT": o_safe, "A": o_a}, lambda v, p, st: {"A": 1.0}, {}, **kw)
    assert close(r["dividends"], 87.0), r["dividends"]

    # заявка не «знает» close дня исполнения: два прогона, различающихся только close дня 3, совпадают до open дня 3
    def _look(close3):
        la = _series("A", d0, [10, 10, close3, 10, 10, 10], opens=[10] * 6)
        return simulate(dict(series, A=la), lambda v, p, st: {"A": 0.5}, {}, **kw)
    assert [t for t in _look(10)["trades"] if t["date"] <= date(2024, 1, 3)] == \
        [t for t in _look(5)["trades"] if t["date"] <= date(2024, 1, 3)]

    # fill: сигнал на закрытии дня 2 -> open дня 3 = 10, close дня 3 = 12
    sig2 = lambda v, p, s: {"A": 1.0} if v.today == date(2024, 1, 2) else {}  # noqa: E731
    ro = simulate(series, sig2, {}, fill="open", **kw)
    rr = simulate(series, sig2, {}, fill="next_close", **kw)
    # (пустая цель дня 1 -> остаток уходит в LQDT на дне 2, поэтому берём первую сделку именно по A)
    fa = lambda res: next(t for t in res["trades"] if t["ticker"] == "A")  # noqa: E731
    assert fa(ro)["price"] == 10 and fa(ro)["date"] == date(2024, 1, 3), fa(ro)
    assert fa(rr)["price"] == 12 and fa(rr)["date"] == date(2024, 1, 3), fa(rr)

    # monthly: стратегия вызывается только в последний торговый день месяца
    calls = []
    def spy(v, p, st):
        calls.append(v.today)
        return {}
    big = {k: _flat(k, date(2024, 1, 29), 6) for k in ("IMOEX", "LQDT", "A")}
    simulate(big, spy, {}, start=date(2024, 1, 29), end=date(2024, 2, 3), capital=1000.0, rebalance="monthly")
    assert calls == [date(2024, 1, 31), date(2024, 2, 3)], calls  # конец января и последний день окна

    # summarize
    r = simulate(series, half, {}, **kw)
    s = summarize(r, safe_series=safe)
    assert close(s["final"], 1100.0) and s["xirr"] is not None and s["mdd"] <= 0 and s["n_obs"] == 5 and s["trades"] >= 2
    assert 2024 in s["yearly"] and 0 < s["in_market"] <= 1
    # View.active: бумага, не торгующаяся дольше 7 дней, неактивна
    from bt_sim import View
    old = _flat("OLD", date(2024, 1, 1), 10)          # последний бар 2024-01-10
    v_act = View({"OLD": old}, date(2024, 1, 20), ["OLD"])
    assert not v_act.active("OLD") and not v_act.active("NOPE")
    assert View({"OLD": old}, date(2024, 1, 11), ["OLD"]).active("OLD")
    assert not View({"OLD": old}, date(2023, 12, 31), ["OLD"]).active("OLD")   # баров ещё нет
    print("bt_sim ok")


def test_bt_strategies():
    from bt_sim import View
    from bt_strategies import FAMILIES, div_runup, donchian, equal_weight, grid_variants, momentum, random_portfolio, trend_vol
    d0 = date(2020, 1, 1)
    n = 300
    up = _series("UP", d0, [10 + i * 0.1 for i in range(n)])          # ровный рост
    flat = _series("FL", d0, [10.0] * n)
    down = _series("DN", d0, [40 - i * 0.1 for i in range(n)])        # падение
    # последний месяц: UP падает (12-1 должен это пропустить и всё равно выбрать UP по 6 месяцам)
    up_rev = _series("UR", d0, [10 + i * 0.1 for i in range(n - 21)] + [10 + (n - 21) * 0.1 - i * 0.2 for i in range(21)])
    idx = _series("IMOEX", d0, [100 + i * 0.1 for i in range(n)])
    series = {"IMOEX": idx, "UP": up, "FL": flat, "DN": down, "UR": up_rev}
    today = up["dates"][-1]
    v = View(series, today, ["UP", "FL", "DN", "UR"])

    # div_runup: дивиденд с ex на баре 50; L=5 -> вход при 7 барах до ex (сигнал), выход при 2; объявление и доходность фильтруют
    ex = up["dates"][50]
    dv_a = {"ex": ex, "pay": ex, "amount": 0.5, "declared": up["dates"][30]}
    a = _series("A", d0, [10.0] * n, divs=[dv_a])
    b = _series("B", d0, [10.0] * n, divs=[dict(dv_a, amount=1.0)])
    c = _series("C", d0, [10.0] * n, divs=[dict(dv_a, declared=up["dates"][46])])
    ser = {"IMOEX": idx, "A": a, "B": b, "C": c}
    prm = {"L": 5, "K": 2, "min_yield": 0.0}
    v43, v44, v47, v48 = (View(ser, up["dates"][i], ["A", "B", "C"]) for i in (43, 44, 47, 48))
    assert v43.bars_until(ex) == 7 and v48.bars_until(ex) == 2
    assert div_runup(v42 := View(ser, up["dates"][42], ["A", "B", "C"]), prm, {}) == {} and v42.bars_until(ex) == 8
    w = div_runup(v43, prm, {})
    assert set(w) == {"A", "B"} and close(w["A"], 0.5), w                      # C ещё не объявлен (declared = бар 46)
    assert set(div_runup(v47, prm, {})) == {"B", "C"}, div_runup(v47, prm, {})  # C объявлен; K=2: B (10%), затем тикер при равных 5%
    assert div_runup(v48, prm, {}) == {}                                        # n=2: выход (продажа на open cum-дня)
    assert set(div_runup(v44, {"L": 5, "K": 5, "min_yield": 0.08}, {})) == {"B"}
    assert "div_runup" in FAMILIES and len(grid_variants(FAMILIES["div_runup"]["grid"])) == 18

    w = momentum(v, {"L": 6, "K": 2, "market_filter": 0}, {})
    assert set(w) == {"UP", "UR"} and close(sum(w.values()), 1.0), w
    # фильтр: индекс ниже SMA200 -> пусто
    idx_dn = _series("IMOEX", d0, [200 - i * 0.1 for i in range(n)])
    assert momentum(View(dict(series, IMOEX=idx_dn), today, ["UP", "FL"]), {"L": 6, "K": 1, "market_filter": 1}, {}) == {}
    # мало истории -> пусто
    assert momentum(View(series, up["dates"][30], ["UP"]), {"L": 6, "K": 1, "market_filter": 0}, {}) == {}

    # momentum пропускает последний месяц: у UR 3-мес. доходность «без пропуска» отрицательна, «с пропуском» положительна
    ur2 = _series("UR2", d0, [10 + i * 0.1 for i in range(79)] + [17.9 - j * 0.5 for j in range(21)])
    fl2 = _series("FL2", d0, [10.0] * 100)
    v2 = View({"IMOEX": idx, "UR2": ur2, "FL2": fl2}, ur2["dates"][-1], ["UR2", "FL2"])
    trs = v2.last("UR2", "tr", 64)
    skip_score, plain_score = trs[-22] / trs[0] - 1, trs[-1] / trs[0] - 1
    assert skip_score > 0 > plain_score, (skip_score, plain_score)   # без пропуска выбрали бы FL2 (0 > отрицательного)
    assert set(momentum(v2, {"L": 3, "K": 1, "market_filter": 0}, {})) == {"UR2"}

    # Donchian: UP на новом максимуме -> вход; FL (не выше max 20) -> нет; удержание через state, выход при пробое вниз
    st = {}
    w = donchian(v, {"N": 20, "M": 10, "K": 5}, st)
    assert "UP" in w and "FL" not in w and "DN" not in w and close(w["UP"], 0.2), w
    # следующий день: DN продолжает падать (не входим); UP держим
    w2 = donchian(v, {"N": 20, "M": 10, "K": 5}, st)
    assert "UP" in w2
    # выход: серия, которая росла, потом упала ниже min за 10 дней
    peak = _series("PK", d0, [10 + i * 0.1 for i in range(n - 15)] + [10 + (n - 15) * 0.1 - i * 0.5 for i in range(15)])
    sp = {"IMOEX": idx, "PK": peak}
    st = {}
    donchian(View(sp, peak["dates"][n - 16], ["PK"]), {"N": 20, "M": 10, "K": 5}, st)   # вход на максимуме
    assert "PK" in st.get("held", {})
    w3 = donchian(View(sp, peak["dates"][-1], ["PK"]), {"N": 20, "M": 10, "K": 5}, st)  # пробой вниз
    assert "PK" not in w3

    # бумага перестала торговаться: слот Donchian освобождается, equal_weight её не берёт
    dead = _series("DD", d0, [10 + i * 0.1 for i in range(n - 10)])        # последний бар за 10 дней до today
    vd = View({"IMOEX": idx, "DD": dead, "UP": up}, today, ["DD", "UP"])
    st = {"held": {"DD": True}}
    wd = donchian(vd, {"N": 20, "M": 10, "K": 5}, st)
    assert "DD" not in wd and "DD" not in st["held"], (wd, st)
    assert equal_weight(vd, {}, {}) == {"UP": 1.0}
    assert "DD" not in momentum(vd, {"L": 3, "K": 2, "market_filter": 0}, {})
    assert "DD" not in trend_vol(vd, {"N": 50, "target_vol": 0.15}, {})
    assert "DD" not in random_portfolio(vd, {"K": 2, "seed": 1}, {})

    # trend_vol: обе выше SMA50; менее волатильная получает больший вес; сумма <= 1
    noisy = _series("NZ", d0, [10 + i * 0.1 + (1.5 if i % 2 else -1.5) for i in range(n)])
    tv = {"IMOEX": idx, "UP": up, "NZ": noisy, "DN": down}
    w = trend_vol(View(tv, today, ["UP", "NZ", "DN"]), {"N": 50, "target_vol": 0.15}, {})
    assert "DN" not in w and w.get("UP", 0) > w.get("NZ", 0) > 0 and sum(w.values()) <= 1.0 + 1e-9, w

    # random: K бумаг, детерминирован по seed, равные веса
    w = random_portfolio(v, {"K": 2, "seed": 7}, {})
    assert len(w) == 2 and close(sum(w.values()), 1.0) and w == random_portfolio(v, {"K": 2, "seed": 7}, {})
    assert equal_weight(v, {}, {}) == {t: 0.25 for t in ["UP", "FL", "DN", "UR"]}

    vs = grid_variants({"a": [1, 2], "b": [0, 1]})
    assert vs == [{"a": 1, "b": 0}, {"a": 1, "b": 1}, {"a": 2, "b": 0}, {"a": 2, "b": 1}]
    assert len(grid_variants(FAMILIES["momentum"]["grid"])) == 18 and len(grid_variants(FAMILIES["donchian"]["grid"])) == 12 \
        and len(grid_variants(FAMILIES["trend_vol"]["grid"])) == 9

    # дивидендный гэп: close падает на 10 %, tr не меняется -> ни выхода Donchian, ни пробоя SMA;
    # то же падение без дивиденда (tr падает вместе с close) -> выход и пробой
    base = [10 + i * 0.01 for i in range(299)]
    gap_closes = base + [base[-1] * 0.9]
    no_div = _series("G", d0, gap_closes)
    with_div = _series("G", d0, gap_closes)
    with_div["tr"] = no_div["tr"][:-1] + [no_div["tr"][-2]]
    for s_g, dropped in ((with_div, False), (no_div, True)):
        vg = View({"IMOEX": idx, "G": s_g}, s_g["dates"][-1], ["G"])
        st = {"held": {"G": True}}
        donchian(vg, {"N": 20, "M": 10, "K": 5}, st)
        assert ("G" not in st["held"]) == dropped, (dropped, st)
        assert ("G" not in trend_vol(vg, {"N": 50, "target_vol": 0.15}, {})) == dropped, dropped
    print("bt_strategies ok")


def test_bt_protocol():
    import json
    import bt_protocol as bp
    # соседи по сетке: ±1 шаг по одной оси
    grid = {"L": [3, 6, 12], "K": [3, 5, 8]}
    nb = bp.neighbors({"L": 6, "K": 3}, grid)
    assert {json.dumps(x, sort_keys=True) for x in nb} == {json.dumps(x, sort_keys=True) for x in
                                                           [{"L": 3, "K": 3}, {"L": 12, "K": 3}, {"L": 6, "K": 5}]}
    # плато: пик 1.0 в углу с соседями 0; плато 0.6 с соседями 0.6 -> выбираем плато
    scores = [({"L": 3, "K": 3}, 1.0), ({"L": 6, "K": 3}, 0.0), ({"L": 3, "K": 5}, 0.0),
              ({"L": 12, "K": 8}, 0.6), ({"L": 6, "K": 8}, 0.6), ({"L": 12, "K": 5}, 0.6)]
    best, score = bp.pick_plateau(scores, grid)
    assert best == {"L": 12, "K": 8} and close(score, 0.6), (best, score)
    # сетка с одной осью из одного значения — нет деления на ноль
    best, score = bp.pick_plateau([({"N": 50, "t": 0.1}, 0.5)], {"N": [50], "t": [0.1]})
    assert best == {"N": 50, "t": 0.1} and close(score, 0.5)
    assert close(bp.percentile([1, 2, 3, 4], 3.5), 0.75) and close(bp.percentile([], 1.0), 0.0)

    # валидация
    tr = {"mdd": -0.20}
    ok, why = bp.validate(tr, {"xirr": 0.20, "sharpe": 1.0, "mdd": -0.25}, {"xirr": 0.10}, [0.1, 0.2, 0.3, 0.4, 0.5])
    assert ok and not why
    ok, why = bp.validate(tr, {"xirr": 0.05, "sharpe": 1.0, "mdd": -0.25}, {"xirr": 0.10}, [0.1])
    assert not ok and any("LQDT" in w for w in why)
    ok, why = bp.validate(tr, {"xirr": 0.20, "sharpe": 0.1, "mdd": -0.25}, {"xirr": 0.10}, [0.2, 0.3, 0.4, 0.5, 0.6])
    assert not ok and any("случайн" in w for w in why)
    ok, why = bp.validate(tr, {"xirr": 0.20, "sharpe": 1.0, "mdd": -0.35}, {"xirr": 0.10}, [0.1])
    assert not ok and any("просадка" in w for w in why)
    ok, why = bp.validate(tr, {"xirr": None, "sharpe": 1.0, "mdd": -0.25}, {"xirr": 0.10}, [0.1])
    assert not ok and why

    assert bp.years_beating({2020: 0.1, 2021: 0.2, 2022: -0.1}, {2020: 0.05, 2021: 0.3, 2022: 0.05}) == (1, 3)
    assert close(bp.top2_share({2020: 0.1, 2021: 0.3, 2022: 0.1, 2023: 0.5}), 0.8)

    fam, params = bp.parse_finalist("momentum:L=12,K=5,market_filter=1")
    assert fam == "momentum" and params == {"L": 12, "K": 5, "market_filter": 1}
    fam, params = bp.parse_finalist("trend_vol:N=100,target_vol=0.15")
    assert params == {"N": 100, "target_vol": 0.15}
    for bad in ("momentum:L=7,K=5,market_filter=1", "momentum:L=12,K=5.9,market_filter=1"):
        try:
            bp.parse_finalist(bad)
            assert False, bad
        except ValueError:
            pass

    # журнал закрытого окна во временной папке
    m12 = ("momentum", {"L": 12, "K": 5, "market_filter": 1})
    m6 = ("momentum", {"L": 6, "K": 5, "market_filter": 1})
    dn = ("donchian", {"N": 55, "M": 20, "K": 5})
    old_home = os.environ.get("TINVEST_HOME")
    try:
        os.environ["TINVEST_HOME"] = tempfile.mkdtemp(prefix="tinvest-holdout-")
        assert bp.holdout_trials() == 1                       # пустой журнал — не меньше 1
        assert bp.record_holdout([m12]) == []
        assert bp.record_holdout([m12]) == []                 # тот же набор — не нарушение
        assert bp.holdout_trials() == 1                       # один и тот же финалист дважды
        viol = bp.record_holdout([m6])
        assert viol and all("momentum" in v and "L\": 12" in v for v in viol), viol
        assert len(json.loads(bp.holdout_log_path().read_text(encoding="utf-8"))) == 3
        assert bp.holdout_trials() == 2                       # два различных финалиста
        # другое семейство тоже нарушение: ранний набор назван в сообщении
        os.environ["TINVEST_HOME"] = tempfile.mkdtemp(prefix="tinvest-holdout-")
        assert bp.record_holdout([m12]) == []
        viol = bp.record_holdout([dn])
        assert len(viol) == 1 and "momentum" in viol[0] and "L\": 12" in viol[0], viol
        # набор из двух — повтор того же набора не нарушение, подмножество — нарушение
        os.environ["TINVEST_HOME"] = tempfile.mkdtemp(prefix="tinvest-holdout-")
        assert bp.record_holdout([m12, dn]) == [] and bp.record_holdout([dn, m12]) == []
        assert bp.record_holdout([m12]) and bp.holdout_trials() == 2
    finally:
        if old_home is None:
            os.environ.pop("TINVEST_HOME", None)
        else:
            os.environ["TINVEST_HOME"] = old_home

    # вердикт: (ИИС, брокерский, LQDT ИИС, LQDT брокерский, TMOS, равные веса, просадка VALID, p)
    lq_i, lq_b, tm, ew = {"xirr": 0.18}, {"xirr": 0.16}, {"xirr": 0.05}, {"xirr": 0.08}
    v, _ = bp.verdict({"xirr": 0.25, "mdd": -0.2}, {"xirr": 0.22}, lq_i, lq_b, tm, ew, -0.2, 0.01)
    assert v == 1
    # ИИС выше LQDT ИИС, но брокерский (0.15) ниже LQDT брокерского (0.16) -> не вердикт 1
    v, _ = bp.verdict({"xirr": 0.25, "mdd": -0.2}, {"xirr": 0.15}, lq_i, lq_b, tm, ew, -0.2, 0.01)
    assert v == 2
    # брокерский 0.17 ниже LQDT ИИС 0.18, но выше LQDT брокерского 0.16 -> вердикт 1
    v, _ = bp.verdict({"xirr": 0.25, "mdd": -0.2}, {"xirr": 0.17}, lq_i, lq_b, tm, ew, -0.2, 0.01)
    assert v == 1
    v, _ = bp.verdict({"xirr": 0.15, "mdd": -0.2}, {"xirr": 0.13}, lq_i, lq_b, tm, ew, -0.2, 0.01)
    assert v == 2
    v, _ = bp.verdict({"xirr": 0.03, "mdd": -0.2}, {"xirr": 0.02}, lq_i, lq_b, tm, ew, -0.2, 0.3)
    assert v == 3

    # сквозной прогон на синтетике: run_variant и random_distribution работают, threshold_rate возвращает число
    d0 = date(2014, 1, 1)
    n = 400
    series = {"IMOEX": _series("IMOEX", d0, [100 + i * 0.05 for i in range(n)]),
              "LQDT": _series("LQDT", d0, [1 + i * 0.0002 for i in range(n)]),
              "TMOS": _series("TMOS", d0, [5 + i * 0.002 for i in range(n)]),
              "A": _series("A", d0, [10 + i * 0.02 for i in range(n)]),
              "B": _series("B", d0, [10 + (i % 7) * 0.1 for i in range(n)])}
    win = (d0, series["A"]["dates"][-1])
    s = bp.run_variant(series, "trend_vol", {"N": 50, "target_vol": 0.15}, win, universe=["A", "B"])
    assert s["xirr"] is not None and "result" in s
    dist = bp.random_distribution(series, win, n_runs=3, k=1, universe=["A", "B"])
    assert len(dist) == 3
    b = bp.run_benchmark(series, "TMOS", win)
    assert b["xirr"] is not None and b["in_market"] > 0.9
    thr = bp.threshold_rate(series, "trend_vol", {"N": 50, "target_vol": 0.15}, win, universe=["A", "B"])
    assert thr is None or 0.0 < thr < 0.5
    rob = bp.robustness(series, "trend_vol", {"N": 50, "target_vol": 0.15}, win, universe=["A", "B"], capital=1000.0)
    assert all(r["result"]["capital"] == 1000.0 for r in rob.values()), rob.keys()
    assert thr is None   # на этой фикстуре нет смены знака между 0.02 и 0.40 — ветка «не нашли»
    # скобка есть: стратегия держит только A с постоянным дневным ростом; эталон — независимая бисекция по synthetic_safe
    from bt_data import synthetic_safe
    series2 = dict(series)
    series2["A"] = _series("A", d0, [10 * 1.0006 ** i for i in range(n)])
    bp.FAMILIES["_hold_a"] = {"fn": lambda v, p, s: {"A": 1.0} if v.has("A", 1) else {}, "grid": {"x": [1]},
                              "rebalance": "monthly"}
    try:
        win2 = (d0, series2["A"]["dates"][-1])
        x_strat = bp.run_variant(series2, "_hold_a", {"x": 1}, win2, universe=["A"])["xirr"]
        cal = series2["IMOEX"]["dates"]
        days = (cal[-1] - cal[0]).days
        lo_r, hi_r = 0.0, 0.6
        for _ in range(50):
            mid = (lo_r + hi_r) / 2
            syn = synthetic_safe(cal, [(date(1900, 1, 1), mid)], spread=0.0)
            if (syn[-1] / syn[0]) ** (365.0 / days) - 1.0 < x_strat:
                lo_r = mid
            else:
                hi_r = mid
        thr2 = bp.threshold_rate(series2, "_hold_a", {"x": 1}, win2, universe=["A"])
        assert thr2 is not None and abs(thr2 - lo_r) < 0.005, (thr2, lo_r)
    finally:
        del bp.FAMILIES["_hold_a"]
    print("bt_protocol ok")


def test_research_report():
    import research
    d0 = date(2014, 1, 1)
    n = 700
    series = {"IMOEX": _series("IMOEX", d0, [100 + i * 0.05 for i in range(n)]),
              "LQDT": _series("LQDT", d0, [1 + i * 0.0002 for i in range(n)]),
              "TMOS": _series("TMOS", d0, [5 + i * 0.002 for i in range(n)]),
              "A": _series("A", d0, [10 + i * 0.02 for i in range(n)]),
              "B": _series("B", d0, [10 + (i % 7) * 0.1 for i in range(n)]),
              "C": _series("C", d0, [20 - i * 0.01 for i in range(n)])}
    end = series["A"]["dates"][-1]
    windows = {"TRAIN": (d0, series["A"]["dates"][400]), "VALID": (series["A"]["dates"][401], end)}
    text, data = research.train_report(series, ["trend_vol"], n_random=3, capital=1000.0, windows=windows, universe=["A", "B", "C"])
    assert "## trend_vol" in text and "Финалист" in text and "Валидация" in text and "Устойчивость" in text, text[:500]
    assert data["trend_vol"]["finalist"] in [{"N": n_, "target_vol": v} for n_ in (50, 100, 200) for v in (0.1, 0.15, 0.2)]
    text, data = research.holdout_report(series, [("trend_vol", data["trend_vol"]["finalist"])], capital=1000.0,
                                         violations=["trend_vol: тест"], window=(series["A"]["dates"][401], end),
                                         valid_mdd={"trend_vol": -0.1}, universe=["A", "B", "C"], n_trials=2)
    assert text.startswith("# ") and "Нарушение протокола" in text and "Вердикт" in text and data["verdict"] in (1, 2, 3)
    assert "при 2 финалистах на закрытом окне" in text, text
    # у LQDT вместо Sharpe прочерк; в колонке сделок — только сделки с акциями
    lq_rows = [ln for ln in text.splitlines() if ln.startswith("| LQDT (")]
    assert lq_rows and all(ln.split(" | ")[4] == "—" and ln.split(" | ")[5] == "0" for ln in lq_rows), lq_rows
    print("research ok")


def _bond_series(n=550, d0=date(2020, 1, 1)):
    """Синтетика облигационной части: индексы растут с разной скоростью и месячной «волной», ставка 6%, КБД 6.5%."""
    import bond_rules as br
    import moex_iss as mi
    from datetime import timedelta
    days = [d0 + timedelta(days=i) for i in range(n)]

    def lvl(t, vals):
        return mi._series(t, days, vals)

    s = {"RGBITR": lvl("RGBITR", [100.0 * 1.0004 ** i for i in range(n)]),
         "LQDT": lvl("LQDT", [1.0003 ** i for i in range(n)]), "KEYRATE": lvl("KEYRATE", [0.06] * n)}
    s["RGBITR"]["duration"], s["RGBITR"]["yield"] = [4.0] * n, [0.07] * n
    for k, b in enumerate(br.BUCKETS):
        s[b] = lvl(b, [100.0 * (1.0002 + 0.0001 * k) ** i * (1 + 0.01 * ((i // 30) % 2)) for i in range(n)])
        s[b]["duration"], s[b]["yield"] = [float(k + 1)] * n, [0.065 + 0.001 * k] * n
    for t in mi.ZCYC_TICKERS.values():
        s[t] = lvl(t, [0.065] * n)
    return s


def test_bond_research():
    import bond_research as bres
    import bond_rules as br
    import bt_protocol as bp
    old_home = os.environ.get("TINVEST_HOME")
    try:
        os.environ["TINVEST_HOME"] = tempfile.mkdtemp(prefix="tinvest-bonds-")
        s = _bond_series()
        windows = {"TRAIN": (date(2020, 1, 1), date(2020, 12, 31)), "VALID": (date(2021, 1, 1), date(2021, 6, 30)),
                   "HOLDOUT": (date(2021, 1, 1), date(2021, 7, 3))}
        text, data = bres.train_report(s, ["curve"], 3, 1_000_000.0, windows)
        assert "Финалист" in text and br.CURRENT_NAME in text and "Допущения" in text, text[:500]
        assert data["curve"]["finalist"] in bp.all_variants("curve", br.FAMILIES)
        # журнал облигаций — свой файл; журнал акций не создаётся
        fam, params = bp.parse_finalist("curve:bucket=OFZ10Y,s=0.01", br.FAMILIES)
        assert (fam, params) == ("curve", {"bucket": "OFZ10Y", "s": 0.01})
        assert bp.record_holdout([(fam, params)], bres.holdout_log()) == []
        assert bres.holdout_log().exists() and not bp.holdout_log_path().exists()
        assert bp.holdout_trials(bres.holdout_log()) == 1
        assert bp.record_holdout([("curve", {"bucket": "OFZ5Y", "s": 0.01})], bres.holdout_log())   # другой набор — нарушение
        text, hd = bres.holdout_report(s, [(fam, params)], 1_000_000.0, [], n_trials=2, windows=windows)
        assert "Вердикт" in text and hd["verdict"] in (1, 2, 3) and hd["results"][0]["params"] == params
        # правило, не обыгравшее «как сейчас», не получает вердикт 1–2
        cur = {"xirr": 0.10}
        assert bres.cap_verdict(1, "x", {"xirr": 0.09}, cur)[0] == 3 and bres.cap_verdict(2, "x", {"xirr": 0.11}, cur) == (2, "x")
    finally:
        if old_home is None:
            os.environ.pop("TINVEST_HOME", None)
        else:
            os.environ["TINVEST_HOME"] = old_home
    print("bond_research ok")


def test_bond_advisor():
    import yaml
    import bond_advisor as ba
    import bond_research as bres
    from datetime import timedelta
    t0 = date(2026, 1, 1)
    assert [ba.bucket_of(x) for x in (0.5, 2.0, 5.0, 7.0)] == ["OFZ1Y", "OFZ3Y", "OFZ5Y", "OFZ10Y"]
    assert ba.bucket_of(None) is None and ba.bucket_of(None, floater=True) == "OFZ1Y"
    # бескупонная на год: цена 900 при номинале 1000 -> доходность 1000/900 − 1, модифицированная дюрация 1/(1+y)
    y, dur = ba.bond_metrics(900.0, 1000.0, [], t0 + timedelta(days=365), t0)
    assert close(y, 1000 / 900 - 1, 1e-6) and close(dur, 900 / 1000, 1e-6), (y, dur)
    good = {"ticker": "SU26238RMFS4", "maturityDate": "2041-05-15T00:00:00Z", "currency": "rub",
            "forIisFlag": True, "tradingStatus": "SECURITY_TRADING_STATUS_NORMAL_TRADING"}
    assert ba.is_candidate(good, t0)
    assert ba.is_candidate({k: v for k, v in good.items() if k != "tradingStatus"}, t0)  # статус не указан — не проверяем
    for bad in (dict(good, ticker="RU000A10C6F7"), dict(good, amortizationFlag=True), dict(good, ticker="SU52005RMFS4"),
                dict(good, maturityDate="2026-01-15T00:00:00Z"), dict(good, buyAvailableFlag=False), dict(good, currency="usd"),
                dict(good, forIisFlag=False), dict(good, tradingStatus="SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING")):
        assert not ba.is_candidate(bad, t0), bad
    assert close(ba.turnover_rub([{"close": 100.0, "volume": 10}] * 25, 1000.0, 1), 10_000.0)
    assert ba.turnover_rub([], 1000.0, 1) == 0.0
    part = [{"time": "2026-01-01T00:00:00Z", "close": 100.0, "volume": 10}, {"time": "2026-01-02T00:00:00Z", "close": 100.0, "volume": 1000}]
    assert close(ba.turnover_rub(part, 1000.0, 1, today=date(2026, 1, 2)), 10_000.0)  # сегодняшняя свеча неполная
    mat = date(2035, 1, 1)
    rows = [{"ticker": "A", "floater": False, "ytm": 0.15, "duration": 6.0, "turnover": 2e7, "maturity": mat, "next_coupon": None},
            {"ticker": "B", "floater": False, "ytm": 0.16, "duration": 7.0, "turnover": 2e7, "maturity": mat, "next_coupon": None},
            {"ticker": "C", "floater": False, "ytm": 0.20, "duration": 7.0, "turnover": 1e6, "maturity": mat, "next_coupon": None},  # неликвид
            {"ticker": "D", "floater": True, "ytm": None, "duration": None, "turnover": 5e7, "maturity": mat,
             "next_coupon": (date(2026, 2, 1), 40.0)}]
    by, fl = ba.pick(rows)
    assert [r["ticker"] for r in by["OFZ10Y"]] == ["B", "A"] and [r["ticker"] for r in fl] == ["D"]
    groups = ba.fragment_groups({"LQDT": 0.2, "OFZ10Y": 0.5, "OFZ1Y": 0.3}, 50.0, {"OFZ10Y": ["B", "A"], "OFZ1Y": ["D"]},
                                {ba.GROUP_FIXED: ["SU26207RMFS9"]})
    cfg = yaml.safe_load(ba.render_groups(groups))
    g = {x["name"]: x for x in cfg["groups"]}
    assert close(sum(x["weight"] for x in cfg["groups"]), 50.0, 0.2)
    assert close(g[ba.GROUP_FIXED]["weight"], 25.0) and close(g[ba.GROUP_SHORT]["weight"], 15.0) and close(g[ba.GROUP_CASH]["weight"], 10.0)
    assert close(sum(i["weight"] for i in g[ba.GROUP_FIXED]["items"]), 100.0, 0.2)
    assert {"ticker": "SU26207RMFS9", "weight": 0} in g[ba.GROUP_FIXED]["items"]
    # доля корзины без ликвидного выпуска уходит в LQDT; сумма групп = облигационная часть, внутри групп 100
    g2 = ba.fragment_groups({"LQDT": 0.2, "OFZ10Y": 0.5, "OFZ5Y": 0.3}, 50.0, {"OFZ10Y": ["B", "Z"]}, {ba.GROUP_FIXED: ["Z"], ba.GROUP_SHORT: ["Z", "Q"]})
    gd = {x["name"]: x for x in g2}
    assert close(sum(x["weight"] for x in g2), 50.0) and close(gd[ba.GROUP_FIXED]["weight"], 25.0) and close(gd[ba.GROUP_CASH]["weight"], 25.0)
    assert close(gd[ba.GROUP_SHORT]["weight"], 0.0) and [i["ticker"] for i in gd[ba.GROUP_SHORT]["items"]] == ["Q"]  # Z куплен в другой группе
    for x in g2:
        if x["weight"] > 0:
            assert close(sum(i["weight"] for i in x["items"]), 100.0, 0.2), x
    # текущая структура и замена гасящегося выпуска
    cur_groups = [{"name": "Акции РФ", "weight": 40.0, "items": [{"ticker": "TMOS@", "weight": 100.0}]},
                  {"name": ba.GROUP_FIXED, "weight": 35.0, "items": [{"ticker": "X", "weight": 100.0}]},
                  {"name": ba.GROUP_SHORT, "weight": 10.0, "items": [{"ticker": "F", "weight": 100.0}]},
                  {"name": ba.GROUP_CASH, "weight": 5.0, "items": [{"ticker": "LQDT", "weight": 100.0}]}]
    w, sleeve = ba.current_weights(cur_groups)
    assert close(sleeve, 50.0) and close(w["OFZ10Y"], 0.7) and close(w["OFZ1Y"], 0.2) and close(w["LQDT"], 0.1), (w, sleeve)
    new, notes = ba.replace_maturing(cur_groups, {"F": "OFZ1Y"}, {"OFZ1Y": ["D"]})
    short = [x for x in new if x["name"] == ba.GROUP_SHORT][0]
    assert {i["ticker"]: i["weight"] for i in short["items"]} == {"F": 0.0, "D": 100.0} and notes
    assert all(x["name"] != "Акции РФ" for x in new)
    dup = [dict(g_, items=list(g_["items"])) for g_ in cur_groups]
    dup[1]["items"].append({"ticker": "D", "weight": 0.0})  # D уже в другой группе — туда замену не ставим
    new2, n2 = ba.replace_maturing(dup, {"F": "OFZ1Y"}, {"OFZ1Y": ["D", "E"]})  # D занят в другой группе -> берём E
    sh2 = {i["ticker"]: i["weight"] for i in [x for x in new2 if x["name"] == ba.GROUP_SHORT][0]["items"]}
    assert sh2 == {"F": 0.0, "E": 100.0} and any("D" in n and "E" in n for n in n2), (sh2, n2)
    new3, n3 = ba.replace_maturing(dup, {"F": "OFZ1Y"}, {})  # замены нет -> доля 10 из 50 уходит в LQDT
    g3 = {x["name"]: x for x in new3}
    assert close(g3[ba.GROUP_SHORT]["weight"], 0.0) and {i["ticker"]: i["weight"] for i in g3[ba.GROUP_SHORT]["items"]} == {"F": 0.0}
    assert close(g3[ba.GROUP_CASH]["weight"], 15.0) and {i["ticker"]: i["weight"] for i in g3[ba.GROUP_CASH]["items"]} == {"LQDT": 100.0}
    assert close(sum(x["weight"] for x in new3), 50.0) and any("LQDT" in n for n in n3), (g3, n3)
    new4, _ = ba.replace_maturing(dup, {"F": "OFZ1Y"}, {"OFZ1Y": ["D"]})  # единственный кандидат занят -> тоже LQDT
    assert close({x["name"]: x for x in new4}[ba.GROUP_CASH]["weight"], 15.0)

    def check_fragment(frag, maturing, sleeve):
        """Инварианты фрагмента: гасящиеся с весом 0, сумма групп = облигационная часть, внутри групп с весом > 0 — 100,
        тикер не повторяется."""
        seen = []
        for g_ in frag:
            items = {i["ticker"]: i["weight"] for i in g_["items"]}
            seen += list(items)
            assert all(items[t] == 0 for t in maturing if t in items), (g_, maturing)
            if g_["weight"] > 0:
                assert close(sum(items.values()), 100.0), g_
        assert close(sum(g_["weight"] for g_ in frag), sleeve), frag
        assert len(seen) == len(set(seen)), seen
    for frag in (groups, g2):
        check_fragment(frag, set(), 50.0)
    for frag in (new, new2, new3, new4):
        check_fragment(frag, {"F"}, 50.0)
    # два гасящихся выпуска в одной группе: гасящийся кандидат не берётся, доли считаются от исходных весов,
    # порядок строк на результат не влияет
    mat2 = {"A": "OFZ1Y", "B": "OFZ1Y"}

    def short_cash(order, weights=(50.0, 50.0), gw=10.0):
        return [{"name": ba.GROUP_SHORT, "weight": gw, "items": [{"ticker": t, "weight": w} for t, w in zip(order, weights)]},
                {"name": ba.GROUP_CASH, "weight": 5.0, "items": [{"ticker": "LQDT", "weight": 100.0}]}]
    for order in (["B", "A"], ["A", "B"]):
        r1, n1 = ba.replace_maturing(short_cash(order), mat2, {"OFZ1Y": ["B", "C"]})  # B сам гасится -> оба уходят в C
        d1 = {x["name"]: x for x in r1}
        assert close(d1[ba.GROUP_SHORT]["weight"], 10.0) and {i["ticker"]: i["weight"] for i in d1[ba.GROUP_SHORT]["items"]} == {"A": 0.0, "B": 0.0, "C": 100.0}, (order, r1)
        assert close(d1[ba.GROUP_CASH]["weight"], 5.0) and any("B" in n and "гасится" in n and "C" in n for n in n1), n1
        check_fragment(r1, mat2, 15.0)
        r2, n2_ = ba.replace_maturing(short_cash(order), mat2, {"OFZ1Y": ["B"]})  # единственный кандидат гасится -> всё в LQDT
        d2 = {x["name"]: x for x in r2}
        assert close(d2[ba.GROUP_SHORT]["weight"], 0.0) and {i["ticker"]: i["weight"] for i in d2[ba.GROUP_SHORT]["items"]} == {"A": 0.0, "B": 0.0}, (order, r2)
        assert close(d2[ba.GROUP_CASH]["weight"], 15.0) and {i["ticker"]: i["weight"] for i in d2[ba.GROUP_CASH]["items"]} == {"LQDT": 100.0}
        assert sum("LQDT" in n for n in n2_) == 2, n2_
        check_fragment(r2, mat2, 15.0)
        # A -> C, B без замены: в LQDT уходит ровно доля B (10 × 50/100 = 5), остаток группы снова 100
        r3, _ = ba.replace_maturing(short_cash(order), {"A": "OFZ1Y", "B": "FLOAT"}, {"OFZ1Y": ["C"]})
        d3 = {x["name"]: x for x in r3}
        assert close(d3[ba.GROUP_SHORT]["weight"], 5.0) and {i["ticker"]: i["weight"] for i in d3[ba.GROUP_SHORT]["items"]} == {"A": 0.0, "B": 0.0, "C": 100.0}, (order, r3)
        assert close(d3[ba.GROUP_CASH]["weight"], 10.0)
        check_fragment(r3, mat2, 15.0)
    # группа без веса: гасящийся выпуск обнуляется, в LQDT ничего не переносится — и заметка об этом не врёт
    r4, n4 = ba.replace_maturing(short_cash(["A"], (100.0,), gw=0.0), {"A": "OFZ1Y"}, {})
    d4 = {x["name"]: x for x in r4}
    assert d4[ba.GROUP_SHORT]["weight"] == 0.0 and {i["ticker"]: i["weight"] for i in d4[ba.GROUP_SHORT]["items"]} == {"A": 0.0}
    assert close(d4[ba.GROUP_CASH]["weight"], 5.0) and len(n4) == 1 and "LQDT" not in n4[0] and "A" in n4[0], n4
    check_fragment(r4, {"A"}, 5.0)
    # советник: сигнал финалиста с вердиктом 2 становится рекомендацией; без финалистов — текущая структура
    s = _bond_series()
    fin = {"curve": {"params": {"bucket": "OFZ10Y", "s": -0.01}, "passed": True}}
    hold = {"results": [{"family": "curve", "params": {"bucket": "OFZ10Y", "s": -0.01}, "verdict": 2}]}
    sig, rec, name = bres.signals(s, fin, hold)
    assert rec is not None and close(sum(rec.values()), 1.0) and sig[0][3] == "рекомендация", (sig, rec)
    t_last = s["RGBITR"]["dates"][-1]
    held = [{"ticker": "F", "name": "Флоатер", "value_rub": 1000.0, "ytm": None, "duration_mod": 0.2, "floater": True,
             "maturity": t_last + timedelta(days=100), "call": None, "ofz": True}]
    # кандидаты, которые сами гасятся в ближайшие 12 месяцев (S) или уже гасятся в портфеле (F), в замену не идут —
    # хотя по обороту они впереди D
    rows2 = rows + [{"ticker": "S", "floater": True, "ytm": None, "duration": None, "turnover": 9e7, "maturity": t_last + timedelta(days=200),
                     "next_coupon": None},
                    {"ticker": "F", "floater": True, "ytm": None, "duration": None, "turnover": 8e7, "maturity": t_last + timedelta(days=100),
                     "next_coupon": None}]
    text, out = bres.advise_report(s, {}, {}, held, rows2, [], cur_groups)
    assert "Фрагмент targets.yaml" in text and "D" in text and out["weights"] is None, text
    og = {x["name"]: x for x in out["groups"]}
    assert {i["ticker"]: i["weight"] for i in og[ba.GROUP_SHORT]["items"]} == {"F": 0.0, "D": 100.0}, og
    check_fragment(out["groups"], {"F"}, 50.0)
    text, out = bres.advise_report(s, fin, hold, held, rows2, ["предупреждение"], cur_groups)
    assert out["weights"] == rec and "предупреждение" in text
    assert {"ticker": "F", "weight": 0} in [i for g2 in out["groups"] for i in g2["items"]], out["groups"]  # гасящийся — с весом 0
    assert "S" not in {i["ticker"] for g2 in out["groups"] for i in g2["items"]}, out["groups"]
    check_fragment(out["groups"], {"F"}, 50.0)
    # collect_candidates с поддельным API
    class FakeApi:
        def list_bonds(self):
            base = {"currency": "rub", "forIisFlag": True, "maturityDate": "2040-01-01T00:00:00Z", "nominal": {"units": "1000", "nano": 0}}
            return [dict(base, uid="u1", ticker="SU1", name="Один", lot=2, aciValue={"units": "10", "nano": 500000000}),
                    dict(base, uid="u2", ticker="SU2", lot=1),                       # нет цены
                    dict(base, uid="u3", ticker="SU3", lot=1),                       # нет купонов
                    dict(base, uid="u4", ticker="SU4", lot=1, floatingCouponFlag=True),
                    dict(base, uid="u5", ticker="SU5", lot=1),                       # доходность не сходится
                    dict(base, uid="u6", ticker="SU6", lot=1),                       # неликвид
                    dict(base, uid="u7", ticker="RU7", lot=1)]                       # не ОФЗ

        def get_last_prices(self, uids):
            return {"u1": 100.0, "u3": 100.0, "u4": 100.0, "u5": 0.01, "u6": 100.0}

        def get_day_candles(self, uid, a, b):
            vol = 1 if uid == "u6" else 100000
            return [{"time": "2026-01-01T00:00:00Z", "close": 100.0, "volume": vol}]

        def get_bond_coupons(self, uid, figi, a, b):
            if uid == "u3":
                return []
            return [{"couponDate": "2026-07-01T00:00:00Z", "payOneBond": {"units": "50", "nano": 0}},
                    {"couponDate": "2027-01-01T00:00:00Z", "payOneBond": {"units": "50", "nano": 0}}]

    crow, cwarn = bres.collect_candidates(FakeApi(), date(2026, 1, 2))
    cd = {r["ticker"]: r for r in crow}
    assert sorted(cd) == ["SU1", "SU4"], sorted(cd)
    assert close(cd["SU1"]["turnover"], 100000 * 2 * 1000.0), cd["SU1"]  # лоты × лот × цена(% номинала) × номинал
    y1, d1 = ba.bond_metrics(1000.0 + 10.5, 1000.0, [(date(2026, 7, 1), 50.0), (date(2027, 1, 1), 50.0)], date(2040, 1, 1), date(2026, 1, 2))
    assert close(cd["SU1"]["ytm"], y1, 1e-9) and close(cd["SU1"]["duration"], d1, 1e-9), (cd["SU1"], y1, d1)
    assert cd["SU4"]["ytm"] is None and cd["SU4"]["floater"]
    wtext = " ".join(cwarn)
    assert "SU2" in wtext and "SU3" in wtext and "SU5" in wtext and "SU6" not in wtext and "RU7" not in wtext, cwarn
    # advise_main без токена: блоки 1 и 4 есть, кандидаты пропущены с пометкой
    import io
    import contextlib
    import tempfile as _tf
    tvars = ("TINVEST_TOKEN", "T_INVEST_IIS_READ", "T_INVEST_TOKEN", "T_INVEST_READ", "TINVEST_HOME")
    saved = {k: os.environ.get(k) for k in tvars}
    orig = (bres.held_bonds, bres.mi.fetch_zcyc)
    try:
        with _tf.TemporaryDirectory() as td:
            for k in tvars:
                os.environ.pop(k, None)
            os.environ["TINVEST_HOME"] = td
            bres.held_bonds = lambda: []

            def no_net(d):
                raise OSError("нет сети")
            bres.mi.fetch_zcyc = no_net
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                bres.advise_main(_bond_series())
            txt = buf.getvalue()
            assert "## Рынок сейчас" in txt and "## Фрагмент targets.yaml" in txt, txt
            assert "Нет доступа к API — кандидаты не подобраны" in txt and "нет доступа к API" in txt, txt
    finally:
        bres.held_bonds, bres.mi.fetch_zcyc = orig
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("bond_advisor ok")


if __name__ == "__main__":
    test_xirr()
    test_ytm_duration()
    test_fifo()
    test_allocate()
    test_reports()
    test_fetch_targets()
    test_backtest()
    test_sharpe()
    test_bt_data()
    test_bt_sim()
    test_bt_strategies()
    test_bt_protocol()
    test_research_report()
    test_moex_iss()
    test_bond_series()
    test_bond_rules()
    test_bond_research()
    test_bond_advisor()
    print("ALL OK")
