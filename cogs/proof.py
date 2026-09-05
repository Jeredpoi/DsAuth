import asyncio
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
from cogs.servers import get_proof_channel, get_banform_channel, get_log_channel, ensure_server_channels, is_server_role, server_for_channel
from db import record_form, all_user_servers

REMINDER_HOURS = 2

# Discord MediaGallery вмещает до 10 элементов
MAX_EVIDENCE = 10

# Сколько сообщений разбираем в одном канале за проход и сколько ждём между
# каналами — чтобы фоновое сканирование не занимало очередь рейт-лимита,
# в которой стоят ответы на нажатия кнопок
SCAN_LIMIT = 200
SCAN_DELAY = 1.0


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
    if "15 дней" in p and "7-15" not in p:
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
        min_rank = RANKS[APPROVE_MIN_RANK.get(form_type, 2) - 1]
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
        style=discord.ButtonStyle.success,
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

    # Header with violator avatar on the right
    if violator_avatar_url:
        container_items.append(
            discord.ui.Section(
                discord.ui.TextDisplay(header_text),
                accessory=discord.ui.Thumbnail(violator_avatar_url),
            )
        )
    else:
        container_items.append(discord.ui.TextDisplay(header_text))
    container_items.append(discord.ui.Separator())

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
        gallery_items = [discord.MediaGalleryItem(u) for u in evidence_urls[:MAX_EVIDENCE]]
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
    return view


# ─── Log view ─────────────────────────────────────────────────────────────────

def _build_log_view(
    title: str,
    mod_id: int,
    user_id: int,
    rule_id: str,
    punishment: str,
    color: int,
    status_text: str = "",
) -> discord.ui.LayoutView:
    items: list = [
        discord.ui.TextDisplay(f"## {title}"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            f"**Модератор:** <@{mod_id}>\n"
            f"**Нарушитель:** {user_id}\n"
            f"**Причина наказания:** {rule_id}\n"
            f"**Наказание:** {punishment}"
        ),
    ]
    if status_text:
        items.append(discord.ui.Separator())
        items.append(discord.ui.TextDisplay(f"-# {status_text}"))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_color=color))
    return view


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


