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
        if not page or start >= total:
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

def load(refresh: bool = False) -> Dict[str, Any]:
    """Сырые данные из кэша с докачкой хвоста. Сбой сети — предупреждение и кэш; пустой кэш при сбое — SystemExit."""
    bd = bonds_dir()
    stale_before = (today() - timedelta(days=STALE_DAYS)).isoformat()
    out: Dict[str, Any] = {"indices": {}, "zcyc": {}, "key_rate": []}
    for ix in INDICES:
        path = bd / f"{ix}.json"
        cached = [] if refresh else (read_json(path, []) or [])
        if not cached or cached[-1]["date"] < stale_before:
            d_from = date.fromisoformat(cached[-1]["date"]) if cached else HISTORY_FROM
            try:
                cached = merge_rows(cached, fetch_index(ix, d_from))
                write_json(path, cached)
            except requests.RequestException as e:
                warn(f"{ix}: ISS недоступен ({e}) — используется кэш")
        if not cached:
            raise SystemExit(f"Нет данных индекса {ix}: ISS недоступен, кэша нет")
        out["indices"][ix] = cached

    zpath = bd / "zcyc.json"
    z: Dict[str, Dict[str, float]] = {} if refresh else (read_json(zpath, {}) or {})
    want = [d for d in month_ends([r["date"] for r in out["indices"]["RGBITR"]]) if d >= ZCYC_FROM]
    try:
        for d in (d for d in want if d not in z):
            got, curve = fetch_zcyc(d)
            z[d] = {str(k): v for k, v in curve.items()} if got == d else {}  # {} — КБД на дату нет, не перезапрашивать
        got, curve = fetch_zcyc(None)
        if got:
            z[got] = {str(k): v for k, v in curve.items()}
    except requests.RequestException as e:
        warn(f"КБД: ISS недоступен ({e}) — используется кэш")
    write_json(zpath, z)
    out["zcyc"] = {d: {float(k): v for k, v in c.items()} for d, c in sorted(z.items()) if c}

    kpath = bd / "key_rate.json"
    kr = [] if refresh else (read_json(kpath, []) or [])
    if not kr or kr[-1][0] < stale_before:
        d_from = date.fromisoformat(kr[-1][0]) if kr else KEY_RATE_FROM
        try:
            m = {d: r for d, r in kr}
            m.update(dict(fetch_key_rate(d_from, today())))
            kr = [[d, m[d]] for d in sorted(m)]
            write_json(kpath, kr)
        except requests.RequestException as e:
            warn(f"Ключевая ставка: сервис ЦБ недоступен ({e}) — используется кэш")
    if not kr:
        raise SystemExit("Нет данных ключевой ставки: сервис ЦБ недоступен, кэша нет")
    out["key_rate"] = [(d, r) for d, r in kr]
    return out
