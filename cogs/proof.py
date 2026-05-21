import re
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from helpers import (
    RULES, PUNISHMENTS, build_form, fmt_date, date_end,
    get_guild_cfg, get_member_rank_level, build_command,
    APPROVE_MIN_RANK, RANKS,
)
from cogs.servers import get_server_for_member, get_proof_channel, get_log_channel, ensure_server_channels
from stats_db import record_form

REMINDER_HOURS = 2


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


async def server_autocomplete(interaction: discord.Interaction, current: str):
    cfg = interaction.client.cfg
    guild_cfg = get_guild_cfg(cfg, interaction.guild_id)
    known = list(guild_cfg.get("servers", {}).keys())
    known += list(cfg.get("user_servers", {}).values())
    seen, choices = set(), []
    for s in known:
        if s not in seen and current in s:
            choices.append(app_commands.Choice(name=f"Сервер {s}", value=s))
            seen.add(s)
    return choices[:25]


async def punishment_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=p, value=p)
        for p in PUNISHMENTS
        if current.lower() in p.lower()
    ][:25]


# ─── Embed helpers ────────────────────────────────────────────────────────────

def _punishment_title(punishment: str) -> str:
    p = punishment.lower()
    if "устное" in p:        return "Устное предупреждение"
    if "предупрежд" in p:   return "Предупреждение пользователя"
    if "мут" in p:           return "Мут пользователя"
    if "глобальн" in p:      return "Глобальный бан пользователя"
    if "бан" in p or "блокировк" in p or "обнул" in p:
        return "Бан пользователя"
    return "Наказание пользователя"


def _punishment_color(punishment: str) -> int:
    p = punishment.lower()
    if "устное" in p:   return 0x95A5A6
    if "предупрежд" in p: return 0xF39C12
    if "мут" in p:      return 0xE67E22
    if "глобальн" in p: return 0x8B0000
    if "бан" in p or "блокировк" in p or "обнул" in p: return 0xE74C3C
    return 0x3498DB


def _end_timestamp(punishment: str) -> int | None:
    now = datetime.now()
    p = punishment.lower()
    if "мут" in p:
        return int((now + timedelta(minutes=90)).timestamp())
    if "предупрежд" in p and "устное" not in p:
        return int((now + timedelta(days=3)).timestamp())
    if "7-15" in p or ("бан" in p and "перманент" not in p and "глобальн" not in p):
        return int((now + timedelta(days=7)).timestamp())
    return None  # permanent


def _build_punishment_embed(
    moderator: discord.Member,
    violator: discord.Member,
    rule_id: str,
    punishment: str,
    form_type: str,
    evidence_url: str = "",
) -> discord.Embed:
    now_ts = int(datetime.now().timestamp())
    end_ts = _end_timestamp(punishment)

    embed = discord.Embed(
        title=_punishment_title(punishment),
        color=_punishment_color(punishment),
    )
    embed.add_field(name="Модератор",        value=moderator.mention,    inline=True)
    embed.add_field(name="Нарушитель",       value=str(violator.id),     inline=True)
    embed.add_field(name="Причина наказания", value=rule_id,             inline=False)
    # stored for command-building on approve; shown as small inline field
    embed.add_field(name="Наказание",        value=punishment,           inline=True)
    embed.add_field(name="Время",            value=f"<t:{now_ts}:f>",   inline=True)
    embed.add_field(name="Снятие",           value=(f"<t:{end_ts}:R>" if end_ts else "Перманентно"), inline=True)

    if form_type in ("banform", "gbanform"):
        min_rank = RANKS[APPROVE_MIN_RANK[form_type] - 1]
        embed.set_footer(text=f"⏳ Ожидает одобрения: {min_rank}+")

    if evidence_url:
        embed.set_image(url=evidence_url)

    return embed


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _extract_embed_data(embed: discord.Embed) -> dict:
    data = {}
    for field in embed.fields:
        name = field.name
        if name == "Нарушитель":
            try:
                data["user_id"] = int(field.value.strip())
            except ValueError:
                m = re.search(r"\d{15,20}", field.value)
                if m:
                    data["user_id"] = int(m.group())
        elif name == "Наказание":
            data["punishment"] = field.value
        elif name == "Причина наказания":
            data["rule_id"] = field.value.strip()
        elif name == "Модератор":
            m = re.search(r"<@(\d+)>", field.value)
            if m:
                data["mod_id"] = int(m.group(1))
    return data


