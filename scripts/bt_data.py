"""Данные для исследования стратегий: дневные свечи вселенной, сплиты, дивиденды, синтетический LQDT до 2020.

Кэш — ~/.tinvest/data/backtest/<TICKER>.json (как у backtest.py) и dividends.json. Серия (Series) — словарь
со списками одной длины: dates, open, high, low, close, volume, tr (индекс полной доходности, старт 1.0),
плюс divs [{ex, pay, amount}], lot, warnings. Все функции кроме load_universe — чистые.
"""
import bisect
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import currency_of, data_dir, money, parse_date, read_json, today, warn, write_json  # noqa: E402

Series = Dict[str, Any]

UNIVERSE = ["SBER", "GAZP", "LKOH", "GMKN", "NVTK", "ROSN", "TATN", "YDEX", "PLZL", "MGNT",
            "SNGS", "SNGSP", "MTSS", "CHMF", "NLMK", "ALRS", "VTBR", "MOEX", "T", "OZON",
            "PHOR", "IRAO", "AFLT", "RUAL", "MAGN", "PIKK", "FLOT", "X5", "BSPB", "SMLT"]
AUX = ["IMOEX", "LQDT", "TMOS"]
# 10 бумаг с историей с 2014 — для оценки survivorship bias (уточняется по датам первых свечей в отчёте)
OLDEST10 = ["SBER", "GAZP", "LKOH", "GMKN", "NVTK", "ROSN", "TATN", "MGNT", "SNGS", "MTSS"]
# Площадка по умолчанию TQBR; исключения — где в API торгуемый инструмент на другой
CLASS_CODE = {"IMOEX": "TQBR", "LQDT": "SPBRU", "TMOS": "SPBRU"}
API_TICKER = {"TMOS": "TMOS@"}
# Дневные свечи API уже скорректированы на сплиты (проверено: PLZL 2025-03, T 2026-04 без разрыва в сырых данных),
# поэтому записи сюда нужны только для сплитов, которых в сырой истории видно скачок (check_quality подскажет).
SPLITS: Dict[str, List[Tuple[str, float]]] = {}
# Ключевая ставка ЦБ РФ (дата вступления, ставка) — для синтетического LQDT до 2020-01-20
KEY_RATE: List[Tuple[date, float]] = [
    (date(2013, 9, 13), 0.055), (date(2014, 3, 3), 0.07), (date(2014, 4, 28), 0.075), (date(2014, 7, 28), 0.08),
    (date(2014, 11, 5), 0.095), (date(2014, 12, 12), 0.105), (date(2014, 12, 16), 0.17), (date(2015, 2, 2), 0.15),
    (date(2015, 3, 16), 0.14), (date(2015, 5, 5), 0.125), (date(2015, 6, 16), 0.115), (date(2015, 8, 3), 0.11),
    (date(2016, 6, 14), 0.105), (date(2016, 9, 19), 0.10), (date(2017, 3, 27), 0.0975), (date(2017, 5, 2), 0.0925),
    (date(2017, 6, 19), 0.09), (date(2017, 9, 18), 0.085), (date(2017, 10, 30), 0.0825), (date(2017, 12, 18), 0.0775),
    (date(2018, 2, 12), 0.075), (date(2018, 3, 26), 0.0725), (date(2018, 9, 17), 0.075), (date(2018, 12, 17), 0.0775),
    (date(2019, 6, 17), 0.075), (date(2019, 7, 29), 0.0725), (date(2019, 9, 9), 0.07), (date(2019, 10, 28), 0.065),
    (date(2019, 12, 16), 0.0625), (date(2020, 2, 10), 0.06),
]
SAFE_SPREAD = 0.005  # LQDT ≈ RUONIA ≈ ключевая − 0.5 п.п.
HISTORY_FROM = date(2013, 1, 1)  # год прогрева перед TRAIN
PAUSE_2022 = (date(2022, 2, 25), date(2022, 3, 31))


# ---------- построение серии ----------

def _split_factor(d: date, splits: Sequence[Tuple[str, float]]) -> float:
    f = 1.0
    for sd, ratio in splits:
        if d < date.fromisoformat(str(sd)[:10]):
            f *= float(ratio)
    return f


