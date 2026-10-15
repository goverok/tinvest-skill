#!/usr/bin/env python3
"""Исследование стратегий на дневных свечах: сетки на TRAIN, валидация, закрытое окно, отчёты в markdown.

  python research.py fetch [--refresh]                      свечи, дивиденды, проверки качества
  python research.py train [--family momentum] [--random 200] [--capital 1000000]
  python research.py holdout momentum:L=12,K=5,market_filter=1 [donchian:N=55,M=20,K=5] [--yes]
  python research.py all                                    fetch + train (holdout — только явно)

Отчёты: ~/.tinvest/reports/research_train.md и research_holdout.md. Ничего не покупает и не продаёт.
"""
import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bt_protocol as bp  # noqa: E402
from bt_data import OLDEST10, UNIVERSE, load_universe  # noqa: E402
from bt_strategies import FAMILIES  # noqa: E402
from common import fmt_money, fmt_pct, load_env, md_table, reports_dir, today  # noqa: E402

HEAD = ["Вариант", "Итог", "Доходность, год.", "Макс. просадка", "Sharpe", "Сделок/год", "В акциях", "Издержки", "Налог"]


def _w(w: bp.Window) -> str:
    return f"{w[0]} — {w[1] or today()}"


def _years(s: dict) -> float:
    r = s["result"]
    return max((r["end"] - r["start"]).days / 365.25, 1e-9)


def row(name: str, s: dict) -> List[str]:
    return [name, fmt_money(s["final"]), fmt_pct(s["xirr"]), fmt_pct(s["mdd"]), f"{s['sharpe'] or 0:.2f}",
            f"{len(s['result']['trades']) / _years(s):.0f}", fmt_pct(s["in_market"], 0), fmt_money(s["costs"]), fmt_money(s["taxes"])]


def _pstr(p: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in p.items())


def universe_section(series: Dict[str, Any], universe: Sequence[str]) -> str:
    rows = [[t, str(series[t]["dates"][0]), str(series[t]["lot"]), str(len(series[t]["divs"])),
             "; ".join(series[t]["warnings"][:3]) or "—"] for t in universe if t in series]
    missing = [t for t in universe if t not in series]
    out = [md_table(["Тикер", "Первая свеча", "Лот", "Дивидендов", "Предупреждения"], rows)]
    if missing:
        out.append(f"\nНет данных: {', '.join(missing)}")
    out.append(f"\nOLDEST10 (оценка survivorship bias): {', '.join(t for t in OLDEST10 if t in series)}")
    return "\n".join(out)


def family_section(series, family: str, windows: Dict[str, bp.Window], n_random: int, capital: float,
                   universe: Optional[Sequence[str]]) -> Tuple[str, dict]:
    fam = FAMILIES[family]
    train, valid = windows["TRAIN"], windows["VALID"]
    variants = bp.all_variants(family)
    scored = []
    rows = []
    for v in variants:
        s = bp.run_variant(series, family, v, train, universe=universe, capital=capital)
        scored.append((v, s["sharpe"] or 0.0))
        rows.append([_pstr(v)] + row("", s)[1:])
    finalist, plateau_score = bp.pick_plateau(scored, fam["grid"])
    neigh = []
    for v, sc in scored:
        vals = [sc] + [s2 for v2, s2 in scored if v2 in bp.neighbors(v, fam["grid"])]
        neigh.append([_pstr(v), f"{sc:.2f}", f"{sum(vals) / len(vals):.2f}", str(len(vals) - 1)])
    s_train = bp.run_variant(series, family, finalist, train, universe=universe, capital=capital)
    s_valid = bp.run_variant(series, family, finalist, valid, universe=universe, capital=capital)
    bench_valid = {b: bp.run_benchmark(series, b, valid, capital=capital, universe=universe) for b in ("LQDT", "TMOS", "equal_weight")}
    rand_valid = bp.random_distribution(series, valid, n_runs=n_random, k=bp.K_RANDOM, universe=universe, capital=capital)
    ok, why = bp.validate(s_train, s_valid, bench_valid["LQDT"], rand_valid)
    rob = bp.robustness(series, family, finalist, (train[0], valid[1]), universe=universe)
    lq_full = bp.run_benchmark(series, "LQDT", (train[0], valid[1]), capital=capital, universe=universe)
    s_full = bp.run_variant(series, family, finalist, (train[0], valid[1]), universe=universe, capital=capital)
    beat, total = bp.years_beating(s_full["yearly"], lq_full["yearly"])
    sr_star, p = bp.dsr(s_train, len(variants))
    out = [f"## {family} ({fam['rebalance']}, {len(variants)} вариантов)", "",
           f"### Сетка на TRAIN ({_w(train)})", md_table(HEAD, rows), "",
           "### Окрестности (отбор по плато)", md_table(["Вариант", "Sharpe", "Среднее по окрестности", "Соседей"], neigh), "",
           f"**Финалист:** {_pstr(finalist)} — среднее по окрестности {plateau_score:.2f}, собственный Sharpe "
           f"{s_train['sharpe'] or 0:.2f}; deflated Sharpe: порог {sr_star * (252 ** 0.5):.2f} год., p = {p:.3f} "
           f"при {len(variants)} попытках семейства.", "",
           f"### Валидация ({_w(valid)})",
           md_table(HEAD, [row(f"{family} {_pstr(finalist)}", s_valid)] + [row(b, bench_valid[b]) for b in bench_valid]), "",
           f"Перцентиль среди {len(rand_valid)} случайных портфелей (K={bp.K_RANDOM}): "
           f"{bp.percentile(rand_valid, s_valid['sharpe'] or 0):.0%}. "
           + ("**Прошёл.**" if ok else "**Не прошёл:** " + "; ".join(why)), "",
           f"### Устойчивость ({_w((train[0], valid[1]))})",
           md_table(HEAD, [row("базовый", s_full), row("издержки ×2", rob["cost_x2"]), row("исполнение по close", rob["next_close"]),
                           row("вселенная OLDEST10", rob["oldest10"])]), "",
           f"Лет с результатом выше LQDT: {beat} из {total}; доля двух лучших лет в прибыли: {bp.top2_share(s_full['yearly']):.0%}.", "",
           "### По годам (финалист / LQDT)",
           md_table(["Год", family, "LQDT"], [[str(y), fmt_pct(s_full["yearly"].get(y)), fmt_pct(lq_full["yearly"].get(y))]
                                               for y in sorted(lq_full["yearly"])])]
    data = {"finalist": finalist, "passed": ok, "why": why, "train": s_train, "valid": s_valid, "dsr_p": p,
            "valid_mdd": s_valid["mdd"]}
    return "\n".join(out), data


