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
from cogs.servers import get_server_for_member, get_proof_channel, get_banform_channel, get_log_channel, ensure_server_channels
from db import record_form, all_user_servers

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
    known += list(all_user_servers().values())
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


# ─── Punishment helpers ───────────────────────────────────────────────────────

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
    if "устное" in p:      return 0x95A5A6
    if "предупрежд" in p:  return 0xF39C12
    if "мут" in p:         return 0xE67E22
    if "глобальн" in p:    return 0x8B0000
    if "бан" in p or "блокировк" in p or "обнул" in p: return 0xE74C3C
    return 0x3498DB


def _end_timestamp(punishment: str) -> tuple[int, str] | None:
    now = datetime.now()
    p = punishment.lower()
    if "мут" in p:
        return (int((now + timedelta(minutes=90)).timestamp()), "R")
    if "15 дней" in p:
        return (int((now + timedelta(days=15)).timestamp()), "f")
    if "7 дней" in p or "7-15" in p or ("бан" in p and "перманент" not in p and "глобальн" not in p):
        return (int((now + timedelta(days=7)).timestamp()), "f")
    return None


def _form_type_from_punishment(punishment: str) -> str:
    p = punishment.lower()
    if "глобальн" in p:  return "gbanform"
    if "бан" in p or "блокировк" in p: return "banform"
    return "proof"


def _form_type_from_title(title: str) -> str:
    t = title.lower()
    if "глобальный" in t: return "gbanform"
    if "бан" in t:         return "banform"
    return "proof"


# ─── Components V2 layout builders ────────────────────────────────────────────

def _build_header_text(title: str, mod_id: int) -> str:
    return f"## {title}\n**Модератор:** <@{mod_id}>"


def _build_fields_text(
    user_id: int,
    rule_id: str,
    punishment: str,
    form_type: str,
) -> str:
    now_ts = int(datetime.now().timestamp())
    end_info = _end_timestamp(punishment)

    lines = [
        f"**Нарушитель:** {user_id}",
        f"**Причина наказания:** {rule_id}",
        f"**Наказание:** {punishment}",
        f"**Время:** <t:{now_ts}:f>",
    ]
    if end_info:
        ts, fmt = end_info
        lines.append(f"**Снятие:** <t:{ts}:{fmt}>")
    if form_type in ("banform", "gbanform"):
        min_rank = RANKS[APPROVE_MIN_RANK[form_type] - 1]
        lines.append(f"⏳ Ожидает одобрения: {min_rank}+")

    return "\n".join(lines)


def _make_layout_view(
    header_text: str,
    fields_text: str,
    evidence_url: str,
    color: int,
    violator_avatar_url: str = "",
    manage_disabled: bool = False,
    evidence_disabled: bool = False,
) -> discord.ui.LayoutView:
    manage_btn = discord.ui.Button(
        label="Управление наказанием",
        style=discord.ButtonStyle.success if not manage_disabled else discord.ButtonStyle.secondary,
        custom_id="punishment:manage",
        disabled=manage_disabled,
    )
    evidence_btn = discord.ui.Button(
        label="Доказательство",
        style=discord.ButtonStyle.secondary,
        custom_id="punishment:evidence",
        emoji="🔗",
        disabled=evidence_disabled,
    )

    # Header section: title + violator avatar thumbnail (if available)
    if violator_avatar_url:
        header_section = discord.ui.Section(
            discord.ui.TextDisplay(header_text),
            accessory=discord.ui.Thumbnail(violator_avatar_url),
        )
    else:
        header_section = discord.ui.Section(
            discord.ui.TextDisplay(header_text),
            accessory=manage_btn,
        )

    container_items: list[discord.ui.Item] = [header_section, discord.ui.Separator()]

    if violator_avatar_url:
        # With thumbnail in header: fields get the manage button
        container_items.append(
            discord.ui.Section(
                discord.ui.TextDisplay(fields_text),
                accessory=manage_btn,
            )
        )
    else:
        # Without thumbnail: fields section has no separate button (manage is in header)
        container_items.append(discord.ui.TextDisplay(fields_text))

    # Evidence row
    container_items.append(
        discord.ui.Section(
            discord.ui.TextDisplay(""),
            accessory=evidence_btn,
        )
    )

    if evidence_url:
        container_items.append(
            discord.ui.MediaGallery(discord.MediaGalleryItem(evidence_url))
        )

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*container_items, accent_color=color))
    return view


# ─── Extract data from V2 message ─────────────────────────────────────────────