def _form_type_from_title(title: str) -> str:
    t = title.lower()
    if "глобальный" in t: return "gbanform"
    if "бан" in t:         return "banform"
    return "proof"


async def _post_to_log(guild: discord.Guild, cfg: dict, server: str, embed: discord.Embed):
    log_ch = get_log_channel(guild, cfg, server)
    if not log_ch:
        return
    await log_ch.send(embed=embed.copy())


def _make_done_view(message_id: int) -> discord.ui.View:
    done = discord.ui.View()
    done.add_item(discord.ui.Button(
        label="Управление наказанием", style=discord.ButtonStyle.success,
        disabled=True, custom_id=f"done:manage:{message_id}",
    ))
    done.add_item(discord.ui.Button(
        label="Доказательство", style=discord.ButtonStyle.secondary,
        disabled=True, custom_id=f"done:evidence:{message_id}",
        emoji="🔗",
    ))
    return done


# ─── Add Evidence Modal ───────────────────────────────────────────────────────

class AddEvidenceModal(discord.ui.Modal, title="Добавить доказательство"):
    url_input = discord.ui.TextInput(
        label="Ссылка на доказательство",
        placeholder="https://cdn.discordapp.com/attachments/...",
        max_length=500,
        required=True,
    )

    def __init__(self, proof_message: discord.Message):
        super().__init__()
        self.proof_message = proof_message

    async def on_submit(self, interaction: discord.Interaction):
        url = self.url_input.value.strip()
        embed = self.proof_message.embeds[0]
        embed.set_image(url=url)
        await self.proof_message.edit(embed=embed)
        await interaction.response.send_message("✅ Доказательство добавлено.", ephemeral=True)


# ─── Management View (ephemeral, per-click) ───────────────────────────────────

