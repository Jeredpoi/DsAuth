# Установка на свой сервер

Инструкция для Ubuntu / Debian. Занимает минут пятнадцать.

Бот нетребователен: около 100 МБ памяти, диска — меньше гигабайта.

---

## 1. Python

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
python3 --version    # нужен 3.10 или новее
```

Версия важна: в коде используется синтаксис `str | None`, на 3.9 он не заработает.

## 2. Отдельный пользователь

Бот не должен ходить под root — если его взломают, доступ будет ко всей машине.

```bash
sudo useradd --system --create-home --home-dir /opt/dsauth --shell /usr/sbin/nologin dsauth
```

## 3. Код

```bash
sudo -u dsauth git clone https://github.com/Jeredpoi/DsAuth.git /opt/dsauth
cd /opt/dsauth
sudo -u dsauth git checkout claude/discord-auth-bot-JPFKq
```

Если репозиторий приватный, понадобится
[personal access token](https://github.com/settings/tokens) — в строке клонирования:
`https://<токен>@github.com/Jeredpoi/DsAuth.git`

## 4. Зависимости

Виртуальное окружение нужно, чтобы пакеты бота не конфликтовали с системными.

```bash
sudo -u dsauth python3 -m venv /opt/dsauth/.venv
sudo -u dsauth /opt/dsauth/.venv/bin/pip install --upgrade pip
sudo -u dsauth /opt/dsauth/.venv/bin/pip install -r /opt/dsauth/requirements.txt
```

## 5. Токен

```bash
sudo -u dsauth tee /opt/dsauth/.env > /dev/null <<'EOF'
DISCORD_TOKEN=сюда_токен_бота
OWNER_ID=твой_discord_id
EOF

sudo chmod 600 /opt/dsauth/.env
```

`chmod 600` обязателен: иначе токен сможет прочитать любой пользователь сервера.
По токену бота можно полностью захватить.

Токен — Discord Developer Portal → приложение → Bot → Reset Token.
Свой ID — включи в Discord «Режим разработчика», ПКМ по себе → Копировать ID.

## 6. Автозапуск

```bash
sudo cp /opt/dsauth/deploy/dsauth.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dsauth
```

Проверка:

```bash
sudo systemctl status dsauth
sudo journalctl -u dsauth -f      # живые логи, Ctrl+C чтобы выйти
```

В логах должно появиться `✅ Загружен: cogs.*` по каждому модулю
и строка `✅ <имя бота> (ID: ...)`.

## 7. Первый запуск в Discord

Один раз выполни на сервере `/deploy` — команда создаёт каналы, категории
и раздаёт ролям права, от которых зависит видимость команд.

Проверить распределение: `/cmdperms`.

---

## Обслуживание

**Обновление**

```bash
cd /opt/dsauth
sudo -u dsauth git pull
sudo -u dsauth /opt/dsauth/.venv/bin/pip install -r requirements.txt
sudo systemctl restart dsauth
```

**Управление**

```bash
sudo systemctl restart dsauth       # перезапуск
sudo systemctl stop dsauth          # остановка
sudo journalctl -u dsauth -n 100    # последние 100 строк
sudo journalctl -u dsauth --since "1 hour ago"
```

**Состояние бота по HTTP**

```bash
curl localhost:8080/health
```

Отдаёт имя бота, число серверов и пинг. Если хочешь подключить внешний
мониторинг (UptimeRobot и подобные) — открой порт 8080 наружу. Для работы
бота это не требуется: self-ping на сервере отключается автоматически,
он был нужен только против засыпания Replit.

---

## Резервные копии

Всё состояние — в двух файлах: `bot_data.db` (статистика, привязки
модераторов к серверам, напоминания) и `config.json` (каналы, шаблоны).
Их потеря означает потерю всей истории форм.

Ежедневная копия в 4 утра:

```bash
sudo crontab -e
```

```cron
0 4 * * * sqlite3 /opt/dsauth/bot_data.db ".backup '/opt/dsauth/backup/bot_data_$(date +\%F).db'" && cp /opt/dsauth/config.json /opt/dsauth/backup/config_$(date +\%F).json && find /opt/dsauth/backup -mtime +14 -delete
```

```bash
sudo -u dsauth mkdir -p /opt/dsauth/backup
sudo apt install -y sqlite3
```

Именно `sqlite3 .backup`, а не `cp`: база работает в режиме WAL, и простое
копирование файла во время записи даёт неконсистентный снимок. Копии старше
двух недель удаляются автоматически.

---

## Если не запускается

**Смотри причину:** `sudo journalctl -u dsauth -n 50`

| В логах | Что не так |
|---|---|
| `Improper token has been passed` | Неверный токен в `.env` |
| `PrivilegedIntentsRequired` | В Developer Portal → Bot не включены Server Members и Message Content Intent |
| `ModuleNotFoundError` | Пропущен шаг 4 либо в `ExecStart` указан не тот python |
| `Permission denied` на `bot_data.db` | Файлы принадлежат не `dsauth`: `sudo chown -R dsauth:dsauth /opt/dsauth` |
| Циклический рестарт | `systemctl status` покажет причину; после 5 падений за минуту systemd останавливает попытки |

**Ветка.** Рабочий код — в `claude/discord-auth-bot-JPFKq`. Если `git pull`
пишет `Already up to date`, а изменений нет, ты на другой ветке:

```bash
git branch --show-current
git checkout claude/discord-auth-bot-JPFKq
```
