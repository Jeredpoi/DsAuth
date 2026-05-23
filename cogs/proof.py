import re
from collections import OrderedDict
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from helpers import (
    RULES, PUNISHMENTS, build_form, fmt_date, date_end,
    get_guild_cfg, get_member_rank_level, build_command,
    APPROVE_MIN_RANK, RANKS,
)
from cogs.servers import get_proof_channel, get_banform_channel, get_log_channel, ensure_server_channels, is_server_role
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
    from cogs.servers import is_server_role
    # interaction.user is discord.User in autocomplete — need Member for roles
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    if member:
        member_servers = [r.name for r in member.roles if is_server_role(r.name)]
    else:
        member_servers = []
    if not member_servers:
        from db import get_user_server
        db_s = get_user_server(interaction.user.id)
        if db_s:
            member_servers = [db_s]
    return [
        app_commands.Choice(name=f"Сервер {s}", value=s)
        for s in member_servers
        if current in s
    ][:25]


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
    violator_name: str = "",
) -> str:
    now_ts = int(datetime.now().timestamp())
    end_info = _end_timestamp(punishment)

    violator_line = (
        f"**Нарушитель:** {violator_name} // {user_id}"
        if violator_name else
        f"**Нарушитель:** {user_id}"
    )
    lines = [
        violator_line,
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
    evidence_urls: list[str],
    color: int,
    violator_avatar_url: str = "",
    form_type: str = "proof",
    done: bool = False,
) -> discord.ui.LayoutView:
    """
    Build a V2 LayoutView for a punishment form.

    Layout (banform/gbanform, not done):
      Section(header, Thumbnail(avatar))
      Separator
      Section(fields, approve_btn)
      Section("Отклонить", reject_btn)
      Separator
      Section("⚙️ Управление · только автор", manage_btn)
      Section("🔗 Доказательства", evidence_btn)
      [MediaGallery if evidence_urls]

    Layout (proof or done state):
      Section(header, Thumbnail(avatar))
      Separator
      Section(fields, manage_btn)   [proof / not done]
      Section("🔗 Доказательства", evidence_btn)
      [MediaGallery if evidence_urls]
    """
    is_banform = form_type in ("banform", "gbanform")

    manage_btn = discord.ui.Button(
        label="⚙️ Управление",
        style=discord.ButtonStyle.secondary,
        custom_id="punishment:manage",
        disabled=done,
    )
    evidence_btn = discord.ui.Button(
        label="Доказательства",
        style=discord.ButtonStyle.secondary,
        custom_id="punishment:evidence",
        emoji="🔗",
        disabled=done,
    )
    approve_btn = discord.ui.Button(
        label="✅ Одобрить",
        style=discord.ButtonStyle.success,
        custom_id="punishment:approve",
        disabled=done,
    )
    reject_btn = discord.ui.Button(
        label="❌ Отклонить",
        style=discord.ButtonStyle.danger,
        custom_id="punishment:reject",
        disabled=done,
    )

    container_items: list = []

    # Header: title + moderator (no thumbnail here — avatar shown below with a clear label)
    container_items.append(discord.ui.TextDisplay(header_text))
    container_items.append(discord.ui.Separator())

    # Violator avatar — labeled explicitly so it's not confused with the moderator's avatar
    if violator_avatar_url:
        container_items.append(
            discord.ui.Section(
                discord.ui.TextDisplay("-# 👤 Нарушитель"),
                accessory=discord.ui.Thumbnail(violator_avatar_url),
            )
        )

    if is_banform and not done:
        # Fields with Approve on the right; Reject below
        container_items.append(
            discord.ui.Section(
                discord.ui.TextDisplay(fields_text),
                accessory=approve_btn,
            )
        )
        container_items.append(
            discord.ui.Section(
                discord.ui.TextDisplay("-# Отклонить форму"),
                accessory=reject_btn,
            )
        )
        container_items.append(discord.ui.Separator())
        container_items.append(
            discord.ui.Section(
                discord.ui.TextDisplay("-# Управление · только автор формы"),
                accessory=manage_btn,
            )
        )
    else:
        # Proof or done state: fields with manage on the right
        container_items.append(
            discord.ui.Section(
                discord.ui.TextDisplay(fields_text),
                accessory=manage_btn,
            )
        )

    container_items.append(
        discord.ui.Section(
            discord.ui.TextDisplay(
                f"🔗 **{len(evidence_urls)}** доказательств прикреплено"
                if evidence_urls else "🔗 Доказательства не прикреплены"
            ),
            accessory=evidence_btn,
        )
    )

    if evidence_urls:
        gallery_items = [discord.MediaGalleryItem(u) for u in evidence_urls[:4]]
        container_items.append(discord.ui.MediaGallery(*gallery_items))

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*container_items, accent_color=color))
    return view


