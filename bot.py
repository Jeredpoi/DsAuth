import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
from keep_alive import keep_alive

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PROXY = os.getenv("PROXY_URL")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

MEMBER_ROLE_NAME = "Участник"
UNVERIFIED_ROLE_NAME = "Не верифицирован"
AUTH_CATEGORY_NAME = "🔐 Авторизация"
AUTH_CHANNEL_NAME = "авторизация"
REVIEW_CHANNEL_NAME = "заявки-на-вход"

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, proxy=PROXY)

pending: set[int] = set()


# ─── Persistent view for the auth button ────────────────────────────────────

class PersistentAuthView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔐 Авторизоваться",
        style=discord.ButtonStyle.primary,
        custom_id="auth:request",
    )
    async def auth_request(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await handle_auth_request(interaction)


# ─── Helpers ────────────────────────────────────────────────────────────────

async def get_or_create_unverified_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
    if not role:
        role = await guild.create_role(
            name=UNVERIFIED_ROLE_NAME,
            color=discord.Color.light_grey(),
            reason="Автосоздание роли системой авторизации",
        )
    return role


def make_review_embed(member: discord.Member) -> discord.Embed:
    embed = discord.Embed(title="📋 Заявка на авторизацию", color=0x3498DB)
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.add_field(name="Пользователь", value=member.mention, inline=True)
    embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
    embed.add_field(
        name="Аккаунт создан",
        value=f"<t:{int(member.created_at.timestamp())}:D>",
        inline=True,
    )
    if member.joined_at:
        embed.add_field(
            name="Вошёл на сервер",
            value=f"<t:{int(member.joined_at.timestamp())}:R>",
            inline=True,
        )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="Ожидает решения модератора...")
    return embed


def make_review_view(user_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="✅ Принять",
            style=discord.ButtonStyle.success,
            custom_id=f"auth:approve:{user_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="❌ Отклонить",
            style=discord.ButtonStyle.danger,
            custom_id=f"auth:reject:{user_id}",
        )
    )
    return view


def make_disabled_view(approved: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="✅ Принять",
            style=discord.ButtonStyle.success,
            disabled=True,
            custom_id="done:approve",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="❌ Отклонить",
            style=discord.ButtonStyle.danger,
            disabled=True,
            custom_id="done:reject",
        )
    )
    return view


# ─── Interaction handlers ────────────────────────────────────────────────────

async def handle_auth_request(interaction: discord.Interaction):
    member = interaction.user
    guild = interaction.guild

    member_role = discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME)
    if member_role and member_role in member.roles:
        await interaction.response.send_message(
            "✅ Вы уже авторизованы!", ephemeral=True
        )
        return

    if member.id in pending:
        await interaction.response.send_message(
            "⏳ Ваша заявка уже рассматривается. Ожидайте решения модераторов.",
            ephemeral=True,
        )
        return

    review_channel = discord.utils.get(guild.text_channels, name=REVIEW_CHANNEL_NAME)
    if not review_channel:
        await interaction.response.send_message(
            "❌ Ошибка конфигурации: канал заявок не найден. Обратитесь к администратору.",
            ephemeral=True,
        )
        return

    embed = make_review_embed(member)
    view = make_review_view(member.id)
    await review_channel.send(embed=embed, view=view)
    pending.add(member.id)

    await interaction.response.send_message(
        "✅ Заявка отправлена! Ожидайте решения модераторов.", ephemeral=True
    )


async def handle_approve(interaction: discord.Interaction, user_id: int):
    guild = interaction.guild
    member = guild.get_member(user_id)

    if not member:
        await interaction.response.send_message(
            "❌ Пользователь покинул сервер.", ephemeral=True
        )
        pending.discard(user_id)
        await _finalize_review(interaction, approved=True, label="Покинул сервер")
        return

    member_role = discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME)
    if not member_role:
        member_role = await guild.create_role(
            name=MEMBER_ROLE_NAME,
            color=discord.Color.green(),
            hoist=True,
            reason="Автосоздание роли системой авторизации",
        )

    unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
    roles_to_remove = [unverified_role] if unverified_role and unverified_role in member.roles else []

    await member.add_roles(member_role, reason=f"Авторизован модератором {interaction.user}")
    if roles_to_remove:
        await member.remove_roles(*roles_to_remove, reason="Верификация пройдена")

    pending.discard(user_id)

    embed = interaction.message.embeds[0]
    embed.color = 0x2ECC71
    embed.set_footer(
        text=f"✅ Принят модератором {interaction.user} ({interaction.user.id})"
    )
    await interaction.message.edit(embed=embed, view=make_disabled_view(True))

    try:
        await member.send(
            f"🎉 Ваша заявка на сервер **{guild.name}** одобрена! Добро пожаловать!"
        )
    except discord.Forbidden:
        pass

    await interaction.response.send_message(
        f"✅ Пользователь {member.mention} авторизован!", ephemeral=True
    )


