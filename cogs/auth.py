import discord
from discord import app_commands
from discord.ext import commands

from helpers import save_config, get_guild_cfg, RANKS, UNVERIFIED_ROLE_NAME, get_member_rank_level
from cogs.servers import is_server_role

RANK_ABBR: dict[str, str] = {
    "мм":  "Младший модератор",
    "м":   "Модератор",
    "см":  "Старший модератор",
    "км":  "Куратор модерации",
    "згм": "Заместитель главного модератора",
    "гм":  "Главный модератор",
}

AUTH_MIN_LEVEL = 5  # ЗГМ или ГМ могут одобрять

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
        server_val = self.server.value.strip()
        if not server_val.isdigit() or not (1 <= int(server_val) <= 90):
            await interaction.response.send_message(
                "❌ Такого сервера нет. Введите число от **1** до **90**.", ephemeral=True
            )
            return

        rank_val = self.rank.value.strip()
        rank_expanded = RANK_ABBR.get(rank_val.lower(), rank_val)

        guild_cfg = get_guild_cfg(self.bot.cfg, interaction.guild_id)
        review_ch_id = guild_cfg.get("auth_review_channel_id", 0)

        if not review_ch_id:
            await interaction.response.send_message(
                "❌ Канал рассмотрения заявок не настроен. Обратитесь к администратору.",
                ephemeral=True,
            )
            return

        review_ch = interaction.guild.get_channel(review_ch_id)
        if not review_ch:
            await interaction.response.send_message("❌ Канал не найден.", ephemeral=True)
            return

        member = interaction.user
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

        # Пинг ЗГМ/ГМ
        ping_roles = [r for r in interaction.guild.roles
                      if r.name in ("Заместитель главного модератора", "Главный модератор")]
        ping_text = " ".join(r.mention for r in ping_roles) if ping_roles else None

        view = AuthReviewView(member.id)
        await review_ch.send(content=ping_text, embed=embed, view=view)

        await interaction.response.send_message(
            "✅ Заявка отправлена! Ожидайте решения модераторов.", ephemeral=True
        )


# ─── Кнопка подачи заявки (персистентная) ─────────────────────────────────────

class AuthButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📋 Подать заявку",
        style=discord.ButtonStyle.primary,
        custom_id="auth:apply",
    )
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        unverified = discord.utils.get(interaction.guild.roles, name=UNVERIFIED_ROLE_NAME)
        if not unverified or unverified not in interaction.user.roles:
            await interaction.response.send_message("✅ Вы уже авторизованы!", ephemeral=True)
            return
        await interaction.response.send_modal(AuthModal(interaction.client))


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


# ─── Cog ─────────────────────────────────────────────────────────────────────

class AuthCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
        # Владелец сервера и владелец бота могут одобрять без ограничений по рангу
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

        guild = interaction.guild
        member = guild.get_member(user_id)

        if not member:
            await interaction.response.send_message("❌ Пользователь покинул сервер.", ephemeral=True)
            await self._disable_review(interaction, approved=False, label="Покинул сервер")
            return

        # Убираем роль "Не авторизован"
        unverified = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
        if unverified and unverified in member.roles:
            await member.remove_roles(unverified, reason="Авторизация одобрена")

        # Выдаём роль должности (синяя)
        if rank:
            role = discord.utils.get(guild.roles, name=rank)
            if not role:
                role = await guild.create_role(
                    name=rank, color=RANK_COLOR, hoist=True,
                    reason="Автосоздание роли авторизации",
                )
            await member.add_roles(role, reason=f"Авторизован: {interaction.user}")

        # Выдаём роль сервера и создаём каналы
        server_num = None
        for field in interaction.message.embeds[0].fields:
            if field.name == "Сервер":
                server_num = field.value.strip()
                break

        role_error = None
        if server_num and server_num.isdigit():
            try:
                server_role = discord.utils.get(guild.roles, name=server_num)
                if not server_role:
                    server_role = await guild.create_role(
                        name=server_num, color=discord.Color.green(),
                        hoist=True, reason=f"Авторизация на сервер {server_num}",
                    )
                await member.add_roles(server_role, reason=f"Авторизован на сервер {server_num}")
            except discord.Forbidden:
                role_error = f"❌ Нет прав выдать роль **{server_num}** (Manage Roles / иерархия ролей)"
            except Exception as e:
                role_error = f"❌ Ошибка при выдаче роли сервера: {e}"

            # Сохраняем привязку user → server в конфиг даже при ошибке роли
            guild_cfg = get_guild_cfg(self.bot.cfg, guild.id)
            guild_cfg.setdefault("user_servers", {})[str(member.id)] = server_num
            save_config(self.bot.cfg)

        await self._disable_review(
            interaction, approved=True,
            label=f"✅ Одобрено: {interaction.user} → должность: {rank or '—'}, сервер: {server_num or '?'}"
        )

        try:
            await member.send(
                f"🎉 Ваша заявка на **{guild.name}** одобрена!\n"
                f"Должность: **{rank}**"
                + (f"\nСервер: **{server_num}**" if server_num else "")
            )
        except discord.Forbidden:
            pass

        msg = f"✅ {member.mention} авторизован как **{rank}**" + (f", сервер **{server_num}**" if server_num else "") + "."
        if role_error:
            msg += f"\n{role_error}\nПривязка сервера сохранена в БД — /proof будет работать, но выдайте роль **{server_num}** вручную."
        await interaction.response.send_message(msg, ephemeral=True)

    async def _handle_reject(self, interaction: discord.Interaction, user_id: int):
        member = interaction.guild.get_member(user_id)
        await self._disable_review(
            interaction, approved=False,
            label=f"❌ Отклонено: {interaction.user}"
        )
        if member:
            try:
                await member.send(
                    f"😔 Ваша заявка на **{interaction.guild.name}** была отклонена."
                )
            except discord.Forbidden:
                pass
        await interaction.response.send_message("❌ Заявка отклонена.", ephemeral=True)

    async def _disable_review(self, interaction: discord.Interaction, approved: bool, label: str):
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
    @app_commands.command(name="setup-auth", description="Настроить систему авторизации на этом сервере")
    async def setup_auth_cmd(self, interaction: discord.Interaction):
        guild = interaction.guild

        # Только владелец сервера или бота
        if interaction.user.id != guild.owner_id and not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ Только для владельца сервера.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild_cfg = get_guild_cfg(self.bot.cfg, guild.id)

        # Сохраняем владельца сервера в конфиг
        guild_cfg["owner_id"] = guild.owner_id

        # Проверка повторного запуска
        existing_ch_id = guild_cfg.get("auth_channel_id", 0)
        if existing_ch_id and guild.get_channel(existing_ch_id):
            await interaction.followup.send(
                "⚠️ Система авторизации уже настроена на этом сервере.\n"
                "Все каналы и роли уже существуют.", ephemeral=True
            )
            return

        everyone = guild.default_role

        # 1. Роль "Не авторизован"
        unverified = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
        if not unverified:
            unverified = await guild.create_role(
                name=UNVERIFIED_ROLE_NAME,
                color=discord.Color.light_grey(),
                reason="Системная роль авторизации",
            )

        # 2. Роли должностей (синие)
        created_roles = []
        for rank in RANKS:
            if not discord.utils.get(guild.roles, name=rank):
                await guild.create_role(
                    name=rank, color=RANK_COLOR, hoist=True,
                    reason="Автосоздание ролей авторизации",
                )
                created_roles.append(rank)

        # 3. Категория
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

        # 4. Канал авторизации
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

        # 5. Канал заявок (только модераторы/администраторы)
        review_channel = discord.utils.get(guild.text_channels, name="заявки-на-авторизацию")
        if not review_channel:
            rev_overwrites = {
                everyone: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            }
            for role in guild.roles:
                if role.permissions.administrator and role != everyone:
                    rev_overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True
                    )
            review_channel = await guild.create_text_channel(
                name="заявки-на-авторизацию",
                category=category,
                overwrites=rev_overwrites,
                topic="Заявки на авторизацию — только для модераторов",
                reason="Канал рассмотрения заявок",
            )

        # Сохраняем ID каналов
        guild_cfg["auth_channel_id"] = auth_channel.id
        guild_cfg["auth_review_channel_id"] = review_channel.id
        save_config(self.bot.cfg)

        # 6. Постим embed с кнопкой
        await auth_channel.purge(limit=10, check=lambda m: m.author == guild.me)
        embed = discord.Embed(
            title="🔐 Авторизация",
            description=(
                "Добро пожаловать!\n\n"
                "Для получения доступа нажмите кнопку ниже и заполните форму.\n"
                "Ваша заявка будет рассмотрена в ближайшее время."
            ),
            color=0x5865F2,
        )
        embed.set_footer(text="После одобрения заявки вам будет выдана должность.")
        await auth_channel.send(embed=embed, view=AuthButtonView())

        lines = [
            "🎉 **Система авторизации настроена!**",
            f"• Категория: **🔐 Авторизация**",
            f"• Канал авторизации: {auth_channel.mention}",
            f"• Канал заявок: {review_channel.mention}",
            f"• Роль новых участников: **{UNVERIFIED_ROLE_NAME}**",
            f"• Владелец сервера сохранён в конфиг: <@{guild.owner_id}>",
        ]
        if created_roles:
            lines.append(f"• Созданы должности: {', '.join(f'**{r}**' for r in created_roles)}")

        await interaction.followup.send("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AuthCog(bot))
    bot.add_view(AuthButtonView())