def train_report(series, families: Sequence[str], n_random: int, capital: float,
                 windows: Optional[Dict[str, bp.Window]] = None, universe: Optional[Sequence[str]] = None) -> Tuple[str, dict]:
    windows = windows or {"TRAIN": bp.TRAIN, "VALID": bp.VALID}
    uni = list(universe) if universe else [t for t in UNIVERSE if t in series]
    out = ["# Исследование стратегий: обучение и валидация", "",
           f"Сформировано {today()}. Капитал {fmt_money(capital)}. Окна: TRAIN {_w(windows['TRAIN'])}, VALID {_w(windows['VALID'])}; "
           f"закрытое окно {_w(bp.HOLDOUT)} в этом отчёте не используется.", "",
           "## Допущения",
           f"- Издержки {bp.COST:.2%} комиссия + {bp.SLIP:.1%} проскальзывание на сторону; лоты целые; порог ребалансировки 2%.",
           "- ИИС (без налога с продаж). Дивиденды зачисляются в кэш на дату выплаты за вычетом 13% налога у источника "
           "в обоих режимах (ИИС тоже); сигналы считаются по полной доходности.",
           "- Свечи и дивиденды из API уже скорректированы на сплиты. Дивиденды не в рублях (T 2014–2021, RUAL) пропущены — "
           "см. предупреждения во вселенной.",
           "- Тикер без свечи за последние 7 дней считается не торгующимся: после паузы февраля–марта 2022 бумаги, "
           "вернувшиеся позже других (BSPB, SMLT, FLOT, OZON, T, X5, YDEX, SBMX), на 1–2 дня выпадают из портфеля.",
           "- LQDT до 2020-01-20 — синтетика: ключевая ставка ЦБ − 0,5 п.п.",
           "- Вселенная по сегодняшней ликвидности — survivorship bias; оценка — строка «OLDEST10» в устойчивости.", "",
           "## Вселенная", universe_section(series, uni), ""]
    data = {}
    for fam in families:
        text, d = family_section(series, fam, windows, n_random, capital, uni)
        out += [text, ""]
        data[fam] = d
    out += ["## Итог обучения",
            md_table(["Семейство", "Финалист", "VALID XIRR", "Прошёл", "Причина"],
                     [[f, _pstr(d["finalist"]), fmt_pct(d["valid"]["xirr"]), "да" if d["passed"] else "нет", "; ".join(d["why"]) or "—"]
                      for f, d in data.items()]), "",
            "Команда для закрытого окна (только прошедшие): `research.py holdout " +
            " ".join(f"{f}:{','.join(f'{k}={v}' for k, v in d['finalist'].items())}" for f, d in data.items() if d["passed"]) + "`"]
    return "\n".join(out) + "\n", data