async def handle_reject(interaction: discord.Interaction, user_id: int):
    guild = interaction.guild
    member = guild.get_member(user_id)
    pending.discard(user_id)

    embed = interaction.message.embeds[0]
    embed.color = 0xE74C3C
    embed.set_footer(
        text=f"❌ Отклонён модератором {interaction.user} ({interaction.user.id})"
    )
    await interaction.message.edit(embed=embed, view=make_disabled_view(False))

    if member:
        try:
            await member.send(
                f"😔 Ваша заявка на сервер **{guild.name}** была отклонена."
            )
        except discord.Forbidden:
            pass

    name = str(member) if member else f"ID: {user_id}"
    await interaction.response.send_message(
        f"❌ Заявка пользователя **{name}** отклонена.", ephemeral=True
    )


async def _finalize_review(
    interaction: discord.Interaction, approved: bool, label: str
):
    embed = interaction.message.embeds[0]
    embed.set_footer(text=label)
    await interaction.message.edit(embed=embed, view=make_disabled_view(approved))


# ─── Bot events ─────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    bot.add_view(PersistentAuthView())
    print(f"✅ Бот запущен как {bot.user} (ID: {bot.user.id})")
    print("Используйте !setup в своём Discord-сервере для настройки авторизации.")


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return

    custom_id: str = interaction.data.get("custom_id", "")
    parts = custom_id.split(":")

    if len(parts) == 3 and parts[0] == "auth":
        try:
            user_id = int(parts[2])
        except ValueError:
            return
        if parts[1] == "approve":
            await handle_approve(interaction, user_id)
        elif parts[1] == "reject":
            await handle_reject(interaction, user_id)


@bot.event
async def on_member_join(member: discord.Member):
    unverified_role = await get_or_create_unverified_role(member.guild)
    try:
        await member.add_roles(unverified_role, reason="Новый участник — ожидает верификации")
    except discord.Forbidden:
        pass


# ─── Setup command ───────────────────────────────────────────────────────────

def is_owner():
    async def predicate(ctx: commands.Context) -> bool:
        if OWNER_ID and ctx.author.id == OWNER_ID:
            return True
        return await ctx.bot.is_owner(ctx.author)
    return commands.check(predicate)


def is_setup_done(guild: discord.Guild) -> bool:
    has_category = discord.utils.get(guild.categories, name=AUTH_CATEGORY_NAME) is not None
    has_auth_ch = discord.utils.get(guild.text_channels, name=AUTH_CHANNEL_NAME) is not None
    has_review_ch = discord.utils.get(guild.text_channels, name=REVIEW_CHANNEL_NAME) is not None
    has_member_role = discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME) is not None
    return has_category and has_auth_ch and has_review_ch and has_member_role


