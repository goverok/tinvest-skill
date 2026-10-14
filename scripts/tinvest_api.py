"""Тонкий REST-клиент T-Invest API (invest-public-api.tbank.ru/rest).

Все методы — POST на /tinkoff.public.invest.api.contract.v1.<Service>/<Method> с JSON-телом.
Ответы — JSON в proto-формате: int64 как строки, деньги как {units, nano}, даты как ISO.
Токен только в заголовке Authorization. Повторяет запрос при 429 и 5xx.
"""
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

import requests

from common import PROD_BASE, SANDBOX_BASE, iso_z, money

API_PREFIX = "tinkoff.public.invest.api.contract.v1"


class TInvestError(RuntimeError):
    def __init__(self, status: int, code: Any, message: str, description: str = ""):
        super().__init__(f"HTTP {status} code={code}: {message} {description}".strip())
        self.status = status
        self.code = code
        self.message = message
        self.description = description


class TInvest:
    def __init__(self, token: str, base: Optional[str] = None, verify: Any = None,
                 app_name: str = "viktor.tinvest-skill", timeout: int = 40, pause: float = 0.12):
        if not token:
            raise ValueError("Пустой токен: задай TINVEST_TOKEN (или TINVEST_SANDBOX_TOKEN) в ~/.tinvest/.env")
        self.base = (base or PROD_BASE).rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-app-name": app_name,
        })
        if verify is None:
            verify = os.environ.get("TINVEST_CA_BUNDLE") or True
        self.verify = verify
        self.timeout = timeout
        self.pause = pause
        self.calls = 0

    # ---------- низкий уровень ----------
    def call(self, service: str, method: str, body: Optional[Dict[str, Any]] = None, retries: int = 4) -> Dict[str, Any]:
        url = f"{self.base}/{API_PREFIX}.{service}/{method}"
        body = body or {}
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            try:
                r = self.session.post(url, json=body, timeout=self.timeout, verify=self.verify)
            except requests.exceptions.SSLError as e:
                raise TInvestError(0, "SSL", "Ошибка TLS. Нужен CA-бандл с сертификатами Минцифры: "
                                   "собери его scripts/make_ca_bundle.py и укажи TINVEST_CA_BUNDLE в .env", str(e))
            except requests.exceptions.RequestException as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
                continue
            self.calls += 1
            if r.status_code == 200:
                if self.pause:
                    time.sleep(self.pause)
                return r.json() if r.content else {}
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After") or (2 * (attempt + 1)))
                time.sleep(min(wait, 30))
                last_err = TInvestError(r.status_code, None, r.text[:300])
                continue
            # 4xx — объясняем и выходим
            try:
                j = r.json()
            except ValueError:
                j = {}
            code = j.get("code", r.status_code)
            msg = j.get("message") or r.text[:300]
            desc = j.get("description", "")
            if r.status_code in (401, 403):
                msg = f"{msg}. Проверь токен и его права (нужен доступ к счёту хотя бы «только чтение») — {self.base}"
            raise TInvestError(r.status_code, code, msg, desc)
        raise TInvestError(0, None, f"Не удалось выполнить {service}/{method} после {retries} попыток", str(last_err))

    # ---------- счета ----------
    def get_accounts(self) -> List[Dict[str, Any]]:
        return self.call("UsersService", "GetAccounts").get("accounts", [])

    def get_info(self) -> Dict[str, Any]:
        return self.call("UsersService", "GetInfo")

    # ---------- портфель и операции ----------
    def get_portfolio(self, account_id: str, currency: str = "RUB") -> Dict[str, Any]:
        return self.call("OperationsService", "GetPortfolio", {"accountId": account_id, "currency": currency})

    def get_positions(self, account_id: str) -> Dict[str, Any]:
        return self.call("OperationsService", "GetPositions", {"accountId": account_id})

    def iter_operations(self, account_id: str, dt_from: Any, dt_to: Any, limit: int = 1000) -> Iterator[Dict[str, Any]]:
        """Все исполненные операции счёта за период (GetOperationsByCursor, с пагинацией)."""
        cursor = ""
        seen = set()
        while True:
            body = {
                "accountId": account_id,
                "from": iso_z(dt_from),
                "to": iso_z(dt_to),
                "cursor": cursor,
                "limit": limit,
                "state": "OPERATION_STATE_EXECUTED",
                "withoutCommissions": False,
                "withoutTrades": False,
                "withoutOvernights": False,
            }
            resp = self.call("OperationsService", "GetOperationsByCursor", body)
            items = resp.get("items", [])
            for it in items:
                key = it.get("id") or it.get("cursor")
                if key in seen:
                    continue
                seen.add(key)
                yield it
            cursor = resp.get("nextCursor") or ""
            if not resp.get("hasNext") or not cursor:
                break

    # ---------- справочник ----------
    def get_instrument(self, uid: str) -> Dict[str, Any]:
        return self.call("InstrumentsService", "GetInstrumentBy",
                         {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid}).get("instrument", {})

    def get_bond(self, uid: str) -> Dict[str, Any]:
        return self.call("InstrumentsService", "BondBy",
                         {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid}).get("instrument", {})

    def get_share(self, uid: str) -> Dict[str, Any]:
        return self.call("InstrumentsService", "ShareBy",
                         {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid}).get("instrument", {})

    def get_asset(self, asset_uid: str) -> Dict[str, Any]:
        return self.call("InstrumentsService", "GetAssetBy", {"id": asset_uid}).get("asset", {})

    def find_instrument(self, query: str, kind: Optional[str] = None, api_trade_available: Optional[bool] = None) -> List[Dict[str, Any]]:
        body: Dict[str, Any] = {"query": query}
        if kind:
            body["instrumentKind"] = kind
        if api_trade_available is not None:
            body["apiTradeAvailableFlag"] = api_trade_available
        return self.call("InstrumentsService", "FindInstrument", body).get("instruments", [])

    def _id_or_figi(self, service: str, method: str, uid: str, figi: Optional[str], extra: Dict[str, Any]) -> Dict[str, Any]:
        """Новые методы принимают instrumentId; старые — figi. Пробуем первое, при 400 — второе."""
        try:
            return self.call(service, method, dict(extra, instrumentId=uid))
        except TInvestError as e:
            if figi and e.status in (400, 404):
                return self.call(service, method, dict(extra, figi=figi))
            raise

    def get_bond_coupons(self, uid: str, figi: Optional[str], d_from: date, d_to: date) -> List[Dict[str, Any]]:
        resp = self._id_or_figi("InstrumentsService", "GetBondCoupons", uid, figi, {"from": iso_z(d_from), "to": iso_z(d_to)})
        return resp.get("events", [])

    def get_bond_events(self, uid: str, d_from: date, d_to: date) -> List[Dict[str, Any]]:
        try:
            resp = self.call("InstrumentsService", "GetBondEvents",
                             {"instrumentId": uid, "from": iso_z(d_from), "to": iso_z(d_to)})
        except TInvestError as e:
            if e.status in (400, 404, 501):
                return []
            raise
        return resp.get("events", [])

    def get_dividends(self, uid: str, figi: Optional[str], d_from: date, d_to: date) -> List[Dict[str, Any]]:
        resp = self._id_or_figi("InstrumentsService", "GetDividends", uid, figi, {"from": iso_z(d_from), "to": iso_z(d_to)})
        return resp.get("dividends", [])

    # ---------- рыночные данные ----------
    def get_last_prices(self, uids: List[str]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for i in range(0, len(uids), 100):
            chunk = uids[i:i + 100]
            resp = self.call("MarketDataService", "GetLastPrices", {"instrumentId": chunk})
            for lp in resp.get("lastPrices", []):
                uid = lp.get("instrumentUid") or lp.get("figi")
                if uid and lp.get("price") is not None:
                    out[uid] = money(lp["price"])
        return out

    def get_day_candles(self, uid: str, d_from: date, d_to: date) -> List[Dict[str, Any]]:
        """Дневные свечи за любой период (API отдаёт не больше года за запрос)."""
        candles: List[Dict[str, Any]] = []
        start = d_from
        while start <= d_to:
            end = min(start + timedelta(days=364), d_to)
            resp = self.call("MarketDataService", "GetCandles", {
                "instrumentId": uid,
                "from": iso_z(start),
                "to": iso_z(datetime.combine(end, datetime.max.time()).replace(tzinfo=timezone.utc)),
                "interval": "CANDLE_INTERVAL_DAY",
            })
            for c in resp.get("candles", []):
                candles.append({"time": c.get("time"), "open": money(c.get("open")), "close": money(c.get("close")),
                                "high": money(c.get("high")), "low": money(c.get("low")), "volume": int(c.get("volume") or 0)})
            start = end + timedelta(days=1)
        return candles


# Откуда берётся токен (первое непустое): переменные окружения Windows/Unix или ~/.tinvest/.env
TOKEN_VARS = ("TINVEST_TOKEN", "T_INVEST_IIS_READ", "T_INVEST_TOKEN", "T_INVEST_READ")
SANDBOX_TOKEN_VARS = ("TINVEST_SANDBOX_TOKEN", "T_INVEST_SANDBOX", "T_INVEST_SANDBOX_TOKEN")


def _first_env(names) -> str:
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v and not v.lower().startswith("t.xxx") and "<" not in v:  # пропускаем заглушки из .env.example
            return v
    return ""


def use_system_certs() -> bool:
    """Если установлен truststore, requests начинает доверять системному хранилищу (Windows/macOS),
    где обычно уже есть сертификаты Минцифры. Тогда CA-бандл не нужен."""
    try:
        import truststore  # type: ignore
        truststore.inject_into_ssl()
        return True
    except Exception:  # noqa: BLE001
        return False


def client_from_env(sandbox: bool = False) -> TInvest:
    if not os.environ.get("TINVEST_CA_BUNDLE"):
        use_system_certs()
    if sandbox:
        token = _first_env(SANDBOX_TOKEN_VARS)
        base = os.environ.get("TINVEST_SANDBOX_BASE_URL", SANDBOX_BASE)
        if not token:
            raise ValueError("Нет токена песочницы: задай переменную T_INVEST_SANDBOX (или TINVEST_SANDBOX_TOKEN в ~/.tinvest/.env)")
    else:
        token = _first_env(TOKEN_VARS)
        base = os.environ.get("TINVEST_BASE_URL", PROD_BASE)
        if not token:
            raise ValueError("Нет токена: задай переменную T_INVEST_IIS_READ (или TINVEST_TOKEN в ~/.tinvest/.env)")
    return TInvest(token, base)
