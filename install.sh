#!/usr/bin/env bash
# Первичная установка МЫС Web на чистый сервер (Debian/Ubuntu).
# Дальше обновления делает deploy.sh — сюда возвращаться не нужно.
#
#   ./install.sh --domain soufos.ru           полная установка с nginx и HTTPS
#   ./install.sh --domain soufos.ru --no-tls  без certbot
#   ./install.sh --no-nginx                   только сервис на 127.0.0.1:8000
#
# ponytail: ставит недостающее и зовёт deploy.sh — окружение, таймер очистки,
# рестарт и проверки живут там, дублировать их здесь нечего.
set -euo pipefail

REPO="${REPO:-https://github.com/terrentii/vsc_web.git}"
APP_DIR="${APP_DIR:-$HOME/vsc}"
VENV="${VENV:-$APP_DIR/venv}"
SERVICE="${SERVICE:-vsc}"
PORT="${PORT:-8000}"

DOMAIN=""
WITH_NGINX=1
WITH_TLS=1
while [ $# -gt 0 ]; do
    case "$1" in
        --domain)   DOMAIN="${2:?--domain требует аргумент}"; shift ;;
        --no-nginx) WITH_NGINX=0 ;;
        --no-tls)   WITH_TLS=0 ;;
        -h|--help)  sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
    shift
done

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }

RUN_USER="$(id -un)"
[ "$(id -u)" -eq 0 ] && die "запускать от обычного пользователя, не от root (sudo вызывается точечно)"
command -v apt-get >/dev/null || die "скрипт рассчитан на Debian/Ubuntu; на другой системе поставь python3-venv, git и nginx сам"
sudo -n true 2>/dev/null || warn "sudo спросит пароль — это нормально"
[ "$WITH_NGINX" -eq 1 ] && [ -z "$DOMAIN" ] && warn "домен не задан — nginx будет слушать по IP, HTTPS не поставить (--domain)"

# ── 1. Пакеты ─────────────────────────────────────────────────────────────
say "Ставлю системные пакеты"
PKGS="git python3 python3-venv python3-pip curl"
[ "$WITH_NGINX" -eq 1 ] && PKGS="$PKGS nginx"
sudo apt-get update -qq
sudo apt-get install -y -qq $PKGS

# ── 2. Код ────────────────────────────────────────────────────────────────
if [ -d "$APP_DIR/.git" ]; then
    say "Репозиторий уже в $APP_DIR — оставляю как есть"
else
    say "Клонирую $REPO в $APP_DIR"
    git clone "$REPO" "$APP_DIR"
fi

# ── 3. Виртуальное окружение ──────────────────────────────────────────────
if [ -x "$VENV/bin/pip" ]; then
    say "venv уже есть: $VENV"
else
    say "Создаю venv в $VENV"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q --upgrade pip
fi
"$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# ── 4. systemd-сервис ─────────────────────────────────────────────────────
# deploy.sh дописывает к нему override.conf с переменными окружения.
say "Ставлю сервис $SERVICE"
sudo tee "/etc/systemd/system/${SERVICE}.service" >/dev/null <<EOF
[Unit]
Description=МЫС Web — анонимный мессенджер
After=network.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
# --timeout 0: загрузки без ограничения по размеру идут долго, воркер убивать нельзя
ExecStart=$VENV/bin/gunicorn --worker-class eventlet -w 1 --timeout 0 --bind 127.0.0.1:$PORT app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null

# ── 5. nginx ──────────────────────────────────────────────────────────────
if [ "$WITH_NGINX" -eq 1 ]; then
    say "Настраиваю nginx"
    SITE="/etc/nginx/sites-available/$SERVICE"
    sudo tee "$SITE" >/dev/null <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN:-_};

    # 0 = без лимита: размер загрузки ограничивает только место на диске
    client_max_body_size 0;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        # большие файлы не копим целиком на диске прокси, а стримим в приложение
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # чат, десктоп-клиент и p2p-рандеву — апгрейд в WebSocket
    location ~ ^/(ws|p2p|socket\.io) {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 86400s;
    }
}
EOF
    sudo ln -sf "$SITE" "/etc/nginx/sites-enabled/$SERVICE"
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo nginx -t || die "nginx не принял конфиг"
    sudo systemctl reload nginx

    if command -v ufw >/dev/null && sudo ufw status 2>/dev/null | grep -q '^Status: active'; then
        sudo ufw allow 'Nginx Full' >/dev/null || true
    fi

    if [ -n "$DOMAIN" ] && [ "$WITH_TLS" -eq 1 ]; then
        say "Выпускаю сертификат для $DOMAIN"
        sudo apt-get install -y -qq certbot python3-certbot-nginx
        sudo certbot --nginx -d "$DOMAIN" --agree-tos --register-unsafely-without-email --non-interactive \
            || warn "certbot не справился — домен должен уже указывать на этот сервер. Повтори: sudo certbot --nginx -d $DOMAIN"
    fi
fi

# ── 6. Дальше обычный деплой ──────────────────────────────────────────────
say "Передаю управление deploy.sh"
cd "$APP_DIR"
APP_DIR="$APP_DIR" VENV="$VENV" SERVICE="$SERVICE" APP_URL="http://127.0.0.1:$PORT" \
    ./deploy.sh ${DOMAIN:+--domain "$DOMAIN"}

say "Установка завершена"
if [ -n "$DOMAIN" ]; then
    if [ "$WITH_TLS" -eq 1 ]; then echo "Адрес: https://$DOMAIN"; else echo "Адрес: http://$DOMAIN"; fi
fi
echo "Обновлять дальше: cd $APP_DIR && ./deploy.sh"
echo "Голосовой канал пока не входит в сборку; когда вернётся — ./deploy.sh --turn поставит TURN"
