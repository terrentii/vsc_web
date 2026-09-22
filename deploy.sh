#!/usr/bin/env bash
# Обновление vsc_web на сервере: код, зависимости, окружение, TURN, рестарт.
# Идемпотентен — можно гонять сколько угодно раз.
#
#   ./deploy.sh                          обычное обновление
#   ./deploy.sh --turn                   плюс поставить и настроить coturn
#   ./deploy.sh --turn --domain x.ru     то же, но realm задать явно
#
# ponytail: один файл, никакого ansible. Если деплоев станет больше одного
# сервера — вот тогда и заводить нормальный инструмент.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/vsc}"
VENV="${VENV:-$APP_DIR/venv}"
SERVICE="${SERVICE:-vsc}"
DROPIN="${DROPIN:-/etc/systemd/system/${SERVICE}.service.d/override.conf}"
TURN_CONF="${TURN_CONF:-/etc/turnserver.conf}"
APP_URL="${APP_URL:-http://127.0.0.1:8000}"
TURN_USER="vsc"
TURN_PORT=3478
RELAY_MIN=49160
RELAY_MAX=49200

WITH_TURN=0
DOMAIN=""
while [ $# -gt 0 ]; do
    case "$1" in
        --turn)   WITH_TURN=1 ;;
        --domain) DOMAIN="${2:?--domain требует аргумент}"; shift ;;
        -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
        *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
    shift
done

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then die "запускать от пользователя приложения, не от root (sudo вызывается точечно)"; fi
[ -d "$APP_DIR/.git" ] || die "$APP_DIR не git-репозиторий (задай APP_DIR=...)"
[ -x "$VENV/bin/pip" ] || die "venv не найден: $VENV"
sudo -n true 2>/dev/null || warn "sudo спросит пароль — это нормально"

# ── 1. Код ────────────────────────────────────────────────────────────────
say "Обновляю код в $APP_DIR"
cd "$APP_DIR"
if [ -n "$(git status --porcelain)" ]; then
    die "в $APP_DIR есть незакоммиченные правки — разберись с ними и запусти снова"
fi
git pull --ff-only

# ── 2. Зависимости ────────────────────────────────────────────────────────
say "Ставлю зависимости"
"$VENV/bin/pip" install -q --upgrade -r requirements.txt

say "Проверяю, что приложение импортируется"
# Схема БД мигрируется сама при импорте app.py, поэтому это заодно и миграция.
"$VENV/bin/python" -c 'import app; print("модули ок")' \
    || die "приложение не импортируется — рестарт не делаю, прод остаётся на старой версии"

# ── 3. TURN (опционально) ─────────────────────────────────────────────────
# Без TURN звонки соединяются только внутри одной сети и через дружелюбный NAT.
# Источник правды по TURN — сам turnserver.conf: из него же читаем настройки
# при обычном запуске, чтобы не потерять их и не плодить второе место хранения.
TURN_PASS=""
TURN_REALM=""
if sudo test -f "$TURN_CONF" && sudo grep -q "^user=$TURN_USER:" "$TURN_CONF"; then
    TURN_PASS="$(sudo sed -n "s/^user=$TURN_USER://p" "$TURN_CONF" | head -1)"
    TURN_REALM="$(sudo sed -n 's/^realm=//p' "$TURN_CONF" | head -1)"
fi

if [ "$WITH_TURN" -eq 1 ]; then
    if [ -z "$DOMAIN" ]; then DOMAIN="${TURN_REALM:-$(hostname -f)}"; fi
    say "Настраиваю coturn (realm $DOMAIN)"

    command -v turnserver >/dev/null || sudo apt-get install -y coturn

    # Пароль генерим один раз и переиспользуем — иначе каждый деплой рвал бы
    # уже работающие соединения.
    if [ -n "$TURN_PASS" ]; then
        echo "пароль TURN уже задан, оставляю как есть"
    else
        TURN_PASS="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)"
    fi

    sudo tee "$TURN_CONF" >/dev/null <<EOF
# Сгенерировано deploy.sh — правки перезапишутся при следующем запуске.
listening-port=$TURN_PORT
fingerprint
lt-cred-mech
user=$TURN_USER:$TURN_PASS
realm=$DOMAIN
min-port=$RELAY_MIN
max-port=$RELAY_MAX
no-cli
no-tlsv1
no-tlsv1_1
EOF
    sudo sed -i 's/^#\?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn 2>/dev/null || true
    sudo systemctl enable --now coturn
    sudo systemctl restart coturn

    if command -v ufw >/dev/null && sudo ufw status | grep -q '^Status: active'; then
        sudo ufw allow "$TURN_PORT"/udp
        sudo ufw allow "$TURN_PORT"/tcp
        sudo ufw allow "$RELAY_MIN:$RELAY_MAX"/udp
    else
        warn "ufw не активен — открой порты $TURN_PORT/udp,tcp и $RELAY_MIN-$RELAY_MAX/udp сам"
    fi