def holdout_report(series, finalists: List[Tuple[str, dict]], capital: float, violations: List[str],
                   window: Optional[bp.Window] = None, valid_mdd: Optional[Dict[str, float]] = None,
                   universe: Optional[Sequence[str]] = None) -> Tuple[str, dict]:
    window = window or bp.HOLDOUT
    uni = list(universe) if universe else [t for t in UNIVERSE if t in series]
    out = [f"# Закрытое окно ({_w(window)})", ""]
    if violations:
        out += ["## Нарушение протокола"] + [f"- {v}" for v in violations] + [""]
    out += [f"Сформировано {today()}. Капитал {fmt_money(capital)}. Параметры финалистов зафиксированы на TRAIN.", ""]
    bench = {m: {b: bp.run_benchmark(series, b, window, capital=capital, universe=uni, tax_rate=t) for b in ("LQDT", "TMOS", "equal_weight")}
             for m, t in (("ИИС", None), ("брокерский", bp.TAX_RATE))}
    data = {"finalists": {}, "verdict": 3}
    best_verdict = 3
    for fam, params in finalists:
        name = f"{fam} {_pstr(params)}"
        iis = bp.run_variant(series, fam, params, window, universe=uni, capital=capital)
        brk = bp.run_variant(series, fam, params, window, universe=uni, capital=capital, tax_rate=bp.TAX_RATE)
        sr_star, p = bp.dsr(iis, bp.TOTAL_TRIALS)
        vm = (valid_mdd or {}).get(fam, iis["mdd"])
        v, text = bp.verdict(iis, brk, bench["ИИС"]["LQDT"], bench["ИИС"]["TMOS"], bench["ИИС"]["equal_weight"], vm, p)
        thr = bp.threshold_rate(series, fam, params, window, universe=uni) if v == 2 else None
        out += [f"## {name}",
                md_table(HEAD, [row(f"{name} (ИИС)", iis), row(f"{name} (брокерский)", brk)]
                         + [row(f"{b} (ИИС)", bench["ИИС"][b]) for b in bench["ИИС"]]
                         + [row(f"{b} (брокерский)", bench["брокерский"][b]) for b in bench["брокерский"]]), "",
                f"Deflated Sharpe при {bp.TOTAL_TRIALS} попытках: порог {sr_star * (252 ** 0.5):.2f} год., p = {p:.3f}.",
                f"Просадка на VALID: {fmt_pct(vm)} (допуск ×{bp.MDD_TOLERANCE}).",
                (f"Пороговая ставка LQDT, ниже которой стратегия выигрывает: {fmt_pct(thr)}." if thr is not None else ""),
                f"**Вердикт {v}.** {text}", ""]
        data["finalists"][name] = {"verdict": v, "iis": iis, "brk": brk, "dsr_p": p, "threshold": thr}
        best_verdict = min(best_verdict, v)
    data["verdict"] = best_verdict
    out += ["## Вердикт по шагу", f"Лучший результат среди финалистов — вердикт {best_verdict}."]
    return "\n".join(out) + "\n", data


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fetch", "train", "holdout", "all"])
    ap.add_argument("finalists", nargs="*", help="для holdout: family:k=v,k=v")
    ap.add_argument("--family", action="append", choices=list(FAMILIES))
    ap.add_argument("--random", type=int, default=bp.N_RANDOM)
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--yes", action="store_true", help="holdout без подтверждения")
    args = ap.parse_args()
    load_env()
    series = load_universe(refresh=args.refresh)
    if args.command == "fetch" or args.command == "all":
        warnings = [w for t in series for w in series[t]["warnings"]]
        print(f"Загружено {len(series)} серий; предупреждений: {len(warnings)}")
        for w in warnings:
            print("  " + w)
        if args.command == "fetch":
            return
    if args.command in ("train", "all"):
        text, _ = train_report(series, args.family or list(FAMILIES), args.random, args.capital)
        path = reports_dir() / "research_train.md"
        path.write_text(text, encoding="utf-8")
        print(text)
        print(f"Сохранено: {path}", file=sys.stderr)
    if args.command == "holdout":
        if not args.finalists:
            raise SystemExit("Укажи финалистов: research.py holdout momentum:L=12,K=5,market_filter=1 ...")
        if len(args.finalists) > 3:
            raise SystemExit("Не больше трёх финалистов на закрытом окне")
        finalists = [bp.parse_finalist(f) for f in args.finalists]
        print("Закрытое окно открывается для:", "; ".join(f"{f} {_pstr(p)}" for f, p in finalists), file=sys.stderr)
        if not args.yes:
            if input("Продолжить? [y/N] ").strip().lower() != "y":
                raise SystemExit("Отменено")
        violations = bp.record_holdout(finalists)
        valid_mdd = {f: bp.run_variant(series, f, p, bp.VALID, capital=args.capital)["mdd"] for f, p in finalists}
        text, _ = holdout_report(series, finalists, args.capital, violations, valid_mdd=valid_mdd)
        path = reports_dir() / "research_holdout.md"
        path.write_text(text, encoding="utf-8")
        print(text)
        print(f"Сохранено: {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
