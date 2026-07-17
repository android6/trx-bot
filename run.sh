#!/usr/bin/env bash
# Запуск бота на Linux. Первый раз: chmod +x run.sh && ./run.sh
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "[setup] Создаю виртуальное окружение..."
    python3 -m venv .venv
    .venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f .env ]; then
    cp .env.example .env
    echo "[setup] Создал .env из шаблона — заполни его (nano .env) и запусти снова."
    exit 1
fi

exec .venv/bin/python bot.py
