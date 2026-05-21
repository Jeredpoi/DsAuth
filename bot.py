import discord
from discord import app_commands
from discord.ext import commands
import os
import json
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
from keep_alive import keep_alive

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PROXY = os.getenv("PROXY_URL")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
START_TIME = time.time()

CONFIG_PATH = "config.json"


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"proof_channel_id": 0, "review_role_id": 0, "moderator_nick": "Ваш_Nick_Name"}


def save_config(data: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


cfg = load_config()

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

PUNISHMENTS = [
    "Устное предупреждение",
    "Предупреждение",
    "Мут 90 минут",
    "Бан 7-15 дней",
    "Перманентная блокировка",
    "Глобальная блокировка",
    "Обнуление",
]

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, proxy=PROXY)


# ─── Helpers ─────────────────────────────────────────────────────────────────

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


def build_form(mod_nick: str, user: discord.Member, rule_id: str, punishment: str,
               evidence_url: str = "") -> str:
    rule_text = RULES.get(rule_id, rule_id)
    now = datetime.now()
    proof_line = evidence_url if evidence_url else "(прикреплено выше)"
    return (
        f"1) Ваш Nick_Name: {mod_nick}\n"
        f"2) ID Discord и тег нарушителя: {user.id} / {user}\n"
        f"3) Пункт правил, который был нарушен: {rule_id} — {rule_text}\n"
        f"4) Выданное наказание: {punishment}\n"
        f"5) Дата выдачи: {fmt_date(now)}\n"
        f"6) Дата снятия: {date_end(punishment)}\n"
        f"7) Доказательства: {proof_line}"
    )


# ─── Autocomplete ─────────────────────────────────────────────────────────────

async def rule_autocomplete(interaction: discord.Interaction, current: str):
    results = []
    for rule_id, rule_text in RULES.items():
        label = f"{rule_id} — {rule_text}"
        if current.lower() in label.lower():
            results.append(app_commands.Choice(name=label[:100], value=rule_id))
        if len(results) >= 25:
            break
    return results


async def punishment_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=p, value=p)
        for p in PUNISHMENTS
        if current.lower() in p.lower()
    ][:25]


# ─── Proof buttons ────────────────────────────────────────────────────────────

class ProofView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Одобрить", style=discord.ButtonStyle.success, custom_id="proof:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.set_footer(text=f"✅ Одобрено: {interaction.user} ({interaction.user.id})")
        mid = interaction.message.id
        done = discord.ui.View()
        done.add_item(discord.ui.Button(label="✅ Одобрено", style=discord.ButtonStyle.success,
                                        disabled=True, custom_id=f"done:a:{mid}"))
        done.add_item(discord.ui.Button(label="❌ Отклонить", style=discord.ButtonStyle.danger,
                                        disabled=True, custom_id=f"done:r:{mid}"))
        await interaction.message.edit(embed=embed, view=done)
        await interaction.response.send_message("✅ Доказательство одобрено.", ephemeral=True)

    @discord.ui.button(label="❌ Отклонить", style=discord.ButtonStyle.danger, custom_id="proof:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.set_footer(text=f"❌ Отклонено: {interaction.user} ({interaction.user.id})")
        mid = interaction.message.id
        done = discord.ui.View()
        done.add_item(discord.ui.Button(label="✅ Одобрить", style=discord.ButtonStyle.success,
                                        disabled=True, custom_id=f"done:a:{mid}"))
        done.add_item(discord.ui.Button(label="❌ Отклонено", style=discord.ButtonStyle.danger,
                                        disabled=True, custom_id=f"done:r:{mid}"))
        await interaction.message.edit(embed=embed, view=done)
        await interaction.response.send_message("❌ Доказательство отклонено.", ephemeral=True)


# ─── /proof ──────────────────────────────────────────────────────────────────

@bot.tree.command(name="proof", description="Отправить доказательство нарушения")
@app_commands.describe(
    user="Нарушитель",
    rule="Пункт правил (2.1, 3.1 и т.д.)",
    punishment="Выданное наказание",
    evidence="Скриншот доказательства",
)
@app_commands.autocomplete(rule=rule_autocomplete, punishment=punishment_autocomplete)
async def proof_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    rule: str,
    punishment: str,
    evidence: discord.Attachment | None = None,
):
    await interaction.response.defer(ephemeral=True)

    proof_ch_id = cfg.get("proof_channel_id", 0)
    review_role_id = cfg.get("review_role_id", 0)

    if not proof_ch_id:
        await interaction.followup.send("❌ Канал доказательств не настроен. Используйте `/setup`.", ephemeral=True)
        return

    proof_channel = interaction.guild.get_channel(proof_ch_id)
    if not proof_channel:
        await interaction.followup.send("❌ Канал не найден. Проверьте `/setup`.", ephemeral=True)
        return

    rule_text = RULES.get(rule, rule)

    embed = discord.Embed(
        title="📋 Доказательство нарушения",
        color=0x3498DB,
        timestamp=datetime.now(),
    )
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.add_field(name="Нарушитель", value=f"{user.mention}\n`{user.id}`", inline=True)
    embed.add_field(name="Пункт правил", value=f"`{rule}` — {rule_text}", inline=True)
    embed.add_field(name="Наказание", value=punishment, inline=True)
    embed.add_field(name="Модератор", value=interaction.user.mention, inline=True)
    embed.add_field(name="Дата", value=fmt_date(datetime.now()), inline=True)

    if evidence:
        embed.set_image(url=evidence.url)

    embed.set_footer(text="Ожидает проверки...")

    mention = f"<@&{review_role_id}>" if review_role_id else None
    await proof_channel.send(content=mention, embed=embed, view=ProofView())

    # DM форма модератору
    mod_nick = cfg.get("moderator_nick", "Ваш_Nick_Name")
    form_text = build_form(mod_nick, user, rule, punishment,
                           evidence_url=evidence.url if evidence else "")
    try:
        await interaction.user.send(f"📝 **Форма для отчёта:**\n```\n{form_text}\n```")
    except discord.Forbidden:
        pass

    await interaction.followup.send(f"✅ Доказательство отправлено в {proof_channel.mention}!", ephemeral=True)


