"""
Инструменты модерации: /warns, /myforms, /leaderboard, /checkuser,
/remind, /inactive, /transfer
"""
import re
import time
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from helpers import RULES, RANKS, get_member_rank_level
from db import _conn, all_user_servers, get_user_server, set_user_server, \
    add_reminder, pop_due_reminders, list_reminders
from cogs.servers import is_server_role

LEADER_MIN_LEVEL = 4  # КМ+ для /inactive


def _status_emoji(status: str) -> str:
    return {"sent": "📨", "approved": "✅", "rejected": "❌"}.get(status, "▫️")


def _simple_v2(title: str, body: str, color: int,
               thumbnail_url: str = "") -> discord.ui.LayoutView:
    items: list = []
    if thumbnail_url:
        items.append(discord.ui.Section(
            discord.ui.TextDisplay(f"## {title}"),
            accessory=discord.ui.Thumbnail(thumbnail_url),
        ))
    else:
        items.append(discord.ui.TextDisplay(f"## {title}"))
    items.append(discord.ui.Separator())
    items.append(discord.ui.TextDisplay(body))
    items.append(discord.ui.TextDisplay(f"-# <t:{int(time.time())}:f>"))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_color=color))
    return view


def _parse_duration(raw: str) -> int | None:
    """'30м'/'2ч'/'1д'/'45m'/'2h'/'1d' → секунды. None если не распознано."""
    m = re.fullmatch(r"(\d+)\s*([мчдmhd])", raw.strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    mult = {"м": 60, "m": 60, "ч": 3600, "h": 3600, "д": 86400, "d": 86400}[unit]
    sec = n * mult
    if sec < 60 or sec > 30 * 86400:
        return None
    return sec


class ModToolsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.reminder_check_loop.start()

    def cog_unload(self):
        self.reminder_check_loop.cancel()

    # ─── /warns ───────────────────────────────────────────────────────────────

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="warns", description="История наказаний пользователя по формам")
    @app_commands.describe(user="Пользователь")
    async def warns_cmd(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(ephemeral=True)
        with _conn() as c:
            rows = c.execute(
                "SELECT mod_id, status, rule_id, punishment, ts FROM form_stats "
                "WHERE violator_id=? AND status IN ('sent','approved') "
                "ORDER BY ts DESC LIMIT 15",
                (user.id,),
            ).fetchall()
            total = c.execute(
                "SELECT COUNT(*) FROM form_stats WHERE violator_id=? AND status IN ('sent','approved')",
                (user.id,),
            ).fetchone()[0]

        if not rows:
            await interaction.followup.send(
                f"✅ У {user.mention} нет зафиксированных наказаний.", ephemeral=True
            )
            return

        lines = []
        for r in rows:
            rule_text = RULES.get(r["rule_id"], "")
            rule = f"{r['rule_id']}" + (f" ({rule_text})" if rule_text else "")
            lines.append(
                f"{_status_emoji(r['status'])} <t:{r['ts']}:d> — **{r['punishment'] or '?'}** "
                f"| {rule} | мод: <@{r['mod_id']}>"
            )
        if total > len(rows):
            lines.append(f"-# …и ещё {total - len(rows)} (показаны последние {len(rows)})")

        view = _simple_v2(
            f"📕 Наказания: {user.display_name} ({total})",
            "\n".join(lines), 0xE74C3C,
            thumbnail_url=str(user.display_avatar.url) if user.display_avatar else "",
        )
        await interaction.followup.send(view=view, ephemeral=True)

    # ─── /myforms ─────────────────────────────────────────────────────────────

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="myforms", description="Мои формы за день / неделю / месяц")
    async def myforms_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid = interaction.user.id
        now = datetime.now()
        day_ts   = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        week_ts  = day_ts - 6 * 86400
        month_ts = day_ts - 29 * 86400

        def counts(since: int) -> tuple[int, int, int]:
            with _conn() as c:
                row = c.execute(
                    "SELECT COUNT(*) as total, "
                    "SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) as ok, "
                    "SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) as bad "
                    "FROM form_stats WHERE mod_id=? AND ts>=? AND status!='sent'",
                    (uid, since),
                ).fetchone()
                sent = c.execute(
                    "SELECT COUNT(*) FROM form_stats WHERE mod_id=? AND ts>=? AND status='sent'",
                    (uid, since),
                ).fetchone()[0]
            return sent, row["ok"] or 0, row["bad"] or 0

        with _conn() as c:
            by_type = c.execute(
                "SELECT form_type, COUNT(*) as cnt FROM form_stats "
                "WHERE mod_id=? AND ts>=? AND status='sent' GROUP BY form_type",
                (uid, month_ts),
            ).fetchall()

        d = counts(day_ts); w = counts(week_ts); m = counts(month_ts); a = counts(0)
        type_names = {"proof": "📋 Proof", "banform": "⚖️ Бан-формы", "gbanform": "🌐 Гбан-формы"}
        type_lines = "\n".join(
            f"{type_names.get(r['form_type'], r['form_type'])}: **{r['cnt']}**" for r in by_type
        ) or "_за месяц форм нет_"

        body = (
            f"**Сегодня:** 📨 {d[0]} | ✅ {d[1]} | ❌ {d[2]}\n"
            f"**Неделя:** 📨 {w[0]} | ✅ {w[1]} | ❌ {w[2]}\n"
            f"**Месяц:** 📨 {m[0]} | ✅ {m[1]} | ❌ {m[2]}\n"
            f"**Всё время:** 📨 {a[0]} | ✅ {a[1]} | ❌ {a[2]}\n\n"
            f"**По типам (месяц):**\n{type_lines}"
        )
        view = _simple_v2(
            f"📊 Мои формы — {interaction.user.display_name}", body, 0x3498DB,
            thumbnail_url=str(interaction.user.display_avatar.url) if interaction.user.display_avatar else "",
        )
        await interaction.followup.send(view=view, ephemeral=True)

    # ─── /leaderboard ─────────────────────────────────────────────────────────

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="leaderboard", description="Топ модераторов по формам")
    @app_commands.describe(period="Период")
    @app_commands.choices(period=[
        app_commands.Choice(name="Неделя", value="week"),
        app_commands.Choice(name="Месяц",  value="month"),
    ])
    async def leaderboard_cmd(self, interaction: discord.Interaction, period: str = "week"):
        await interaction.response.defer(ephemeral=True)
        days = 7 if period == "week" else 30
        since = int(time.time()) - days * 86400
        with _conn() as c:
            rows = c.execute(
                "SELECT mod_id, "
                "SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) as sent, "
                "SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) as ok "
                "FROM form_stats WHERE ts>=? GROUP BY mod_id "
                "HAVING sent > 0 ORDER BY sent DESC LIMIT 10",
                (since,),
            ).fetchall()

        if not rows:
            await interaction.followup.send("📭 За этот период форм не было.", ephemeral=True)
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows):
            prefix = medals[i] if i < 3 else f"`{i + 1:2}.`"
            lines.append(f"{prefix} <@{r['mod_id']}> — 📨 {r['sent']} | ✅ {r['ok']}")

        title = "🏆 Топ модераторов — " + ("неделя" if period == "week" else "месяц")
        view = _simple_v2(title, "\n".join(lines), 0xF1C40F)
        await interaction.followup.send(view=view, ephemeral=True)

    # ─── /checkuser ───────────────────────────────────────────────────────────

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="checkuser", description="Досье: сервер, ранг, авторизация, наказания")
    @app_commands.describe(user="Пользователь")
    async def checkuser_cmd(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(ephemeral=True)

        server = get_user_server(user.id)
        rank = next((r.name for r in reversed(user.roles) if r.name in RANKS), None)
        server_roles = [r.name for r in user.roles if is_server_role(r.name)]

        with _conn() as c:
            auth_row = c.execute(
                "SELECT auth_at FROM user_servers WHERE user_id=?", (str(user.id),)
            ).fetchone()
            viol_total = c.execute(
                "SELECT COUNT(*) FROM form_stats WHERE violator_id=? AND status IN ('sent','approved')",
                (user.id,),
            ).fetchone()[0]
            sent_total = c.execute(
                "SELECT COUNT(*) FROM form_stats WHERE mod_id=? AND status='sent'", (user.id,)
            ).fetchone()[0]
            last_viol = c.execute(
                "SELECT punishment, rule_id, ts FROM form_stats "
                "WHERE violator_id=? AND status IN ('sent','approved') ORDER BY ts DESC LIMIT 3",
                (user.id,),
            ).fetchall()

        lines = [
            f"**Пользователь:** {user.mention} (`{user.id}`)",
            f"**Сервер:** {server or ', '.join(server_roles) or '—'}",
            f"**Ранг:** {rank or '—'}",
        ]
        if auth_row and auth_row["auth_at"]:
            lines.append(f"**Авторизован:** <t:{auth_row['auth_at']}:d>")
        lines.append(f"**На сервере Discord с:** <t:{int(user.joined_at.timestamp())}:d>" if user.joined_at else "")
        lines.append("")
        lines.append(f"**Отправлено форм (как модератор):** {sent_total}")
        lines.append(f"**Получено наказаний:** {viol_total}")
        if last_viol:
            lines.append("**Последние наказания:**")
            for r in last_viol:
                lines.append(f"-# <t:{r['ts']}:d> — {r['punishment'] or '?'} ({r['rule_id'] or '?'})")

        view = _simple_v2(
            f"🔍 Досье: {user.display_name}",
            "\n".join(l for l in lines if l is not None), 0x9B59B6,
            thumbnail_url=str(user.display_avatar.url) if user.display_avatar else "",
        )
        await interaction.followup.send(view=view, ephemeral=True)

    # ─── /remind ──────────────────────────────────────────────────────────────

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="remind", description="Личное напоминание в ЛС")
    @app_commands.describe(time="Через сколько: 30м / 2ч / 1д", text="Текст напоминания")
    async def remind_cmd(self, interaction: discord.Interaction, time: str, text: str):
        sec = _parse_duration(time)
        if sec is None:
            await interaction.response.send_message(
                "❌ Неверный формат времени. Примеры: `30м`, `2ч`, `1д` (от 1 минуты до 30 дней).",
                ephemeral=True,
            )
            return
        remind_at = int(datetime.now().timestamp()) + sec
        add_reminder(interaction.user.id, remind_at, text[:500])
        await interaction.response.send_message(
            f"⏰ Напомню <t:{remind_at}:R>: **{text[:200]}**\n"
            "-# Напоминание придёт в ЛС — проверьте, что ЛС от участников сервера открыты.",
            ephemeral=True,
        )

    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name="reminders", description="Мои активные напоминания")
    async def reminders_cmd(self, interaction: discord.Interaction):
        rows = list_reminders(interaction.user.id)
        if not rows:
            await interaction.response.send_message("📭 Активных напоминаний нет.", ephemeral=True)
            return
        lines = [f"⏰ <t:{r['remind_at']}:R> — {r['text'][:100]}" for r in rows[:15]]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @tasks.loop(seconds=60)
    async def reminder_check_loop(self):
        due = pop_due_reminders(int(datetime.now().timestamp()))
        for r in due:
            user = self.bot.get_user(r["user_id"])
            if not user:
                try:
                    user = await self.bot.fetch_user(r["user_id"])
                except (discord.NotFound, discord.HTTPException):
                    continue
            try:
                await user.send(f"⏰ **Напоминание:** {r['text']}")
            except (discord.Forbidden, discord.HTTPException):
                pass

    @reminder_check_loop.before_loop
    async def before_reminder_check(self):
        await self.bot.wait_until_ready()

    # ─── /inactive ────────────────────────────────────────────────────────────

    @app_commands.default_permissions(manage_roles=True)
    @app_commands.command(name="inactive", description="Модераторы без форм N+ дней (КМ+)")
    @app_commands.describe(days="Минимум дней без форм (по умолчанию 7)")
    async def inactive_cmd(self, interaction: discord.Interaction, days: int = 7):
        await interaction.response.defer(ephemeral=True)
        if get_member_rank_level(interaction.user) < LEADER_MIN_LEVEL:
            await interaction.followup.send("❌ Требуется ранг **Куратор модерации**+.", ephemeral=True)
            return
        days = max(1, min(days, 90))
        cutoff = int(time.time()) - days * 86400

        mods = all_user_servers()
        if not mods:
            await interaction.followup.send("📭 В базе нет модераторов.", ephemeral=True)
            return

        with _conn() as c:
            rows = c.execute(
                "SELECT mod_id, MAX(ts) as last_ts FROM form_stats GROUP BY mod_id"
            ).fetchall()
        last_form = {r["mod_id"]: r["last_ts"] for r in rows}

        inactive: list[tuple[str, str, int]] = []  # (uid, server, last_ts)
        for uid_str, server in mods.items():
            uid = int(uid_str)
            ts = last_form.get(uid, 0)
            if ts < cutoff:
                inactive.append((uid_str, server, ts))

        if not inactive:
            await interaction.followup.send(
                f"✅ Все модераторы отправляли формы за последние **{days}** дней.", ephemeral=True
            )
            return

        inactive.sort(key=lambda x: x[2])  # самые давние сверху
        lines = []
        for uid_str, server, ts in inactive[:25]:
            last = f"<t:{ts}:R>" if ts else "никогда"
            lines.append(f"• <@{uid_str}> (сервер {server}) — последняя форма: {last}")
        if len(inactive) > 25:
            lines.append(f"-# …и ещё {len(inactive) - 25}")

        view = _simple_v2(
            f"😴 Неактивные модераторы ({days}+ дней): {len(inactive)}",
            "\n".join(lines), 0xE67E22,
        )
        await interaction.followup.send(view=view, ephemeral=True)

    # ─── /transfer ────────────────────────────────────────────────────────────

    @app_commands.default_permissions(manage_roles=True)
    @app_commands.command(name="transfer", description="Перевести модератора на другой сервер (КМ+)")
    @app_commands.describe(user="Модератор", server="Новый номер сервера (1–90)")
    async def transfer_cmd(self, interaction: discord.Interaction, user: discord.Member, server: str):
        await interaction.response.defer(ephemeral=True)
        if get_member_rank_level(interaction.user) < LEADER_MIN_LEVEL:
            await interaction.followup.send("❌ Требуется ранг **Куратор модерации**+.", ephemeral=True)
            return
        if not (server.isdigit() and 1 <= int(server) <= 90):
            await interaction.followup.send(
                f"❌ Некорректный номер сервера: **{server}** (1–90).", ephemeral=True
            )
            return

        guild = interaction.guild
        old_server = get_user_server(user.id) or "—"

        old_roles = [r for r in user.roles if is_server_role(r.name)]
        new_role = discord.utils.get(guild.roles, name=server)
        if new_role is None:
            try:
                new_role = await guild.create_role(name=server, reason=f"Перевод {user} на сервер {server}")
            except (discord.Forbidden, discord.HTTPException) as e:
                await interaction.followup.send(f"❌ Не удалось создать роль **{server}**: {e}", ephemeral=True)
                return

        try:
            if old_roles:
                await user.remove_roles(*old_roles, reason=f"Перевод на сервер {server}")
            await user.add_roles(new_role, reason=f"Перевод на сервер {server} ({interaction.user})")
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.followup.send(f"❌ Не удалось изменить роли: {e}", ephemeral=True)
            return

        set_user_server(user.id, server)

        view = _simple_v2(
            "🔄 Перевод модератора",
            f"**Модератор:** {user.mention}\n"
            f"**Сервер:** {old_server} → **{server}**\n"
            f"**Перевёл:** {interaction.user.mention}",
            0x2ECC71,
            thumbnail_url=str(user.display_avatar.url) if user.display_avatar else "",
        )
        await interaction.followup.send(view=view, ephemeral=True)
        try:
            await user.send(f"🔄 Вас перевели на сервер **{server}** (был: {old_server}).")
        except (discord.Forbidden, discord.HTTPException):
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ModToolsCog(bot))
