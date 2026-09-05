#!/usr/bin/env bash
#
# Установка DsAuth на Ubuntu / Debian.
#
#   sudo bash deploy/setup.sh
#
# Скрипт идемпотентный: повторный запуск обновляет код и зависимости,
# не трогая .env и базу. Годится и для установки, и для обновления.

set -euo pipefail

REPO="${DSAUTH_REPO:-https://github.com/Jeredpoi/DsAuth.git}"
BRANCH="${DSAUTH_BRANCH:-claude/discord-auth-bot-JPFKq}"
DIR="${DSAUTH_DIR:-/opt/dsauth}"
SVC_USER="${DSAUTH_USER:-dsauth}"
SERVICE="dsauth"

RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; BLD=$'\e[1m'; RST=$'\e[0m'
step() { printf '\n%s▸ %s%s\n' "$BLD" "$1" "$RST"; }
ok()   { printf '  %s✓%s %s\n' "$GRN" "$RST" "$1"; }
warn() { printf '  %s!%s %s\n' "$YLW" "$RST" "$1"; }
die()  { printf '\n%s✗ %s%s\n\n' "$RED" "$1" "$RST" >&2; exit 1; }

# ── Проверки окружения ────────────────────────────────────────────────────

[ "$(id -u)" -eq 0 ] || die "Запусти через sudo:  sudo bash $0"

command -v apt-get >/dev/null || die "Скрипт рассчитан на Ubuntu/Debian (нужен apt)."

step "Проверяю систему"

# В коде используется синтаксис `str | None` — он появился в Python 3.10
if ! command -v python3 >/dev/null; then
    warn "Python не найден, ставлю"
    apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip
fi

PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
[ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 10 ] \
    || die "Нужен Python 3.10+, найден $PY_MAJOR.$PY_MINOR. Синтаксис 'str | None' на старых версиях не работает."
ok "Python $PY_MAJOR.$PY_MINOR"

MISSING=""
for pkg in git python3-venv sqlite3; do
    dpkg -s "$pkg" >/dev/null 2>&1 || MISSING="$MISSING $pkg"
done
if [ -n "$MISSING" ]; then
    warn "Доустанавливаю:$MISSING"
    apt-get update -qq
    # shellcheck disable=SC2086
    apt-get install -y -qq $MISSING
fi
ok "git, venv, sqlite3 на месте"

# ── Пользователь ──────────────────────────────────────────────────────────

step "Пользователь $SVC_USER"
if id "$SVC_USER" >/dev/null 2>&1; then
    ok "уже существует"
else
    # Системный пользователь без входа в систему: если бота скомпрометируют,
    # доступа к остальной машине не будет
    useradd --system --create-home --home-dir "$DIR" --shell /usr/sbin/nologin "$SVC_USER"
    ok "создан"
fi

# ── Код ───────────────────────────────────────────────────────────────────

step "Код в $DIR"
if [ -d "$DIR/.git" ]; then
    sudo -u "$SVC_USER" git -C "$DIR" fetch --quiet origin
    sudo -u "$SVC_USER" git -C "$DIR" checkout --quiet "$BRANCH"
    sudo -u "$SVC_USER" git -C "$DIR" reset --hard --quiet "origin/$BRANCH"
    ok "обновлён до $(sudo -u "$SVC_USER" git -C "$DIR" rev-parse --short HEAD)"
else
    mkdir -p "$DIR"
    chown "$SVC_USER:$SVC_USER" "$DIR"
    # Не git clone: useradd --create-home уже создал каталог и положил туда
    # файлы из /etc/skel, а clone требует пустой. init + fetch так умеет.
    sudo -u "$SVC_USER" git -C "$DIR" init --quiet
    sudo -u "$SVC_USER" git -C "$DIR" remote add origin "$REPO" 2>/dev/null \
        || sudo -u "$SVC_USER" git -C "$DIR" remote set-url origin "$REPO"
    sudo -u "$SVC_USER" git -C "$DIR" fetch --quiet origin "$BRANCH" \
        || die "Не удалось получить $REPO. Если репозиторий приватный, укажи токен: DSAUTH_REPO=https://<токен>@github.com/Jeredpoi/DsAuth.git"
    sudo -u "$SVC_USER" git -C "$DIR" checkout --quiet -B "$BRANCH" FETCH_HEAD
    ok "получен ($(sudo -u "$SVC_USER" git -C "$DIR" rev-parse --short HEAD))"
fi

# ── Зависимости ───────────────────────────────────────────────────────────

step "Зависимости"
[ -d "$DIR/.venv" ] || sudo -u "$SVC_USER" python3 -m venv "$DIR/.venv"
sudo -u "$SVC_USER" "$DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$SVC_USER" "$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"
ok "установлены в отдельное окружение"

