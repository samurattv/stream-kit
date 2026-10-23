user www-data;
worker_processes 1;
pid /run/nginx.pid;
error_log /dev/stderr warn;
include /etc/nginx/modules-enabled/*.conf;

events {
    worker_connections 512;
}

rtmp {
    server {
        listen 1935;
        chunk_size 4096;
        # Потоки пользователей панели создаются автоматически (apply.py).
        include /etc/nginx/rtmp.d/*.conf;
    }
}

http {
    include /etc/nginx/mime.types;
    access_log off;
    server_tokens off;
    client_body_temp_path /tmp/nginx-body;
    proxy_temp_path /tmp/nginx-proxy;

    # Подбор пароля упирается в 6 попыток в минуту с одного адреса.
    limit_req_zone $binary_remote_addr zone=sk_login:10m rate=6r/m;
    limit_req_zone $binary_remote_addr zone=sk_all:10m rate=10r/s;

    server {
        listen 8444 ssl;
        http2 on;
        server_name __HOST__;

        ssl_certificate     /certs/fullchain.pem;
        ssl_certificate_key /certs/privkey.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_session_cache shared:sk_ssl:5m;
        ssl_session_tickets off;

        add_header Strict-Transport-Security "max-age=31536000" always;

        client_max_body_size 16k;
        client_body_timeout 10s;

        # Только настоящее имя сервера: запросы по голому IP отбрасываем.
        if ($host != "__HOST__") { return 444; }

        resolver 127.0.0.11 valid=10s;
        set $panel http://panel:8090;

        location = /login {
            limit_req zone=sk_login burst=5 nodelay;
            include /etc/nginx/proxy.inc;
        }
        location /invite/ {
            limit_req zone=sk_login burst=10 nodelay;
            include /etc/nginx/proxy.inc;
        }
        location / {
            limit_req zone=sk_all burst=30 nodelay;
            include /etc/nginx/proxy.inc;
        }
    }
}
