import discord
from discord import app_commands
from discord.ext import commands

from helpers import RANKS, get_member_rank_level
from cogs.proof import _post_form

OWNER_ROLES = [
    "Куратор Главных модераторов",
    "Зам. Руководителя модерации",
    "Руководитель модерации",
]


# ─── Proof modal ──────────────────────────────────────────────────────────────

class ProofContextModal(discord.ui.Modal, title="Выдать наказание"):
    rule_input = discord.ui.TextInput(
        label="Пункт правил",
        placeholder="Например: 2.1",
        max_length=8,
        required=True,
    )
    punishment_input = discord.ui.TextInput(
        label="Наказание",
        placeholder="Предупреждение / Мут 90 минут / Бан 7 дней...",
        max_length=64,
        required=True,
    )
    evidence_input = discord.ui.TextInput(
        label="Ссылка на доказательство (необязательно)",
        placeholder="https://cdn.discordapp.com/...",
        max_length=500,
        required=False,
    )

    def __init__(self, target: discord.Member):
        super().__init__()
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await _post_form(
            interaction,
            moderator=interaction.user,
            violator=self.target,
            rule_id=self.rule_input.value.strip(),
            punishment=self.punishment_input.value.strip(),
            form_type="proof",
            evidence_url=self.evidence_input.value.strip(),
        )


# ─── Cog ─────────────────────────────────────────────────────────────────────

class ContextMenuCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self._proof_menu = app_commands.ContextMenu(
            name="📋 Выдать наказание",
            callback=self._proof_context_callback,
        )
        self.bot.tree.add_command(self._proof_menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(self._proof_menu.name, type=self._proof_menu.type)

    async def _proof_context_callback(
        self, interaction: discord.Interaction, member: discord.Member
    ):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                "❌ У вас нет прав для этой команды.", ephemeral=True
            )
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ Нельзя выдать наказание самому себе.", ephemeral=True
            )
            return
        await interaction.response.send_modal(ProofContextModal(member))


async def setup(bot: commands.Bot):
    await bot.add_cog(ContextMenuCog(bot))