# ─── Extract data from V2 message ─────────────────────────────────────────────

def _collect_text_from_components(comps) -> str:
    texts = []
    for comp in comps:
        if hasattr(comp, 'content'):
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

    m = re.search(r'\*\*Нарушитель:\*\*.*?(\d{15,})', all_text)
    if m: data["user_id"] = int(m.group(1))

    m = re.search(r'\*\*Причина наказания:\*\* (.+)', all_text)
    if m: data["rule_id"] = m.group(1).strip()

    m = re.search(r'\*\*Наказание:\*\* (.+)', all_text)
    if m: data["punishment"] = m.group(1).strip()

    m = re.search(r'## (.+)', all_text)
    if m: data["title"] = m.group(1).strip()

    return data


def _extract_evidence_urls(comps) -> list[str]:
    """Return all evidence URLs from MediaGallery components (duck-typed)."""
    urls = []
    for comp in comps:
        # Duck-type MediaGallery: has .items (list of MediaGalleryItem with .media).
        # Section/Container have .children, not .items — so this is unambiguous.
        items_attr = getattr(comp, 'items', None)
        if items_attr is not None:
            for item in items_attr:
                media = getattr(item, 'media', None)
                if media:
                    url = getattr(media, 'url', None) or getattr(media, 'proxy_url', None)
                    if url and url.startswith('http'):
                        urls.append(url)
        elif hasattr(comp, 'children') and comp.children:
            urls.extend(_extract_evidence_urls(comp.children))
    return urls


def _extract_avatar_url(comps) -> str:
    """Return the Thumbnail URL from a Section's accessory (duck-typing, no type magic)."""
    for comp in comps:
        # Section accessory is either Button (has custom_id) or Thumbnail (has media, no custom_id)
        acc = getattr(comp, 'accessory', None)
        if acc is not None and getattr(acc, 'custom_id', None) is None:
            media = getattr(acc, 'media', None)
            if media:
                url = getattr(media, 'url', None) or getattr(media, 'proxy_url', None)
                if url and url.startswith('http'):
                    return url
        if hasattr(comp, 'children') and comp.children:
            url = _extract_avatar_url(comp.children)
            if url:
                return url
    return ""


def _is_pending_v2_message(message: discord.Message) -> bool:
    def walk(comps):
        for comp in comps:
            cid = getattr(comp, 'custom_id', None)
            if cid in ("punishment:manage", "punishment:approve") and not getattr(comp, 'disabled', False):
                return True
            if hasattr(comp, 'children') and comp.children and walk(comp.children):
                return True
            acc = getattr(comp, 'accessory', None)
            if acc:
                cid_acc = getattr(acc, 'custom_id', None)
                if cid_acc in ("punishment:manage", "punishment:approve") and not getattr(acc, 'disabled', False):
                    return True
        return False
    return walk(message.components)


# ─── Rebuild helper ───────────────────────────────────────────────────────────

