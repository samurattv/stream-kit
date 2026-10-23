#!/bin/bash
# Запускает nginx (RTMP + HTTPS) и применяет настройки панели, когда она об этом просит.
set -eu

: "${PANEL_HOST:?не задано имя сервера}"
sed "s|__HOST__|${PANEL_HOST}|g" /etc/nginx/nginx.conf.tpl > /etc/nginx/nginx.conf
nginx -t

nginx -g 'daemon off;' &
NGINX_PID=$!
trap 'nginx -s quit; wait "$NGINX_PID"; exit 0' TERM INT

python3 /opt/streamkit-panel/apply.py || true

# Панель кладёт файл-сигнал при каждом изменении ключей; проверяем его раз в 2 секунды.
while kill -0 "$NGINX_PID" 2>/dev/null; do
    if [ -e /data/apply.trigger ]; then
        rm -f /data/apply.trigger
        python3 /opt/streamkit-panel/apply.py || true
    fi
    sleep 2 &
    wait $! || true
done
echo "nginx завершился" >&2
exit 1
