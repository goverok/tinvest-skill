"""Общие помощники: .env, пути, разбор денег/дат из ответов T-Invest API, markdown-таблицы."""
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional

# Windows: консоль и stdout дочерних процессов бывают в cp1251 — переводим вывод в UTF-8, чтобы ₽, → и ⚠ не роняли скрипт
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

PROD_BASE = "https://invest-public-api.tbank.ru/rest"
SANDBOX_BASE = "https://sandbox-invest-public-api.tbank.ru/rest"
ALT_PROD_BASE = "https://invest-public-api.tinkoff.ru/rest"
ALT_SANDBOX_BASE = "https://sandbox-invest-public-api.tinkoff.ru/rest"


def home() -> Path:
    """Домашняя папка системы: ~/.tinvest (или TINVEST_HOME)."""
    p = Path(os.environ.get("TINVEST_HOME", "~/.tinvest")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_env(path: Optional[str] = None) -> Path:
    """Читает ~/.tinvest/.env (KEY=VALUE построчно). Уже заданные переменные окружения не перезаписывает."""
    h = home()
    p = Path(path).expanduser() if path else h / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    return h


def data_dir(sandbox: bool = False) -> Path:
    d = home() / ("data-sandbox" if sandbox else "data")
    d.mkdir(parents=True, exist_ok=True)
    return d


def reports_dir(sandbox: bool = False) -> Path:
    d = home() / ("reports-sandbox" if sandbox else "reports")
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- разбор значений API ----------

def money(v: Any) -> float:
    """MoneyValue / Quotation {units, nano} -> float. Принимает также числа и строки."""
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        return float(v) if v.strip() else 0.0
    if isinstance(v, dict):
        if "value" in v and "units" not in v:  # формат MCP-сервера: {"currency": "rub", "value": "123.45"}
            return money(v.get("value"))
        units = v.get("units", 0) or 0
        nano = v.get("nano", 0) or 0
        return float(int(units)) + float(int(nano)) / 1e9
    return 0.0


def currency_of(v: Any, default: str = "rub") -> str:
    if isinstance(v, dict) and v.get("currency"):
        return str(v["currency"]).lower()
    return default


_FRAC = re.compile(r"(\.\d{6})\d+")


def parse_dt(s: Any) -> Optional[datetime]:
    """ISO-8601 из API ('2024-01-15T10:30:00.123456789Z') -> aware datetime (UTC)."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    s = str(s).strip()
    s = _FRAC.sub(r"\1", s)  # не больше 6 знаков в дробной части секунд
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_date(s: Any) -> Optional[date]:
    d = parse_dt(s)
    return d.date() if d else None


def today() -> date:
    return datetime.now(timezone.utc).date()


def iso_z(d: Any) -> str:
    """date/datetime -> '2024-01-15T00:00:00Z' для тела запроса."""
    if isinstance(d, datetime):
        d = d.astimezone(timezone.utc)
        return d.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{d.isoformat()}T00:00:00Z"


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 февраля
        return d.replace(year=d.year + years, day=28)


# ---------- файлы ----------

def read_json(path: Path, default: Any = None) -> Any:
    if not Path(path).exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, obj: Any) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


# ---------- форматирование ----------

def fmt_money(x: Optional[float], cur: str = "₽", digits: int = 0) -> str:
    if x is None:
        return "—"
    s = f"{x:,.{digits}f}".replace(",", " ")
    return f"{s} {cur}".strip()


def fmt_pct(x: Optional[float], digits: int = 1) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.{digits}f}%"


def md_table(headers: List[str], rows: Iterable[Iterable[Any]]) -> str:
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)


def warn(msg: str) -> None:
    print(f"⚠ {msg}", file=sys.stderr)


CUR_SYMBOL = {"rub": "₽", "usd": "$", "eur": "€", "cny": "¥", "hkd": "HK$", "kzt": "₸", "try": "₺", "byn": "Br", "chf": "CHF", "gbp": "£", "jpy": "¥"}


def cur_sym(c: str) -> str:
    return CUR_SYMBOL.get((c or "").lower(), (c or "").upper())
