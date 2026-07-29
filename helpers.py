import json
import os
import shutil
from datetime import datetime, timedelta

import discord

CONFIG_PATH = "config.json"

RULES: dict[str, str] = {
    "2.1":  "Неадекватное поведение",
    "2.2":  "Трансфер Discord валюты",
    "2.3":  "Реклама",
    "2.4":  "Возрастной контент",
    "2.5":  "Распространение персональной информации",
    "2.6":  "Обман пользователей",
    "2.7":  "Споры о политике и религии",
    "2.8":  "Продажа за реальные деньги",
    "2.9":  "Использование уязвимостей",
    "2.10": "Вымогательство и попрошайничество",
    "2.11": "Деструктивные действия",
    "2.12": "Обход наказаний",
    "2.13": "Оскорбления родных",
    "2.14": "Распространение файлов",
    "2.15": "Пропаганда наркотиков и терроризма",
    "2.16": "Расизм, сексизм, нацизм",
    "2.17": "Помехи работе модерации",
    "2.18": "Провокация к нарушениям",
    "2.19": "Угрозы",
    "2.20": "Многократное нарушение",
    "2.21": "Приватные комнаты с нарушениями",
    "3.1":  "Флуд и спам",
    "3.2":  "Упоминание без сообщения",
    "3.3":  "Чрезмерный CapsLock",
    "3.4":  "Злоупотребление символами",
    "3.5":  "Многократное упоминание",
    "4.1":  "Помехи общению",
    "4.2":  "Программы для воспроизведения звуков",
    "4.3":  "Плохо настроенный микрофон",
    "4.4":  "Программы изменения голоса",
}

PUNISHMENTS: list[str] = [
    "Устное предупреждение",
    "Предупреждение",
    "Мут 90 минут",
    "Бан 7-15 дней",
    "Перманентная блокировка",
    "Глобальная блокировка",
    "Обнуление",
]

DEFAULT_TEMPLATE = (
    "1) Ваш Nick_Name: {moderatorNick}\n"
    "2) ID Discord и тег нарушителя: {userId} / {userTag}\n"
    "3) Пункт правил, который был нарушен: {ruleId} — {ruleText}\n"
    "4) Выданное наказание: {punishment}\n"
    "5) Дата выдачи: {dateIssued}\n"
    "6) Дата снятия: {dateEnd}\n"
    "7) Доказательства: {evidence}"
)

DEFAULT_CONFIG: dict = {
    "moderator_nick": "Ваш_Nick_Name",
    "home_guild_id": 0,
    "templates": {
        "general": DEFAULT_TEMPLATE,
        "oral": "", "warn": "", "mute": "", "ban": "", "gban": "",
    },
    "guilds": {},
}

DEFAULT_GUILD_CFG: dict = {
    "proof_channel_id": 0,
    "banform_channel_id": 0,
    "review_role_id": 0,
    "auth_channel_id": 0,
    "auth_review_channel_id": 0,
    "auth_forum_channel_id": 0,
    "auth_forum_tag_pending_id": 0,
    "auth_forum_tag_approved_id": 0,
    "auth_forum_tag_rejected_id": 0,
    "team_role_id": 0,
}


def _default_config() -> dict:
    return {
        "moderator_nick": DEFAULT_CONFIG["moderator_nick"],
        "templates": DEFAULT_CONFIG["templates"].copy(),
        "user_servers": {},
        "guilds": {},
    }


def _normalize_config(data: dict) -> dict:
    data.setdefault("moderator_nick", DEFAULT_CONFIG["moderator_nick"])
    data.setdefault("home_guild_id", DEFAULT_CONFIG["home_guild_id"])
    data.setdefault("templates", DEFAULT_CONFIG["templates"].copy())
    data.setdefault("guilds", {})
    for k, v in DEFAULT_CONFIG["templates"].items():
        data["templates"].setdefault(k, v)
    for rule_id, rule_text in data.get("rules", {}).items():
        RULES[rule_id] = rule_text
    # Собираем user_servers из guild-конфигов для последующей миграции в SQLite
    merged = data.pop("user_servers", {})
    for g_cfg in data["guilds"].values():
        for uid, srv in g_cfg.pop("user_servers", {}).items():
            merged.setdefault(uid, srv)
    data["user_servers"] = merged   # bot.py передаст это в migrate_from_json
    return data


