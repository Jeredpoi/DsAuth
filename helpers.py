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
    "2.5":  "Персональная информация",
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


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"proof_channel_id": 0, "review_role_id": 0, "moderator_nick": "Ваш_Nick_Name"}


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
    if "7-15" in p or ("бан" in p and "перманент" not in p and "глобальн" not in p):
        return fmt_date(now + timedelta(days=7))
    if "перманент" in p or "глобальн" in p:
        return "Перманентно"
    return fmt_date(now)


def build_form(mod_nick: str, user: discord.Member, rule_id: str,
               punishment: str, evidence_url: str = "") -> str:
    rule_text = RULES.get(rule_id, rule_id)
    now = datetime.now()
    proof_line = evidence_url if evidence_url else "(нет)"
    return (
        f"1) Ваш Nick_Name: {mod_nick}\n"
        f"2) ID Discord и тег нарушителя: {user.id} / {user}\n"
        f"3) Пункт правил: {rule_id} — {rule_text}\n"
        f"4) Выданное наказание: {punishment}\n"
        f"5) Дата выдачи: {fmt_date(now)}\n"
        f"6) Дата снятия: {date_end(punishment)}\n"
        f"7) Доказательства: {proof_line}"
    )