def _collect_text_from_components(comps) -> str:
    """Recursively collect TextDisplay content from discord.components objects."""
    texts = []
    for comp in comps:
        if hasattr(comp, 'content'):  # TextDisplay component
            texts.append(comp.content)
        if hasattr(comp, 'children') and comp.children:
            texts.append(_collect_text_from_components(comp.children))
        if hasattr(comp, 'accessory') and comp.accessory:
            if hasattr(comp.accessory, 'content'):
                texts.append(comp.accessory.content)
    return "\n".join(t for t in texts if t)


def _extract_v2_data(message: discord.Message) -> dict:
    all_text = _collect_text_from_components(message.components)
    data: dict = {}

    m = re.search(r'\*\*Модератор:\*\* <@(\d+)>', all_text)
    if m: data["mod_id"] = int(m.group(1))

    m = re.search(r'\*\*Нарушитель:\*\* (\d+)', all_text)
    if m: data["user_id"] = int(m.group(1))

    m = re.search(r'\*\*Причина наказания:\*\* (.+)', all_text)
    if m: data["rule_id"] = m.group(1).strip()

    m = re.search(r'\*\*Наказание:\*\* (.+)', all_text)
    if m: data["punishment"] = m.group(1).strip()

    m = re.search(r'## (.+)', all_text)
    if m: data["title"] = m.group(1).strip()

    return data


def _extract_evidence_url_from_components(comps) -> str:
    """Walk MediaGallery items to find evidence URL."""
    for comp in comps:
        # discord.components.MediaGalleryComponent has .children with media items
        type_val = getattr(getattr(comp, 'type', None), 'value', None)
        if type_val == 20:  # ComponentType.media_gallery
            items = getattr(comp, 'children', []) or getattr(comp, 'items', [])
            for item in items:
                media = getattr(item, 'media', None)
                if media:
                    url = getattr(media, 'url', None) or getattr(media, 'proxy_url', None)
                    if url:
                        return url
        if hasattr(comp, 'children') and comp.children:
            url = _extract_evidence_url_from_components(comp.children)
            if url:
                return url
    return ""


def _is_pending_v2_message(message: discord.Message) -> bool:
    """Check if a V2 message has a non-disabled punishment:manage button."""
    def walk(comps):
        for comp in comps:
            cid = getattr(comp, 'custom_id', None)
            if cid == "punishment:manage" and not getattr(comp, 'disabled', False):
                return True
            if hasattr(comp, 'children') and comp.children and walk(comp.children):
                return True
            if hasattr(comp, 'accessory') and comp.accessory:
                a = comp.accessory
                if getattr(a, 'custom_id', None) == "punishment:manage" and not getattr(a, 'disabled', False):
                    return True
        return False
    return walk(message.components)


# ─── Log embed (for log channels, not subject to V2 restrictions) ─────────────

def _build_log_embed(
    title: str,
    mod_id: int,
    user_id: int,
    rule_id: str,
    punishment: str,
    color: int,
    status_text: str = "",
) -> discord.Embed:
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Модератор",          value=f"<@{mod_id}>",   inline=False)
    embed.add_field(name="Нарушитель",         value=str(user_id),     inline=False)
    embed.add_field(name="Причина наказания",  value=rule_id,          inline=False)
    embed.add_field(name="Наказание",          value=punishment,       inline=False)
    if status_text:
        embed.set_footer(text=status_text)
    return embed


async def _post_to_log(guild: discord.Guild, cfg: dict, server: str, embed: discord.Embed):
    log_ch = get_log_channel(guild, cfg, server)
    if not log_ch:
        return
    try:
        await log_ch.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


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

        # Rebuild layout view with the new evidence URL
        data = _extract_v2_data(self.proof_message)
        header_text = _collect_text_from_components(self.proof_message.components).split("**Нарушитель:**")[0].strip()
        fields_text_match = re.search(
            r'(\*\*Нарушитель:\*\*.*?)(?=\n## |\Z)',
            _collect_text_from_components(self.proof_message.components),
            re.DOTALL,
        )
        fields_text = fields_text_match.group(1).strip() if fields_text_match else ""

        # Simpler: just re-extract header and fields from the stored data
        title = data.get("title", "Наказание пользователя")
        mod_id = data.get("mod_id", 0)
        user_id = data.get("user_id", 0)
        rule_id = data.get("rule_id", "")
        punishment = data.get("punishment", "")
        form_type = _form_type_from_punishment(punishment)

        h_text = _build_header_text(title, mod_id)
        f_text = _build_fields_text(user_id, rule_id, punishment, form_type)
        color = _punishment_color(punishment)

        new_view = _make_layout_view(h_text, f_text, url, color)
        _wire_callbacks(new_view)

        await self.proof_message.edit(view=new_view)
        await interaction.response.send_message("✅ Доказательство добавлено.", ephemeral=True)


