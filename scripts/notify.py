"""Отправка сообщений в Telegram через Bot API. Токен бота — T_INVEST_TG_BOT, chat_id — T_INVEST_TG_CHAT.

  python notify.py "текст"      # отправить
  python notify.py --test       # проверить связку бот + chat_id
"""
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_env  # noqa: E402

MAX_LEN = 4000  # лимит Telegram — 4096 символов на сообщение


def send(text: str) -> bool:
    load_env()
    token = (os.environ.get("T_INVEST_TG_BOT") or os.environ.get("TINVEST_TG_BOT") or "").strip()
    chat = (os.environ.get("T_INVEST_TG_CHAT") or os.environ.get("TINVEST_TG_CHAT") or "").strip()
    if not token or not chat:
        print("✗ нет T_INVEST_TG_BOT / T_INVEST_TG_CHAT в переменных окружения", file=sys.stderr)
        return False
    try:
        import truststore  # type: ignore
        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001
        pass
    ok = True
    chunks = [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)] or [text]
    for chunk in chunks:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": chunk, "disable_web_page_preview": True}, timeout=30)
        if r.status_code != 200:
            print(f"✗ Telegram {r.status_code}: {r.text[:200]}", file=sys.stderr)
            ok = False
    return ok


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    msg = "✓ tinvest: связь с Telegram работает" if sys.argv[1] == "--test" else " ".join(sys.argv[1:])
    sys.exit(0 if send(msg) else 1)
