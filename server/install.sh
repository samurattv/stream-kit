#!/usr/bin/env bash
# Ставит StreamKit (RTMP-сервер + веб-панель) в Docker, отдельно от остальных сервисов сервера.
#
#   LE_AGREE_TOS=yes bash install.sh <имя-сервера> <ip> <логин-админа>
#   пример: LE_AGREE_TOS=yes bash install.sh stream.example.com 203.0.113.10 admin
#
# Если раньше на хосте стоял nginx-rtmp из старой версии (setup.sh), добавь MIGRATE_HOST_RTMP=yes:
# его настройки будут убраны из nginx хоста и сохранены в /root/nginx-rtmp-old, иначе порт 1935 будет занят.
set -euo pipefail

HOST="${1:?имя сервера, например stream.example.com}"
IP="${2:?IP сервера}"
ADMIN="${3:?логин администратора}"
HERE="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/streamkit
ACME_ROOT="${ACME_ROOT:-/usr/share/nginx/html}"   # корень, который отдаёт порт 80 хоста

if [ "$(id -u)" -ne 0 ]; then echo "Запусти от root" >&2; exit 1; fi
if [ "${LE_AGREE_TOS:-}" != "yes" ]; then
    echo "Для сертификата Let's Encrypt нужно принять их условия:" >&2
    echo "https://letsencrypt.org/documents/LE-SA-v1.5-February-24-2025.pdf" >&2
    echo "Если согласен, запусти снова с LE_AGREE_TOS=yes" >&2
    exit 1
fi
command -v docker >/dev/null || { echo "Нужен docker" >&2; exit 1; }
command -v certbot >/dev/null || { echo "Нужен certbot (apt install certbot)" >&2; exit 1; }

# --- файлы проекта
install -d -m 755 "$APP"
rm -rf "$APP/docker" "$APP/panel"
cp -r "$HERE/Dockerfile" "$HERE/docker" "$HERE/panel" "$APP/"
rm -f "$APP/panel/test_panel.py"

# --- сертификат: выпускает и продлевает certbot хоста, контейнеру отдаём только копию
if [ ! -d "/etc/letsencrypt/live/${HOST}" ]; then
    mkdir -p "$ACME_ROOT/.well-known/acme-challenge"
    certbot certonly --webroot -w "$ACME_ROOT" -d "${HOST}" \
        --agree-tos --register-unsafely-without-email --non-interactive
fi
install -d -m 750 "$APP/certs"
cp -L "/etc/letsencrypt/live/${HOST}/fullchain.pem" "/etc/letsencrypt/live/${HOST}/privkey.pem" "$APP/certs/"
chmod 640 "$APP/certs/"*.pem

mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/streamkit.sh <<EOF
#!/bin/sh
[ "\$RENEWED_LINEAGE" = "/etc/letsencrypt/live/${HOST}" ] || exit 0
cp -L "\$RENEWED_LINEAGE/fullchain.pem" "\$RENEWED_LINEAGE/privkey.pem" ${APP}/certs/
chmod 640 ${APP}/certs/*.pem
docker exec streamkit-edge nginx -s reload
EOF
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/streamkit.sh

# --- старый nginx-rtmp на хосте занимает порт 1935
if grep -qs "include /etc/nginx/rtmp.conf;" /etc/nginx/nginx.conf; then
    if [ "${MIGRATE_HOST_RTMP:-}" != "yes" ]; then
        echo "На хосте найден старый nginx-rtmp (порт 1935). Запусти снова с MIGRATE_HOST_RTMP=yes." >&2
        exit 1
    fi
    OLD=/root/nginx-rtmp-old
    mkdir -p "$OLD"
    cp -a /etc/nginx/nginx.conf /etc/nginx/rtmp.conf "$OLD/"
    [ -f /etc/nginx/conf.d/rtmp-auth.conf ] && mv /etc/nginx/conf.d/rtmp-auth.conf "$OLD/"
    sed -i '/include \/etc\/nginx\/rtmp.conf;/d' /etc/nginx/nginx.conf
    rm -f /etc/nginx/rtmp.conf
    nginx -t
    systemctl reload nginx
    echo "Старые настройки сохранены в $OLD"
fi

# --- запуск: два контейнера в своих сетях, ничего общего с остальными контейнерами сервера
cd "$APP"
docker build -t streamkit:local -f Dockerfile .

docker network inspect streamkit-internal >/dev/null 2>&1 || docker network create --internal streamkit-internal >/dev/null
docker network inspect streamkit-public >/dev/null 2>&1 || docker network create streamkit-public >/dev/null
docker volume create streamkit-data >/dev/null
docker volume create streamkit-rtmpd >/dev/null
docker rm -f streamkit-edge streamkit-panel >/dev/null 2>&1 || true

# panel: без прав, без записи на диск, без доступа в интернет и без портов наружу
docker run -d --name streamkit-panel --network streamkit-internal --network-alias panel     --restart unless-stopped --user 10001:10001 --read-only --tmpfs /tmp     --cap-drop ALL --security-opt no-new-privileges:true     -e PANEL_DATA=/data -e PANEL_SECRET_FILE=/data/secret     -e "PANEL_PUBLIC_URL=https://${HOST}:8444" -e "RTMP_HOST=${IP}"     -v streamkit-data:/data     streamkit:local gunicorn --workers 2 --bind 0.0.0.0:8090 --access-logfile - app:app >/dev/null

# edge: принимает RTMP, отдаёт HTTPS, применяет настройки
docker run -d --name streamkit-edge --network streamkit-public     --restart unless-stopped     --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add SETUID --cap-add SETGID     --security-opt no-new-privileges:true     -e "PANEL_HOST=${HOST}" -e PANEL_DATA=/data -e RTMP_USERS_CONF=/etc/nginx/rtmp.d/users.conf     -e "RELOAD_CMD=nginx -s reload"     -p 1935:1935 -p 8444:8444     -v streamkit-data:/data -v streamkit-rtmpd:/etc/nginx/rtmp.d     -v "$APP/certs:/certs:ro" -v "$APP/docker/proxy.inc:/etc/nginx/proxy.inc:ro"     streamkit:local /usr/local/bin/edge-entrypoint.sh >/dev/null
docker network connect --alias edge streamkit-internal streamkit-edge

for _ in $(seq 1 30); do
    if curl -ksf -o /dev/null --resolve "${HOST}:8444:127.0.0.1" "https://${HOST}:8444/login"; then break; fi
    sleep 1
done
curl -ksf -o /dev/null --resolve "${HOST}:8444:127.0.0.1" "https://${HOST}:8444/login"     || { echo "Панель не отвечает. Логи: docker logs streamkit-edge; docker logs streamkit-panel" >&2; exit 1; }

echo
# Существующему администратору приглашение не выдаём: оно сбросило бы его пароль и 2FA.
if docker exec -u 10001 streamkit-panel python3 /opt/streamkit-panel/cli.py list | awk '{print $1}' | grep -qx "$ADMIN"; then
    echo "Аккаунт $ADMIN уже есть, приглашение не создаю. Сброс доступа: docker exec -u 10001 streamkit-panel python3 /opt/streamkit-panel/cli.py invite $ADMIN"
else
    docker exec -u 10001 streamkit-panel python3 /opt/streamkit-panel/cli.py invite "$ADMIN" --admin
fi
