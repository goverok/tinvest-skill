"""Данные без токена для облигационной части: индексы полной доходности ОФЗ и кривая КБД из ISS Мосбиржи,
ключевая ставка из сервиса ЦБ. Разбор ответов — чистые функции; load() кэширует в ~/.tinvest/data/bonds/
и докачивает только хвост. Нет сети — работа по кэшу с предупреждением.

Доходности и ставки хранятся долями (0.14), дюрация индексов — в днях, как отдаёт ISS.
"""
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import data_dir, read_json, today, warn, write_json  # noqa: E402

ISS = "https://iss.moex.com/iss"
CBR_SOAP = "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx"
INDICES = ["RGBITR", "RUGBITR1Y", "RUGBITR3Y", "RUGBITR5Y", "RUGBITR10Y"]
ZCYC_TERMS = [1.0, 3.0, 5.0, 10.0]
HISTORY_FROM = date(2013, 1, 1)
ZCYC_FROM = "2013-12-01"
KEY_RATE_FROM = date(2013, 9, 13)  # первая ключевая ставка ЦБ
STALE_DAYS = 3
ZCYC_RETRY_DAYS = 7  # пустую КБД за последние дни не запоминаем: данные могут появиться позже
TIMEOUT = 40


def bonds_dir() -> Path:
    d = data_dir() / "bonds"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- разбор ----------

def parse_index_page(j: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int, int]:
    """Страница history индекса -> (строки {date, close, duration (дни), yield (доля)}, start следующей страницы, total)."""
    h = j.get("history") or {}
    ci = {c: i for i, c in enumerate(h.get("columns") or [])}
    rows = []
    for r in h.get("data") or []:
        close = r[ci["CLOSE"]]
        if close is None:
            continue
        dur = r[ci["DURATION"]] if "DURATION" in ci else None
        yld = r[ci["YIELD"]] if "YIELD" in ci else None
        rows.append({"date": r[ci["TRADEDATE"]], "close": float(close),
                     "duration": float(dur) if dur is not None else None,
                     "yield": float(yld) / 100.0 if yld is not None else None})
    cur = ((j.get("history.cursor") or {}).get("data") or [[0, 0, 100]])[0]
    return rows, int(cur[0]) + int(cur[2]), int(cur[1])


def parse_zcyc(j: Dict[str, Any]) -> Tuple[Optional[str], Dict[float, float]]:
    """Ответ zcyc (блок yearyields) -> (дата, {срок в годах: доходность долей}); пустой ответ (выходной) -> (None, {})."""
    y = j.get("yearyields") or {}
    ci = {c: i for i, c in enumerate(y.get("columns") or [])}
    data = y.get("data") or []
    if not data:
        return None, {}
    d = data[0][ci["tradedate"]]
    return d, {float(r[ci["period"]]): float(r[ci["value"]]) / 100.0 for r in data if r[ci["tradedate"]] == d}


def parse_key_rate(xml: str) -> List[Tuple[str, float]]:
    """SOAP-ответ ЦБ KeyRate -> [(дата ISO, ставка долей)] по возрастанию даты."""
    rows = re.findall(r"<DT>(\d{4}-\d{2}-\d{2})T[^<]*</DT>\s*<Rate>([\d.]+)</Rate>", xml)
    return sorted({d: float(r) / 100.0 for d, r in rows}.items())


def month_ends(dates: Sequence[str]) -> List[str]:
    """Последняя дата каждого месяца из отсортированного списка ISO-дат."""
    return [d for i, d in enumerate(dates) if i == len(dates) - 1 or dates[i + 1][:7] != d[:7]]


