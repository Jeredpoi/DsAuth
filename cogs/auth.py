import discord
from discord import app_commands
from discord.ext import commands

from helpers import save_config, get_guild_cfg, RANKS, UNVERIFIED_ROLE_NAME

RANK_COLOR = discord.Color.blue()


# ─── Модальная форма заявки ───────────────────────────────────────────────────

class AuthModal(discord.ui.Modal, title="Заявка на авторизацию"):
    nickname = discord.ui.TextInput(
        label="Ваш никнейм",
        placeholder="Ваш ник в команде модерации",
        max_length=64,
    )
    server = discord.ui.TextInput(
        label="Ваш сервер",
        placeholder="Например: 49-й, Хабаровск, BlackRussia...",
        max_length=64,
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
        embed.add_field(name="Сервер", value=self.server.value, inline=True)
        embed.add_field(name="Заявленная должность", value=self.rank.value, inline=True)
        embed.set_footer(text="Ожидает решения...")

        view = AuthReviewView(member.id)
        await review_ch.send(embed=embed, view=view)

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

        await self._disable_review(
            interaction, approved=True,
            label=f"✅ Одобрено: {interaction.user} → должность: {rank or '—'}"
        )

        try:
            await member.send(
                f"🎉 Ваша заявка на **{guild.name}** одобрена!\n"
                f"Должность: **{rank}**"
            )
        except discord.Forbidden:
            pass

        await interaction.response.send_message(
            f"✅ {member.mention} авторизован как **{rank}**.", ephemeral=True
        )

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
    @app_commands.command(name="setup-auth", description="Настроить систему авторизации (только для владельца)")
    @app_commands.describe(
        auth_channel="Канал авторизации (куда постить кнопку)",
        review_channel="Канал рассмотрения заявок (только для модераторов)",
    )
    async def setup_auth_cmd(
        self,
        interaction: discord.Interaction,
        auth_channel: discord.TextChannel,
        review_channel: discord.TextChannel,
    ):
        owner_id = getattr(self.bot, "owner_id_cfg", 0)
        is_owner = interaction.user.id == owner_id or await self.bot.is_owner(interaction.user)
        if not is_owner:
            await interaction.response.send_message("❌ Только для владельца.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        guild_cfg = get_guild_cfg(self.bot.cfg, guild.id)
        guild_cfg["auth_channel_id"] = auth_channel.id
        guild_cfg["auth_review_channel_id"] = review_channel.id
        save_config(self.bot.cfg)

        # Создаём роль "Не авторизован" если нет
        unverified = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
        if not unverified:
            unverified = await guild.create_role(
                name=UNVERIFIED_ROLE_NAME,
                color=discord.Color.light_grey(),
                reason="Системная роль авторизации",
            )

        # Создаём роли должностей если нет
        created_roles = []
        for rank in RANKS:
            if not discord.utils.get(guild.roles, name=rank):
                await guild.create_role(name=rank, color=RANK_COLOR, hoist=True,
                                        reason="Автосоздание ролей авторизации")
                created_roles.append(rank)

        # Очищаем старые сообщения бота и постим кнопку
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
            "✅ **Система авторизации настроена:**",
            f"• Канал авторизации: {auth_channel.mention}",
            f"• Канал заявок: {review_channel.mention}",
            f"• Роль новых участников: **{UNVERIFIED_ROLE_NAME}**",
        ]
        if created_roles:
            lines.append(f"• Созданы роли: {', '.join(f'**{r}**' for r in created_roles)}")

        await interaction.followup.send("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AuthCog(bot))
    bot.add_view(AuthButtonView())