# ─── /banform ────────────────────────────────────────────────────────────────

@bot.tree.command(name="banform", description="Сгенерировать форму бана")
@app_commands.describe(
    user="Нарушитель",
    rule="Пункт правил",
    punishment="Наказание (по умолчанию: Бан 7-15 дней)",
    evidence="Скриншот доказательства (необязательно)",
)
@app_commands.autocomplete(rule=rule_autocomplete, punishment=punishment_autocomplete)
async def banform_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    rule: str,
    punishment: str = "Бан 7-15 дней",
    evidence: discord.Attachment | None = None,
):
    await interaction.response.defer(ephemeral=True)

    mod_nick = cfg.get("moderator_nick", "Ваш_Nick_Name")
    form_text = build_form(mod_nick, user, rule, punishment,
                           evidence_url=evidence.url if evidence else "")
    rule_text = RULES.get(rule, rule)

    embed = discord.Embed(title="🔨 Форма бана", color=0xE74C3C, timestamp=datetime.now())
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.add_field(name="Нарушитель", value=f"{user.mention} (`{user.id}`)", inline=False)
    embed.add_field(name="Пункт правил", value=f"`{rule}` — {rule_text}", inline=False)
    embed.add_field(name="Наказание", value=punishment, inline=False)
    if evidence:
        embed.set_image(url=evidence.url)

    try:
        await interaction.user.send(
            content=f"📝 **Форма для отчёта:**\n```\n{form_text}\n```",
            embed=embed,
        )
        await interaction.followup.send("✅ Форма бана отправлена в личку!", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(
            f"📝 **Форма для отчёта:**\n```\n{form_text}\n```",
            embed=embed,
            ephemeral=True,
        )


# ─── /gbanform ───────────────────────────────────────────────────────────────

@bot.tree.command(name="gbanform", description="Сгенерировать форму глобального бана")
@app_commands.describe(
    user="Нарушитель",
    rule="Пункт правил",
    evidence="Скриншот доказательства (необязательно)",
)
@app_commands.autocomplete(rule=rule_autocomplete)
async def gbanform_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    rule: str,
    evidence: discord.Attachment | None = None,
):
    await interaction.response.defer(ephemeral=True)

    mod_nick = cfg.get("moderator_nick", "Ваш_Nick_Name")
    form_text = build_form(mod_nick, user, rule, "Глобальная блокировка",
                           evidence_url=evidence.url if evidence else "")
    rule_text = RULES.get(rule, rule)

    embed = discord.Embed(title="🌐 Форма глобального бана", color=0x8B0000, timestamp=datetime.now())
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.add_field(name="Нарушитель", value=f"{user.mention} (`{user.id}`)", inline=False)
    embed.add_field(name="Пункт правил", value=f"`{rule}` — {rule_text}", inline=False)
    embed.add_field(name="Наказание", value="Глобальная блокировка", inline=False)
    if evidence:
        embed.set_image(url=evidence.url)

    try:
        await interaction.user.send(
            content=f"📝 **Форма для отчёта:**\n```\n{form_text}\n```",
            embed=embed,
        )
        await interaction.followup.send("✅ Форма G-бана отправлена в личку!", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(
            f"📝 **Форма для отчёта:**\n```\n{form_text}\n```",
            embed=embed,
            ephemeral=True,
        )


# ─── /uptime ─────────────────────────────────────────────────────────────────

@bot.tree.command(name="uptime", description="Время работы бота")
async def uptime_cmd(interaction: discord.Interaction):
    elapsed = int(time.time() - START_TIME)
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)
    await interaction.response.send_message(
        f"⏱️ Бот работает: **{h}ч {m}м {s}с**", ephemeral=True
    )


# ─── /setup ──────────────────────────────────────────────────────────────────

@bot.tree.command(name="setup", description="Настройка бота (только для владельца)")
@app_commands.describe(
    proof_channel="Канал куда постить доказательства",
    review_role="Роль, которая получает уведомление и может одобрять",
    moderator_nick="Ваш ник для форм отчётов",
)
async def setup_cmd(
    interaction: discord.Interaction,
    proof_channel: discord.TextChannel | None = None,
    review_role: discord.Role | None = None,
    moderator_nick: str | None = None,
):
    is_owner = interaction.user.id == OWNER_ID or await bot.is_owner(interaction.user)
    if not is_owner:
        await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
        return

    if proof_channel:
        cfg["proof_channel_id"] = proof_channel.id
    if review_role:
        cfg["review_role_id"] = review_role.id
    if moderator_nick:
        cfg["moderator_nick"] = moderator_nick

    save_config(cfg)

    lines = ["✅ **Настройки сохранены:**"]
    lines.append(f"• Канал доказательств: {interaction.guild.get_channel(cfg['proof_channel_id']).mention if cfg.get('proof_channel_id') else 'не задан'}")
    lines.append(f"• Роль проверяющих: <@&{cfg['review_role_id']}>" if cfg.get("review_role_id") else "• Роль проверяющих: не задана")
    lines.append(f"• Ник модератора: `{cfg.get('moderator_nick', 'не задан')}`")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ─── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    bot.add_view(ProofView())
    await bot.tree.sync()
    print(f"✅ Бот запущен как {bot.user} (ID: {bot.user.id})")


keep_alive()
bot.run(TOKEN)