@bot.command(name="setup")
@is_owner()
async def setup_cmd(ctx: commands.Context):
    """One-time setup: creates auth category, channels, roles, and permissions."""
    guild = ctx.guild

    if is_setup_done(guild):
        await ctx.send(
            "⚠️ Система авторизации уже настроена на этом сервере.\n"
            "Все каналы, роли и категория уже существуют. Повторная настройка не требуется."
        )
        return

    await ctx.send("⚙️ Настройка системы авторизации...")

    # 1. Create the Member role
    member_role = discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME)
    if not member_role:
        member_role = await guild.create_role(
            name=MEMBER_ROLE_NAME,
            color=discord.Color.green(),
            hoist=True,
            reason="Системная роль авторизации",
        )
        await ctx.send(f"✅ Создана роль **{MEMBER_ROLE_NAME}**")
    else:
        await ctx.send(f"ℹ️ Роль **{MEMBER_ROLE_NAME}** уже существует")

    # 2. Create the Unverified role
    unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
    if not unverified_role:
        unverified_role = await guild.create_role(
            name=UNVERIFIED_ROLE_NAME,
            color=discord.Color.light_grey(),
            reason="Системная роль неверифицированных участников",
        )
        await ctx.send(f"✅ Создана роль **{UNVERIFIED_ROLE_NAME}**")
    else:
        await ctx.send(f"ℹ️ Роль **{UNVERIFIED_ROLE_NAME}** уже существует")

    everyone = guild.default_role

    # 3. Hide all existing channels from @everyone; grant access to Участник
    await ctx.send("⚙️ Настройка прав доступа к каналам...")
    for channel in guild.channels:
        if channel.name in (AUTH_CHANNEL_NAME, REVIEW_CHANNEL_NAME):
            continue
        try:
            await channel.set_permissions(everyone, view_channel=False)
            await channel.set_permissions(member_role, view_channel=True)
        except discord.Forbidden:
            pass

    # 4. Create auth category
    auth_category = discord.utils.get(guild.categories, name=AUTH_CATEGORY_NAME)
    if not auth_category:
        cat_overwrites = {
            everyone: discord.PermissionOverwrite(
                view_channel=True, send_messages=False
            ),
            member_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            ),
        }
        auth_category = await guild.create_category(
            name=AUTH_CATEGORY_NAME,
            overwrites=cat_overwrites,
            reason="Категория авторизации",
        )
        await ctx.send(f"✅ Создана категория **{AUTH_CATEGORY_NAME}**")
    else:
        await ctx.send(f"ℹ️ Категория **{AUTH_CATEGORY_NAME}** уже существует")

    # 5. Create auth channel
    auth_channel = discord.utils.get(guild.text_channels, name=AUTH_CHANNEL_NAME)
    if not auth_channel:
        ch_overwrites = {
            everyone: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                read_message_history=True,
            ),
            member_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            ),
        }
        auth_channel = await guild.create_text_channel(
            name=AUTH_CHANNEL_NAME,
            category=auth_category,
            overwrites=ch_overwrites,
            topic="Нажмите кнопку, чтобы получить доступ к серверу",
            reason="Канал авторизации",
        )
        await ctx.send(f"✅ Создан канал {auth_channel.mention}")
    else:
        await ctx.send(f"ℹ️ Канал **#{AUTH_CHANNEL_NAME}** уже существует")

    # 6. Create review channel (staff only)
    review_channel = discord.utils.get(guild.text_channels, name=REVIEW_CHANNEL_NAME)
    if not review_channel:
        rev_overwrites: dict = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            member_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            ),
        }
        for role in guild.roles:
            if role.permissions.administrator and role != everyone:
                rev_overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True
                )

        review_channel = await guild.create_text_channel(
            name=REVIEW_CHANNEL_NAME,
            category=auth_category,
            overwrites=rev_overwrites,
            topic="Заявки на авторизацию — только для модераторов",
            reason="Канал рассмотрения заявок",
        )
        await ctx.send(f"✅ Создан канал {review_channel.mention}")
    else:
        await ctx.send(f"ℹ️ Канал **#{REVIEW_CHANNEL_NAME}** уже существует")

    # 7. Post the auth embed with button
    await auth_channel.purge(limit=20, check=lambda m: m.author == guild.me)

    embed = discord.Embed(
        title="🔐 Авторизация",
        description=(
            "Добро пожаловать на сервер!\n\n"
            "Для получения доступа к каналам нажмите кнопку ниже.\n"
            "Ваша заявка будет рассмотрена модераторами в ближайшее время."
        ),
        color=0x3498DB,
    )
    embed.set_footer(text="После одобрения заявки вам откроется доступ ко всем каналам.")

    await auth_channel.send(embed=embed, view=PersistentAuthView())

    await ctx.send(
        f"🎉 **Настройка завершена!**\n"
        f"• Канал авторизации: {auth_channel.mention}\n"
        f"• Канал заявок (только модераторы): {review_channel.mention}\n"
        f"• Роль авторизованных участников: **{MEMBER_ROLE_NAME}**\n"
        f"• Роль неверифицированных: **{UNVERIFIED_ROLE_NAME}**\n\n"
        f"Новые участники видят **только** канал авторизации."
    )


@setup_cmd.error
async def setup_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("❌ Эта команда доступна только владельцу бота.")
    else:
        await ctx.send(f"❌ Ошибка: {error}")


keep_alive()
bot.run(TOKEN)