# ─── Ephemeral management views ───────────────────────────────────────────────

class ProofManagementView(discord.ui.View):
    """Для /proof — только добавление доказательства."""
    def __init__(self, proof_message: discord.Message):
        super().__init__(timeout=120)
        self.proof_message = proof_message

    @discord.ui.button(label="📎 Добавить доказательство", style=discord.ButtonStyle.secondary)
    async def add_evidence(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddEvidenceModal(self.proof_message))


class FormManagementView(discord.ui.View):
    """Для /banform и /gbanform — одобрение, отклонение и доказательство."""
    def __init__(self, proof_message: discord.Message, form_type: str):
        super().__init__(timeout=120)
        self.proof_message = proof_message
        self.form_type = form_type

    @discord.ui.button(label="✅ Одобрить", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        min_level = APPROVE_MIN_RANK.get(self.form_type, 2)
        if get_member_rank_level(interaction.user) < min_level:
            await interaction.response.send_message(
                f"❌ Требуется минимум: **{RANKS[min_level - 1]}**.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        data = _extract_v2_data(self.proof_message)
        user_id   = data.get("user_id", 0)
        punishment = data.get("punishment", "")
        rule_id   = data.get("rule_id", "")
        mod_id    = data.get("mod_id", 0)
        title     = data.get("title", "Наказание пользователя")

        command = build_command(punishment, user_id, rule_id)
        rank_display = next(
            (r.name for r in reversed(getattr(interaction.user, "roles", [])) if r.name in RANKS), "—"
        )
        status = f"✅ Одобрено: {interaction.user} ({rank_display})"
        if command:
            status += f"\n💻 `{command}`"

        h_text = _build_header_text(title, mod_id)
        f_text = _build_fields_text(user_id, rule_id, punishment, self.form_type)
        f_text = f_text.replace(
            next((l for l in f_text.split("\n") if "⏳" in l), "___NONE___"),
            status,
        )
        evidence_url = _extract_evidence_url_from_components(self.proof_message.components)
        done_view = _make_layout_view(h_text, f_text, evidence_url, 0x2ECC71, manage_disabled=True, evidence_disabled=True)

        await self.proof_message.edit(view=done_view)
        record_form(mod_id, self.form_type, "approved")

        server = get_server_for_member(interaction.user, interaction.client.cfg)
        if server:
            log_embed = _build_log_embed(title, mod_id, user_id, rule_id, punishment, 0x2ECC71, status)
            await _post_to_log(interaction.guild, interaction.client.cfg, server, log_embed)

        await interaction.followup.send("✅ Форма одобрена.", ephemeral=True)

    @discord.ui.button(label="❌ Отклонить", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        data = _extract_v2_data(self.proof_message)
        mod_id    = data.get("mod_id", 0)
        user_id   = data.get("user_id", 0)
        rule_id   = data.get("rule_id", "")
        punishment = data.get("punishment", "")
        title     = data.get("title", "Наказание пользователя")

        rank_display = next(
            (r.name for r in reversed(getattr(interaction.user, "roles", [])) if r.name in RANKS), "—"
        )
        status = f"❌ Отклонено: {interaction.user} ({rank_display})"

        h_text = _build_header_text(title, mod_id)
        f_text = _build_fields_text(user_id, rule_id, punishment, self.form_type)
        f_text = f_text.replace(
            next((l for l in f_text.split("\n") if "⏳" in l), "___NONE___"),
            status,
        )
        evidence_url = _extract_evidence_url_from_components(self.proof_message.components)
        done_view = _make_layout_view(h_text, f_text, evidence_url, 0xE74C3C, manage_disabled=True, evidence_disabled=True)

        await self.proof_message.edit(view=done_view)
        record_form(mod_id, self.form_type, "rejected")

        server = get_server_for_member(interaction.user, interaction.client.cfg)
        if server:
            log_embed = _build_log_embed(title, mod_id, user_id, rule_id, punishment, 0xE74C3C, status)
            await _post_to_log(interaction.guild, interaction.client.cfg, server, log_embed)

        await interaction.followup.send("❌ Форма отклонена.", ephemeral=True)

    @discord.ui.button(label="📎 Добавить доказательство", style=discord.ButtonStyle.secondary)
    async def add_evidence(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddEvidenceModal(self.proof_message))


# ─── PunishmentLayoutView (persistent) ───────────────────────────────────────

def _wire_callbacks(view: discord.ui.LayoutView) -> None:
    """Set manage/evidence callbacks on buttons inside a LayoutView."""
    for item in view.walk_children():
        cid = getattr(item, 'custom_id', None)
        if cid == "punishment:manage":
            item.callback = _manage_callback
        elif cid == "punishment:evidence":
            item.callback = _evidence_callback


async def _manage_callback(interaction: discord.Interaction):
    data = _extract_v2_data(interaction.message)
    punishment = data.get("punishment", "")
    title = data.get("title", "")
    form_type = _form_type_from_title(title) if title else _form_type_from_punishment(punishment)

    if form_type in ("banform", "gbanform"):
        min_level = APPROVE_MIN_RANK.get(form_type, 2)
        if get_member_rank_level(interaction.user) < min_level:
            await interaction.response.send_message(
                f"❌ Требуется минимум: **{RANKS[min_level - 1]}**.", ephemeral=True
            )
            return
        view = FormManagementView(interaction.message, form_type)
    else:
        view = ProofManagementView(interaction.message)

    await interaction.response.send_message(
        "**Управление наказанием** — выберите действие:",
        view=view,
        ephemeral=True,
    )


async def _evidence_callback(interaction: discord.Interaction):
    evidence_url = _extract_evidence_url_from_components(interaction.message.components)
    if evidence_url:
        await interaction.response.send_message(
            f"🔗 **Доказательство:** {evidence_url}", ephemeral=True
        )
    else:
        await interaction.response.send_modal(AddEvidenceModal(interaction.message))


class PunishmentLayoutView(discord.ui.LayoutView):
    """Persistent LayoutView — registered once at startup to handle all punishment forms."""

    def __init__(self):
        super().__init__(timeout=None)

        manage_btn = discord.ui.Button(
            label="Управление наказанием",
            style=discord.ButtonStyle.success,
            custom_id="punishment:manage",
        )
        manage_btn.callback = _manage_callback

        evidence_btn = discord.ui.Button(
            label="Доказательство",
            style=discord.ButtonStyle.secondary,
            custom_id="punishment:evidence",
            emoji="🔗",
        )
        evidence_btn.callback = _evidence_callback

        container = discord.ui.Container(
            discord.ui.Section(
                discord.ui.TextDisplay("## Форма наказания\n**Модератор:** —"),
                accessory=manage_btn,
            ),
            discord.ui.Separator(),
            discord.ui.Section(
                discord.ui.TextDisplay("..."),
                accessory=evidence_btn,
            ),
            accent_color=0x3498DB,
        )
        self.add_item(container)


# ─── Post form ────────────────────────────────────────────────────────────────

async def _post_form(
    interaction: discord.Interaction,
    moderator: discord.Member,
    violator: discord.Member,
    rule_id: str,
    punishment: str,
    form_type: str,
    evidence_url: str = "",
    server_override: str | None = None,
):
    guild  = interaction.guild
    cfg    = interaction.client.cfg
    member = interaction.user

    server = server_override or get_server_for_member(member, cfg)
    is_ban = form_type in ("banform", "gbanform")
    proof_ch = None

    if server:
        proof_ch = (get_banform_channel(guild, cfg, server) if is_ban
                    else get_proof_channel(guild, cfg, server))

    if not proof_ch:
        for g_cfg in cfg.get("guilds", {}).values():
            ch_id = g_cfg.get("banform_channel_id" if is_ban else "proof_channel_id", 0)
            if ch_id:
                proof_ch = interaction.client.get_channel(ch_id)
                if proof_ch:
                    break

    if not proof_ch and server:
        try:
            await ensure_server_channels(guild, server, cfg)
            proof_ch = (get_banform_channel(guild, cfg, server) if is_ban
                        else get_proof_channel(guild, cfg, server))
        except discord.Forbidden:
            pass

    if not proof_ch:
        ch_label = "банов" if is_ban else "наказаний"
        if server:
            await interaction.followup.send(
                f"❌ Канал форм {ch_label} не найден (сервер **{server}**).\n"
                f"Запустите `/manage-category action:create_missing server:{server}`.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "❌ Не удалось определить ваш сервер. Используйте `/assignserver`.",
                ephemeral=True,
            )
        return

    title = _punishment_title(punishment)
    color = _punishment_color(punishment)
    h_text = _build_header_text(title, moderator.id)
    f_text = _build_fields_text(violator.id, rule_id, punishment, form_type)
    avatar_url = str(violator.display_avatar.url) if violator.display_avatar else ""

    layout_view = _make_layout_view(h_text, f_text, evidence_url, color, violator_avatar_url=avatar_url)
    _wire_callbacks(layout_view)

    await proof_ch.send(view=layout_view)

    record_form(interaction.user.id, form_type, "sent")

    form_text = build_form(cfg, violator, rule_id, punishment, evidence_url=evidence_url)
    try:
        await interaction.user.send(f"📝 **Форма для отчёта:**\n```\n{form_text}\n```")
    except discord.Forbidden:
        pass

    await interaction.followup.send(f"✅ Отправлено в {proof_ch.mention}!", ephemeral=True)


# ─── Cog ──────────────────────────────────────────────────────────────────────

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
                for key in ("proof", "banform"):
                    srv_ch_id = srv_data.get(key, 0)
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
                    if msg.id in self._reminded:
                        continue
                    if not _is_pending_v2_message(msg):
                        continue
                    # Only remind for banform/gbanform
                    data = _extract_v2_data(msg)
                    punishment = data.get("punishment", "")
                    if _form_type_from_punishment(punishment) == "proof":
                        continue
                    age_hours = (now - msg.created_at).total_seconds() / 3600
                    if age_hours >= REMINDER_HOURS:
                        self._reminded.add(msg.id)
                        try:
                            await ch.send(
                                f"{mention} Форма ожидает одобрения уже **{int(age_hours)}ч**!",
                                reference=msg,
                                mention_author=False,
                            )
                        except discord.NotFound:
                            await ch.send(
                                f"{mention} Форма ожидает одобрения уже **{int(age_hours)}ч**!"
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
        evidence="Скриншот доказательства (файл)",
        evidence_url="Ссылка на доказательство (если нет файла)",
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
        evidence_url: str | None = None,
        server: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if user.id == interaction.user.id:
            await interaction.followup.send("❌ Нельзя выдать наказание самому себе.", ephemeral=True)
            return

        ev_url = evidence.url if evidence else (evidence_url or "")
        await _post_form(interaction, interaction.user, user, rule, punishment, "proof",
                         evidence_url=ev_url, server_override=server)

    @app_commands.command(name="banform", description="Сгенерировать форму бана")
    @app_commands.describe(
        user="Нарушитель",
        rule="Пункт правил",
        punishment="Срок бана",
        evidence="Скриншот (файл)",
        evidence_url="Ссылка на доказательство (если нет файла)",
    )
    @app_commands.choices(punishment=[
        app_commands.Choice(name="Бан 7 дней",  value="Бан 7 дней"),
        app_commands.Choice(name="Бан 15 дней", value="Бан 15 дней"),
    ])
    @app_commands.autocomplete(rule=rule_autocomplete)
    async def banform_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        punishment: str = "Бан 7 дней",
        evidence: discord.Attachment | None = None,
        evidence_url: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if user.id == interaction.user.id:
            await interaction.followup.send("❌ Нельзя выдать наказание самому себе.", ephemeral=True)
            return

        ev_url = evidence.url if evidence else (evidence_url or "")
        await _post_form(interaction, interaction.user, user, rule, punishment, "banform",
                         evidence_url=ev_url)

    @app_commands.command(name="gbanform", description="Сгенерировать форму глобального бана")
    @app_commands.describe(
        user="Нарушитель",
        rule="Пункт правил",
        evidence="Скриншот (файл)",
        evidence_url="Ссылка на доказательство (если нет файла)",
    )
    @app_commands.autocomplete(rule=rule_autocomplete)
    async def gbanform_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        evidence: discord.Attachment | None = None,
        evidence_url: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if user.id == interaction.user.id:
            await interaction.followup.send("❌ Нельзя выдать наказание самому себе.", ephemeral=True)
            return

        ev_url = evidence.url if evidence else (evidence_url or "")
        await _post_form(interaction, interaction.user, user, rule, "Глобальная блокировка", "gbanform",
                         evidence_url=ev_url)


async def setup(bot: commands.Bot):
    await bot.add_cog(ProofCog(bot))
    bot.add_view(PunishmentLayoutView())
