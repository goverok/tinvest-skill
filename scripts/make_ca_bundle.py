#!/usr/bin/env python3
"""Собирает CA-бандл: стандартные корни (certifi) + сертификаты Минцифры -> ~/.tinvest/ca-bundle.pem

  python make_ca_bundle.py ~/Downloads/Russian_Trusted_Root_CA.cer ~/Downloads/Russian_Trusted_Sub_CA.cer

Файлы с Госуслуг (gosuslugi.ru/crt) бывают в PEM или DER — оба варианта распознаются.
Полученный путь укажи в .env как TINVEST_CA_BUNDLE и в NODE_EXTRA_CA_CERTS для Claude Code.
"""
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import home  # noqa: E402


def to_pem(path: Path) -> str:
    raw = path.read_bytes()
    if b"-----BEGIN CERTIFICATE-----" in raw:
        return raw.decode("utf-8", errors="ignore").strip() + "\n"
    return ssl.DER_cert_to_PEM_cert(raw)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    parts = []
    try:
        import certifi  # type: ignore
        parts.append(Path(certifi.where()).read_text(encoding="utf-8").strip() + "\n")
        print(f"+ certifi: {certifi.where()}")
    except ImportError:
        print("certifi не найден (pip install certifi) — бандл будет только из переданных файлов")
    for a in sys.argv[1:]:
        p = Path(a).expanduser()
        if not p.exists():
            print(f"✗ нет файла {p}")
            return 1
        parts.append(to_pem(p))
        print(f"+ {p.name}")
    out = home() / "ca-bundle.pem"
    out.write_text("".join(parts), encoding="utf-8")
    print(f"✓ {out}\nДобавь в ~/.tinvest/.env:  TINVEST_CA_BUNDLE={out}\nДля Claude Code:  export NODE_EXTRA_CA_CERTS={out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