# ── Токен ─────────────────────────────────────────────────────────────────

step "Настройки бота"
if [ -f "$DIR/.env" ] && grep -q '^DISCORD_TOKEN=.\+' "$DIR/.env"; then
    ok ".env уже заполнен — не трогаю"
else
    echo
    echo "  Токен: Developer Portal → приложение → Bot → Reset Token"
    echo "  Свой ID: включи Режим разработчика, ПКМ по себе → Копировать ID"
    echo
    TOKEN="${DISCORD_TOKEN:-}"
    OWNER="${OWNER_ID:-}"
    if [ -z "$TOKEN" ]; then
        read -rsp "  Токен бота: " TOKEN; echo
    fi
    if [ -z "$OWNER" ]; then
        read -rp  "  Твой Discord ID: " OWNER
    fi
    [ -n "$TOKEN" ] || die "Токен обязателен."
    [ -n "$OWNER" ] || die "OWNER_ID обязателен — без него команды владельца не работают."

    printf 'DISCORD_TOKEN=%s\nOWNER_ID=%s\n' "$TOKEN" "$OWNER" > "$DIR/.env"
    chown "$SVC_USER:$SVC_USER" "$DIR/.env"
    # По токену бота можно полностью его захватить — читать должен только владелец
    chmod 600 "$DIR/.env"
    ok ".env создан (chmod 600)"
fi

# ── Права на файлы ────────────────────────────────────────────────────────

chown -R "$SVC_USER:$SVC_USER" "$DIR"

# ── systemd ───────────────────────────────────────────────────────────────

step "Автозапуск"
UNIT_SRC="$DIR/deploy/dsauth.service"
[ -f "$UNIT_SRC" ] || die "Не найден $UNIT_SRC"

# Подставляем реальные пути на случай нестандартных DSAUTH_DIR / DSAUTH_USER
sed -e "s|^User=.*|User=$SVC_USER|" \
    -e "s|^Group=.*|Group=$SVC_USER|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$DIR|" \
    -e "s|^ExecStart=.*|ExecStart=$DIR/.venv/bin/python $DIR/bot.py|" \
    -e "s|^EnvironmentFile=.*|EnvironmentFile=$DIR/.env|" \
    -e "s|^ReadWritePaths=.*|ReadWritePaths=$DIR|" \
    "$UNIT_SRC" > "/etc/systemd/system/$SERVICE.service"

systemctl daemon-reload
systemctl enable --quiet "$SERVICE"
systemctl restart "$SERVICE"
ok "сервис включён и запущен"

# ── Бэкапы ────────────────────────────────────────────────────────────────

step "Резервные копии"
sudo -u "$SVC_USER" mkdir -p "$DIR/backup"
CRON="/etc/cron.d/dsauth-backup"
if [ -f "$CRON" ]; then
    ok "уже настроены"
else
    # sqlite3 .backup, а не cp: база в режиме WAL, и копирование файла во
    # время записи даёт неконсистентный снимок
    cat > "$CRON" <<EOF
# Ежедневная копия базы и конфига DsAuth в 04:00, хранение 14 дней
0 4 * * * $SVC_USER sqlite3 $DIR/bot_data.db ".backup '$DIR/backup/bot_data_\$(date +\\%F).db'" 2>/dev/null; cp -f $DIR/config.json $DIR/backup/config_\$(date +\\%F).json 2>/dev/null; find $DIR/backup -type f -mtime +14 -delete
EOF
    chmod 644 "$CRON"
    ok "ежедневно в 04:00, хранение 14 дней"
fi

# ── Итог ──────────────────────────────────────────────────────────────────

sleep 3
echo
if systemctl is-active --quiet "$SERVICE"; then
    printf '%s✓ Бот запущен%s\n\n' "$GRN$BLD" "$RST"
    echo "  Логи:        journalctl -u $SERVICE -f"
    echo "  Перезапуск:  systemctl restart $SERVICE"
    echo "  Состояние:   curl localhost:1324/health"
    echo
    echo "  Дальше выполни в Discord команду /deploy — она создаёт каналы"
    echo "  и раздаёт ролям права, от которых зависит видимость команд."
    echo
    echo "  Обновление в будущем: sudo bash $DIR/deploy/setup.sh"
else
    printf '%s✗ Сервис не поднялся%s\n\n' "$RED$BLD" "$RST"
    echo "  Последние строки лога:"
    journalctl -u "$SERVICE" -n 20 --no-pager | sed 's/^/    /'
    echo
    echo "  Частые причины:"
    echo "    • Improper token          — неверный токен в $DIR/.env"
    echo "    • PrivilegedIntentsRequired — в Developer Portal → Bot включи"
    echo "      Server Members Intent и Message Content Intent"
    exit 1
fi