else
    if [ -z "$DOMAIN" ]; then DOMAIN="$TURN_REALM"; fi
fi

# ── 4. Окружение сервиса ──────────────────────────────────────────────────
say "Обновляю окружение сервиса $SERVICE"
sudo mkdir -p "$(dirname "$DROPIN")"
{
    echo "# Сгенерировано deploy.sh — правки перезапишутся при следующем запуске."
    echo "[Service]"
    # Без этого ProxyFix выключен и rate-limit логина видит всех как 127.0.0.1.
    echo 'Environment="BEHIND_PROXY=1"'
    # Три простые переменные вместо JSON: у systemd свои правила кавычек,
    # и JSON в Environment= доезжает до приложения покалеченным.
    if [ -n "$TURN_PASS" ]; then
        echo "Environment=\"TURN_URL=turn:$DOMAIN:$TURN_PORT\""
        echo "Environment=\"TURN_USER=$TURN_USER\""
        echo "Environment=\"TURN_PASSWORD=$TURN_PASS\""
    fi
} | sudo tee "$DROPIN" >/dev/null
sudo chmod 600 "$DROPIN"   # внутри пароль TURN
if [ -z "$TURN_PASS" ]; then warn "TURN не настроен: голос заработает только по STUN (одна сеть / простой NAT). Прогони с --turn."; fi

# ── 4б. Таймер очистки медиа ──────────────────────────────────────────────
# Приложение чистит само после каждой загрузки; таймер нужен на случай, когда
# диск забивают не загрузки (логи, бэкапы), а чистить всё равно надо.
say "Ставлю таймер очистки медиа (порог 70%)"
sudo tee /etc/systemd/system/${SERVICE}-cleanup.service >/dev/null <<EOF
[Unit]
Description=Очистка медиа МЫС Web при заполнении диска

[Service]
Type=oneshot
User=$(id -un)
WorkingDirectory=$APP_DIR
ExecStart=$VENV/bin/python $APP_DIR/media_cleanup.py
EOF
sudo tee /etc/systemd/system/${SERVICE}-cleanup.timer >/dev/null <<EOF
[Unit]
Description=Ежечасная проверка места под медиа МЫС Web

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE}-cleanup.timer" >/dev/null

# ── 5. Рестарт ────────────────────────────────────────────────────────────
say "Перезапускаю $SERVICE"
# Именно restart: reload оставит крутиться старый код.
sudo systemctl daemon-reload
sudo systemctl restart "$SERVICE"
sleep 2
sudo systemctl is-active --quiet "$SERVICE" \
    || { sudo journalctl -u "$SERVICE" -n 30 --no-pager; die "$SERVICE не поднялся"; }

# ── 6. Проверки ───────────────────────────────────────────────────────────
say "Проверяю"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$APP_URL/" || echo 000)"
if [ "$code" = "000" ]; then die "приложение не отвечает на $APP_URL"; fi
echo "HTTP на $APP_URL → $code"

ws="$(curl -s -i --max-time 5 \
        -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
        -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGVzdHRlc3R0ZXN0dGVzdA==' \
        "$APP_URL/ws" | head -1 || true)"
case "$ws" in
    *101*) echo "WebSocket-апгрейд на /ws → 101 ok" ;;
    *)     warn "WebSocket-апгрейд не прошёл ($ws) — без него чат откатится на polling" ;;
esac

if [ -n "$DOMAIN" ]; then
    scheme="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "https://$DOMAIN/" || echo 000)"
    if [ "$scheme" = "000" ]; then warn "https://$DOMAIN не отвечает. Микрофон и демонстрация экрана работают ТОЛЬКО по HTTPS."; fi
fi

say "Готово"
if [ "$WITH_TURN" -eq 1 ]; then echo "TURN: $DOMAIN:$TURN_PORT, пользователь $TURN_USER (пароль в $TURN_CONF)"; fi
echo "Логи: sudo journalctl -u $SERVICE -f"
echo "Очистка медиа: $VENV/bin/python $APP_DIR/media_cleanup.py --dry-run (таймер ${SERVICE}-cleanup.timer)"
