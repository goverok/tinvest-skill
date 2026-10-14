#!/usr/bin/env bash
# Установка окружения скилла (macOS / Linux). Запуск из папки скилла: bash setup.sh
set -euo pipefail
HOME_DIR="${TINVEST_HOME:-$HOME/.tinvest}"
SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME_DIR"
if [ ! -d "$HOME_DIR/venv" ]; then
  python3 -m venv "$HOME_DIR/venv"
fi
"$HOME_DIR/venv/bin/pip" install -q --upgrade pip
"$HOME_DIR/venv/bin/pip" install -q -r "$SKILL_DIR/requirements.txt"
[ -f "$HOME_DIR/.env" ] || cp "$SKILL_DIR/.env.example" "$HOME_DIR/.env"
[ -f "$HOME_DIR/targets.yaml" ] || cp "$SKILL_DIR/targets.example.yaml" "$HOME_DIR/targets.yaml"
echo "✓ venv: $HOME_DIR/venv"
echo "✓ .env: $HOME_DIR/.env  ← впиши TINVEST_TOKEN (только чтение) и, если нужно, TINVEST_CA_BUNDLE"
echo "✓ targets: $HOME_DIR/targets.yaml  ← целевые доли для deploy"
echo "Проверка: $HOME_DIR/venv/bin/python $SKILL_DIR/scripts/report.py check"
echo "Тесты:    $HOME_DIR/venv/bin/python $SKILL_DIR/tests/test_calc.py"
