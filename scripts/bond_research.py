#!/usr/bin/env python3
"""Облигационная часть: правило дюрации по протоколу TRAIN/VALID/HOLDOUT и советник.

  python bond_research.py fetch [--refresh]       индексы ОФЗ, КБД, ключевая ставка, LQDT
  python bond_research.py train [--family curve] [--random 200]
  python bond_research.py holdout curve:bucket=OFZ10Y,s=0.01 [--yes]
  python bond_research.py advise                  рынок, ваши облигации, кандидаты, фрагмент targets.yaml
  python bond_research.py all                     fetch + train + advise (holdout — только явно)

Отчёты: ~/.tinvest/reports/bonds_train.md, bonds_holdout.md, bonds_advise.md. Ничего не покупает и не продаёт.
"""
import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bond_rules as br  # noqa: E402
import bt_protocol as bp  # noqa: E402
import moex_iss as mi  # noqa: E402
from bt_data import SAFE_SPREAD, load_universe  # noqa: E402
from bt_sim import REBALANCE_THRESHOLD, TAX, simulate, summarize  # noqa: E402
from common import fmt_money, fmt_pct, load_env, md_table, reports_dir, today, write_json  # noqa: E402

TRAIN: bp.Window = (date(2014, 1, 1), date(2020, 12, 31))
VALID: bp.Window = (date(2021, 1, 1), date(2022, 12, 31))
HOLDOUT: bp.Window = (date(2023, 1, 1), None)
WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "HOLDOUT": HOLDOUT}
COST, SLIP = 0.0004, 0.0005
CAL = "RGBITR"
SAFE = "LQDT"
HEAD = ["Вариант", "Итог", "Доходность, год.", "Макс. просадка", "Sharpe", "Сделок с ОФЗ/год", "В ОФЗ", "Издержки", "Налог"]
VERDICT_TEXT = {
    1: "Рекомендация: выше LQDT и структуры «как сейчас» в обоих налоговых режимах, просадка в допуске, эффект значим после поправки на перебор.",
    2: "Эффект есть, но не бьёт LQDT: выше RGBITR и равных долей, значим после поправки; справочно.",
    3: "Ничего не найдено: результат на закрытом окне не отличим от перебора или не обыгрывает LQDT / «как сейчас».",
}


def holdout_log() -> Path:
    return mi.bonds_dir() / "holdout_log.json"


def finalists_path() -> Path:
    return mi.bonds_dir() / "finalists.json"


def holdout_result_path() -> Path:
    return mi.bonds_dir() / "holdout_result.json"


def load_series(refresh: bool = False) -> Dict[str, Any]:
    raw = mi.load(refresh)
    lqdt = load_universe(refresh=False, tickers=["IMOEX", "LQDT"])["LQDT"]
    return mi.build_series(raw, lqdt)


# ---------- прогоны ----------

def run(series, fn, params: dict, window: bp.Window, *, tax_rate: Optional[float] = None, cost: float = COST,
        slip: float = SLIP, fill: str = "open", capital: float = 1_000_000.0) -> Dict[str, Any]:
    res = simulate(series, fn, params, start=window[0], end=bp._end(window), universe=br.UNIVERSE, capital=capital,
                   cost=cost, slip=slip, tax_rate=tax_rate, fill=fill, rebalance="monthly", calendar=CAL)
    s = summarize(res, series[SAFE])
    s["result"] = res
    return s


def run_family(series, family: str, params: dict, window: bp.Window, **kw) -> Dict[str, Any]:
    return run(series, br.FAMILIES[family]["fn"], params, window, **kw)


def run_bench(series, name: str, window: bp.Window, **kw) -> Dict[str, Any]:
    fn, params = br.BENCHMARKS[name]
    return run(series, fn, params, window, **kw)


def random_sharpes(series, window: bp.Window, n: int, capital: float = 1_000_000.0) -> List[float]:
    return [run(series, br.random_bucket, {"seed": i}, window, capital=capital)["sharpe"] or 0.0 for i in range(n)]


def cap_verdict(v: int, text: str, iis: dict, current: dict) -> Tuple[int, str]:
    """Условие 4 спека: вердикты 1–2 — только если правило обыграло структуру «как сейчас»."""
    if v < 3 and (iis["xirr"] or -1) <= (current["xirr"] or -1):
        return 3, f"Не выше структуры «{br.CURRENT_NAME}» — менять облигационную часть незачем."
    return v, text