def merge_rows(cached: Sequence[Dict[str, Any]], fresh: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Объединение по дате; свежая строка заменяет закэшированную (последний день мог быть неполным)."""
    m = {r["date"]: r for r in cached}
    m.update({r["date"]: r for r in fresh})
    return [m[k] for k in sorted(m)]


# ---------- сеть ----------

def _get_json(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.get(url, params=dict(params, **{"iss.meta": "off"}), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_index(ix: str, d_from: date) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    start = 0
    while True:
        j = _get_json(f"{ISS}/history/engines/stock/markets/index/securities/{ix}.json",
                      {"from": d_from.isoformat(), "history.columns": "TRADEDATE,CLOSE,DURATION,YIELD", "start": start})
        page, start, total = parse_index_page(j)
        rows += page
        if not (j.get("history") or {}).get("data") or start >= total:
            return rows


def fetch_zcyc(d: Optional[str]) -> Tuple[Optional[str], Dict[float, float]]:
    """КБД на дату (None — последняя доступная)."""
    params: Dict[str, Any] = {"iss.only": "yearyields", "yearyields.columns": "tradedate,period,value"}
    if d:
        params["date"] = d
    return parse_zcyc(_get_json(f"{ISS}/engines/stock/zcyc.json", params))


def fetch_key_rate(d_from: date, d_to: date) -> List[Tuple[str, float]]:
    body = ('<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            f'<soap:Body><KeyRate xmlns="http://web.cbr.ru/"><fromDate>{d_from.isoformat()}</fromDate>'
            f'<ToDate>{d_to.isoformat()}</ToDate></KeyRate></soap:Body></soap:Envelope>')
    r = requests.post(CBR_SOAP, data=body.encode("utf-8"), timeout=TIMEOUT,
                      headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"http://web.cbr.ru/KeyRate"'})
    r.raise_for_status()
    return parse_key_rate(r.text)


# ---------- кэш ----------

def _offline(what: str, err: Exception, last: Optional[str]) -> None:
    warn(f"{what}: источник недоступен ({err}) — используется кэш, данные на {last}")


def _load_indices(bd: Path, refresh: bool, stale_before: str) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for ix in INDICES:
        path = bd / f"{ix}.json"
        cached = read_json(path, []) or []
        if refresh or not cached or cached[-1]["date"] < stale_before:
            d_from = HISTORY_FROM if refresh or not cached else date.fromisoformat(cached[-1]["date"])
            try:
                cached = merge_rows(cached, fetch_index(ix, d_from))
                write_json(path, cached)
            except requests.RequestException as e:
                if not cached:
                    raise SystemExit(f"Нет данных индекса {ix}: ISS недоступен ({e}), кэша нет")
                _offline(f"Индекс {ix}", e, cached[-1]["date"])
        if not cached:
            raise SystemExit(f"Нет данных индекса {ix}: ISS вернул пустой ответ, кэша нет")
        out[ix] = cached
    return out


def _load_zcyc(bd: Path, refresh: bool, index_dates: Sequence[str]) -> Dict[float, Dict[float, float]]:
    zpath = bd / "zcyc.json"
    cached: Dict[str, Dict[str, float]] = read_json(zpath, {}) or {}
    z = dict(cached)
    this_month = today().isoformat()[:7]
    finished = [d for d in month_ends(index_dates) if d >= ZCYC_FROM and d[:7] < this_month]
    recent = (today() - timedelta(days=ZCYC_RETRY_DAYS)).isoformat()
    try:
        for d in (d for d in finished if refresh or d not in z):
            got, curve = fetch_zcyc(d)
            if got == d:
                z[d] = {str(k): v for k, v in curve.items()}
            elif d < recent and d not in z:
                z[d] = {}  # {} — КБД на дату нет, не перезапрашивать; свежие даты пробуем снова
        got, curve = fetch_zcyc(None)
        if got:
            z = {d: c for d, c in z.items() if d in finished}  # прежняя «последняя» точка заменяется
            z[got] = {str(k): v for k, v in curve.items()}
    except requests.RequestException as e:
        have = [d for d, c in z.items() if c]
        if not have:
            raise SystemExit(f"Нет данных КБД: ISS недоступен ({e}), кэша нет")
        _offline("КБД", e, max(have))
    if z != cached:
        write_json(zpath, z)
    out = {d: {float(k): v for k, v in c.items()} for d, c in sorted(z.items()) if c}
    if not out:
        raise SystemExit("Нет данных КБД: ISS вернул пустой ответ, кэша нет")
    return out


def _load_key_rate(bd: Path, refresh: bool, stale_before: str) -> List[Tuple[str, float]]:
    kpath = bd / "key_rate.json"
    kr = read_json(kpath, []) or []
    if refresh or not kr or kr[-1][0] < stale_before:
        d_from = KEY_RATE_FROM if refresh or not kr else date.fromisoformat(kr[-1][0])
        try:
            m = {d: r for d, r in kr}
            m.update(dict(fetch_key_rate(d_from, today())))
            kr = [[d, m[d]] for d in sorted(m)]
            write_json(kpath, kr)
        except requests.RequestException as e:
            if not kr:
                raise SystemExit(f"Нет данных ключевой ставки: сервис ЦБ недоступен ({e}), кэша нет")
            _offline("Ключевая ставка", e, kr[-1][0])
    if not kr:
        raise SystemExit("Нет данных ключевой ставки: сервис ЦБ вернул пустой ответ, кэша нет")
    return [(d, r) for d, r in kr]


def load(refresh: bool = False) -> Dict[str, Any]:
    """Сырые данные из кэша с докачкой хвоста. Сбой сети — предупреждение с датой последних данных и кэш;
    пустой кэш при сбое — SystemExit. refresh=True перекачивает всё, но кэш не удаляет: файлы перезаписываются
    только после успешной загрузки. Данные не старше STALE_DAYS считаются свежими (в сеть не ходим)."""
    bd = bonds_dir()
    stale_before = (today() - timedelta(days=STALE_DAYS)).isoformat()
    indices = _load_indices(bd, refresh, stale_before)
    zcyc = _load_zcyc(bd, refresh, [r["date"] for r in indices["RGBITR"]])
    return {"indices": indices, "zcyc": zcyc, "key_rate": _load_key_rate(bd, refresh, stale_before)}


# ---------- серии для симулятора ----------

BUCKETS = {"OFZ1Y": "RUGBITR1Y", "OFZ3Y": "RUGBITR3Y", "OFZ5Y": "RUGBITR5Y", "OFZ10Y": "RUGBITR10Y"}
ZCYC_TICKERS = {1.0: "ZCYC1", 3.0: "ZCYC3", 5.0: "ZCYC5", 10.0: "ZCYC10"}


def _series(ticker: str, dates: Sequence[date], values: Sequence[float], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Серия формата bt_data.Series из одного ряда значений (open = high = low = close = tr), лот 1."""
    v = [float(x) for x in values]
    s = {"ticker": ticker, "lot": 1, "dates": list(dates), "open": list(v), "high": list(v), "low": list(v), "close": list(v),
         "volume": [0.0] * len(v), "tr": list(v), "divs": [], "warnings": []}
    s.update(extra or {})
    return s


def build_series(raw: Dict[str, Any], lqdt: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Сырые данные load() + серия LQDT -> серии симулятора: индексы (с полями duration в годах и yield),
    уровни КБД ZCYC1…ZCYC10 и KEYRATE (close = доходность/ставка долей), LQDT."""
    out: Dict[str, Dict[str, Any]] = {}
    for name, ix in [("RGBITR", "RGBITR")] + list(BUCKETS.items()):
        rows = raw["indices"][ix]
        out[name] = _series(name, [date.fromisoformat(r["date"]) for r in rows], [r["close"] for r in rows],
                            {"duration": [r["duration"] / 365.0 if r.get("duration") is not None else None for r in rows],
                             "yield": [r.get("yield") for r in rows]})
    zd = sorted(raw["zcyc"])
    for term, t in ZCYC_TICKERS.items():
        pts = [(date.fromisoformat(d), raw["zcyc"][d][term]) for d in zd if term in raw["zcyc"][d]]
        out[t] = _series(t, [p[0] for p in pts], [p[1] for p in pts])
    kr = raw["key_rate"]
    out["KEYRATE"] = _series("KEYRATE", [date.fromisoformat(d) for d, _ in kr], [r for _, r in kr])
    out["LQDT"] = lqdt
    return out


def bucket_durations(series: Dict[str, Dict[str, Any]]) -> Dict[str, Tuple[Optional[float], Optional[float], Optional[float]]]:
    """Медиана, минимум, максимум дюрации (лет) каждой корзины — проверка, что корзины упорядочены по сроку."""
    out = {}
    for b in BUCKETS:
        v = sorted(x for x in series[b]["duration"] if x is not None)
        out[b] = (v[len(v) // 2], v[0], v[-1]) if v else (None, None, None)
    return out
