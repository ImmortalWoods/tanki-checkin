#!/bin/sh
# Обёртка для systemd: задаёт интерпретатор и каталог проекта.
set -u

PY="${TANKI_CHECKIN_PYTHON:-$HOME/miniconda3/bin/python3}"
DIR="$(cd "$(dirname "$0")" && pwd)"

exec "$PY" "$DIR/checkin.py"