def make_series(ticker: str, candles: Sequence[Dict[str, Any]], lot: int = 1,
                splits: Sequence[Tuple[str, float]] = (), dividends: Sequence[Dict[str, Any]] = ()) -> Series:
    """Свечи API -> серия с поправкой на сплиты и индексом полной доходности. Дивиденд попадает в tr в день ex
    (первый торговый день после lastBuyDate), amount приводится к масштабу после сплита."""
    rows = sorted(((parse_date(c["time"]), c) for c in candles if c.get("close") and c.get("open")),
                  key=lambda r: r[0])
    s: Series = {"ticker": ticker, "lot": lot, "dates": [], "open": [], "high": [], "low": [], "close": [],
                 "volume": [], "tr": [], "divs": [], "warnings": []}
    for d, c in rows:
        f = _split_factor(d, splits)
        s["dates"].append(d)
        for k in ("open", "high", "low", "close"):
            s[k].append(money(c.get(k)) / f)
        s["volume"].append(float(c.get("volume") or 0) * f)
    for dv in dividends:
        amount = money(dv.get("dividendNet"))
        if amount <= 0:
            continue
        cur = currency_of(dv.get("dividendNet"))
        if cur != "rub":
            s["warnings"].append(f"{ticker}: дивиденд {str(dv.get('lastBuyDate') or dv.get('recordDate'))[:10]} в {cur} пропущен (нет курса)")
            continue
        last_buy = parse_date(dv.get("lastBuyDate")) or parse_date(dv.get("recordDate"))
        pay = parse_date(dv.get("paymentDate")) or last_buy
        if not last_buy or not s["dates"]:
            continue
        i = bisect.bisect_right(s["dates"], last_buy)
        if i >= len(s["dates"]):
            continue  # отсечка позже последней свечи — в истории не участвует
        ex = s["dates"][i]
        s["divs"].append({"ex": ex, "pay": pay, "amount": amount / _split_factor(ex, splits)})
    s["divs"].sort(key=lambda x: x["ex"])
    div_on_ex = {}
    for dv in s["divs"]:
        div_on_ex[dv["ex"]] = div_on_ex.get(dv["ex"], 0.0) + dv["amount"]
    tr = 1.0
    for i, d in enumerate(s["dates"]):
        if i:
            tr *= (s["close"][i] + div_on_ex.get(d, 0.0)) / s["close"][i - 1]
        s["tr"].append(tr)
    return s


def check_quality(s: Series) -> List[str]:
    out = []
    for i in range(1, len(s["dates"])):
        d0, d1 = s["dates"][i - 1], s["dates"][i]
        if (d1 - d0).days > 7 and not (d0 >= PAUSE_2022[0] - timedelta(days=1) and d1 <= PAUSE_2022[1] + timedelta(days=7)):
            out.append(f"{s['ticker']}: разрыв {d0} → {d1}")
        c0, c1 = s["close"][i - 1], s["close"][i]
        if c0 > 0 and abs(c1 / c0 - 1) > 0.30:
            out.append(f"{s['ticker']}: скачок {d1} {c1 / c0:.2f}× — сплит без записи в SPLITS?")
    zeros = sum(1 for v in s["volume"] if v == 0)
    if zeros:
        out.append(f"{s['ticker']}: нулевой объём в {zeros} днях")
    if any(c <= 0 for c in s["close"]):
        out.append(f"{s['ticker']}: нулевые или отрицательные цены")
    return out


# ---------- безрисковый актив ----------

def synthetic_safe(dates: Sequence[date], key_rate: Sequence[Tuple[date, float]], spread: float = SAFE_SPREAD) -> List[float]:
    """Пай с ежедневным начислением (ставка − spread) / 365 за календарные дни между датами. Старт 1.0."""
    out, v = [], 1.0
    for i, d in enumerate(dates):
        if i:
            j = bisect.bisect_right([sd for sd, _ in key_rate], d) - 1  # последняя запись, вступившая в силу
            rate = key_rate[max(j, 0)][1] - spread
            v *= 1.0 + rate * (d - dates[i - 1]).days / 365.0
        out.append(v)
    return out


def splice_safe(calendar_dates: Sequence[date], real: Optional[Series],
                key_rate: Sequence[Tuple[date, float]] = KEY_RATE) -> Series:
    """LQDT на весь календарь: до первой реальной свечи — синтетика, дальше реальные close в непрерывном масштабе.
    Даты результата — объединение дат календаря (синтетика) и дат реального LQDT (могут быть выходные сессии),
    поэтому потребитель обязан искать значения по дате (as-of), а не по позиции."""
    first_real = real["dates"][0] if real and real["dates"] else None
    syn_dates = [d for d in calendar_dates if first_real is None or d < first_real]
    syn = synthetic_safe(syn_dates, key_rate)
    s: Series = {"ticker": "LQDT", "lot": 1, "dates": list(syn_dates), "open": list(syn), "high": list(syn),
                 "low": list(syn), "close": list(syn), "volume": [0.0] * len(syn), "tr": [], "divs": [], "warnings": []}
    if real and real["dates"]:
        scale = (syn[-1] if syn else 1.0) / real["close"][0]
        for i, d in enumerate(real["dates"]):
            if d < (syn_dates[-1] if syn_dates else d):
                continue
            s["dates"].append(d)
            for k in ("open", "high", "low", "close"):
                s[k].append(real[k][i] * scale)
            s["volume"].append(real["volume"][i])
    s["tr"] = [c / s["close"][0] for c in s["close"]]
    return s


def calendar(series: Dict[str, Series]) -> List[date]:
    return list(series["IMOEX"]["dates"])


# ---------- загрузка ----------

def _cache_path(ticker: str) -> Path:
    return data_dir() / "backtest" / f"{ticker}.json"


