# Один образ на два контейнера: panel (веб-панель, без прав) и edge (nginx-rtmp + HTTPS + применение настроек).
FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        nginx libnginx-mod-rtmp python3 python3-flask gunicorn ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin streampanel \
 && install -d -o 10001 -g 10001 -m 755 /data \
 && install -d -m 755 /etc/nginx/rtmp.d

# Код принадлежит root: панель не может переписать собственные файлы.
COPY panel/app.py panel/core.py panel/apply.py panel/cli.py /opt/streamkit-panel/
COPY panel/templates /opt/streamkit-panel/templates
COPY panel/static /opt/streamkit-panel/static
COPY docker/nginx.conf.tpl /etc/nginx/nginx.conf.tpl
COPY docker/edge-entrypoint.sh /usr/local/bin/edge-entrypoint.sh
RUN chmod 755 /usr/local/bin/edge-entrypoint.sh && chmod -R go-w /opt/streamkit-panel

WORKDIR /opt/streamkit-panel
