#!/usr/bin/env python3
"""Утренний бриф и алерты: оценка и движение за день, выплаты на неделю, оферты, резкие движения позиций.

  python brief.py                 # собрать бриф, напечатать и сохранить в ~/.tinvest/reports/brief.md
  python brief.py --send          # то же + отправить в Telegram (notify.py)
  python brief.py --alerts-only   # молчать, если нет ни одного алерта (для вечерних/часовых проверок)
  python brief.py --no-refresh    # не ходить в API, взять последнюю выгрузку

Данные обновляются через fetch.py (REST, токен только-чтение). Снимки оценки хранятся в ~/.tinvest/history/
и нужны, чтобы считать изменение «с прошлого брифа». Ничего не покупает и не продаёт.
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import data_dir, fmt_money, home, load_env, read_json, reports_dir, today, write_json  # noqa: E402

DEFAULT_ALERTS = {"offer_days": 30, "payouts_days": 7, "move_pct": 7, "portfolio_move_pct": 2, "cash_min": 0}


def load_alerts() -> dict:
    p = home() / "alerts.yaml"
    cfg = dict(DEFAULT_ALERTS)
    if p.exists():
        try:
            import yaml  # type: ignore
            cfg.update({k: v for k, v in (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).items() if k in cfg})
        except Exception as e:  # noqa: BLE001
            print(f"⚠ alerts.yaml: {e}", file=sys.stderr)
    return cfg


def refresh(sandbox: bool) -> bool:
    cmd = [sys.executable, str(HERE / "fetch.py"), "--no-benchmarks"] + (["--sandbox"] if sandbox else [])
    return subprocess.call(cmd, stdout=subprocess.DEVNULL) == 0


def build(sandbox: bool, cfg: dict):
    from model import Store
    import events as events_mod

    store = Store(data_dir(sandbox))
    hdir = home() / ("history-sandbox" if sandbox else "history")
    hdir.mkdir(parents=True, exist_ok=True)
    t0 = today()
    lines, alerts = [], []
    snapshot_all = {}
    for acc_id in store.account_ids():
        acc = store.account(acc_id)
        pos = store.valued_positions(acc_id)
        cash = sum(c[2] or 0 for c in store.cash(acc_id))
        total = store.totals(acc_id)["portfolio"] or (sum(p["value_rub"] or 0 for p in pos) + cash)
        snap = {"total": total, "cash": cash, "positions": {p["ticker"]: {"value": p["value_rub"], "price": p["price"]} for p in pos}}
        snapshot_all[acc_id] = snap

        # прошлый снимок — последний файл, датированный раньше сегодняшнего
        prev, prev_date = None, None
        for f in sorted(hdir.glob(f"{acc_id}_*.json"), reverse=True):
            d = f.stem.split("_")[-1]
            if d < t0.isoformat():
                prev, prev_date = (read_json(f, {}) or {}).get(acc_id) or read_json(f, {}), d
                break
        head = f"{acc.get('name') or acc_id}: {fmt_money(total)}"
        if prev and prev.get("total"):
            d_abs = total - prev["total"]
            d_pct = d_abs / prev["total"] * 100
            sign = "+" if d_abs >= 0 else "−"
            head += f" ({sign}{fmt_money(abs(d_abs))}, {d_pct:+.2f}% с {prev_date})"
            if abs(d_pct) >= float(cfg["portfolio_move_pct"]):
                alerts.append(f"портфель {d_pct:+.2f}% за день")
        head += f" · деньги {fmt_money(cash)}"
        lines.append(head)

        # движения позиций с прошлого снимка
        if prev:
            moves = []
            for p in pos:
                pp = (prev.get("positions") or {}).get(p["ticker"])
                if pp and pp.get("price") and p["price"]:
                    ch = (p["price"] / pp["price"] - 1) * 100
                    if abs(ch) >= float(cfg["move_pct"]):
                        moves.append(f"{p['ticker']} {ch:+.1f}%")
            if moves:
                alerts.append("движения: " + ", ".join(moves))

        # выплаты и оферты
        items = events_mod.collect(store, acc_id, max(int(cfg["payouts_days"]), int(cfg["offer_days"])))
        soon = [i for i in items if (i["date"] - t0).days <= int(cfg["payouts_days"])]
        offers = [i for i in items if i["kind"] == "оферта" and (i["date"] - t0).days <= int(cfg["offer_days"])]
        for o in offers:
            alerts.append(f"ОФЕРТА {o['ticker']} {o['date'].isoformat()} (через {(o['date'] - t0).days} дн.) — подай поручение заранее")
        if soon:
            lines.append("Выплаты на неделю:")
            for i in soon:
                lines.append(f"  {i['date'].strftime('%d.%m')} {i['kind']} {i['ticker']}" + (f" ≈ {fmt_money(i['total_rub'])}" if i.get("total_rub") else ""))
            lines.append(f"  итого ≈ {fmt_money(sum(i['total_rub'] or 0 for i in soon))}")
        else:
            lines.append(f"Выплат в ближайшие {cfg['payouts_days']} дн. нет.")
        if float(cfg["cash_min"]) and cash < float(cfg["cash_min"]):
            alerts.append(f"свободных рублей {fmt_money(cash)} — ниже порога {fmt_money(float(cfg['cash_min']))}")

    write_json(hdir / f"{'_'.join(snapshot_all)}_{t0.isoformat()}.json", snapshot_all)
    for acc_id, snap in snapshot_all.items():
        write_json(hdir / f"{acc_id}_{t0.isoformat()}.json", snap)
    return lines, alerts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--send", action="store_true", help="отправить в Telegram")
    ap.add_argument("--alerts-only", action="store_true", help="сообщение только при наличии алертов")
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--sandbox", action="store_true")
    args = ap.parse_args()
    load_env()
    cfg = load_alerts()
    stale_note = ""
    if not args.no_refresh and not refresh(args.sandbox):
        stale_note = " ⚠ API недоступен, данные из последней выгрузки"
    lines, alerts = build(args.sandbox, cfg)
    if args.alerts_only and not alerts:
        print("тихо: алертов нет")
        return 0
    text = f"T-Invest · {today().strftime('%d.%m.%Y')}{stale_note}\n" + "\n".join(lines)
    if alerts:
        text += "\n\n⚠ " + "\n⚠ ".join(alerts)
    print(text)
    (reports_dir(args.sandbox) / "brief.md").write_text(text, encoding="utf-8")
    if args.send:
        from notify import send
        return 0 if send(text) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
