import re
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import timedelta

from helpers import save_config, get_guild_cfg, RANKS, RANK_LEVELS, UNVERIFIED_ROLE_NAME, get_member_rank_level
from cogs.servers import is_server_role, get_monitoring_channel
import db

RANK_ABBR: dict[str, str] = {
    "мм":  "Младший модератор",
    "м":   "Модератор",
    "см":  "Старший модератор",
    "км":  "Куратор модерации",
    "згм": "Заместитель главного модератора",
    "гм":  "Главный модератор",
}

RANK_ABBR_SHORT: dict[str, str] = {
    "Младший модератор":                "ММ",
    "Модератор":                        "М",
    "Старший модератор":                "СМ",
    "Куратор модерации":                "КМ",
    "Заместитель главного модератора":  "ЗГМ",
    "Главный модератор":                "ГМ",
}

AUTH_MIN_LEVEL = 5  # ЗГМ или ГМ могут одобрять
AUTOKICK_DAYS  = 2  # кик неавторизованных через N дней
AUTH_COOLDOWN_HOURS = 24  # cooldown после отклонения заявки

RANK_COLOR = discord.Color.blue()


# ─── Модальная форма заявки ───────────────────────────────────────────────────

class AuthModal(discord.ui.Modal, title="Заявка на авторизацию"):
    nickname = discord.ui.TextInput(
        label="Ваш никнейм",
        placeholder="Ваш ник в команде модерации",
        max_length=64,
    )
    server = discord.ui.TextInput(
        label="Номер сервера (1–90)",
        placeholder="Введите цифру, например: 49",
        max_length=3,
    )
    rank = discord.ui.TextInput(
        label="Ваша должность",
        placeholder="Модератор / Старший модератор / Куратор...",
        max_length=64,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        # Антиспам: проверяем cooldown после отклонения
        remaining = db.get_auth_cooldown_remaining(interaction.user.id)
        if remaining > 0:
            h, m = divmod(remaining // 60, 60)
            await interaction.response.send_message(
                f"❌ Повторная заявка доступна через **{h}ч {m}м**.", ephemeral=True
            )
            return

        server_val = self.server.value.strip()
        if not server_val.isdigit() or not (1 <= int(server_val) <= 90):
            await interaction.response.send_message(
                "❌ Такого сервера нет. Введите число от **1** до **90**.", ephemeral=True
            )
            return

        rank_val = self.rank.value.strip()
        rank_expanded = RANK_ABBR.get(rank_val.lower(), rank_val)

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        cfg = self.bot.cfg
        guild_cfg = get_guild_cfg(cfg, guild.id)
        member = interaction.user

        # Route to the server's own category channel (📋-заявки-авт inside category "N")
        from cogs.servers import ensure_server_channels, get_auth_applications_channel
        dest_ch = get_auth_applications_channel(guild, cfg, server_val)
        if not dest_ch:
            try:
                await ensure_server_channels(guild, server_val, cfg)
                dest_ch = get_auth_applications_channel(guild, cfg, server_val)
            except discord.Forbidden:
                pass

        # Fallback: global review channel if configured
        if not dest_ch:
            review_ch_id = guild_cfg.get("auth_review_channel_id", 0)
            if review_ch_id:
                dest_ch = guild.get_channel(review_ch_id)

        if not dest_ch:
            await interaction.followup.send(
                "❌ Канал заявок не найден. Попросите администратора запустить `/deploy`.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="📋 Заявка на авторизацию", color=0x3498DB)
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Участник", value=f"{member.mention}\n`{member.id}`", inline=True)
        embed.add_field(name="Аккаунт создан",
                        value=f"<t:{int(member.created_at.timestamp())}:D>", inline=True)
        embed.add_field(name="Никнейм", value=self.nickname.value, inline=False)
        embed.add_field(name="Сервер", value=server_val, inline=True)
        embed.add_field(name="Заявленная должность", value=rank_expanded, inline=True)
        embed.set_footer(text="Ожидает решения...")

        view = AuthReviewView(member.id)
        await dest_ch.send(embed=embed, view=view)

        await interaction.followup.send(
            f"✅ Заявка отправлена в канал сервера **{server_val}**! Ожидайте решения.", ephemeral=True
        )

        log_ch = get_monitoring_channel(interaction.guild, self.bot.cfg, "🔔-авторизации")
        if log_ch:
            log_embed = discord.Embed(title="📥 Новая заявка на авторизацию", color=0x3498DB,
                                      timestamp=discord.utils.utcnow())
            log_embed.set_thumbnail(url=member.display_avatar.url)
            log_embed.add_field(name="Участник", value=f"{member.mention} (`{member.id}`)", inline=True)
            log_embed.add_field(name="Сервер", value=server_val, inline=True)
            log_embed.add_field(name="Должность", value=rank_expanded, inline=True)
            log_embed.add_field(name="Аккаунт создан",
                                value=f"<t:{int(member.created_at.timestamp())}:D>", inline=True)
            try:
                await log_ch.send(embed=log_embed)
            except (discord.Forbidden, discord.HTTPException):
                pass


# ─── Кнопка подачи заявки — Components V2 (персистентная) ────────────────────

async def _auth_apply_callback(interaction: discord.Interaction):
    unverified = discord.utils.get(interaction.guild.roles, name=UNVERIFIED_ROLE_NAME)
    if not unverified or unverified not in interaction.user.roles:
        await interaction.response.send_message("✅ Вы уже авторизованы!", ephemeral=True)
        return

    remaining = db.get_auth_cooldown_remaining(interaction.user.id)
    if remaining > 0:
        h, m = divmod(remaining // 60, 60)
        await interaction.response.send_message(
            f"❌ Повторная заявка доступна через **{h}ч {m}м**.", ephemeral=True
        )
        return

    await interaction.response.send_modal(AuthModal(interaction.client))


class AuthButtonView(discord.ui.LayoutView):
    """Persistent V2 layout — auth channel welcome message with apply button."""

    def __init__(self):
        super().__init__(timeout=None)

        btn = discord.ui.Button(
            label="📋 Подать заявку",
            style=discord.ButtonStyle.primary,
            custom_id="auth:apply",
        )
        btn.callback = _auth_apply_callback

        container = discord.ui.Container(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "## 🔐 Авторизация\n"
                    "Добро пожаловать!\n\n"
                    "Для получения доступа нажмите кнопку и заполните форму.\n"
                    "Ваша заявка будет рассмотрена в ближайшее время."
                ),
                accessory=btn,
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay("После одобрения заявки вам будет выдана должность."),
            accent_color=0x5865F2,
        )
        self.add_item(container)


# ─── Кнопки рассмотрения заявки ───────────────────────────────────────────────

class AuthReviewView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

        options = [discord.SelectOption(label=r, value=r) for r in RANKS]
        select = discord.ui.Select(
            placeholder="Выбрать должность и одобрить...",
            options=options,
            custom_id=f"auth:approve:{user_id}",
        )
        self.add_item(select)
        self.add_item(discord.ui.Button(
            label="❌ Отклонить",
            style=discord.ButtonStyle.danger,
            custom_id=f"auth:reject:{user_id}",
        ))


# ─── Forum helpers ────────────────────────────────────────────────────────────

async def _get_or_create_forum_tag(
    forum: discord.ForumChannel, name: str, emoji: str = ""
) -> discord.ForumTag:
    """Find existing tag by name or create it."""
    for tag in forum.available_tags:
        if tag.name == name:
            return tag
    partial_emoji = discord.PartialEmoji.from_str(emoji) if emoji else None
    return await forum.create_tag(name=name, emoji=partial_emoji)


async def _post_auth_to_forum(
    forum: discord.ForumChannel,
    guild_cfg: dict,
    member: discord.Member,
    server_val: str,
    embed: discord.Embed,
    view: discord.ui.View,
    cfg: dict,
    guild: discord.Guild,
):
    """Create a forum thread for an auth application, tagged by server number."""
    from helpers import save_config

    # Ensure base status tags exist
    pending_tag = await _get_or_create_forum_tag(forum, "⏳ Ожидает", "⏳")
    approved_tag = await _get_or_create_forum_tag(forum, "✅ Одобрено", "✅")
    rejected_tag = await _get_or_create_forum_tag(forum, "❌ Отклонено", "❌")

    guild_cfg["auth_forum_tag_pending_id"]  = pending_tag.id
    guild_cfg["auth_forum_tag_approved_id"] = approved_tag.id
    guild_cfg["auth_forum_tag_rejected_id"] = rejected_tag.id

    # Per-server tag
    server_tag = await _get_or_create_forum_tag(forum, f"Сервер {server_val}")

    save_config(cfg)

    ping_roles = [r for r in guild.roles
                  if r.name in ("Заместитель главного модератора", "Главный модератор")]
    ping_text = " ".join(r.mention for r in ping_roles) if ping_roles else None

    await forum.create_thread(
        name=f"Заявка — {member.display_name} — Сервер {server_val}",
        embed=embed,
        view=view,
        applied_tags=[pending_tag, server_tag],
        content=ping_text,
        auto_archive_duration=10080,  # 7 дней
    )


async def _close_forum_thread(
    interaction: discord.Interaction,
    guild_cfg: dict,
    approved: bool,
):
    """If the interaction happened in a forum thread, update its tags and archive it."""
    if not isinstance(interaction.channel, discord.Thread):
        return
    thread = interaction.channel
    if not isinstance(thread.parent, discord.ForumChannel):
        return
    forum = thread.parent

    tag_id = guild_cfg.get(
        "auth_forum_tag_approved_id" if approved else "auth_forum_tag_rejected_id", 0
    )
    done_tag = forum.get_tag(tag_id) if tag_id else None
    if not done_tag:
        done_tag = await _get_or_create_forum_tag(
            forum, "✅ Одобрено" if approved else "❌ Отклонено"
        )

    try:
        new_tags = [done_tag]
        # Keep the server tag if present
        for t in thread.applied_tags:
            if t.name.startswith("Сервер "):
                new_tags.append(t)
                break
        await thread.edit(applied_tags=new_tags, archived=True, locked=False)
    except (discord.Forbidden, discord.HTTPException):
        pass


# ─── Cog ─────────────────────────────────────────────────────────────────────

class AuthCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.autokick_loop.start()

    def cog_unload(self):
        self.autokick_loop.cancel()

    async def _log_event(self, guild: discord.Guild, embed: discord.Embed) -> None:
        ch = get_monitoring_channel(guild, self.bot.cfg, "🔔-авторизации")
        if ch:
            try:
                await ch.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ─── Автокик неавторизованных ─────────────────────────────────────────
    @tasks.loop(hours=1)
    async def autokick_loop(self):
        cutoff = discord.utils.utcnow() - timedelta(days=AUTOKICK_DAYS)
        for guild in self.bot.guilds:
            guild_cfg = get_guild_cfg(self.bot.cfg, guild.id)
            if not guild_cfg.get("auth_channel_id"):
                continue
            unverified = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
            if not unverified:
                continue
            for member in list(unverified.members):
                if member.id == guild.owner_id:
                    continue
                if member.id == getattr(self.bot, "owner_id_cfg", 0):
                    continue
                if any(r.name in RANKS for r in member.roles):
                    continue
                if not (member.joined_at and member.joined_at < cutoff):
                    continue
                try:
                    try:
                        await member.send(
                            f"👋 Вы были исключены с сервера **{guild.name}**, "
                            f"так как не прошли авторизацию в течение {AUTOKICK_DAYS} дней.\n"
                            f"Для вступления повторно вступите на сервер и подайте заявку."
                        )
                    except discord.Forbidden:
                        pass
                    await guild.kick(member, reason=f"Не авторизован за {AUTOKICK_DAYS} дня")
                    e = discord.Embed(title="⚡ Автокик", color=0xE74C3C,
                                      timestamp=discord.utils.utcnow())
                    e.add_field(name="Участник", value=f"{member} (`{member.id}`)", inline=False)
                    e.add_field(name="Причина", value=f"Не авторизован в течение {AUTOKICK_DAYS} дней", inline=False)
                    await self._log_event(guild, e)
                except (discord.Forbidden, discord.HTTPException):
                    pass

    @autokick_loop.before_loop
    async def before_autokick(self):
        await self.bot.wait_until_ready()

    # ─── Обработка кнопок из AuthReviewView ───────────────────────────────
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id: str = interaction.data.get("custom_id", "")

        if custom_id.startswith("auth:approve:"):
            user_id = int(custom_id.split(":")[2])
            rank = interaction.data.get("values", [None])[0]
            await self._handle_approve(interaction, user_id, rank)

        elif custom_id.startswith("auth:reject:"):
            user_id = int(custom_id.split(":")[2])
            await self._handle_reject(interaction, user_id)

    async def _handle_approve(self, interaction: discord.Interaction, user_id: int, rank: str | None):
        is_owner = (
            interaction.user.id == interaction.guild.owner_id
            or interaction.user.id == getattr(self.bot, "owner_id_cfg", 0)
        )
        if not is_owner and get_member_rank_level(interaction.user) < AUTH_MIN_LEVEL:
            await interaction.response.send_message(
                "❌ Одобрять заявки могут только **Заместитель главного модератора** или **Главный модератор**.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = guild.get_member(user_id)

        if not member:
            await self._disable_review(interaction, approved=False, label="Покинул сервер")
            await interaction.followup.send("❌ Пользователь покинул сервер.", ephemeral=True)
            return

        unverified = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
        if unverified and unverified in member.roles:
            await member.remove_roles(unverified, reason="Авторизация одобрена")

        rank_role = None
        if rank:
            rank_role = discord.utils.get(guild.roles, name=rank)
            if not rank_role:
                rank_role = await guild.create_role(
                    name=rank, color=RANK_COLOR, hoist=True,
                    reason="Автосоздание роли авторизации",
                )

        server_num = None
        if interaction.message.embeds:
            for field in interaction.message.embeds[0].fields:
                if field.name == "Сервер":
                    server_num = field.value.strip()
                    break

        # Team role (общая роль для всех авторизованных)
        guild_cfg_cur = get_guild_cfg(self.bot.cfg, guild.id)
        team_role_id = guild_cfg_cur.get("team_role_id", 0)
        team_role = guild.get_role(team_role_id) if team_role_id else None

        role_error = None
        if server_num and server_num.isdigit():
            try:
                server_role = discord.utils.get(guild.roles, name=server_num)
                if not server_role:
                    server_role = await guild.create_role(
                        name=server_num, color=discord.Color.green(),
                        hoist=True, reason=f"Авторизация на сервер {server_num}",
                    )
                roles_to_add = [r for r in (rank_role, server_role, team_role) if r]
                if roles_to_add:
                    await member.add_roles(*roles_to_add, reason=f"Авторизован: {interaction.user}")
            except discord.Forbidden:
                role_error = f"❌ Нет прав выдать роль **{server_num}** (Manage Roles / иерархия ролей)"
            except Exception as e:
                role_error = f"❌ Ошибка при выдаче роли сервера: {e}"

            db.set_user_server(member.id, server_num, rank or "")
            db.clear_auth_cooldown(member.id)

        elif rank_role:
            roles_to_add = [r for r in (rank_role, team_role) if r]
            await member.add_roles(*roles_to_add, reason=f"Авторизован: {interaction.user}")
            db.clear_auth_cooldown(member.id)

        # Автоник: [СМ | 50] Имя
        if rank and server_num:
            abbr = RANK_ABBR_SHORT.get(rank, rank[:2])
            prefix = f"[{abbr} | {server_num}] "
            display = member.display_name[:max(0, 32 - len(prefix))]
            new_nick = prefix + display
            try:
                await member.edit(nick=new_nick, reason="Автоник при авторизации")
            except discord.Forbidden:
                pass

        guild_cfg_approve = get_guild_cfg(self.bot.cfg, interaction.guild_id)
        await self._disable_review(
            interaction, approved=True,
            label=f"✅ Одобрено: {interaction.user} → должность: {rank or '—'}, сервер: {server_num or '?'}"
        )
        await _close_forum_thread(interaction, guild_cfg_approve, approved=True)

        try:
            await member.send(
                f"🎉 Ваша заявка на **{guild.name}** одобрена!\n"
                f"Должность: **{rank or '—'}**"
                + (f"\nСервер: **{server_num}**" if server_num else "")
            )
        except discord.Forbidden:
            pass

        msg = f"✅ {member.mention} авторизован как **{rank or '—'}**" + (f", сервер **{server_num}**" if server_num else "") + "."
        if role_error:
            msg += f"\n{role_error}\nПривязка сервера сохранена в БД — /proof будет работать, но выдайте роль **{server_num}** вручную."
        await interaction.followup.send(msg, ephemeral=True)

        e = discord.Embed(title="✅ Авторизация одобрена", color=0x2ECC71,
                          timestamp=discord.utils.utcnow())
        e.add_field(name="Участник", value=f"{member.mention} (`{member.id}`)", inline=True)
        e.add_field(name="Должность", value=rank or "—", inline=True)
        e.add_field(name="Сервер", value=server_num or "—", inline=True)
        e.add_field(name="Одобрил", value=str(interaction.user), inline=False)
        await self._log_event(interaction.guild, e)

    async def _handle_reject(self, interaction: discord.Interaction, user_id: int):
        await interaction.response.defer(ephemeral=True)
        member = interaction.guild.get_member(user_id)

        db.set_auth_cooldown(user_id)  # 24ч cooldown

        guild_cfg_reject = get_guild_cfg(self.bot.cfg, interaction.guild_id)
        await self._disable_review(
            interaction, approved=False,
            label=f"❌ Отклонено: {interaction.user}"
        )
        await _close_forum_thread(interaction, guild_cfg_reject, approved=False)

        if member:
            try:
                await member.send(
                    f"😔 Ваша заявка на **{interaction.guild.name}** была отклонена.\n"
                    f"Повторную заявку можно подать через **{AUTH_COOLDOWN_HOURS} часов**."
                )
            except discord.Forbidden:
                pass
        await interaction.followup.send("❌ Заявка отклонена.", ephemeral=True)

        e = discord.Embed(title="❌ Заявка отклонена", color=0xE74C3C,
                          timestamp=discord.utils.utcnow())
        e.add_field(name="Участник",
                    value=f"{member.mention} (`{user_id}`)" if member else f"`{user_id}`",
                    inline=True)
        e.add_field(name="Отклонил", value=str(interaction.user), inline=True)
        e.add_field(name="Cooldown", value=f"{AUTH_COOLDOWN_HOURS}ч", inline=True)
        await self._log_event(interaction.guild, e)

    async def _disable_review(self, interaction: discord.Interaction, approved: bool, label: str):
        if not interaction.message.embeds:
            await interaction.message.edit(view=None)
            return
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green() if approved else discord.Color.red()
        embed.set_footer(text=label)
        await interaction.message.edit(embed=embed, view=None)

    # ─── on_member_join ────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild_cfg = get_guild_cfg(self.bot.cfg, member.guild.id)
        if not guild_cfg.get("auth_channel_id"):
            return

        unverified = discord.utils.get(member.guild.roles, name=UNVERIFIED_ROLE_NAME)
        if not unverified:
            unverified = await member.guild.create_role(
                name=UNVERIFIED_ROLE_NAME,
                color=discord.Color.light_grey(),
                reason="Автосоздание роли авторизации",
            )
        try:
            await member.add_roles(unverified, reason="Новый участник")
        except discord.Forbidden:
            pass

    # ─── /setup-auth ───────────────────────────────────────────────────────
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="setup-auth", description="Настроить систему авторизации на этом сервере")
    @app_commands.describe(team_role="Роль, выдаваемая всем авторизованным (необязательно)")
    async def setup_auth_cmd(self, interaction: discord.Interaction, team_role: discord.Role | None = None):
        guild = interaction.guild

        if interaction.user.id != guild.owner_id and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца сервера.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild_cfg = get_guild_cfg(self.bot.cfg, guild.id)
        guild_cfg["owner_id"] = guild.owner_id

        everyone = guild.default_role

        unverified = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
        if not unverified:
            unverified = await guild.create_role(
                name=UNVERIFIED_ROLE_NAME,
                color=discord.Color.light_grey(),
                reason="Системная роль авторизации",
            )

        created_roles = []
        for rank in RANKS:
            existing = discord.utils.get(guild.roles, name=rank)
            if not existing:
                await guild.create_role(
                    name=rank, color=RANK_COLOR, hoist=True,
                    reason="Автосоздание ролей авторизации",
                )
                created_roles.append(rank)
            elif not existing.hoist:
                try:
                    await existing.edit(hoist=True, reason="Исправление: роль должна быть hoisted")
                except discord.Forbidden:
                    pass

        category = discord.utils.get(guild.categories, name="🔐 Авторизация")
        if not category:
            cat_overwrites = {
                everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                unverified: discord.PermissionOverwrite(view_channel=True),
            }
            category = await guild.create_category(
                name="🔐 Авторизация",
                overwrites=cat_overwrites,
                reason="Категория авторизации",
            )

        auth_channel = discord.utils.get(guild.text_channels, name="авторизация")
        if not auth_channel:
            ch_overwrites = {
                everyone: discord.PermissionOverwrite(
                    view_channel=True, send_messages=False, read_message_history=True
                ),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                unverified: discord.PermissionOverwrite(view_channel=True),
            }
            auth_channel = await guild.create_text_channel(
                name="авторизация",
                category=category,
                overwrites=ch_overwrites,
                topic="Нажмите кнопку для подачи заявки на авторизацию",
                reason="Канал авторизации",
            )

        # Fallback review channel (applications now go to per-server categories via 📋-заявки-авт)
        mgmt_roles = [r for r in guild.roles
                      if r.name in {"Куратор модерации", "Заместитель главного модератора", "Главный модератор"}]
        review_channel = discord.utils.get(guild.text_channels, name="заявки-авт-лог")
        if not review_channel:
            rev_overwrites = {
                everyone: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            }
            for r in mgmt_roles:
                rev_overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            review_channel = await guild.create_text_channel(
                name="заявки-авт-лог",
                category=category,
                overwrites=rev_overwrites,
                topic="Резервный канал: заявки без привязки к серверу",
                reason="Резервный канал рассмотрения заявок",
            )

        guild_cfg["auth_channel_id"] = auth_channel.id
        guild_cfg["auth_review_channel_id"] = review_channel.id
        if team_role:
            guild_cfg["team_role_id"] = team_role.id
        save_config(self.bot.cfg)

        await auth_channel.purge(limit=10, check=lambda m: m.author == guild.me)
        await auth_channel.send(view=AuthButtonView())

        lines = [
            "🎉 **Система авторизации настроена!**",
            f"• Категория: **🔐 Авторизация**",
            f"• Канал авторизации: {auth_channel.mention}",
            f"• Заявки: попадают в **📋-заявки-авт** категории сервера (создаётся при `/deploy` или первой заявке)",
            f"• Роль новых участников: **{UNVERIFIED_ROLE_NAME}**",
            f"• Автокик неавторизованных: через **{AUTOKICK_DAYS} дня**",
            f"• Командная роль: {team_role.mention if team_role else '❌ не задана (задайте через /setup-auth team_role:...)'}",
        ]
        if created_roles:
            lines.append(f"• Созданы должности: {', '.join(f'**{r}**' for r in created_roles)}")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ─── /dismiss ─────────────────────────────────────────────────────────
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.command(name="dismiss", description="Исключить модератора по собственному желанию")
    @app_commands.describe(
        member="Модератор, покидающий команду",
        reason="Причина (необязательно)",
    )
    async def dismiss_cmd(self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
        is_owner = (
            interaction.user.id == interaction.guild.owner_id
            or interaction.user.id == getattr(self.bot, "owner_id_cfg", 0)
        )
        if not is_owner and get_member_rank_level(interaction.user) < AUTH_MIN_LEVEL:
            await interaction.response.send_message(
                "❌ Команда доступна только **Заместителю главного модератора** и **Главному модератору**.",
                ephemeral=True,
            )
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message("❌ Нельзя исключить самого себя.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild_cfg_d = get_guild_cfg(self.bot.cfg, interaction.guild_id)
        team_role_id_d = guild_cfg_d.get("team_role_id", 0)
        team_role_d = interaction.guild.get_role(team_role_id_d) if team_role_id_d else None

        roles_to_remove = [
            r for r in member.roles
            if r.name in RANKS or is_server_role(r.name)
            or (team_role_d and r.id == team_role_d.id)
        ]
        unverified = discord.utils.get(interaction.guild.roles, name=UNVERIFIED_ROLE_NAME)
        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason=f"Исключён: {interaction.user}")
            if unverified and unverified not in member.roles:
                await member.add_roles(unverified, reason="Исключён из команды модерации")
        except discord.Forbidden:
            await interaction.followup.send("❌ Нет прав для управления ролями.", ephemeral=True)
            return

        # Сбрасываем ник
        try:
            await member.edit(nick=None, reason="Dismiss: сброс ника")
        except discord.Forbidden:
            pass

        db.remove_user_server(member.id)

        farewell = (
            f"👋 Вы были исключены из команды модерации **{interaction.guild.name}**.\n"
            + (f"Причина: {reason}\n" if reason else "")
            + "\nЖелаем всего наилучшего и успехов в дальнейшем! 🌟"
        )
        try:
            await member.send(farewell)
        except discord.Forbidden:
            pass

        removed_names = ", ".join(r.name for r in roles_to_remove) or "нет"
        await interaction.followup.send(
            f"✅ **{member}** исключён из команды модерации.\n"
            f"Сняты роли: {removed_names}\n"
            f"Прощальное сообщение отправлено в ЛС.",
            ephemeral=True,
        )

        e = discord.Embed(title="🔵 Исключение из команды", color=0x3498DB,
                          timestamp=discord.utils.utcnow())
        e.add_field(name="Участник", value=f"{member.mention} (`{member.id}`)", inline=True)
        e.add_field(name="Исключил", value=str(interaction.user), inline=True)
        if reason:
            e.add_field(name="Причина", value=reason, inline=False)
        e.add_field(name="Сняты роли", value=removed_names, inline=False)
        await self._log_event(interaction.guild, e)


    # ─── /promote ─────────────────────────────────────────────────────────
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.command(name="promote", description="Изменить звание модератора")
    @app_commands.describe(member="Модератор", rank="Новое звание")
    @app_commands.choices(rank=[app_commands.Choice(name=r, value=r) for r in RANKS])
    async def promote_cmd(self, interaction: discord.Interaction, member: discord.Member, rank: str):
        is_owner = (
            interaction.user.id == interaction.guild.owner_id
            or interaction.user.id == getattr(self.bot, "owner_id_cfg", 0)
        )
        if not is_owner and get_member_rank_level(interaction.user) < AUTH_MIN_LEVEL:
            await interaction.response.send_message(
                "❌ Команда доступна только **Заместителю главного модератора** и **Главному модератору**.",
                ephemeral=True,
            )
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message("❌ Нельзя изменить звание самому себе.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild

        # Убираем все текущие ранговые роли
        old_rank_roles = [r for r in member.roles if r.name in RANKS]
        old_rank = old_rank_roles[0].name if old_rank_roles else None

        # Создаём новую роль если нет
        rank_role = discord.utils.get(guild.roles, name=rank)
        if not rank_role:
            rank_role = await guild.create_role(
                name=rank, color=RANK_COLOR, hoist=True,
                reason="Автосоздание роли при смене звания",
            )

        guild_cfg_p = get_guild_cfg(self.bot.cfg, guild.id)
        team_role_p = guild.get_role(guild_cfg_p.get("team_role_id", 0) or 0)
        roles_to_add_p = [r for r in (rank_role, team_role_p) if r and r not in member.roles]

        try:
            if old_rank_roles:
                await member.remove_roles(*old_rank_roles, reason=f"Смена звания: {interaction.user}")
            if roles_to_add_p:
                await member.add_roles(*roles_to_add_p, reason=f"Новое звание: {rank} ({interaction.user})")
        except discord.Forbidden:
            await interaction.followup.send("❌ Нет прав для управления ролями.", ephemeral=True)
            return

        # Парсим сервер из текущего ника или берём из БД
        nick_match = re.match(r'^\[.+?\s*\|\s*(\d+)\]\s*(.*)', member.display_name)
        if nick_match:
            server_num = nick_match.group(1)
            display_name = nick_match.group(2)
        else:
            server_num = db.get_user_server(member.id)
            display_name = re.sub(r'^\[.+?\]\s*', '', member.display_name)

        if server_num:
            abbr = RANK_ABBR_SHORT.get(rank, rank[:2])
            prefix = f"[{abbr} | {server_num}] "
            display = display_name[:max(0, 32 - len(prefix))]
            try:
                await member.edit(nick=prefix + display, reason="Автоник при смене звания")
            except discord.Forbidden:
                pass
            db.set_user_server(member.id, server_num, rank)

        change = f"{old_rank or '—'} → **{rank}**"
        await interaction.followup.send(
            f"✅ Звание **{member}** изменено: {change}.", ephemeral=True
        )

        try:
            await member.send(
                f"📋 Ваше звание на **{guild.name}** изменено.\n"
                f"Новое звание: **{rank}**\n"
                f"Изменил: **{interaction.user}**"
            )
        except discord.Forbidden:
            pass

        e = discord.Embed(title="📋 Смена звания", color=0x9B59B6,
                          timestamp=discord.utils.utcnow())
        e.add_field(name="Участник", value=f"{member.mention} (`{member.id}`)", inline=True)
        e.add_field(name="Звание", value=change, inline=True)
        e.add_field(name="Изменил", value=str(interaction.user), inline=False)
        await self._log_event(guild, e)


async def setup(bot: commands.Bot):
    await bot.add_cog(AuthCog(bot))
    bot.add_view(AuthButtonView())
