#!/usr/bin/env bash
# ЛОКАЛЬНЫЙ скрипт дозаливки на сервер (запускать с ноута из Git Bash):
#   bash deploy.sh
# Пакует код проекта И .env, копирует на сервер, обновляет зависимости и
# перезапускает systemd-сервис tron-bot.
#
# Правило «один писатель — один файл», чтобы не было рассинхрона:
#   .env               — правишь ТОЛЬКО локально, deploy перевозит его на сервер;
#   wallets.json,      — их пишет только бот, живут ТОЛЬКО на сервере,
#   users.json           deploy их не трогает.
# Если серверный .env вдруг отличается от локального (правили напрямую на
# сервере) — деплой остановится и покажет разницу. Обойти: FORCE_ENV=1 bash deploy.sh
set -euo pipefail

# Координаты сервера — в .env (не в git): DEPLOY_HOST и DEPLOY_KEY.
SRC="$(cd "$(dirname "$0")" && pwd)"
_env_value() { grep -E "^$1=" "$SRC/.env" | tail -1 | cut -d= -f2- | tr -d '\r'; }
SRV_HOST="$(_env_value DEPLOY_HOST)"
KEY="$(_env_value DEPLOY_KEY)"
KEY="${KEY/#\~/$HOME}"
if [ -z "$SRV_HOST" ] || [ -z "$KEY" ]; then
  echo "✗ Задай в .env строки DEPLOY_HOST=<ip сервера> и DEPLOY_KEY=<путь к ssh-ключу>." >&2
  exit 1
fi

SRV_USER=root
SRV_DIR=/root/tron-bot
SRV_SVC=tron-bot
TAR="$(mktemp -t tron_bot_deploy.XXXXXX).tar.gz"
REMOTE_ENV="$(mktemp -t tron_env.XXXXXX)"
trap 'rm -f "$TAR" "$REMOTE_ENV"' EXIT

# Сверка .env: сравниваем СЕРВЕРНЫЙ файл со снимком последнего деплоя
# (.env.deployed). Локальные правки — норма и проходят свободно; тревога
# только если .env меняли напрямую НА СЕРВЕРЕ после последнего деплоя.
echo "→ сверяю серверный .env…"
scp -q -i "$KEY" "$SRV_USER@$SRV_HOST:$SRV_DIR/.env" "$REMOTE_ENV" 2>/dev/null || true
SNAPSHOT="$SRC/.env.deployed"
REF="$SNAPSHOT"
[ -f "$REF" ] || REF="$SRC/.env"   # первый запуск — снимка ещё нет
if [ -s "$REMOTE_ENV" ] && \
   ! diff -q <(tr -d '\r' < "$REF") <(tr -d '\r' < "$REMOTE_ENV") >/dev/null; then
  if [ "${FORCE_ENV:-0}" != "1" ]; then
    echo "✗ Серверный .env правили после последнего деплоя — не затираю."
    echo "  Разница (слева было при деплое, справа сервер сейчас):"
    diff <(tr -d '\r' < "$REF") <(tr -d '\r' < "$REMOTE_ENV") || true
    echo "  Перенеси серверные правки в локальный .env и повтори."
    echo "  Либо осознанно затереть серверный: FORCE_ENV=1 bash deploy.sh"
    exit 1
  fi
  echo "⚠ FORCE_ENV=1 — серверный .env будет заменён локальным."
fi

echo "→ пакую…"
tar czf "$TAR" -C "$SRC" \
  --exclude='.venv' --exclude='__pycache__' --exclude='.claude' \
  --exclude='bot.log*' --exclude='run.bat' --exclude='deploy.sh' \
  --exclude='.env.deployed' --exclude='wallets.json' --exclude='users.json' .

echo "→ копирую на $SRV_HOST…"
scp -i "$KEY" "$TAR" "$SRV_USER@$SRV_HOST:/tmp/tron_bot_deploy.tar.gz"

echo "→ распаковываю, обновляю зависимости, перезапускаю $SRV_SVC…"
ssh -i "$KEY" "$SRV_USER@$SRV_HOST" \
  "tar xzf /tmp/tron_bot_deploy.tar.gz -C '$SRV_DIR' && rm -f /tmp/tron_bot_deploy.tar.gz \
   && '$SRV_DIR/.venv/bin/pip' install -q -r '$SRV_DIR/requirements.txt' \
   && systemctl restart '$SRV_SVC' && sleep 4 \
   && echo -n 'сервис: ' && systemctl is-active '$SRV_SVC'"

cp "$SRC/.env" "$SNAPSHOT"   # снимок: что теперь лежит на сервере

echo "✓ готово. Лог:"
echo "  ssh -i \"$KEY\" $SRV_USER@$SRV_HOST 'journalctl -u $SRV_SVC -n 20 --no-pager'"
