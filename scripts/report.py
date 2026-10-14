#!/usr/bin/env python3
"""Единая точка входа для скилла. Печатает отчёт в markdown и сохраняет его в ~/.tinvest/reports/.

  python report.py check                      # токен и счета
  python report.py audit  [--refresh]         # доходность + карта рисков + облигации + календарь
  python report.py events [--days 60]         # оферты, погашения, купоны, дивиденды
  python report.py tax                        # лоты, ЛДВ, сальдирование, НДФЛ
  python report.py deploy 400000 [--account ID] [--targets ~/.tinvest/targets.yaml]
  python report.py all                        # всё сразу

Данные берутся из последней выгрузки fetch.py; если ей больше 12 часов или задан --refresh, выгрузка обновляется.
Флаг --sandbox переключает на песочницу. Ничего не покупает и не продаёт.
"""
import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import data_dir, home, load_env, parse_dt, read_json, reports_dir, today  # noqa: E402

MAX_AGE_HOURS = 12


def ensure_data(sandbox: bool, refresh: bool, targets: str) -> None:
    dd = data_dir(sandbox)
    meta = read_json(dd / "meta.json", {}) or {}
    fetched = parse_dt(meta.get("fetched_at"))
    stale = not fetched or (datetime.now(timezone.utc) - fetched) > timedelta(hours=MAX_AGE_HOURS)
    if refresh or stale:
        cmd = [sys.executable, str(HERE / "fetch.py")]
        if sandbox:
            cmd.append("--sandbox")
        if targets and Path(targets).expanduser().exists():
            cmd += ["--targets", targets]
        print(f"Обновляю данные ({'по запросу' if refresh else 'выгрузка старше 12 ч'})…", file=sys.stderr)
        rc = subprocess.call(cmd)
        if rc != 0:
            print("✗ Выгрузка не удалась — отчёт строится по старым данным, если они есть.", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "audit", "events", "tax", "deploy", "all"])
    ap.add_argument("amount", nargs="?", type=float, help="сумма для deploy")
    ap.add_argument("--account", help="id счёта (по умолчанию — все выгруженные)")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--sandbox", action="store_true")
    ap.add_argument("--targets", default=str(home() / "targets.yaml"))
    args = ap.parse_args()
    load_env()

    if args.command == "check":
        cmd = [sys.executable, str(HERE / "fetch.py"), "--check"] + (["--sandbox"] if args.sandbox else [])
        return subprocess.call(cmd)

    ensure_data(args.sandbox, args.refresh, args.targets)
    from model import Store  # noqa: E402
    import audit, events, perf, tax, deploy  # noqa: E402

    store = Store(data_dir(args.sandbox))
    acc_ids = [args.account] if args.account else store.account_ids()
    if not acc_ids:
        print("✗ Нет выгруженных счетов. Запусти: python scripts/fetch.py")
        return 1
    meta = store.meta
    header = [f"# T-Invest · {args.command} · {today().isoformat()}",
              f"Данные выгружены {(meta.get('fetched_at') or '?')[:16].replace('T', ' ')} UTC · контур: {'песочница' if args.sandbox else 'боевой'}", ""]
    parts = []
    for acc_id in acc_ids:
        if args.command in ("audit", "all"):
            t, _ = audit.build(store, acc_id)
            parts.append(t)
            t, _ = perf.build(store, acc_id)
            parts.append(t)
            t, _ = events.build(store, acc_id, args.days)
            parts.append(t)
        if args.command == "events":
            t, _ = events.build(store, acc_id, args.days)
            parts.append(t)
        if args.command in ("tax", "all"):
            t, data = tax.build(store, acc_id)
            parts.append(t)
            if data.get("open_lots_rows"):
                from common import md_table
                (reports_dir(args.sandbox) / f"tax_lots_{acc_id}.md").write_text(
                    md_table(["Тикер", "Куплено", "Шт.", "Стоимость", "Оценка", "P/L", "Статус"], data["open_lots_rows"]), encoding="utf-8")
        if args.command == "deploy":
            if not args.amount:
                print("✗ Укажи сумму: report.py deploy 400000")
                return 2
            tp = Path(args.targets).expanduser()
            if not tp.exists():
                print(f"✗ Нет файла целевых долей {tp}. Скопируй targets.example.yaml в ~/.tinvest/targets.yaml и заполни.")
                return 2
            t, _ = deploy.build(store, acc_id, args.amount, tp)
            parts.append(t)
        parts.append("")
    text = "\n".join(header) + "\n\n" + "\n\n".join(p for p in parts if p)
    out = reports_dir(args.sandbox) / f"{args.command}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n_Сохранено: {out}_")
    return 0


if __name__ == "__main__":
    sys.exit(main())