# ---------- отчёты ----------

def _w(w: bp.Window) -> str:
    return f"{w[0]} — {w[1] or today()}"


def _pstr(p: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in p.items())


def _years(s: dict) -> float:
    r = s["result"]
    return max((r["end"] - r["start"]).days / 365.25, 1e-9)


def row(name: str, s: dict, show_sharpe: bool = True) -> List[str]:
    moves = sum(1 for t in s["result"]["trades"] if t["ticker"] != SAFE)
    return [name, fmt_money(s["final"]), fmt_pct(s["xirr"]), fmt_pct(s["mdd"]), f"{s['sharpe'] or 0:.2f}" if show_sharpe else "—",
            f"{moves / _years(s):.1f}", fmt_pct(s["in_market"], 0), fmt_money(s["costs"]), fmt_money(s["taxes"])]


def data_section(series) -> str:
    dur = mi.bucket_durations(series)
    rows = []
    for t in ["RGBITR"] + list(mi.BUCKETS):
        s = series[t]
        m = dur.get(t)
        rows.append([t, str(s["dates"][0]), str(s["dates"][-1]),
                     "—" if not m or m[0] is None else f"{m[0]:.1f} ({m[1]:.1f}–{m[2]:.1f})"])
    for t in list(mi.ZCYC_TICKERS.values()) + ["KEYRATE", SAFE]:
        s = series[t]
        rows.append([t, str(s["dates"][0]), str(s["dates"][-1]), "—"])
    med = [dur[b][0] for b in mi.BUCKETS]
    ordered = all(x is not None for x in med) and all(med[i] < med[i + 1] for i in range(len(med) - 1))
    out = md_table(["Ряд", "С", "По", "Дюрация, лет: медиана (мин–макс)"], rows)
    return out + ("" if ordered else "\n\n⚠ Корзины не упорядочены по медианной дюрации — проверь индексы.")


def family_section(series, family: str, windows: Dict[str, bp.Window], n_random: int, capital: float) -> Tuple[str, dict]:
    fam = br.FAMILIES[family]
    train, valid = windows["TRAIN"], windows["VALID"]
    variants = bp.all_variants(family, br.FAMILIES)
    scored, rows = [], []
    for v in variants:
        s = run_family(series, family, v, train, capital=capital)
        scored.append((v, s["sharpe"] or 0.0))
        rows.append([_pstr(v)] + row("", s)[1:])
    finalist, plateau = bp.pick_plateau(scored, fam["grid"])
    s_train = run_family(series, family, finalist, train, capital=capital)
    s_valid = run_family(series, family, finalist, valid, capital=capital)
    bench = {b: run_bench(series, b, valid, capital=capital) for b in br.BENCHMARKS}
    rand = random_sharpes(series, valid, n_random, capital)
    ok, why = bp.validate(s_train, s_valid, bench["LQDT"], rand)
    cur = bench[br.CURRENT_NAME]
    if (s_valid["xirr"] or -1) <= (cur["xirr"] or -1):
        ok = False
        why.append(f"XIRR {fmt_pct(s_valid['xirr'])} не выше «{br.CURRENT_NAME}» {fmt_pct(cur['xirr'])}")
    full = (train[0], valid[1])
    s_full = run_family(series, family, finalist, full, capital=capital)
    cost2 = run_family(series, family, finalist, full, capital=capital, cost=COST * 2, slip=SLIP * 2)
    nclose = run_family(series, family, finalist, full, capital=capital, fill="next_close")
    lq_full = run_bench(series, "LQDT", full, capital=capital)
    beat, total = bp.years_beating(s_full["yearly"], lq_full["yearly"])
    sr_star, p = bp.dsr(s_train, len(variants))
    out = [f"## {family} ({len(variants)} вариантов)", "",
           f"### Сетка на TRAIN ({_w(train)})", md_table(HEAD, rows), "",
           f"**Финалист:** {_pstr(finalist)} — среднее Sharpe по окрестности {plateau:.2f}, собственный {s_train['sharpe'] or 0:.2f}; "
           f"deflated Sharpe: порог {sr_star * (252 ** 0.5):.2f} год., p = {p:.3f} при {len(variants)} вариантах.", "",
           f"### Валидация ({_w(valid)})",
           md_table(HEAD, [row(f"{family} {_pstr(finalist)}", s_valid)] + [row(b, bench[b], b != SAFE) for b in bench]), "",
           f"Перцентиль Sharpe среди {len(rand)} случайных стратегий: {bp.percentile(rand, s_valid['sharpe'] or 0):.0%}. "
           + ("**Прошёл.**" if ok else "**Не прошёл:** " + "; ".join(why)), "",
           f"### Устойчивость ({_w(full)})",
           md_table(HEAD, [row("базовый", s_full), row("издержки ×2", cost2), row("исполнение по close", nclose)]), "",
           f"Лет выше LQDT: {beat} из {total}; доля двух лучших лет в прибыли: {bp.top2_share(s_full['yearly']):.0%}.", "",
           "### По годам (финалист / LQDT)",
           md_table(["Год", family, SAFE], [[str(y), fmt_pct(s_full["yearly"].get(y)), fmt_pct(lq_full["yearly"].get(y))]
                                            for y in sorted(lq_full["yearly"])])]
    return "\n".join(out), {"finalist": finalist, "passed": ok, "why": why, "valid": s_valid, "dsr_p": p, "valid_mdd": s_valid["mdd"]}


