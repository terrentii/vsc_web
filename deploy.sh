#!/usr/bin/env bash
# Обновление vsc_web на сервере: код, зависимости, окружение, TURN, рестарт.
# Идемпотентен — можно гонять сколько угодно раз.
#
#   ./deploy.sh                          обычное обновление
#   ./deploy.sh --turn                   плюс поставить и настроить coturn
#   ./deploy.sh --turn --domain x.ru     то же, но realm задать явно
#   ./deploy.sh --stun                   только STUN (coturn stun-only, 3478/udp) —
#                                        прямой канал P2P (WebRTC) телефона и десктопа
#
# Mesh-узел (vsc-mesh, 127.0.0.1:7811, снаружи — через /mesh) ставится всегда;
# свой публичный адрес для PEERS: --domain x.ru или MESH_PUBLIC_URL=wss://x.ru/mesh.
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
WITH_STUN=0
DOMAIN=""
while [ $# -gt 0 ]; do
    case "$1" in
        --turn)   WITH_TURN=1 ;;
        --stun)   WITH_STUN=1 ;;
        --domain) DOMAIN="${2:?--domain требует аргумент}"; shift ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
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

    # update — иначе устаревшие списки пакетов дают 404 на обновлённых зависимостях
    command -v turnserver >/dev/null || { sudo apt-get update && sudo apt-get install -y coturn; }

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

# ── 3б. STUN для прямого канала P2P ─────────────────────────────────────
# Клиенты (телефон, десктоп) ходят на stun:<домен>:3478, чтобы узнать свой
# внешний адрес для WebRTC. Нужен только STUN: без паролей и relay-портов
# (relay у нас свой — /p2p). При --turn тот же coturn отвечает и на STUN.
if [ "$WITH_STUN" -eq 1 ] && [ "$WITH_TURN" -eq 0 ]; then
    say "Настраиваю coturn в режиме STUN-only"
    # update — иначе устаревшие списки пакетов дают 404 на обновлённых зависимостях
    command -v turnserver >/dev/null || { sudo apt-get update && sudo apt-get install -y coturn; }
    sudo tee "$TURN_CONF" >/dev/null <<STUNCONF
# Сгенерировано deploy.sh --stun — правки перезапишутся при следующем запуске.
# Только STUN Binding: TURN-relay выключен, учётных записей нет.
listening-port=$TURN_PORT
stun-only
fingerprint
no-tls
no-dtls
STUNCONF
    sudo sed -i 's/^#\?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn 2>/dev/null || true
    sudo systemctl enable --now coturn
    sudo systemctl restart coturn
    if command -v ufw >/dev/null && sudo ufw status | grep -q '^Status: active'; then
        sudo ufw allow "$TURN_PORT"/udp
    else
        warn "ufw не активен — открой порт $TURN_PORT/udp сам"
    fi
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

# ── 4в. Mesh-узел ──────────────────────────────────────────────────────────
# Ретранслятор mesh-сети: P2P-каналы идут через него, когда relay через сервер
# недоступен. Отдельный процесс (asyncio), снаружи — через веб-маршрут /mesh.
say "Ставлю mesh-узел (${SERVICE}-mesh)"
if ! ldconfig -p 2>/dev/null | grep -q libsodium; then
    sudo apt-get update && sudo apt-get install -y libsodium23
fi
MESH_PUBLIC_URL="${MESH_PUBLIC_URL:-}"
if [ -z "$MESH_PUBLIC_URL" ] && [ -n "$DOMAIN" ]; then MESH_PUBLIC_URL="wss://$DOMAIN/mesh"; fi
sudo tee /etc/systemd/system/${SERVICE}-mesh.service >/dev/null <<EOF
[Unit]
Description=Mesh-узел МЫС (ретрансляция P2P-каналов)
After=network.target

[Service]
User=$(id -un)
WorkingDirectory=$APP_DIR
Environment="MESH_PORT=7811"
Environment="MESH_PUBLIC_URL=$MESH_PUBLIC_URL"
ExecStart=$VENV/bin/python $APP_DIR/mesh_daemon.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE}-mesh" >/dev/null
sudo systemctl restart "${SERVICE}-mesh"

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

meshws="$(curl -s -i --max-time 5 \
        -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
        -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGVzdHRlc3R0ZXN0dGVzdA==' \
        "$APP_URL/mesh" | head -1 || true)"
case "$meshws" in
    *101*) echo "Mesh-узел: апгрейд /mesh → 101 ok" ;;
    *)     warn "/mesh не апгрейдится ($meshws) — mesh-сеть без публичного узла" ;;
esac
if sudo systemctl is-active --quiet "${SERVICE}-mesh"; then echo "${SERVICE}-mesh активен"
else warn "${SERVICE}-mesh не запущен: sudo journalctl -u ${SERVICE}-mesh -n 30"; fi

if [ -n "$DOMAIN" ]; then
    scheme="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "https://$DOMAIN/" || echo 000)"
    if [ "$scheme" = "000" ]; then warn "https://$DOMAIN не отвечает. Микрофон и демонстрация экрана работают ТОЛЬКО по HTTPS."; fi
fi

say "Готово"
if [ "$WITH_TURN" -eq 1 ]; then echo "TURN: $DOMAIN:$TURN_PORT, пользователь $TURN_USER (пароль в $TURN_CONF)"; fi
echo "Логи: sudo journalctl -u $SERVICE -f"
echo "Очистка медиа: $VENV/bin/python $APP_DIR/media_cleanup.py --dry-run (таймер ${SERVICE}-cleanup.timer)"