def _extract_fields_text(all_text: str) -> str:
    """Extract the fields block (Нарушитель … last field) preserving original timestamps."""
    m = re.search(r'(\*\*Нарушитель:\*\*.+?)(?=\n-# |\n🔗 |\Z)', all_text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _rebuild_view_from_message(
    message: discord.Message,
    color: int,
    done: bool = False,
    status_line: str | None = None,
) -> discord.ui.LayoutView:
    """Reconstruct a V2 layout view from an existing message, preserving original timestamps."""
    data     = _extract_v2_data(message)
    all_text = _collect_text_from_components(message.components)

    title      = data.get("title", "Наказание пользователя")
    mod_id     = data.get("mod_id", 0)
    punishment = data.get("punishment", "")
    form_type  = _form_type_from_title(title) if title else _form_type_from_punishment(punishment)

    h_text = _build_header_text(title, mod_id)

    # Use original fields text (preserves timestamps); fall back to fresh rebuild only if missing
    f_text = _extract_fields_text(all_text) or _build_fields_text(
        data.get("user_id", 0), data.get("rule_id", ""), punishment, form_type
    )

    if status_line:
        pending_line = next((l for l in f_text.split("\n") if "⏳" in l), None)
        if pending_line:
            f_text = f_text.replace(pending_line, status_line)
        else:
            f_text += f"\n{status_line}"

    avatar_url    = _extract_avatar_url(message.components)
    evidence_urls = _extract_evidence_urls(message.components)

    view = _make_layout_view(
        h_text, f_text, evidence_urls, color,
        violator_avatar_url=avatar_url,
        form_type=form_type,
        done=done,
    )
    _wire_callbacks(view)
    return view


# ─── Log embed ────────────────────────────────────────────────────────────────

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


def _server_for_mod_id(guild: discord.Guild | None, mod_id: int) -> str | None:
    """Return the server number for a moderator by member roles, fall back to DB."""
    if guild:
        member = guild.get_member(mod_id)
        if member:
            roles = [r.name for r in member.roles if is_server_role(r.name)]
            if len(roles) == 1:
                return roles[0]
    from db import get_user_server
    return get_user_server(mod_id)


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
        label="Ссылка на доказательство (или несколько через пробел)",
        placeholder="https://cdn.discordapp.com/attachments/...",
        style=discord.TextStyle.paragraph,
        max_length=800,
        required=True,
    )

    def __init__(self, proof_message: discord.Message):
        super().__init__()
        self.proof_message = proof_message

    async def on_submit(self, interaction: discord.Interaction):
        new_urls = [u.strip() for u in self.url_input.value.split() if u.strip().startswith("http")]
        if not new_urls:
            await interaction.response.send_message("❌ Укажите корректную ссылку (http...).", ephemeral=True)
            return

        existing = _extract_evidence_urls(self.proof_message.components)
        combined = list(dict.fromkeys(existing + new_urls))[:4]  # deduplicate, max 4

        data      = _extract_v2_data(self.proof_message)
        all_text  = _collect_text_from_components(self.proof_message.components)
        title     = data.get("title", "Наказание пользователя")
        mod_id    = data.get("mod_id", 0)
        punishment = data.get("punishment", "")
        form_type  = _form_type_from_title(title) if title else _form_type_from_punishment(punishment)

        h_text     = _build_header_text(title, mod_id)
        f_text     = _extract_fields_text(all_text) or _build_fields_text(
            data.get("user_id", 0), data.get("rule_id", ""), punishment, form_type
        )
        avatar_url = _extract_avatar_url(self.proof_message.components)
        color      = _punishment_color(punishment)

        new_view = _make_layout_view(
            h_text, f_text, combined, color,
            violator_avatar_url=avatar_url, form_type=form_type,
        )
        _wire_callbacks(new_view)

        await self.proof_message.edit(view=new_view)
        await interaction.response.send_message(
            f"✅ Доказательства добавлены ({len(combined)} шт.).", ephemeral=True
        )


FORM_DELETE_AUTHOR_WINDOW = 300   # seconds author can delete their own form
FORM_LEADER_MIN_LEVEL     = 4     # КМ+ can delete any form at any time