def train_report(series, families: Sequence[str], n_random: int, capital: float,
                 windows: Optional[Dict[str, bp.Window]] = None) -> Tuple[str, dict]:
    windows = windows or WINDOWS
    until = series[SAFE].get("synthetic_until")
    out = ["# Облигационная часть: обучение и валидация", "",
           f"Сформировано {today()}. Капитал {fmt_money(capital)}. Окна: TRAIN {_w(windows['TRAIN'])}, VALID {_w(windows['VALID'])}; "
           f"закрытое окно {_w(windows['HOLDOUT'])} в этом отчёте не используется.", "",
           "## Допущения",
           f"- Издержки {COST:.2%} комиссия + {SLIP:.2%} проскальзывание на сторону при переходах между корзинами; "
           f"порог ребалансировки {REBALANCE_THRESHOLD:.0%}; ребаланс раз в месяц.",
           "- Корзины — индексы полной доходности ОФЗ Мосбиржи (RUGBITR1Y/3Y/5Y/10Y): ребаланс внутри корзины бесплатный, "
           "купоны реинвестируются сразу; погашенные выпуски входят в индекс — survivorship bias нет.",
           "- ИИС (без налога). Брокерский режим считается только на закрытом окне и приближённо: купон внутри индекса "
           "неотделим от цены, поэтому налог на купоны занижен.",
           f"- Доходность LQDT в правилах — ключевая ставка ЦБ − {SAFE_SPREAD * 100:.1f} п.п.; "
           + (f"серия LQDT до {until} — синтетика по той же формуле." if until else "серия LQDT — реальные свечи."),
           f"- Условие 4 валидации: XIRR выше бенчмарка «{br.CURRENT_NAME}» (35/45 в OFZ10Y и 10/45 в OFZ1Y).", "",
           "## Данные", data_section(series), ""]
    data = {}
    for fam in families:
        text, d = family_section(series, fam, windows, n_random, capital)
        out += [text, ""]
        data[fam] = d
    out += ["## Итог обучения",
            md_table(["Семейство", "Финалист", "VALID XIRR", "Прошёл", "Причина"],
                     [[f, _pstr(d["finalist"]), fmt_pct(d["valid"]["xirr"]), "да" if d["passed"] else "нет", "; ".join(d["why"]) or "—"]
                      for f, d in data.items()]), "",
            "Команда для закрытого окна (только прошедшие): `bond_research.py holdout " +
            " ".join(f"{f}:{','.join(f'{k}={v}' for k, v in d['finalist'].items())}" for f, d in data.items() if d["passed"]) + "`"]
    return "\n".join(out) + "\n", data


