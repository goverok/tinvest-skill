#!/usr/bin/env python3
"""Песочница T-Invest через REST (токен из T_INVEST_SANDBOX): виртуальные счета для ступени 3.

  python sandbox.py accounts                 # список счетов песочницы
  python sandbox.py open [название]          # открыть виртуальный счёт
  python sandbox.py payin <accountId> <сумма> # пополнить виртуальными рублями
  python sandbox.py close <accountId>        # закрыть счёт

Работает только с песочницей (sandbox-invest-public-api.tbank.ru): боевой токен сюда не подставляется.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env, money  # noqa: E402
from tinvest_api import TInvestError, client_from_env  # noqa: E402


def main() -> int:
    load_env()
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    try:
        api = client_from_env(sandbox=True)
    except ValueError as e:
        print(f"✗ {e}")
        return 2
    if "sandbox" not in api.base:
        print(f"✗ База {api.base} не похожа на песочницу — остановлено")
        return 2
    cmd = args[0]
    try:
        if cmd == "accounts":
            for a in api.call("SandboxService", "GetSandboxAccounts").get("accounts", []):
                print(f"{a.get('id')} · {a.get('name') or '—'} · {a.get('status')} · открыт {str(a.get('openedDate', ''))[:10]}")
        elif cmd == "open":
            name = args[1] if len(args) > 1 else "robot-sandbox"
            r = api.call("SandboxService", "OpenSandboxAccount", {"name": name})
            print(f"✓ открыт счёт {r.get('accountId')} ({name})")
        elif cmd == "payin":
            acc, amount = args[1], float(args[2])
            units = int(amount)
            nano = int(round((amount - units) * 1e9))
            r = api.call("SandboxService", "SandboxPayIn", {"accountId": acc, "amount": {"currency": "rub", "units": str(units), "nano": nano}})
            print(f"✓ баланс счёта {acc}: {money(r.get('balance')):,.2f} {r.get('balance', {}).get('currency', 'rub')}")
        elif cmd == "close":
            api.call("SandboxService", "CloseSandboxAccount", {"accountId": args[1]})
            print(f"✓ счёт {args[1]} закрыт")
        else:
            print(__doc__)
            return 2
    except TInvestError as e:
        print(f"✗ {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