async def _log_form_deletion(
    interaction: discord.Interaction,
    data: dict,
    by_leader: bool,
) -> None:
    title     = data.get("title", "Наказание")
    user_id   = data.get("user_id", 0)
    punishment = data.get("punishment", "?")
    if not interaction.guild:
        return
    server = _server_for_mod_id(interaction.guild, data.get("mod_id", 0))
    if not server:
        return
    log_ch = get_log_channel(interaction.guild, interaction.client.cfg, server)
    if not log_ch:
        return
    embed = discord.Embed(
        title="🗑️ Форма удалена руководством" if by_leader else "🗑️ Форма удалена автором",
        color=0x95A5A6,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Форма",      value=title,                               inline=False)
    embed.add_field(name="Нарушитель", value=f"<@{user_id}>" if user_id else "?", inline=True)
    embed.add_field(name="Удалил",     value=str(interaction.user),               inline=True)
    embed.add_field(name="Наказание",  value=punishment,                          inline=False)
    try:
        await log_ch.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


# ─── Leader management panel (КМ+, no time restriction) ─────────────────────

class LeaderManageView(discord.ui.View):
    """Shown to leadership (КМ+) for any form — delete without time restriction."""

    def __init__(self, proof_message: discord.Message):
        super().__init__(timeout=60)
        self.proof_message = proof_message

    @discord.ui.button(label="🗑️ Удалить форму", style=discord.ButtonStyle.danger)
    async def delete_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Extract log data before deletion while components are guaranteed accessible
        log_data = _extract_v2_data(self.proof_message)
        try:
            await self.proof_message.delete()
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message("❌ Не удалось удалить форму.", ephemeral=True)
            return
        await interaction.response.send_message("🗑️ Форма удалена.", ephemeral=True)
        await _log_form_deletion(interaction, log_data, by_leader=True)


# ─── Owner management panel ───────────────────────────────────────────────────

class OwnerManageView(discord.ui.View):
    """Ephemeral panel shown to the form's author."""

    def __init__(self, proof_message: discord.Message):
        super().__init__(timeout=120)
        self.proof_message = proof_message

    @discord.ui.button(label="📎 Добавить доказательство", style=discord.ButtonStyle.secondary)
    async def add_evidence(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddEvidenceModal(self.proof_message))

    @discord.ui.button(label="🗑️ Удалить форму", style=discord.ButtonStyle.danger)
    async def delete_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_leader = get_member_rank_level(interaction.user) >= FORM_LEADER_MIN_LEVEL
        age_sec = (discord.utils.utcnow() - self.proof_message.created_at).total_seconds()
        if age_sec > FORM_DELETE_AUTHOR_WINDOW and not is_leader:
            await interaction.response.send_message(
                "❌ Форму можно удалить только в первые **5 минут** после отправки.\n"
                "Для удаления обратитесь к руководству (КМ+).",
                ephemeral=True,
            )
            return
        # Extract log data before deletion
        log_data = _extract_v2_data(self.proof_message)
        try:
            await self.proof_message.delete()
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message("❌ Не удалось удалить форму.", ephemeral=True)
            return
        await interaction.response.send_message("🗑️ Форма удалена.", ephemeral=True)
        await _log_form_deletion(interaction, log_data, by_leader=is_leader)


# ─── Button callbacks ─────────────────────────────────────────────────────────

def _wire_callbacks(view: discord.ui.LayoutView) -> None:
    for item in view.walk_children():
        cid = getattr(item, 'custom_id', None)
        if cid == "punishment:manage":
            item.callback = _manage_callback
        elif cid == "punishment:evidence":
            item.callback = _evidence_callback
        elif cid == "punishment:approve":
            item.callback = _approve_callback
        elif cid == "punishment:reject":
            item.callback = _reject_callback


async def _manage_callback(interaction: discord.Interaction):
    data   = _extract_v2_data(interaction.message)
    mod_id = data.get("mod_id", 0)
    is_author = interaction.user.id == mod_id
    is_leader = get_member_rank_level(interaction.user) >= FORM_LEADER_MIN_LEVEL

    if not is_author and not is_leader:
        await interaction.response.send_message(
            "❌ Управление доступно только **автору** формы или руководству (КМ+).", ephemeral=True
        )
        return

    # Authors get OwnerManageView (add evidence + delete); leaders who aren't the
    # author get LeaderManageView (delete without the 5-min window).
    # When the author is also a leader, OwnerManageView.delete_form bypasses the
    # time restriction via the is_leader check below.
    if is_author:
        panel = OwnerManageView(interaction.message)
        label = "**⚙️ Управление формой:**"
    else:
        panel = LeaderManageView(interaction.message)
        label = "**⚙️ Управление формой (руководство):**"

    await interaction.response.send_message(label, view=panel, ephemeral=True)


async def _evidence_callback(interaction: discord.Interaction):
    try:
        evidence_urls = _extract_evidence_urls(interaction.message.components)
        data          = _extract_v2_data(interaction.message)
        mod_id        = data.get("mod_id", 0)
        is_author     = interaction.user.id == mod_id

        if evidence_urls:
            lines = [f"🔗 **Доказательства ({len(evidence_urls)}):**"]
            for i, url in enumerate(evidence_urls, 1):
                lines.append(f"{i}. {url}")
            if is_author:
                lines.append("\n*(Добавить ещё — через кнопку ⚙️ Управление)*")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
        elif is_author:
            await interaction.response.send_modal(AddEvidenceModal(interaction.message))
        else:
            await interaction.response.send_message(
                "🔗 Доказательства не прикреплены.", ephemeral=True
            )
    except Exception:
        import traceback
        traceback.print_exc()
        try:
            await interaction.response.send_message(
                "❌ Не удалось загрузить доказательства.", ephemeral=True
            )
        except discord.InteractionResponded:
            pass


async def _approve_callback(interaction: discord.Interaction):
    data = _extract_v2_data(interaction.message)
    punishment = data.get("punishment", "")
    title = data.get("title", "")
    form_type = _form_type_from_title(title) if title else _form_type_from_punishment(punishment)

    min_level = APPROVE_MIN_RANK.get(form_type, 2)
    if get_member_rank_level(interaction.user) < min_level:
        await interaction.response.send_message(
            f"❌ Требуется минимум: **{RANKS[min_level - 1]}**.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    user_id  = data.get("user_id", 0)
    rule_id  = data.get("rule_id", "")
    mod_id   = data.get("mod_id", 0)

    command = build_command(punishment, user_id, rule_id)
    rank_display = next(
        (r.name for r in reversed(getattr(interaction.user, "roles", [])) if r.name in RANKS), "—"
    )
    status = f"✅ Одобрено: {interaction.user} ({rank_display})"
    if command:
        status += f"\n💻 `{command}`"

    done_view = _rebuild_view_from_message(interaction.message, 0x2ECC71, done=True, status_line=status)
    try:
        await interaction.message.edit(view=done_view)
    except (discord.Forbidden, discord.HTTPException) as e:
        await interaction.followup.send(f"❌ Не удалось обновить форму: {e}", ephemeral=True)
        return
    record_form(mod_id, form_type, "approved")

    server = _server_for_mod_id(interaction.guild, mod_id)
    if server:
        log_embed = _build_log_embed(title or "Наказание", mod_id, user_id, rule_id, punishment, 0x2ECC71, status)
        await _post_to_log(interaction.guild, interaction.client.cfg, server, log_embed)

    await interaction.followup.send("✅ Форма одобрена.", ephemeral=True)


async def _reject_callback(interaction: discord.Interaction):
    data = _extract_v2_data(interaction.message)
    punishment = data.get("punishment", "")
    title = data.get("title", "")
    form_type = _form_type_from_title(title) if title else _form_type_from_punishment(punishment)

    min_level = APPROVE_MIN_RANK.get(form_type, 2)
    if get_member_rank_level(interaction.user) < min_level:
        await interaction.response.send_message(
            f"❌ Требуется минимум: **{RANKS[min_level - 1]}**.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    mod_id  = data.get("mod_id", 0)
    user_id = data.get("user_id", 0)
    rule_id = data.get("rule_id", "")

    rank_display = next(
        (r.name for r in reversed(getattr(interaction.user, "roles", [])) if r.name in RANKS), "—"
    )
    status = f"❌ Отклонено: {interaction.user} ({rank_display})"

    done_view = _rebuild_view_from_message(interaction.message, 0xE74C3C, done=True, status_line=status)
    try:
        await interaction.message.edit(view=done_view)
    except (discord.Forbidden, discord.HTTPException) as e:
        await interaction.followup.send(f"❌ Не удалось обновить форму: {e}", ephemeral=True)
        return
    record_form(mod_id, form_type, "rejected")

    server = _server_for_mod_id(interaction.guild, mod_id)
    if server:
        log_embed = _build_log_embed(title or "Наказание", mod_id, user_id, rule_id, punishment, 0xE74C3C, status)
        await _post_to_log(interaction.guild, interaction.client.cfg, server, log_embed)

    await interaction.followup.send("❌ Форма отклонена.", ephemeral=True)


# ─── PunishmentLayoutView (persistent registration) ──────────────────────────

class PunishmentLayoutView(discord.ui.LayoutView):
    """Registered once at startup so discord.py can dispatch button interactions."""

    def __init__(self):
        super().__init__(timeout=None)

        btns = [
            discord.ui.Button(label="⚙️ Управление",    style=discord.ButtonStyle.secondary, custom_id="punishment:manage"),
            discord.ui.Button(label="Доказательства",   style=discord.ButtonStyle.secondary, custom_id="punishment:evidence", emoji="🔗"),
            discord.ui.Button(label="✅ Одобрить",       style=discord.ButtonStyle.success,   custom_id="punishment:approve"),
            discord.ui.Button(label="❌ Отклонить",      style=discord.ButtonStyle.danger,    custom_id="punishment:reject"),
        ]
        callbacks = [_manage_callback, _evidence_callback, _approve_callback, _reject_callback]
        for btn, cb in zip(btns, callbacks):
            btn.callback = cb

        # Wrap in a minimal container so walk_children() works
        container = discord.ui.Container(
            discord.ui.Section(
                discord.ui.TextDisplay("## Форма наказания\n**Модератор:** —"),
                accessory=btns[0],
            ),
            discord.ui.Separator(),
            discord.ui.Section(
                discord.ui.TextDisplay("..."),
                accessory=btns[1],
            ),
            discord.ui.Separator(),
            discord.ui.Section(
                discord.ui.TextDisplay("-# Одобрить"),
                accessory=btns[2],
            ),
            discord.ui.Section(
                discord.ui.TextDisplay("-# Отклонить"),
                accessory=btns[3],
            ),
            accent_color=0x3498DB,
        )
        self.add_item(container)


# ─── Post form ────────────────────────────────────────────────────────────────

def _resolve_server(member: discord.Member, cfg: dict, server_override: str | None) -> tuple[str | None, str | None]:
    """
    Return (server, error_message).
    Priority:
      1. Explicit server_override (from command param)
      2. Member's numeric server roles (1–90)
         - Exactly one  → use it
         - Multiple     → return error asking to specify
      3. DB fallback (set_user_server) — only if no role at all
    """
    member_roles = [r.name for r in getattr(member, "roles", [])]
    print(f"[_resolve_server] member={getattr(member,'id',member)} override={server_override!r} roles={member_roles}", flush=True)

    if server_override:
        if not (server_override.isdigit() and 1 <= int(server_override) <= 90):
            return None, f"❌ Некорректный номер сервера: **{server_override}** (должно быть 1–90)."
        has_role = any(r.name == server_override and is_server_role(r.name) for r in getattr(member, "roles", []))
        if not has_role:
            return None, (
                f"❌ У вас нет роли сервера **{server_override}**.\n"
                "Нельзя отправлять формы на чужой сервер."
            )
        return server_override, None

    from cogs.servers import is_server_role
    server_roles = [r.name for r in getattr(member, "roles", []) if is_server_role(r.name)]

    if len(server_roles) == 1:
        return server_roles[0], None

    if len(server_roles) > 1:
        role_list = ", ".join(f"**{s}**" for s in sorted(server_roles, key=int))
        return None, (
            f"❌ У вас несколько ролей серверов: {role_list}.\n"
            "Укажите нужный через параметр `server`, например: `/proof server:49 ...`"
        )

    # No server role — try DB
    member_id = getattr(member, "id", None)
    if member_id:
        from db import get_user_server
        db_server = get_user_server(member_id)
        if db_server:
            return db_server, None

    return None, (
        "❌ Не удалось определить ваш сервер.\n"
        "У вас нет роли сервера (1–90). Попросите администратора выполнить `/assignserver`."
    )


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
    guild = interaction.guild
    cfg   = interaction.client.cfg

    # Ensure we have a Member (with .roles); fall back to interaction.user if cache misses
    actor = (guild.get_member(interaction.user.id) if guild else None) or interaction.user
    server, err = _resolve_server(actor, cfg, server_override)
    if err:
        await interaction.followup.send(err, ephemeral=True)
        return

    is_ban = form_type in ("banform", "gbanform")

    proof_ch = (get_banform_channel(guild, cfg, server) if is_ban
                else get_proof_channel(guild, cfg, server))

    if not proof_ch:
        # Auto-create channels for this server on first use
        try:
            await ensure_server_channels(guild, server, cfg)
            proof_ch = (get_banform_channel(guild, cfg, server) if is_ban
                        else get_proof_channel(guild, cfg, server))
        except discord.Forbidden:
            pass

    if not proof_ch:
        ch_label = "банов" if is_ban else "наказаний"
        await interaction.followup.send(
            f"❌ Канал форм {ch_label} не найден (сервер **{server}**).\n"
            f"Попросите администратора запустить `/setupserver {server}`.",
            ephemeral=True,
        )
        return

    title      = _punishment_title(punishment)
    color      = _punishment_color(punishment)
    h_text     = _build_header_text(title, moderator.id)
    # Support multiple space-separated URLs in evidence_url
    ev_urls    = [u.strip() for u in evidence_url.split() if u.strip().startswith("http")] if evidence_url else []
    f_text     = _build_fields_text(violator.id, rule_id, punishment, form_type,
                                     violator_name=violator.name)
    avatar_url = str(violator.display_avatar.url) if violator.display_avatar else ""

    layout_view = _make_layout_view(
        h_text, f_text, ev_urls, color,
        violator_avatar_url=avatar_url, form_type=form_type,
    )
    _wire_callbacks(layout_view)

    try:
        await proof_ch.send(view=layout_view)
    except (discord.Forbidden, discord.HTTPException) as e:
        await interaction.followup.send(
            f"❌ Не удалось отправить форму в {proof_ch.mention}: {e}", ephemeral=True
        )
        return
    record_form(interaction.user.id, form_type, "sent")

    evidence_for_form = " ".join(ev_urls) if ev_urls else ""
    form_text = build_form(cfg, violator, rule_id, punishment, evidence_url=evidence_for_form)
    try:
        await interaction.user.send(f"📝 **Форма для отчёта:**\n```\n{form_text}\n```")
    except (discord.Forbidden, discord.HTTPException):
        pass

    await interaction.followup.send(
        f"✅ Отправлено в {proof_ch.mention} (сервер **{server}**)!", ephemeral=True
    )


# ─── Cog ──────────────────────────────────────────────────────────────────────

class ProofCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._reminded: OrderedDict[int, None] = OrderedDict()
        self.reminder_loop.start()

    def cog_unload(self):
        self.reminder_loop.cancel()

    @tasks.loop(minutes=30)
    async def reminder_loop(self):
        now = discord.utils.utcnow()
        cfg = self.bot.cfg
        seen_ids: set[int] = set()
        channels_to_check: list[discord.TextChannel] = []

        # Resolve channels via category lookup (consistent with form routing) —
        # stale config IDs from old /setup must never be used
        for guild in self.bot.guilds:
            for category in guild.categories:
                if not is_server_role(category.name):
                    continue
                for ch_name in ("⚖️-формы-банов",):  # only banform/gbanform need reminders
                    ch = discord.utils.get(guild.text_channels, name=ch_name, category=category)
                    if ch and ch.id not in seen_ids:
                        channels_to_check.append(ch)
                        seen_ids.add(ch.id)

        # FIFO prune — drop oldest entries first (OrderedDict preserves insertion order)
        while len(self._reminded) > 10_000:
            self._reminded.popitem(last=False)

        cutoff = now - timedelta(days=7)
        for ch in channels_to_check:
            guild_cfg = get_guild_cfg(cfg, ch.guild.id)
            role_id = guild_cfg.get("review_role_id", 0)
            mention = f"<@&{role_id}>" if role_id else ""
            if not mention:
                continue  # skip reminder if no role configured — pinging nobody is useless
            try:
                # No limit — scan all messages in the 7-day window, not just the first 100
                async for msg in ch.history(limit=None, after=cutoff, oldest_first=True):
                    if msg.id in self._reminded:
                        continue
                    if not _is_pending_v2_message(msg):
                        continue
                    data = _extract_v2_data(msg)
                    punishment = data.get("punishment", "")
                    if _form_type_from_punishment(punishment) == "proof":
                        continue
                    age_hours = (now - msg.created_at).total_seconds() / 3600
                    if age_hours >= REMINDER_HOURS:
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
                            continue
                        # Mark as reminded only after successful send
                        self._reminded[msg.id] = None
            except (discord.Forbidden, discord.HTTPException):
                pass

    @reminder_loop.before_loop
    async def before_reminder(self):
        await self.bot.wait_until_ready()

    @app_commands.default_permissions(manage_messages=True)
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

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="banform", description="Сгенерировать форму бана")
    @app_commands.describe(
        user="Нарушитель",
        rule="Пункт правил",
        punishment="Срок бана",
        evidence="Скриншот (файл)",
        evidence_url="Ссылка на доказательство (если нет файла)",
        server="Номер сервера (если у вас несколько ролей серверов)",
    )
    @app_commands.choices(punishment=[
        app_commands.Choice(name="Бан 7 дней",  value="Бан 7 дней"),
        app_commands.Choice(name="Бан 15 дней", value="Бан 15 дней"),
    ])
    @app_commands.autocomplete(rule=rule_autocomplete, server=server_autocomplete)
    async def banform_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        punishment: str = "Бан 7 дней",
        evidence: discord.Attachment | None = None,
        evidence_url: str | None = None,
        server: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if user.id == interaction.user.id:
            await interaction.followup.send("❌ Нельзя выдать наказание самому себе.", ephemeral=True)
            return

        ev_url = evidence.url if evidence else (evidence_url or "")
        await _post_form(interaction, interaction.user, user, rule, punishment, "banform",
                         evidence_url=ev_url, server_override=server)

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="gbanform", description="Сгенерировать форму глобального бана")
    @app_commands.describe(
        user="Нарушитель",
        rule="Пункт правил",
        evidence="Скриншот (файл)",
        evidence_url="Ссылка на доказательство (если нет файла)",
        server="Номер сервера (если у вас несколько ролей серверов)",
    )
    @app_commands.autocomplete(rule=rule_autocomplete, server=server_autocomplete)
    async def gbanform_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        evidence: discord.Attachment | None = None,
        evidence_url: str | None = None,
        server: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if user.id == interaction.user.id:
            await interaction.followup.send("❌ Нельзя выдать наказание самому себе.", ephemeral=True)
            return

        ev_url = evidence.url if evidence else (evidence_url or "")
        await _post_form(interaction, interaction.user, user, rule, "Глобальная блокировка", "gbanform",
                         evidence_url=ev_url, server_override=server)


async def setup(bot: commands.Bot):
    await bot.add_cog(ProofCog(bot))
    bot.add_view(PunishmentLayoutView())
