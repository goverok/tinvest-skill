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
    assert first_a["qty"] == 99 and min(e for _, e in r["equity"]) > 0, first_a

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
    assert close(r["dividends"], 100.0) and close(r["final"], 1100.0), (r["dividends"], r["final"])
    # на брокерском счёте дивиденд минус 13%
    r = simulate(s2, hold_then_sell, {}, tax_rate=0.13, **kw)
    assert close(r["dividends"], 87.0), r["dividends"]

    # налог по итогам года: прибыль 200 в 2024 -> 26 в первый день 2025
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
    assert close(s["final"], 1100.0) and s["xirr"] is not None and s["mdd"] <= 0 and s["n_obs"] == 6 and s["trades"] >= 2
    assert 2024 in s["yearly"] and 0 < s["in_market"] <= 1
    print("bt_sim ok")


if __name__ == "__main__":
    test_xirr()
    test_ytm_duration()
    test_fifo()
    test_allocate()
    test_reports()
    test_backtest()
    test_sharpe()
    test_bt_data()
    test_bt_sim()
    print("ALL OK")