class ManagementView(discord.ui.View):
    def __init__(self, proof_message: discord.Message, form_type: str):
        super().__init__(timeout=120)
        self.proof_message = proof_message
        self.form_type = form_type

    @discord.ui.button(label="✅ Одобрить", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.form_type == "proof":
            await interaction.response.send_message("ℹ️ Форма пруфа не требует одобрения.", ephemeral=True)
            return

        min_level = APPROVE_MIN_RANK.get(self.form_type, 2)
        if get_member_rank_level(interaction.user) < min_level:
            await interaction.response.send_message(
                f"❌ Требуется минимум: **{RANKS[min_level - 1]}**.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        embed = self.proof_message.embeds[0]
        data = _extract_embed_data(embed)
        user_id   = data.get("user_id", 0)
        punishment = data.get("punishment", "")
        rule_id   = data.get("rule_id", "")
        mod_id    = data.get("mod_id", 0)

        command = build_command(punishment, user_id, rule_id)
        rank_display = next(
            (r.name for r in reversed(getattr(interaction.user, "roles", [])) if r.name in RANKS), "—"
        )
        embed.color = discord.Color.green()
        embed.set_footer(text=f"✅ Одобрено: {interaction.user} ({rank_display})")
        if command:
            embed.add_field(name="💻 Команда", value=f"```\n{command}\n```", inline=False)

        await self.proof_message.edit(embed=embed, view=_make_done_view(self.proof_message.id))
        record_form(mod_id, self.form_type, "approved")

        server = get_server_for_member(interaction.user, interaction.client.cfg)
        if server:
            await _post_to_log(interaction.guild, interaction.client.cfg, server, embed)

        await interaction.followup.send("✅ Форма одобрена.", ephemeral=True)

    @discord.ui.button(label="❌ Отклонить", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.form_type == "proof":
            await interaction.response.send_message("ℹ️ Форма пруфа не требует отклонения.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        embed = self.proof_message.embeds[0]
        data = _extract_embed_data(embed)
        mod_id = data.get("mod_id", 0)

        rank_display = next(
            (r.name for r in reversed(getattr(interaction.user, "roles", [])) if r.name in RANKS), "—"
        )
        embed.color = discord.Color.red()
        embed.set_footer(text=f"❌ Отклонено: {interaction.user} ({rank_display})")

        await self.proof_message.edit(embed=embed, view=_make_done_view(self.proof_message.id))
        record_form(mod_id, self.form_type, "rejected")

        server = get_server_for_member(interaction.user, interaction.client.cfg)
        if server:
            await _post_to_log(interaction.guild, interaction.client.cfg, server, embed)

        await interaction.followup.send("❌ Форма отклонена.", ephemeral=True)

    @discord.ui.button(label="📎 Добавить доказательство", style=discord.ButtonStyle.secondary)
    async def add_evidence(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddEvidenceModal(self.proof_message))


# ─── PunishmentView (persistent) ─────────────────────────────────────────────

class PunishmentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Управление наказанием",
        style=discord.ButtonStyle.success,
        custom_id="punishment:manage",
    )
    async def manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        form_type = _form_type_from_title(embed.title or "")
        min_level = APPROVE_MIN_RANK.get(form_type, 2)

        if get_member_rank_level(interaction.user) < min_level:
            await interaction.response.send_message(
                f"❌ Требуется минимум: **{RANKS[min_level - 1]}**.", ephemeral=True
            )
            return

        view = ManagementView(interaction.message, form_type)
        await interaction.response.send_message(
            "**Управление наказанием** — выберите действие:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Доказательство",
        style=discord.ButtonStyle.secondary,
        custom_id="punishment:evidence",
        emoji="🔗",
    )
    async def evidence(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        if embed.image and embed.image.url:
            await interaction.response.send_message(
                f"🔗 **Доказательство:** {embed.image.url}", ephemeral=True
            )
        else:
            await interaction.response.send_modal(AddEvidenceModal(interaction.message))


# ─── Общая логика постинга формы в канал ─────────────────────────────────────

async def _post_form(
    interaction: discord.Interaction,
    embed: discord.Embed,
    form_text: str,
    server_override: str | None = None,
):
    guild = interaction.guild
    cfg = interaction.client.cfg
    member = interaction.user

    server = server_override or get_server_for_member(member, cfg)
    proof_ch = None

    if server:
        proof_ch = get_proof_channel(guild, cfg, server)

    if not proof_ch:
        for g_cfg in cfg.get("guilds", {}).values():
            ch_id = g_cfg.get("proof_channel_id", 0)
            if ch_id:
                proof_ch = interaction.client.get_channel(ch_id)
                if proof_ch:
                    break

    if not proof_ch and server:
        try:
            await ensure_server_channels(guild, server, cfg)
            proof_ch = get_proof_channel(guild, cfg, server)
        except discord.Forbidden:
            pass

    if not proof_ch:
        if server:
            await interaction.followup.send(
                f"❌ Канал для форм не найден (сервер **{server}**).\n"
                f"Попросите владельца запустить `/setupserver {server}` или настроить `/setup`.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "❌ Не удалось определить ваш сервер. Используйте `/assignserver` или параметр `server` в команде.",
                ephemeral=True,
            )
        return

    guild_cfg = get_guild_cfg(cfg, proof_ch.guild.id)
    review_role_id = guild_cfg.get("review_role_id", 0)
    mention = f"<@&{review_role_id}>" if review_role_id else None

    form_type = _form_type_from_title(embed.title or "")
    view = PunishmentView()
    await proof_ch.send(content=mention, embed=embed, view=view)

    record_form(interaction.user.id, form_type, "sent")

    try:
        await interaction.user.send(f"📝 **Форма для отчёта:**\n```\n{form_text}\n```")
    except discord.Forbidden:
        pass

    await interaction.followup.send(f"✅ Отправлено в {proof_ch.mention}!", ephemeral=True)


# ─── Cog ─────────────────────────────────────────────────────────────────────

class ProofCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._reminded: set[int] = set()
        self.reminder_loop.start()

    def cog_unload(self):
        self.reminder_loop.cancel()

    @tasks.loop(minutes=30)
    async def reminder_loop(self):
        now = discord.utils.utcnow()
        cfg = self.bot.cfg
        seen_ids: set[int] = set()
        channels_to_check: list[discord.TextChannel] = []

        for guild_cfg in cfg.get("guilds", {}).values():
            ch_id = guild_cfg.get("proof_channel_id", 0)
            if ch_id and ch_id not in seen_ids:
                ch = self.bot.get_channel(ch_id)
                if isinstance(ch, discord.TextChannel):
                    channels_to_check.append(ch)
                    seen_ids.add(ch_id)

            for srv_data in guild_cfg.get("servers", {}).values():
                srv_ch_id = srv_data.get("proof", 0)
                if srv_ch_id and srv_ch_id not in seen_ids:
                    ch = self.bot.get_channel(srv_ch_id)
                    if isinstance(ch, discord.TextChannel):
                        channels_to_check.append(ch)
                        seen_ids.add(srv_ch_id)

        if len(self._reminded) > 10_000:
            self._reminded.clear()

        cutoff = now - timedelta(days=7)
        for ch in channels_to_check:
            guild_cfg = get_guild_cfg(cfg, ch.guild.id)
            role_id = guild_cfg.get("review_role_id", 0)
            mention = f"<@&{role_id}>" if role_id else "⚠️"
            try:
                async for msg in ch.history(limit=100, after=cutoff, oldest_first=True):
                    if msg.id in self._reminded or not msg.embeds:
                        continue
                    pending = any(
                        getattr(c, "custom_id", "") == "punishment:manage"
                        for row in msg.components
                        for c in getattr(row, "children", [])
                    )
                    if not pending:
                        continue
                    # only remind for banform/gbanform
                    title = msg.embeds[0].title or ""
                    if _form_type_from_title(title) == "proof":
                        continue
                    age_hours = (now - msg.created_at).total_seconds() / 3600
                    if age_hours >= REMINDER_HOURS:
                        self._reminded.add(msg.id)
                        await ch.send(
                            f"{mention} Форма ожидает одобрения уже **{int(age_hours)}ч**!",
                            reference=msg,
                            mention_author=False,
                        )
            except (discord.Forbidden, discord.HTTPException):
                pass

    @reminder_loop.before_loop
    async def before_reminder(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="proof", description="Отправить доказательство нарушения")
    @app_commands.describe(
        user="Нарушитель",
        rule="Пункт правил (2.1, 3.1 и т.д.)",
        punishment="Выданное наказание",
        evidence="Скриншот доказательства (необязательно)",
        server="Номер сервера (если не определяется автоматически)",
    )
    @app_commands.autocomplete(rule=rule_autocomplete, punishment=punishment_autocomplete, server=server_autocomplete)
    async def proof_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        punishment: str,
        evidence: discord.Attachment | None = None,
        server: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        embed = _build_punishment_embed(
            interaction.user, user, rule, punishment, "proof",
            evidence_url=evidence.url if evidence else "",
        )
        form_text = build_form(self.bot.cfg, user, rule, punishment,
                               evidence_url=evidence.url if evidence else "")
        await _post_form(interaction, embed, form_text, server_override=server)

    @app_commands.command(name="banform", description="Сгенерировать форму бана")
    @app_commands.describe(
        user="Нарушитель", rule="Пункт правил",
        punishment="Наказание (по умолчанию: Бан 7-15 дней)",
        evidence="Скриншот (необязательно)",
        server="Номер сервера (если не определяется автоматически)",
    )
    @app_commands.autocomplete(rule=rule_autocomplete, punishment=punishment_autocomplete, server=server_autocomplete)
    async def banform_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        punishment: str = "Бан 7-15 дней",
        evidence: discord.Attachment | None = None,
        server: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if user.id == interaction.user.id:
            await interaction.followup.send("❌ Нельзя выдать наказание самому себе.", ephemeral=True)
            return

        embed = _build_punishment_embed(
            interaction.user, user, rule, punishment, "banform",
            evidence_url=evidence.url if evidence else "",
        )
        form_text = build_form(self.bot.cfg, user, rule, punishment,
                               evidence_url=evidence.url if evidence else "")
        await _post_form(interaction, embed, form_text, server_override=server)

    @app_commands.command(name="gbanform", description="Сгенерировать форму глобального бана")
    @app_commands.describe(
        user="Нарушитель", rule="Пункт правил",
        evidence="Скриншот (необязательно)",
        server="Номер сервера (если не определяется автоматически)",
    )
    @app_commands.autocomplete(rule=rule_autocomplete, server=server_autocomplete)
    async def gbanform_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        evidence: discord.Attachment | None = None,
        server: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if user.id == interaction.user.id:
            await interaction.followup.send("❌ Нельзя выдать наказание самому себе.", ephemeral=True)
            return

        embed = _build_punishment_embed(
            interaction.user, user, rule, "Глобальная блокировка", "gbanform",
            evidence_url=evidence.url if evidence else "",
        )
        form_text = build_form(self.bot.cfg, user, rule, "Глобальная блокировка",
                               evidence_url=evidence.url if evidence else "")
        await _post_form(interaction, embed, form_text, server_override=server)


async def setup(bot: commands.Bot):
    await bot.add_cog(ProofCog(bot))
    bot.add_view(PunishmentView())