def _head_gap(cached: Optional[Dict[str, Any]]) -> bool:
    """В кэше нет начала истории, которое доступно в API (по first1dayCandleDate инструмента)."""
    if not cached or not cached.get("candles"):
        return False
    first_avail = parse_date((cached.get("instrument") or {}).get("first1dayCandleDate"))
    if not first_avail:
        return False
    return parse_date(cached["candles"][0]["time"]) > max(HISTORY_FROM, first_avail) + timedelta(days=10)


def _fetch_one(api, ticker: str, cached: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Свечи с HISTORY_FROM; если кэш есть — докачивается только хвост."""
    inst = cached.get("instrument") if cached else None
    if not inst:
        q = API_TICKER.get(ticker, ticker)
        cls = CLASS_CODE.get(ticker, "TQBR")
        inst = next((i for i in api.find_instrument(q.rstrip("@"))
                     if i.get("ticker") == q and i.get("classCode") == cls), None)
        if not inst:
            raise LookupError(f"{ticker}: инструмент {q} ({cls}) не найден в API")
    candles = list(cached.get("candles", [])) if cached else []
    if candles and _head_gap(cached):
        # кэш (например, от backtest.py) начинается позже доступной истории — добираем голову
        head_to = parse_date(candles[0]["time"]) - timedelta(days=1)
        candles = api.get_day_candles(inst["uid"], max(HISTORY_FROM, parse_date(inst["first1dayCandleDate"])), head_to) + candles
    start = parse_date(candles[-1]["time"]) if candles else HISTORY_FROM
    if start <= today():
        # последняя закэшированная свеча могла быть неполной (внутридневной) — заменяем свежей
        candles = [c for c in candles if parse_date(c["time"]) < start]
        candles += api.get_day_candles(inst["uid"], start, today())
    return {"instrument": inst, "candles": candles}


def load_universe(refresh: bool = False, tickers: Optional[Iterable[str]] = None) -> Dict[str, Series]:
    """Серии по UNIVERSE + AUX. Докачка, если последняя свеча старше 4 дней или refresh. Без токена — только кэш."""
    wanted = list(tickers) if tickers else UNIVERSE + AUX
    bdir = data_dir() / "backtest"
    bdir.mkdir(parents=True, exist_ok=True)
    splits_file = read_json(data_dir() / "cache" / "splits.json", {}) or {}
    splits = dict(SPLITS, **{k: [tuple(x) for x in v] for k, v in splits_file.items()})
    divs_all: Dict[str, List[Dict[str, Any]]] = read_json(bdir / "dividends.json", {}) or {}
    api = None
    no_api = False
    raw: Dict[str, Dict[str, Any]] = {}
    for t in wanted:
        cached = read_json(_cache_path(t))
        last = parse_date(cached["candles"][-1]["time"]) if cached and cached.get("candles") else None
        stale = refresh or last is None or last < today() - timedelta(days=4) or _head_gap(cached)
        if stale:
            if api is None and not no_api:
                from tinvest_api import TInvestError, client_from_env
                try:
                    api = client_from_env()
                except (ValueError, TInvestError) as e:
                    no_api = True
                    warn(f"нет доступа к API ({e}) — используется только кэш")
            if api is None:
                if not cached:
                    continue  # нет токена и нет кэша — тикер недоступен
            else:
                try:
                    data = _fetch_one(api, t, None if refresh else cached)
                    write_json(_cache_path(t), data)
                    cached = data
                except Exception as e:  # noqa: BLE001 — один тикер не должен ронять выгрузку
                    warn(f"{t}: {e}")
                    if not cached:
                        continue
        if t not in ("IMOEX", "LQDT") and (refresh or t not in divs_all) and api is not None:
            inst = cached["instrument"]
            try:
                divs_all[t] = api.get_dividends(inst["uid"], inst.get("figi"), HISTORY_FROM, today() + timedelta(days=90))
            except Exception as e:  # noqa: BLE001
                warn(f"{t}: дивиденды не получены: {e}")
                # ключ не пишем — тикер будет перекачан в следующий раз, предупреждение сработает ниже
        raw[t] = cached
    if api is not None:
        write_json(bdir / "dividends.json", divs_all)
    out: Dict[str, Series] = {}
    for t, data in raw.items():
        if t == "LQDT":
            continue
        lot = int(data["instrument"].get("lot") or 1)
        out[t] = make_series(t, data["candles"], lot, splits.get(t, []), divs_all.get(t, []))
        if t not in ("IMOEX",) and t not in divs_all:
            out[t]["warnings"].append(f"{t}: нет данных о дивидендах — полная доходность без них")
        out[t]["warnings"] += check_quality(out[t])
    if "IMOEX" not in out:
        raise SystemExit("Нет свечей IMOEX — календарь не построить (запусти research.py fetch с токеном)")
    real_lqdt = make_series("LQDT", raw["LQDT"]["candles"]) if "LQDT" in raw else None
    out["LQDT"] = splice_safe(calendar(out), real_lqdt)
    return out
