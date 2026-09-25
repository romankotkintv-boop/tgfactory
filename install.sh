#!/usr/bin/env bash
# Установка контент-завода на чистый Ubuntu 22.04/24.04 (новый VPS).
# Запуск: sudo bash install.sh   (из папки с распакованным архивом)
set -euo pipefail

APP_DIR=/opt/tgfactory
APP_USER=tgfactory

echo "== 1/5 пакеты"
apt-get update -qq
apt-get install -y -qq python3 python3-venv git sqlite3 >/dev/null

echo "== 2/5 отдельный пользователь без прав root"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

echo "== 3/5 код и зависимости"
mkdir -p "$APP_DIR"
cp -r ./factory ./config ./prompts ./requirements.txt ./.env.example "$APP_DIR"/
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "   создан $APP_DIR/.env — заполни его (nano $APP_DIR/.env)"
fi
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "== 4/5 cron: цикл каждые 10 минут + ежедневный бэкап базы"
cat > /etc/cron.d/tgfactory <<CRON
*/10 * * * * $APP_USER cd $APP_DIR && flock -n /tmp/tgfactory.lock .venv/bin/python -m factory run >> $APP_DIR/factory.log 2>&1
15 3 * * * $APP_USER cd $APP_DIR && mkdir -p backups && sqlite3 factory.db ".backup backups/factory-\$(date +\%F).db" && find backups -mtime +14 -delete
CRON
chmod 644 /etc/cron.d/tgfactory

echo "== 5/5 проверка"
sudo -u "$APP_USER" bash -c "cd $APP_DIR && .venv/bin/python -m factory status"
echo
echo "Готово. Завод стартует в режиме DRY_RUN=1 (ничего не публикует, пишет в $APP_DIR/outbox)."
echo "Дальше: заполни .env, проверь outbox, потом поставь DRY_RUN=0."
echo "Лог: tail -f $APP_DIR/factory.log"
