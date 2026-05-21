import json
import os
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
    "templates": {
        "general": DEFAULT_TEMPLATE,
        "oral": "", "warn": "", "mute": "", "ban": "", "gban": "",
    },
    "user_servers": {},
    "guilds": {},
}

DEFAULT_GUILD_CFG: dict = {
    "proof_channel_id": 0,
    "review_role_id": 0,
    "auth_channel_id": 0,
    "auth_review_channel_id": 0,
}


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("moderator_nick", DEFAULT_CONFIG["moderator_nick"])
        data.setdefault("templates", DEFAULT_CONFIG["templates"].copy())
        data.setdefault("user_servers", {})
        data.setdefault("guilds", {})
        for k, v in DEFAULT_CONFIG["templates"].items():
            data["templates"].setdefault(k, v)
        for rule_id, rule_text in data.get("rules", {}).items():
            RULES[rule_id] = rule_text
        # Миграция: переносим user_servers из guild-конфигов в глобальный
        for g_cfg in data["guilds"].values():
            for uid, srv in g_cfg.pop("user_servers", {}).items():
                data["user_servers"].setdefault(uid, srv)
        return data
    return {
        "moderator_nick": DEFAULT_CONFIG["moderator_nick"],
        "templates": DEFAULT_CONFIG["templates"].copy(),
        "user_servers": {},
        "guilds": {},
    }


def get_guild_cfg(cfg: dict, guild_id: int) -> dict:
    key = str(guild_id)
    cfg["guilds"].setdefault(key, DEFAULT_GUILD_CFG.copy())
    return cfg["guilds"][key]


def save_config(data: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def fmt_date(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")


def date_end(punishment: str) -> str:
    now = datetime.now()
    p = punishment.lower()
    if "мут" in p:
        return fmt_date(now + timedelta(minutes=90))
    if "предупрежд" in p:
        return fmt_date(now + timedelta(days=3))
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
    "gbanform": RANK_LEVELS["Куратор модерации"],
}

LEADERSHIP_RANKS: frozenset[str] = frozenset({
    "Куратор модерации",
    "Заместитель главного модератора",
    "Главный модератор",
})

UNVERIFIED_ROLE_NAME = "Не авторизован"


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