def holdout_report(series, finalists: List[Tuple[str, dict]], capital: float, violations: List[str], n_trials: int,
                   valid_mdd: Optional[Dict[str, float]] = None, windows: Optional[Dict[str, bp.Window]] = None) -> Tuple[str, dict]:
    windows = windows or WINDOWS
    window = windows["HOLDOUT"]
    out = [f"# Облигационная часть: закрытое окно ({_w(window)})", ""]
    if violations:
        out += ["## Нарушение протокола"] + [f"- {v}" for v in violations] + [""]
    out += [f"Сформировано {today()}. Капитал {fmt_money(capital)}. Параметры финалистов зафиксированы на TRAIN.", ""]
    bench = {m: {b: run_bench(series, b, window, capital=capital, tax_rate=t) for b in br.BENCHMARKS}
             for m, t in (("ИИС", None), ("брокерский", TAX))}
    results, best = [], 3
    for fam, params in finalists:
        name = f"{fam} {_pstr(params)}"
        iis = run_family(series, fam, params, window, capital=capital)
        brk = run_family(series, fam, params, window, capital=capital, tax_rate=TAX)
        sr_star, p = bp.dsr(iis, n_trials)
        vm = (valid_mdd or {}).get(name, iis["mdd"])
        v, _ = bp.verdict(iis, brk, bench["ИИС"]["LQDT"], bench["брокерский"]["LQDT"], bench["ИИС"]["RGBITR"],
                          bench["ИИС"]["равные доли"], vm, p)
        v, text = cap_verdict(v, VERDICT_TEXT[v], iis, bench["ИИС"][br.CURRENT_NAME])
        out += [f"## {name}",
                md_table(HEAD, [row(f"{name} (ИИС)", iis), row(f"{name} (брокерский)", brk)]
                         + [row(f"{b} (ИИС)", bench["ИИС"][b], b != SAFE) for b in bench["ИИС"]]
                         + [row(f"{b} (брокерский)", bench["брокерский"][b], b != SAFE) for b in bench["брокерский"]]), "",
                f"Deflated Sharpe при {n_trials} финалистах в журнале: порог {sr_star * (252 ** 0.5):.2f} год., p = {p:.3f}.",
                f"Просадка на VALID: {fmt_pct(vm)} (допуск ×{bp.MDD_TOLERANCE}).",
                f"**Вердикт {v}.** {text}", ""]
        results.append({"family": fam, "params": params, "verdict": v})
        best = min(best, v)
    out += ["## Вердикт по шагу", f"Лучший результат среди финалистов — вердикт {best}."]
    return "\n".join(out) + "\n", {"verdict": best, "results": results}


# ---------- CLI ----------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fetch", "train", "holdout", "advise", "all"])
    ap.add_argument("finalists", nargs="*", help="для holdout: family:k=v,k=v")
    ap.add_argument("--family", action="append", choices=list(br.FAMILIES))
    ap.add_argument("--random", type=int, default=bp.N_RANDOM)
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--yes", action="store_true", help="holdout без подтверждения")
    args = ap.parse_args()
    load_env()
    series = load_series(args.refresh)
    if args.command in ("fetch", "all"):
        print(data_section(series))
        if args.command == "fetch":
            return
    if args.command in ("train", "all"):
        text, data = train_report(series, args.family or list(br.FAMILIES), args.random, args.capital)
        write_json(finalists_path(), {f: {"params": d["finalist"], "passed": d["passed"]} for f, d in data.items()})
        path = reports_dir() / "bonds_train.md"
        path.write_text(text, encoding="utf-8")
        print(text)
        print(f"Сохранено: {path}", file=sys.stderr)
    if args.command == "holdout":
        if not args.finalists:
            raise SystemExit("Укажи финалистов: bond_research.py holdout curve:bucket=OFZ10Y,s=0.01 ...")
        if len(args.finalists) > 3:
            raise SystemExit("Не больше трёх финалистов на закрытом окне")
        finalists = [bp.parse_finalist(f, br.FAMILIES) for f in args.finalists]
        print("Закрытое окно облигаций открывается для:", "; ".join(f"{f} {_pstr(p)}" for f, p in finalists), file=sys.stderr)
        if not args.yes:
            try:
                answer = input("Продолжить? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer != "y":
                raise SystemExit("Отменено")
        violations = bp.record_holdout(finalists, holdout_log())
        valid_mdd = {f"{f} {_pstr(p)}": run_family(series, f, p, VALID, capital=args.capital)["mdd"] for f, p in finalists}
        text, data = holdout_report(series, finalists, args.capital, violations, bp.holdout_trials(holdout_log()), valid_mdd)
        write_json(holdout_result_path(), {"at": today().isoformat(), "results": data["results"]})
        path = reports_dir() / "bonds_holdout.md"
        path.write_text(text, encoding="utf-8")
        print(text)
        print(f"Сохранено: {path}", file=sys.stderr)
    if args.command in ("advise", "all"):
        advise_main(series)


def advise_main(series) -> None:  # заменяется в Task 5
    raise SystemExit("advise появится в следующей задаче")


if __name__ == "__main__":
    main()