def load_config() -> dict:
    """Читает config.json, при повреждении откатывается на резервную копию.

    Файл может оказаться обрезанным, если процесс убили посреди записи —
    на Replit это штатная ситуация. Раньше это означало JSONDecodeError на
    уровне импорта bot.py, то есть бот не поднимался вообще. Теперь битый
    файл откладывается в .corrupt, и мы пробуем .bak, а в самом худшем
    случае стартуем с настроек по умолчанию.
    """
    for path in (CONFIG_PATH, CONFIG_PATH + ".bak"):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            print(f"⚠️  Конфиг {path} повреждён: {e}")
            if path == CONFIG_PATH:
                try:
                    os.replace(path, CONFIG_PATH + ".corrupt")
                    print(f"   Повреждённый файл сохранён как {CONFIG_PATH}.corrupt")
                except OSError:
                    pass
            continue
        if not isinstance(data, dict):
            print(f"⚠️  Конфиг {path} имеет неверный формат — пропускаю")
            continue
        if path.endswith(".bak"):
            print(f"✅ Конфиг восстановлен из резервной копии {path}")
        return _normalize_config(data)
    print("ℹ️  Конфиг не найден — стартую с настроек по умолчанию")
    return _default_config()


def save_config(data: dict):
    """Атомарная запись конфига.

    Пишем во временный файл, сбрасываем на диск и подменяем через
    os.replace — он атомарен на уровне файловой системы. Поэтому
    config.json в любой момент либо целиком старый, либо целиком новый,
    но никогда не обрезанный. Предыдущая версия остаётся как .bak.
    """
    to_save = {k: v for k, v in data.items() if k != "user_servers"}
    tmp_path = CONFIG_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(to_save, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(CONFIG_PATH):
            try:
                shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak")
            except OSError:
                pass
        os.replace(tmp_path, CONFIG_PATH)
    except OSError as e:
        print(f"⚠️  Не удалось сохранить конфиг: {e}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def get_guild_cfg(cfg: dict, guild_id: int) -> dict:
    key = str(guild_id)
    cfg["guilds"].setdefault(key, DEFAULT_GUILD_CFG.copy())
    return cfg["guilds"][key]


def fmt_date(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")


def date_end(punishment: str) -> str:
    now = datetime.now()
    p = punishment.lower()
    if "устное" in p:
        return "—"
    if "мут" in p:
        return fmt_date(now + timedelta(minutes=90))
    if "предупрежд" in p:
        return fmt_date(now + timedelta(days=3))
    if "15 дней" in p and "7-15" not in p:
        return fmt_date(now + timedelta(days=15))
    if "7-15" in p or ("бан" in p and "перманент" not in p and "глобальн" not in p):
        return fmt_date(now + timedelta(days=7))
    if "перманент" in p or "глобальн" in p:
        return "Перманентно"
    return fmt_date(now)


def _punishment_type(punishment: str) -> str:
    p = punishment.lower()
    if "устное" in p:
        return "oral"
    if "предупрежд" in p:
        return "warn"
    if "мут" in p:
        return "mute"
    if "глобальн" in p:
        return "gban"
    if "бан" in p or "блокировк" in p or "обнул" in p:
        return "ban"
    return "general"


RANKS: list[str] = [
    "Младший модератор",
    "Модератор",
    "Старший модератор",
    "Куратор модерации",
    "Заместитель главного модератора",
    "Главный модератор",
]

RANK_LEVELS: dict[str, int] = {r: i + 1 for i, r in enumerate(RANKS)}

# Минимальный ранг для одобрения по типу формы
APPROVE_MIN_RANK: dict[str, int] = {
    "proof":   RANK_LEVELS["Модератор"],
    "banform": RANK_LEVELS["Старший модератор"],
    "gbanform": RANK_LEVELS["Главный модератор"],
}

LEADERSHIP_RANKS: frozenset[str] = frozenset({
    "Куратор модерации",
    "Заместитель главного модератора",
    "Главный модератор",
})

UNVERIFIED_ROLE_NAME = "Не авторизован"


# Ранги, которые распоряжаются составом команды (совпадает с AUTH_MIN_LEVEL)
TOP_RANKS: frozenset[str] = frozenset({
    "Заместитель главного модератора",
    "Главный модератор",
})


def perms_for_rank(rank: str) -> discord.Permissions:
    """Discord-права для ранговой роли.

    Discord скрывает слэш-команду от участника, у которого нет права,
    указанного в её default_permissions. Поэтому права роли — это и есть
    рычаг видимости команд:

      manage_messages — база модератора    (/proof, /warns, /myforms …)
      manage_roles    — руководство КМ+    (/transfer, /inactive, /listmods …)
      manage_guild    — верхушка ЗГМ+      (/promote, /dismiss)

    Три уровня вместо двух нужны, чтобы видимость совпадала с реальными
    правами: /promote и /dismiss внутри требуют ЗГМ+, и Куратор модерации
    не должен видеть команды, которые всё равно ему откажут.
    """
    if rank in TOP_RANKS:
        return discord.Permissions(manage_messages=True, manage_roles=True, manage_guild=True)
    if rank in LEADERSHIP_RANKS:
        return discord.Permissions(manage_messages=True, manage_roles=True)
    return discord.Permissions(manage_messages=True)


def get_member_rank_level(member: discord.Member) -> int:
    return max((RANK_LEVELS.get(r.name, 0) for r in member.roles), default=0)


def build_command(punishment: str, user_id: int, rule_id: str) -> str:
    p = punishment.lower()
    uid = f"<@{user_id}>"
    if "устное" in p:
        return ""
    if "предупрежд" in p:
        return f"/warn user:{uid} reason:{rule_id}"
    if "мут" in p:
        return f"/mute user:{uid} time:90 reason:{rule_id}"
    if "15 дней" in p and "7-15" not in p:
        return f"/ban user:{uid} time:15 reason:{rule_id}"
    if "7-15" in p or ("бан" in p and "перманент" not in p and "глобальн" not in p):
        return f"/ban user:{uid} time:7 reason:{rule_id}"
    if "перманент" in p:
        return f"/ban user:{uid} time:365 reason:{rule_id}"
    if "глобальн" in p:
        return f"/gban user:{uid} reason:{rule_id}"
    if "обнул" in p:
        return f"/reset user:{uid} reason:{rule_id}"
    return ""


def build_form(cfg: dict, user: discord.Member, rule_id: str,
               punishment: str, evidence_url: str = "") -> str:
    templates = cfg.get("templates", {})
    ptype = _punishment_type(punishment)
    # берём шаблон по типу, если пустой — берём general
    template = templates.get(ptype, "") or templates.get("general", DEFAULT_TEMPLATE)

    rule_text = RULES.get(rule_id, rule_id)
    now = datetime.now()

    variables = {
        "moderatorNick": cfg.get("moderator_nick", "Ваш_Nick_Name"),
        "userId":        str(user.id),
        "userTag":       str(user),
        "ruleId":        rule_id,
        "ruleText":      rule_text,
        "punishment":    punishment,
        "dateIssued":    fmt_date(now),
        "dateEnd":       date_end(punishment),
        "evidence":      evidence_url if evidence_url else "(нет)",
    }

    import re
    return re.sub(r"\{(\w+)\}", lambda m: variables.get(m.group(1), m.group(0)), template)