async def _post_to_log(guild: discord.Guild, cfg: dict, server: str, view: discord.ui.LayoutView):
    log_ch = get_log_channel(guild, cfg, server)
    if not log_ch:
        return
    try:
        await log_ch.send(view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


# ─── Manage modals ────────────────────────────────────────────────────────────

def _split_urls(raw: str) -> list[str]:
    """Split user input into http(s) URLs (whitespace and commas as separators)."""
    parts = re.split(r"[\s,]+", raw)
    return [u.strip() for u in parts if u.strip().startswith("http")]


def _message_has_approve(message: discord.Message) -> bool:
    """True if the form message currently has an approve button (i.e. is a banform layout)."""
    def walk(comps):
        for comp in comps:
            if getattr(comp, 'custom_id', None) == "punishment:approve":
                return True
            acc = getattr(comp, 'accessory', None)
            if acc is not None and getattr(acc, 'custom_id', None) == "punishment:approve":
                return True
            if hasattr(comp, 'children') and comp.children and walk(comp.children):
                return True
        return False
    return walk(message.components)


def _rebuild_form_view(
    proof_message: discord.Message,
    evidence_urls: list[str],
    rule_id: str | None = None,
    punishment: str | None = None,
    force_proof_layout: bool = False,
) -> discord.ui.LayoutView:
    """Rebuild the form view from the message, optionally overriding rule/punishment.

    force_proof_layout=True → render as proof (manage button only, no approve/reject)
    even if the punishment type would normally produce a banform.

    When punishment is NOT being changed, the existing layout is preserved:
    a /proof form with a ban-type punishment (e.g. ГМ already issued the gban)
    must NOT grow approve/reject buttons just because evidence was added.
    """
    data      = _extract_v2_data(proof_message)
    all_text  = _collect_text_from_components(proof_message.components)
    old_punishment = data.get("punishment", "")
    new_punishment = punishment if punishment is not None else old_punishment

    if punishment is not None:
        title = _punishment_title(new_punishment)
        form_type = _form_type_from_punishment(new_punishment)
    else:
        title = data.get("title", "Наказание пользователя")
        form_type = _form_type_from_title(title) if title else _form_type_from_punishment(old_punishment)

    if force_proof_layout:
        effective_form_type = "proof"
    elif punishment is None:
        # Keep whatever layout the message already has
        effective_form_type = form_type if _message_has_approve(proof_message) else "proof"
    else:
        effective_form_type = form_type

    mod_id = data.get("mod_id", 0)
    h_text = _build_header_text(title, mod_id)

    f_text = _extract_fields_text(all_text) or _build_fields_text(
        data.get("user_id", 0), data.get("rule_id", ""), old_punishment, effective_form_type
    )

    if rule_id is not None:
        f_text = re.sub(r"\*\*Причина наказания:\*\* .+", f"**Причина наказания:** {rule_id}", f_text)
    if punishment is not None:
        f_text = re.sub(r"\*\*Наказание:\*\* .+", f"**Наказание:** {new_punishment}", f_text)
        # Remove old approval-wait line — it may no longer apply
        f_text = "\n".join(l for l in f_text.split("\n") if "⏳" not in l)
        # Recompute the removal line for the new punishment
        f_text = "\n".join(l for l in f_text.split("\n") if not l.startswith("**Снятие:**"))
        end_info = _end_timestamp(new_punishment)
        if end_info:
            ts, fmt = end_info
            lines = f_text.split("\n")
            for i, l in enumerate(lines):
                if l.startswith("**Время:**"):
                    lines.insert(i + 1, f"**Снятие:** <t:{ts}:{fmt}>")
                    break
            f_text = "\n".join(lines)
        # Re-add the approval-wait line if the effective form type still requires it
        if effective_form_type in ("banform", "gbanform"):
            min_rank = RANKS[APPROVE_MIN_RANK.get(effective_form_type, 2) - 1]
            f_text += f"\n⏳ Ожидает одобрения: {min_rank}+"

    avatar_url = _extract_avatar_url(proof_message.components)
    color      = _punishment_color(new_punishment)

    view = _make_layout_view(
        h_text, f_text, evidence_urls, color,
        violator_avatar_url=avatar_url, form_type=effective_form_type,
    )
    return view


_MEDIA_LINK_HINT = (
    "-# Ссылка должна вести **напрямую на файл** (заканчиваться на .png/.jpg/.mp4 и т.п.).\n"
    "-# Как получить: загрузи фото в любой канал Discord → ПКМ на фото → "
    "**Копировать ссылку на медиа** → вставь сюда."
)


class AddEvidenceModal(discord.ui.Modal, title="Добавить доказательство"):
    url_input = discord.ui.TextInput(
        label="Ссылка(и) на доказательство",
        placeholder="https://... (фото: загрузи в Discord → ПКМ на фото → Копировать ссылку)",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    def __init__(self, proof_message: discord.Message):
        super().__init__()
        self.proof_message = proof_message

    async def on_submit(self, interaction: discord.Interaction):
        new_urls = _split_urls(self.url_input.value)
        if not new_urls:
            await interaction.response.send_message(
                f"❌ Ссылка не найдена.\n{_MEDIA_LINK_HINT}", ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        existing = _extract_evidence_urls(self.proof_message.components)
        merged   = list(dict.fromkeys(existing + new_urls))
        capped   = len(merged) > MAX_EVIDENCE
        combined = merged[:MAX_EVIDENCE]

        new_view = _rebuild_form_view(self.proof_message, combined)
        try:
            await self.proof_message.edit(view=new_view)
        except discord.HTTPException:
            # Discord rejects MediaGallery items that are not direct media links
            await interaction.followup.send(
                f"❌ Discord отклонил ссылку — это не прямая ссылка на медиафайл.\n{_MEDIA_LINK_HINT}",
                ephemeral=True,
            )
            return
        note = f" (максимум {MAX_EVIDENCE}, лишние отброшены)" if capped else ""
        await interaction.followup.send(
            f"✅ Доказательства добавлены ({len(combined)} шт.){note}.", ephemeral=True
        )


class RemoveEvidenceModal(discord.ui.Modal, title="Убрать доказательство"):
    num_input = discord.ui.TextInput(
        label="Номер(а) для удаления или «все»",
        placeholder="Например: 2  или  1 3  или  все",
        max_length=20,
        required=True,
    )

    def __init__(self, proof_message: discord.Message):
        super().__init__()
        self.proof_message = proof_message

    async def on_submit(self, interaction: discord.Interaction):
        existing = _extract_evidence_urls(self.proof_message.components)
        if not existing:
            await interaction.response.send_message("🔗 Доказательств нет.", ephemeral=True)
            return

        raw = self.num_input.value.strip().lower()
        if raw in ("все", "всё", "all", "*"):
            remaining = []
        else:
            nums = {int(n) for n in re.findall(r"\d+", raw)}
            if not nums:
                await interaction.response.send_message(
                    f"❌ Укажите номер доказательства (1–{MAX_EVIDENCE}) или «все».", ephemeral=True
                )
                return
            remaining = [u for i, u in enumerate(existing, 1) if i not in nums]
            if len(remaining) == len(existing):
                await interaction.response.send_message(
                    f"❌ Нет доказательств с такими номерами (всего: {len(existing)}).", ephemeral=True
                )
                return

        await interaction.response.defer(ephemeral=True)
        new_view = _rebuild_form_view(self.proof_message, remaining)
        try:
            await self.proof_message.edit(view=new_view)
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Не удалось обновить форму: {e}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Удалено {len(existing) - len(remaining)} шт. Осталось: {len(remaining)}.", ephemeral=True
        )


_PUNISHMENT_HINT = (
    "Устное предупреждение / Предупреждение / Мут 90 минут"
    " / Бан 7-15 дней / Перманентная блокировка / Глобальная блокировка"
)


class EditFormModal(discord.ui.Modal, title="Редактировать форму"):
    def __init__(self, proof_message: discord.Message, data: dict):
        super().__init__()
        self.proof_message = proof_message
        self.rule_input = discord.ui.TextInput(
            label="Пункт правил",
            default=data.get("rule_id", ""),
            placeholder="Например: 2.1",
            max_length=50,
            required=True,
        )
        self.punishment_input = discord.ui.TextInput(
            label="Наказание",
            default=data.get("punishment", ""),
            placeholder=_PUNISHMENT_HINT[:100],
            max_length=100,
            required=True,
        )
        self.add_item(self.rule_input)
        self.add_item(self.punishment_input)

    async def on_submit(self, interaction: discord.Interaction):
        rule_id    = self.rule_input.value.strip()
        punishment = self.punishment_input.value.strip()

        old_data       = _extract_v2_data(self.proof_message)
        old_rule       = old_data.get("rule_id", "")
        old_punishment = old_data.get("punishment", "")

        new_form_type = _form_type_from_punishment(punishment)
        min_level     = APPROVE_MIN_RANK.get(new_form_type, 2)

        member = (
            interaction.guild.get_member(interaction.user.id)
            if interaction.guild else None
        ) or interaction.user
        author_level = get_member_rank_level(member)

        await interaction.response.defer(ephemeral=True)
        evidence = _extract_evidence_urls(self.proof_message.components)

        # If new punishment is proof-type, or author outranks the approval requirement:
        # update the form in-place without approval buttons (they can handle it themselves).
        if new_form_type == "proof" or author_level >= min_level:
            new_view = _rebuild_form_view(
                self.proof_message, evidence,
                rule_id=rule_id, punishment=punishment,
                force_proof_layout=True,
            )
            try:
                await self.proof_message.edit(view=new_view)
            except discord.HTTPException as e:
                await interaction.followup.send(f"❌ Не удалось обновить форму: {e}", ephemeral=True)
                return
            await _log_form_edit(
                interaction, old_data, self.proof_message,
                old_rule=old_rule, new_rule=rule_id,
                old_punishment=old_punishment, new_punishment=punishment,
            )
            await interaction.followup.send("✅ Форма обновлена.", ephemeral=True)
        else:
            # Author doesn't have the rank to self-approve this punishment type:
            # move the form to the correct channel with approval buttons.
            await _log_form_edit(
                interaction, old_data, self.proof_message,
                old_rule=old_rule, new_rule=rule_id,
                old_punishment=old_punishment, new_punishment=punishment,
            )
            await _escalate_form(
                interaction, self.proof_message,
                rule_id=rule_id, punishment=punishment, evidence_urls=evidence,
            )


async def _escalate_form(
    interaction: discord.Interaction,
    old_message: discord.Message,
    rule_id: str,
    punishment: str,
    evidence_urls: list[str],
):
    """Delete old proof form and repost it in the correct channel with approval buttons."""
    cfg   = interaction.client.cfg
    guild = interaction.guild

    server = server_for_channel(guild, cfg, old_message.channel.id)
    if not server:
        await interaction.followup.send("❌ Не удалось определить сервер формы.", ephemeral=True)
        return

    home_guild_id = int(cfg.get("home_guild_id") or 0)
    home_guild    = interaction.client.get_guild(home_guild_id) if home_guild_id else None

    new_ch = get_banform_channel(guild, cfg, server)
    if not new_ch and home_guild and home_guild.id != guild.id:
        new_ch = get_banform_channel(home_guild, cfg, server)

    if not new_ch:
        await interaction.followup.send(
            f"❌ Канал форм банов не найден (сервер **{server}**).\n"
            f"-# Привяжите канал командой `/linkserver server:{server} banform:#канал`",
            ephemeral=True,
        )
        return

    new_form_type = _form_type_from_punishment(punishment)
    new_view = _rebuild_form_view(
        old_message, evidence_urls, rule_id=rule_id, punishment=punishment
        # force_proof_layout=False → approve/reject buttons added automatically
    )

    # Сначала отправляем новую форму, потом удаляем старую —
    # иначе при ошибке отправки форма потеряется совсем
    try:
        new_msg = await new_ch.send(view=new_view)
    except (discord.Forbidden, discord.HTTPException) as e:
        await interaction.followup.send(f"❌ Не удалось отправить форму: {e}", ephemeral=True)
        return

    try:
        await old_message.delete()
    except (discord.Forbidden, discord.HTTPException):
        # Новая форма уже на месте — старую можно удалить вручную
        await interaction.followup.send(
            f"⚠️ Форма перенесена в {new_ch.mention}, но старую не удалось удалить — удалите вручную.",
            ephemeral=True,
        )
        return

    min_level = APPROVE_MIN_RANK.get(new_form_type, 2)
    req_rank  = RANKS[min_level - 1]
    await interaction.followup.send(
        f"✅ Форма перемещена в {new_ch.mention} (сервер **{server}**).\n"
        f"-# Ожидает одобрения: **{req_rank}+**",
        ephemeral=True,
    )


async def _log_form_edit(
    interaction: discord.Interaction,
    data: dict,
    proof_message: discord.Message,
    old_rule: str,
    new_rule: str,
    old_punishment: str,
    new_punishment: str,
) -> None:
    """Пишем в лог факт правки формы.

    Удаление, одобрение и отклонение логировались, а изменение пункта
    правил и наказания — нет. Из-за этого наказание можно было тихо
    переписать задним числом, не оставив следа.
    """
    if old_rule == new_rule and old_punishment == new_punishment:
        return  # ничего не изменилось
    if not interaction.guild:
        return
    server = server_for_channel(interaction.guild, interaction.client.cfg, proof_message.channel.id)
    if not server:
        return
    log_ch = get_log_channel(interaction.guild, interaction.client.cfg, server)
    if not log_ch:
        return

    changes = []
    if old_rule != new_rule:
        changes.append(f"**Пункт правил:** ~~{old_rule or '—'}~~ → **{new_rule}**")
    if old_punishment != new_punishment:
        changes.append(f"**Наказание:** ~~{old_punishment or '—'}~~ → **{new_punishment}**")

    user_id = data.get("user_id", 0)
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay("## ✏️ Форма изменена"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            f"**Нарушитель:** {f'<@{user_id}>' if user_id else '?'}\n"
            f"**Изменил:** {interaction.user}\n"
            + "\n".join(changes)
        ),
        discord.ui.TextDisplay(f"-# <t:{int(discord.utils.utcnow().timestamp())}:f>"),
        accent_color=0xF39C12,
    ))
    try:
        await log_ch.send(view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _log_form_deletion(
    interaction: discord.Interaction,
    data: dict,
    by_leader: bool,
    proof_message: discord.Message | None = None,
) -> None:
    title     = data.get("title", "Наказание")
    user_id   = data.get("user_id", 0)
    punishment = data.get("punishment", "?")
    if not interaction.guild:
        return
    # Use the proof form's channel (not the ephemeral panel's channel) for server detection
    form_channel_id = proof_message.channel.id if proof_message else interaction.message.channel.id
    server = server_for_channel(interaction.guild, interaction.client.cfg, form_channel_id)
    if not server:
        return
    log_ch = get_log_channel(interaction.guild, interaction.client.cfg, server)
    if not log_ch:
        return
    header = "🗑️ Форма удалена руководством" if by_leader else "🗑️ Форма удалена автором"
    violator = f"<@{user_id}>" if user_id else "?"
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(f"## {header}"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            f"**Форма:** {title}\n"
            f"**Нарушитель:** {violator}\n"
            f"**Удалил:** {interaction.user}\n"
            f"**Наказание:** {punishment}"
        ),
        discord.ui.TextDisplay(f"-# <t:{int(discord.utils.utcnow().timestamp())}:f>"),
        accent_color=0x95A5A6,
    ))
    try:
        await log_ch.send(view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


# ─── Button callbacks ─────────────────────────────────────────────────────────

async def _manage_callback(interaction: discord.Interaction):
    data      = _extract_v2_data(interaction.message)
    mod_id    = data.get("mod_id", 0)
    is_author = interaction.user.id == mod_id

    # Check if this user has enough rank to approve this form type
    punishment = data.get("punishment", "")
    title      = data.get("title", "")
    form_type  = _form_type_from_title(title) if title else _form_type_from_punishment(punishment)
    min_level  = APPROVE_MIN_RANK.get(form_type, 2)
    is_approver = get_member_rank_level(interaction.user) >= min_level

    if not is_author and not is_approver:
        await interaction.response.send_message(
            "❌ Управлять формой может только её **автор**.", ephemeral=True
        )
        return

    ch_id  = interaction.message.channel.id
    msg_id = interaction.message.id

    view = discord.ui.View(timeout=1800)  # 30 min
    if is_author:
        has_evidence = bool(_extract_evidence_urls(interaction.message.components))
        view.add_item(discord.ui.Button(
            label="📎 Добавить доказательство",
            style=discord.ButtonStyle.primary,
            custom_id=f"manage:evidence:{ch_id}:{msg_id}",
            row=0,
        ))
        if has_evidence:
            view.add_item(discord.ui.Button(
                label="🧹 Убрать доказательство",
                style=discord.ButtonStyle.secondary,
                custom_id=f"manage:remove:{ch_id}:{msg_id}",
                row=0,
            ))
        view.add_item(discord.ui.Button(
            label="✏️ Редактировать форму",
            style=discord.ButtonStyle.secondary,
            custom_id=f"manage:edit:{ch_id}:{msg_id}",
            row=1,
        ))
        view.add_item(discord.ui.Button(
            label="📋 Текст для отчёта",
            style=discord.ButtonStyle.secondary,
            custom_id=f"manage:formtext:{ch_id}:{msg_id}",
            row=1,
        ))
        view.add_item(discord.ui.Button(
            label="📷 Как добавить фото",
            style=discord.ButtonStyle.secondary,
            custom_id="manage:photo_help",
            row=2,
        ))
    view.add_item(discord.ui.Button(
        label="🗑️ Удалить форму",
        style=discord.ButtonStyle.danger,
        custom_id=f"manage:delete:{ch_id}:{msg_id}",
        row=2,
    ))

    label = "**⚙️ Управление формой:**" if is_author else "**⚙️ Управление формой (удаление):**"
    await interaction.response.send_message(label, view=view, ephemeral=True)


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
    record_form(mod_id, form_type, "approved",
                violator_id=user_id, rule_id=rule_id, punishment=punishment)

    # Route log by the channel the form is in, not by moderator's current roles
    server = server_for_channel(interaction.guild, interaction.client.cfg, interaction.message.channel.id)
    if server:
        log_view = _build_log_view(title or "Наказание", mod_id, user_id, rule_id, punishment, 0x2ECC71, status)
        await _post_to_log(interaction.guild, interaction.client.cfg, server, log_view)

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
    record_form(mod_id, form_type, "rejected",
                violator_id=user_id, rule_id=rule_id, punishment=punishment)

    server = server_for_channel(interaction.guild, interaction.client.cfg, interaction.message.channel.id)
    if server:
        log_view = _build_log_view(title or "Наказание", mod_id, user_id, rule_id, punishment, 0xE74C3C, status)
        await _post_to_log(interaction.guild, interaction.client.cfg, server, log_view)

    await interaction.followup.send("❌ Форма отклонена.", ephemeral=True)


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

    server_roles = [r.name for r in getattr(member, "roles", []) if is_server_role(r.name)]

    if len(server_roles) == 1:
        return server_roles[0], None

    if len(server_roles) > 1:
        # Несколько ролей — разводим через закреплённый в БД сервер
        member_id = getattr(member, "id", None)
        if member_id:
            from db import get_user_server
            db_server = get_user_server(member_id)
            if db_server and db_server in server_roles:
                return db_server, None
        role_list = ", ".join(f"**{s}**" for s in sorted(server_roles, key=int))
        return None, (
            f"❌ У вас несколько ролей серверов: {role_list}.\n"
            "Попросите администратора закрепить ваш сервер: `/assignserver`"
        )

    # No server role at all — try DB
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
    actor = guild.get_member(interaction.user.id) if guild else None
    if not actor:
        actor = interaction.user
    server, err = _resolve_server(actor, cfg, server_override)
    if err:
        await interaction.followup.send(err, ephemeral=True)
        return

    is_ban = form_type in ("banform", "gbanform")

    from db import get_server_channel
    db_key = "banform" if is_ban else "proof"

    # Find the proof channel: try current guild first, then home guild
    home_guild_id = int(cfg.get("home_guild_id") or 0)
    home_guild = interaction.client.get_guild(home_guild_id) if home_guild_id else None
    target_guild = guild  # guild where the channel lives

    proof_ch = (get_banform_channel(guild, cfg, server) if is_ban
                else get_proof_channel(guild, cfg, server))

    if not proof_ch and home_guild and home_guild.id != guild.id:
        proof_ch = (get_banform_channel(home_guild, cfg, server) if is_ban
                    else get_proof_channel(home_guild, cfg, server))
        if proof_ch:
            target_guild = home_guild

    raw_id_before = get_server_channel(target_guild.id, server, db_key)

    ensure_error: str | None = None
    if not proof_ch:
        # Auto-create channels only on the home guild
        create_guild = home_guild or guild
        try:
            await ensure_server_channels(create_guild, server, cfg)
            proof_ch = (get_banform_channel(create_guild, cfg, server) if is_ban
                        else get_proof_channel(create_guild, cfg, server))
            if proof_ch:
                target_guild = create_guild
        except discord.Forbidden as e:
            ensure_error = f"Forbidden: {e.text!r} code={e.code} status={e.status}"
        except Exception as e:
            ensure_error = str(e)[:120]
            import traceback; traceback.print_exc()

    if not proof_ch:
        ch_label = "банов" if is_ban else "наказаний"
        from db import get_all_server_channels
        raw_id    = get_server_channel(target_guild.id, server, db_key)
        raw_ch    = target_guild.get_channel(raw_id) if raw_id else None
        cat       = discord.utils.get(target_guild.categories, name=str(server))
        cat_id_db = get_server_channel(target_guild.id, server, "category_id")
        cat_by_id = target_guild.get_channel(cat_id_db) if cat_id_db else None
        all_db    = get_all_server_channels(target_guild.id, server)
        actor_roles = [r.name for r in getattr(actor, "roles", []) if r.name != "@everyone"]
        server_src  = ("override" if server_override
                       else ("role" if any(r.name == server for r in getattr(actor, "roles", []))
                             else "db"))
        me = target_guild.me
        bot_id = interaction.client.user.id if interaction.client.user else "?"
        if me is None:
            bot_perm_str = f"`bot=guild.me is None ❌ (bot_id={bot_id})`"
        elif me.guild_permissions.administrator:
            bot_perm_str = "`bot=ADMINISTRATOR ✓`"
        else:
            p = me.guild_permissions
            bot_perm_str = (
                f"`bot(id={bot_id}) manage_channels={'✓' if p.manage_channels else '✗'}` "
                f"`manage_roles={'✓' if p.manage_roles else '✗'}` "
                f"`send_messages={'✓' if p.send_messages else '✗'}`"
            )
        diag_lines = [
            f"`сервер={server}` `src={server_src}` `форма={form_type}` `target={target_guild.id}` `home={home_guild_id}`",
            f"`actor={'Member' if getattr(actor, 'roles', None) else 'User'}` "
            f"`roles={actor_roles[:5]}`",
            f"`DB[{db_key}] before={raw_id_before} after={raw_id}` "
            f"`guild.get_channel={'ok' if raw_ch else 'None'}`",
            f"`cat_by_name={'найдена id='+str(cat.id) if cat else 'НЕ найдена'}` "
            f"`cat_by_db_id={'найдена' if isinstance(cat_by_id, discord.CategoryChannel) else 'None'} ({cat_id_db})`",
            f"`ch_in_cat={[c.name for c in cat.channels] if cat else []}`",
            f"`all_db={dict(all_db)}`",
            bot_perm_str,
        ]
        if ensure_error:
            diag_lines.append(f"`ensure_error={ensure_error}`")

        # Полный дамп нужен только владельцу — рядовому модератору он ничего
        # не говорит, а сообщение раздувает на пол-экрана. В консоль пишем всегда.
        print(f"[proof] канал не найден: " + " | ".join(diag_lines))
        owner_id = getattr(interaction.client, "owner_id_cfg", 0)
        guild_owner_id = interaction.guild.owner_id if interaction.guild else 0
        show_diag = interaction.user.id in (owner_id, guild_owner_id)

        msg = (
            f"❌ Канал форм {ch_label} не найден (сервер **{server}**).\n"
            f"-# Обратитесь к администратору — нужно привязать канал "
            f"(`/linkserver`) или создать заново (`/setupserver {server}`)."
        )
        if show_diag:
            msg += "\n-# " + "\n-# ".join(diag_lines)
        await interaction.followup.send(msg, ephemeral=True)
        return

    # Если ранг автора и так позволяет выдать это наказание — форма не уходит
    # на одобрение. Та же логика уже действовала при смене наказания через
    # ✏️ Редактировать, но не при создании формы командой — из-за этого один
    # и тот же СМ получал разное поведение в зависимости от пути.
    min_level     = APPROVE_MIN_RANK.get(form_type, 2)
    self_approved = get_member_rank_level(actor) >= min_level if getattr(actor, "roles", None) else False
    layout_form_type = "proof" if self_approved else form_type

    title      = _punishment_title(punishment)
    color      = _punishment_color(punishment)
    h_text     = _build_header_text(title, moderator.id)
    # Support multiple space-separated URLs in evidence_url
    ev_urls    = [u.strip() for u in evidence_url.split() if u.strip().startswith("http")] if evidence_url else []
    f_text     = _build_fields_text(violator.id, rule_id, punishment, layout_form_type,
                                     violator_name=violator.name)
    avatar_url = str(violator.display_avatar.url) if violator.display_avatar else ""

    layout_view = _make_layout_view(
        h_text, f_text, ev_urls, color,
        violator_avatar_url=avatar_url, form_type=layout_form_type,
    )

    try:
        await proof_ch.send(view=layout_view)
    except discord.NotFound:
        # Channel was deleted — clear stale ID so next call recreates it
        id_key = "banform" if is_ban else "proof"
        from db import clear_server_channel
        clear_server_channel(target_guild.id, server, id_key)
        await interaction.followup.send(
            f"❌ Канал был удалён (сервер **{server}**). Попробуйте снова — бот пересоздаст его.",
            ephemeral=True,
        )
        return
    except (discord.Forbidden, discord.HTTPException) as e:
        await interaction.followup.send(
            f"❌ Не удалось отправить форму в {proof_ch.mention}: {e}", ephemeral=True
        )
        return
    record_form(interaction.user.id, form_type, "sent",
                violator_id=violator.id, rule_id=rule_id, punishment=punishment)

    # Текст для отчёта больше не шлём в ЛС (копился мусор) —
    # он доступен в любой момент: ⚙️ Управление → 📋 Текст для отчёта
    if not self_approved and form_type in ("banform", "gbanform"):
        status_hint = f"-# ⏳ Ожидает одобрения: **{RANKS[min_level - 1]}+**\n"
    elif self_approved and form_type in ("banform", "gbanform"):
        status_hint = "-# ✅ Одобрение не требуется — ваш ранг позволяет выдать это наказание\n"
    else:
        status_hint = ""
    await interaction.followup.send(
        f"✅ Отправлено в {proof_ch.mention} (сервер **{server}**)!\n"
        f"{status_hint}"
        f"-# 📋 Текст для отчёта: кнопка **⚙️ Управление** на форме",
        ephemeral=True,
    )


def _combine_evidence(*attachments_and_url) -> str:
    """Combine attachment files and a URL string into one space-separated URL list."""
    urls: list[str] = []
    for item in attachments_and_url:
        if item is None:
            continue
        if isinstance(item, discord.Attachment):
            urls.append(item.url)
        elif isinstance(item, str):
            urls.extend(_split_urls(item))
    return " ".join(dict.fromkeys(urls))


# ─── Cog ──────────────────────────────────────────────────────────────────────

class ProofCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
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

        cutoff = now - timedelta(days=7)

        # Список «уже напомнено» живёт в SQLite: после рестарта бот не должен
        # заново пинговать руководство по всем висящим формам
        from db import load_reminded_forms, mark_form_reminded, prune_reminded_forms
        prune_reminded_forms(int(cutoff.timestamp()))
        reminded = load_reminded_forms()

        for idx, ch in enumerate(channels_to_check):
            guild_cfg = get_guild_cfg(cfg, ch.guild.id)
            role_id = guild_cfg.get("review_role_id", 0)
            mention = f"<@&{role_id}>" if role_id else ""
            if not mention:
                continue  # skip reminder if no role configured — pinging nobody is useless
            # Разносим каналы во времени: раньше сотни REST-запросов уходили
            # одной пачкой и забивали очередь рейт-лимита, из-за чего ответы
            # на нажатия кнопок ждали за ними и упирались в 3-секундный лимит
            if idx:
                await asyncio.sleep(SCAN_DELAY)
            try:
                # oldest_first — самые старые формы и есть те, о которых напоминаем;
                # лимит защищает от разбора всей истории активного канала
                async for msg in ch.history(limit=SCAN_LIMIT, after=cutoff, oldest_first=True):
                    if msg.id in reminded:
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
                        # Отмечаем только после успешной отправки
                        mark_form_reminded(msg.id)
                        reminded.add(msg.id)
            except (discord.Forbidden, discord.HTTPException):
                pass

    @reminder_loop.before_loop
    async def before_reminder(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id: str = interaction.data.get("custom_id", "")

        # Route punishment form buttons directly — avoids relying on discord.py's
        # nested V2 component dispatch (walk_children doesn't traverse Container → Section → accessory).
        if custom_id == "punishment:manage":
            await _manage_callback(interaction)
            return
        if custom_id == "punishment:evidence":
            await _evidence_callback(interaction)
            return
        if custom_id == "punishment:approve":
            await _approve_callback(interaction)
            return
        if custom_id == "punishment:reject":
            await _reject_callback(interaction)
            return

        if not custom_id.startswith("manage:"):
            return

        parts = custom_id.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "photo_help":
            await interaction.response.send_message(
                "**Как добавить фото как доказательство:**\n"
                "1. Загрузи фото в любой канал Discord (можно в ЛС себе)\n"
                "2. Нажми ПКМ на фото → **Копировать ссылку на медиа**\n"
                "3. Нажми **📎 Добавить доказательство** и вставь ссылку",
                ephemeral=True,
            )
            return

        if action == "canceldel":
            await interaction.response.send_message("↩️ Удаление отменено.", ephemeral=True)
            return

        if action not in ("evidence", "remove", "edit", "formtext", "delete", "confirmdel") or len(parts) < 4:
            return
        if not parts[2].isdigit() or not parts[3].isdigit():
            return

        ch_id  = int(parts[2])
        msg_id = int(parts[3])

        ch = interaction.client.get_channel(ch_id)
        if not ch:
            await interaction.response.send_message("❌ Канал не найден.", ephemeral=True)
            return

        # Сначала кэш: у нас всего ~3 секунды на ответ, а REST-запрос в это
        # окно может не уложиться — тогда Discord показывает «Interaction
        # failed», и кнопка выглядит несработавшей. Для модалок отложить
        # ответ через defer() нельзя — модальное окно обязано быть первым.
        proof_msg = discord.utils.get(interaction.client.cached_messages, id=msg_id)
        if proof_msg is None:
            try:
                proof_msg = await ch.fetch_message(msg_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await interaction.response.send_message(
                    "❌ Форма не найдена (возможно, уже удалена).", ephemeral=True
                )
                return

        data      = _extract_v2_data(proof_msg)
        mod_id    = data.get("mod_id", 0)
        is_author = interaction.user.id == mod_id

        # For author-only actions verify immediately
        if action in ("evidence", "remove", "edit", "formtext") and not is_author:
            await interaction.response.send_message(
                "❌ Управлять формой может только её **автор**.", ephemeral=True
            )
            return

        # For delete: allow author OR someone with sufficient rank to approve this form
        if action in ("delete", "confirmdel") and not is_author:
            punishment = data.get("punishment", "")
            title_d    = data.get("title", "")
            ft         = _form_type_from_title(title_d) if title_d else _form_type_from_punishment(punishment)
            min_level  = APPROVE_MIN_RANK.get(ft, 2)
            if get_member_rank_level(interaction.user) < min_level:
                await interaction.response.send_message(
                    "❌ Удалить форму может только её **автор** или модератор с правом одобрения.",
                    ephemeral=True,
                )
                return

        if action == "evidence":
            await interaction.response.send_modal(AddEvidenceModal(proof_msg))

        elif action == "remove":
            await interaction.response.send_modal(RemoveEvidenceModal(proof_msg))

        elif action == "edit":
            await interaction.response.send_modal(EditFormModal(proof_msg, data))

        elif action == "formtext":
            user_id = data.get("user_id", 0)
            violator = None
            if user_id:
                violator = interaction.client.get_user(user_id)
                if not violator:
                    try:
                        violator = await interaction.client.fetch_user(user_id)
                    except (discord.NotFound, discord.HTTPException):
                        violator = None
            if not violator:
                await interaction.response.send_message(
                    "❌ Не удалось определить нарушителя из формы.", ephemeral=True
                )
                return
            evidence = " ".join(_extract_evidence_urls(proof_msg.components))
            form_text = build_form(
                interaction.client.cfg, violator,
                data.get("rule_id", ""), data.get("punishment", ""),
                evidence_url=evidence,
            )
            await interaction.response.send_message(
                f"📝 **Форма для отчёта:**\n```\n{form_text}\n```", ephemeral=True
            )

        elif action == "delete":
            # Шаг 1: подтверждение — само удаление произойдёт по confirmdel
            violator_id = data.get("user_id", 0)
            punishment  = data.get("punishment", "?")
            confirm_view = discord.ui.View(timeout=300)
            confirm_view.add_item(discord.ui.Button(
                label="✅ Да, удалить",
                style=discord.ButtonStyle.danger,
                custom_id=f"manage:confirmdel:{ch_id}:{msg_id}",
            ))
            confirm_view.add_item(discord.ui.Button(
                label="↩️ Отмена",
                style=discord.ButtonStyle.secondary,
                custom_id="manage:canceldel",
            ))
            await interaction.response.send_message(
                f"⚠️ **Точно удалить форму?**\n"
                f"-# Нарушитель: <@{violator_id}> | Наказание: {punishment}\n"
                f"-# Это действие нельзя отменить.",
                view=confirm_view,
                ephemeral=True,
            )

        elif action == "confirmdel":
            by_leader = not is_author
            try:
                await proof_msg.delete()
            except (discord.Forbidden, discord.HTTPException):
                await interaction.response.send_message("❌ Не удалось удалить форму.", ephemeral=True)
                return
            await interaction.response.send_message("🗑️ Форма удалена.", ephemeral=True)
            await _log_form_deletion(
                interaction, data, by_leader=by_leader, proof_message=proof_msg
            )

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="proof", description="Отправить доказательство нарушения")
    @app_commands.describe(
        user="Нарушитель",
        rule="Пункт правил (2.1, 3.1 и т.д.)",
        punishment="Выданное наказание",
        evidence="Скриншот доказательства (файл)",
        evidence2="Второй скриншот (файл)",
        evidence3="Третий скриншот (файл)",
        evidence_url="Ссылка(и) на доказательство (если нет файла)",
    )
    @app_commands.autocomplete(rule=rule_autocomplete, punishment=punishment_autocomplete)
    async def proof_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        punishment: str,
        evidence: discord.Attachment | None = None,
        evidence2: discord.Attachment | None = None,
        evidence3: discord.Attachment | None = None,
        evidence_url: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if user.id == interaction.user.id:
            await interaction.followup.send("❌ Нельзя выдать наказание самому себе.", ephemeral=True)
            return

        ev_url = _combine_evidence(evidence, evidence2, evidence3, evidence_url)
        await _post_form(interaction, interaction.user, user, rule, punishment, "proof",
                         evidence_url=ev_url)

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="banform", description="Сгенерировать форму бана")
    @app_commands.describe(
        user="Нарушитель",
        rule="Пункт правил",
        punishment="Срок бана",
        evidence="Скриншот (файл)",
        evidence2="Второй скриншот (файл)",
        evidence3="Третий скриншот (файл)",
        evidence_url="Ссылка(и) на доказательство (если нет файла)",
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
        evidence2: discord.Attachment | None = None,
        evidence3: discord.Attachment | None = None,
        evidence_url: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if user.id == interaction.user.id:
            await interaction.followup.send("❌ Нельзя выдать наказание самому себе.", ephemeral=True)
            return

        ev_url = _combine_evidence(evidence, evidence2, evidence3, evidence_url)
        await _post_form(interaction, interaction.user, user, rule, punishment, "banform",
                         evidence_url=ev_url)

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="gbanform", description="Сгенерировать форму глобального бана")
    @app_commands.describe(
        user="Нарушитель",
        rule="Пункт правил",
        evidence="Скриншот (файл)",
        evidence2="Второй скриншот (файл)",
        evidence3="Третий скриншот (файл)",
        evidence_url="Ссылка(и) на доказательство (если нет файла)",
    )
    @app_commands.autocomplete(rule=rule_autocomplete)
    async def gbanform_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rule: str,
        evidence: discord.Attachment | None = None,
        evidence2: discord.Attachment | None = None,
        evidence3: discord.Attachment | None = None,
        evidence_url: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if user.id == interaction.user.id:
            await interaction.followup.send("❌ Нельзя выдать наказание самому себе.", ephemeral=True)
            return

        ev_url = _combine_evidence(evidence, evidence2, evidence3, evidence_url)
        await _post_form(interaction, interaction.user, user, rule, "Глобальная блокировка", "gbanform",
                         evidence_url=ev_url)


    @app_commands.default_permissions(manage_roles=True)
    @app_commands.command(name="activforms", description="Список незакрытых форм банов")
    @app_commands.describe(server="Номер сервера (оставьте пустым — автоопределение)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def activforms_cmd(self, interaction: discord.Interaction, server: str | None = None):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("❌ Команда доступна только на сервере.", ephemeral=True)
            return
        cfg   = interaction.client.cfg

        # Determine which servers to scan
        if server:
            servers_to_scan = [server]
        else:
            actor = (guild.get_member(interaction.user.id) if guild else None) or interaction.user
            server_roles = [r.name for r in getattr(actor, "roles", []) if is_server_role(r.name)]
            if server_roles:
                servers_to_scan = server_roles
            else:
                from db import get_user_server
                db_s = get_user_server(interaction.user.id)
                servers_to_scan = [db_s] if db_s else []

        if not servers_to_scan:
            await interaction.followup.send("❌ Не удалось определить сервер.", ephemeral=True)
            return

        cutoff = discord.utils.utcnow() - timedelta(days=30)
        results: list[tuple[str, discord.Message]] = []  # (server, message)

        home_guild_id = int(cfg.get("home_guild_id") or 0)
        home_guild = interaction.client.get_guild(home_guild_id) if home_guild_id else None

        for srv in servers_to_scan:
            banform_ch = get_banform_channel(guild, cfg, srv)
            if not banform_ch and home_guild and home_guild.id != guild.id:
                banform_ch = get_banform_channel(home_guild, cfg, srv)
            if not banform_ch:
                continue
            try:
                async for msg in banform_ch.history(limit=None, after=cutoff, oldest_first=True):
                    if _is_pending_v2_message(msg):
                        results.append((srv, msg))
            except (discord.Forbidden, discord.HTTPException):
                continue

        if not results:
            await interaction.followup.send("✅ Незакрытых форм нет.", ephemeral=True)
            return

        header = f"**⏳ Незакрытые формы банов: {len(results)}**\n"
        lines: list[str] = []
        for srv, msg in results:
            data = _extract_v2_data(msg)
            mod_id     = data.get("mod_id", 0)
            user_id    = data.get("user_id", 0)
            punishment = data.get("punishment", "?")
            age_h = int((discord.utils.utcnow() - msg.created_at).total_seconds() / 3600)
            line = (
                f"• Сервер **{srv}** | <@{mod_id}> → <@{user_id}> | {punishment} | "
                f"ожидает **{age_h}ч** | [перейти]({msg.jump_url})"
            )
            if len(header) + len("\n".join(lines)) + len(line) + 20 > 1900:
                lines.append("…и ещё есть.")
                break
            lines.append(line)

        await interaction.followup.send(header + "\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ProofCog(bot))
