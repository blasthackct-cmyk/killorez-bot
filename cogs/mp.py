import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
import asyncio
from datetime import datetime
import logging
from typing import Literal, Optional

from utils.database import fetch_one, fetch_all, execute_query
from utils.embeds import (
    create_embed,
    create_success_embed,
    create_error_embed,
    create_warning_embed,
    json_to_list,
    list_to_json,
    WATERMARK,
    EMBED_COLOR,
    EMBED_GREEN,
    EMBED_RED,
    EMBED_PURPLE,
    EMBED_ORANGE
)

log = logging.getLogger('bot.mp')

# Списки карт
VZP_MAPS = [
    "Байкерка", "Большой Миррор", "Веспуччи", "Ветряки",
    "Киностудия", "Лесопилка", "Миррор", "Муравейник",
    "Мусорка", "Мясо", "Нефть", "Палетка",
    "Порт биз", "Сендик", "Стройка", "Татушка"
]

VZH_MAPS = [
    "ВЗХ", "Порт", "Стройка биз"
]

MAPS_DIR = os.path.join(os.getcwd(), "assets", "maps")


async def is_mp_organizer(interaction: discord.Interaction) -> bool:
    """
    Проверяет, является ли пользователь администратором или имеет хотя бы одну
    из настроенных выборочных ролей организатора МП.
    """
    if not interaction.guild:
        return False

    member = interaction.user
    if not isinstance(member, discord.Member):
        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            try:
                member = await interaction.guild.fetch_member(interaction.user.id)
            except Exception:
                return False

    if member.guild_permissions.administrator:
        return True

    settings = await fetch_one("SELECT organizer_roles FROM mp_settings WHERE guild_id = ?", (interaction.guild_id,))
    if settings and settings['organizer_roles']:
        role_ids = json_to_list(settings['organizer_roles'])
        user_role_ids = [r.id for r in member.roles]
        if any(rid in user_role_ids for rid in role_ids):
            return True

    return False


def find_map_file(map_name: str):
    """Ищет файл карты в assets/maps/ по названию."""
    if not os.path.exists(MAPS_DIR):
        return None

    clean_target = map_name.lower().replace(" ", "_").strip()
    valid_exts = [".png", ".jpg", ".jpeg", ".webp"]

    for filename in os.listdir(MAPS_DIR):
        name, ext = os.path.splitext(filename)
        if ext.lower() in valid_exts:
            clean_name = name.lower().replace(" ", "_").strip()
            if clean_name == clean_target:
                return os.path.join(MAPS_DIR, filename)

    return None


# ==============================================================================
# UI VIEWS
# ==============================================================================

class MpRoleSelect(discord.ui.RoleSelect):
    """Выпадающее меню для выбора нескольких ролей организаторов сразу."""
    def __init__(self, current_roles: list):
        default_roles = []
        super().__init__(
            placeholder="Выберите роли организаторов (можно несколько)...",
            min_values=0,
            max_values=25
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Только Администратор сервера может настраивать роли!", ephemeral=True)
            return

        selected_role_ids = [role.id for role in self.values]
        await execute_query(
            "INSERT INTO mp_settings (guild_id, organizer_roles) VALUES (?, ?) ON CONFLICT (guild_id) DO UPDATE SET organizer_roles = EXCLUDED.organizer_roles",
            (interaction.guild_id, list_to_json(selected_role_ids))
        )

        if selected_role_ids:
            mentions = ", ".join([f"<@&{rid}>" for rid in selected_role_ids])
            embed = create_success_embed(
                "Роли организаторов обновлены",
                f"Назначено ролей организаторов: **{len(selected_role_ids)}**\n{mentions}\n\n"
                f"Пользователи с этими ролями могут открывать наборы, запускать МП, выбирать карты и позиции."
            )
        else:
            embed = create_warning_embed(
                "Роли организаторов сброшены",
                "Список ролей очищен. Теперь управлять МП могут только Администраторы сервера."
            )

        await interaction.response.edit_message(embed=embed, view=None)


class MpRoleSelectView(discord.ui.View):
    def __init__(self, current_roles: list):
        super().__init__(timeout=300)
        self.add_item(MpRoleSelect(current_roles))


class MpRegistrationView(discord.ui.View):
    """View для набора участников МП с кнопками и меню перемещения в основу."""

    def __init__(self, session_id: int, cog):
        super().__init__(timeout=None)
        self.session_id = session_id
        self.cog = cog

    async def update_view_items(self, session: dict, guild: discord.Guild):
        """Динамически обновляет выпадающий список поднятия в основу."""
        self.clear_items()

        # Кнопка записи / выписки
        signup_btn = discord.ui.Button(
            label="Записаться / Выписаться",
            emoji="📝",
            style=discord.ButtonStyle.primary,
            custom_id=f"mp_signup_{self.session_id}"
        )
        signup_btn.callback = self.on_signup_clicked
        self.add_item(signup_btn)

        reserve_list = json_to_list(session['reserve_list'])
        main_list = json_to_list(session['main_list'])
        main_slots = session['main_slots']

        # Если есть резерв и в основе есть места — выпадающее меню для организатора
        if reserve_list and len(main_list) < main_slots:
            options = []
            for uid in reserve_list[:25]:  # Discord лимит 25 пунктов
                member = guild.get_member(uid)
                name = member.display_name if member else f"ID: {uid}"
                options.append(discord.SelectOption(
                    label=name[:100],
                    value=str(uid),
                    emoji="⬆️"
                ))

            if options:
                promote_select = discord.ui.Select(
                    placeholder="⬆️ Поднять в основной состав (Организатор)",
                    min_values=1,
                    max_values=1,
                    options=options,
                    custom_id=f"mp_promote_{self.session_id}"
                )
                promote_select.callback = self.on_promote_selected
                self.add_item(promote_select)

        # Кнопка отмены набора (для организаторов)
        cancel_btn = discord.ui.Button(
            label="Отменить набор",
            emoji="❌",
            style=discord.ButtonStyle.danger,
            custom_id=f"mp_cancel_{self.session_id}"
        )
        cancel_btn.callback = self.on_cancel_clicked
        self.add_item(cancel_btn)

    async def on_signup_clicked(self, interaction: discord.Interaction):
        session = await fetch_one("SELECT * FROM mp_sessions WHERE session_id = ?", (self.session_id,))
        if not session or not session['is_active']:
            await interaction.response.send_message("Этот набор уже завершён или не активен!", ephemeral=True)
            return

        main_list = json_to_list(session['main_list'])
        reserve_list = json_to_list(session['reserve_list'])
        user_id = interaction.user.id

        # Если уже записан в основу или резерв — выписываем
        if user_id in main_list:
            main_list.remove(user_id)
            await execute_query("UPDATE mp_sessions SET main_list = ? WHERE session_id = ?", (list_to_json(main_list), self.session_id))
            await interaction.response.send_message("Вы выписались из основного состава.", ephemeral=True)
        elif user_id in reserve_list:
            reserve_list.remove(user_id)
            await execute_query("UPDATE mp_sessions SET reserve_list = ? WHERE session_id = ?", (list_to_json(reserve_list), self.session_id))
            await interaction.response.send_message("Вы выписались из запасного состава.", ephemeral=True)
        else:
            # Записываем в резерв
            if len(reserve_list) >= session['reserve_slots']:
                await interaction.response.send_message("Запасной состав уже полностью заполнен!", ephemeral=True)
                return
            reserve_list.append(user_id)
            await execute_query("UPDATE mp_sessions SET reserve_list = ? WHERE session_id = ?", (list_to_json(reserve_list), self.session_id))
            await interaction.response.send_message("Вы успешно записались в запасной состав! Ожидайте решения организатора.", ephemeral=True)

        await self.cog.refresh_nabor_message(self.session_id, interaction.guild)

    async def on_promote_selected(self, interaction: discord.Interaction):
        if not await is_mp_organizer(interaction):
            await interaction.response.send_message("❌ У вас нет прав организатора МП для перемещения участников!", ephemeral=True)
            return

        session = await fetch_one("SELECT * FROM mp_sessions WHERE session_id = ?", (self.session_id,))
        if not session or not session['is_active']:
            await interaction.response.send_message("Набор не активен!", ephemeral=True)
            return

        main_list = json_to_list(session['main_list'])
        reserve_list = json_to_list(session['reserve_list'])

        if len(main_list) >= session['main_slots']:
            await interaction.response.send_message("Основной состав уже полон!", ephemeral=True)
            return

        selected_uid = int(interaction.data['values'][0])
        if selected_uid in reserve_list:
            reserve_list.remove(selected_uid)
            main_list.append(selected_uid)
            await execute_query(
                "UPDATE mp_sessions SET main_list = ?, reserve_list = ? WHERE session_id = ?",
                (list_to_json(main_list), list_to_json(reserve_list), self.session_id)
            )
            member = interaction.guild.get_member(selected_uid)
            name = member.mention if member else f"<@{selected_uid}>"
            await interaction.response.send_message(f"Участник {name} перемещён в **основной состав**! ⬆️", ephemeral=True)
            await self.cog.refresh_nabor_message(self.session_id, interaction.guild)
        else:
            await interaction.response.send_message("Участник больше не находится в запасном списке.", ephemeral=True)

    async def on_cancel_clicked(self, interaction: discord.Interaction):
        if not await is_mp_organizer(interaction):
            await interaction.response.send_message("❌ Только организаторы могут отменить набор!", ephemeral=True)
            return

        await execute_query("UPDATE mp_sessions SET is_active = 0 WHERE session_id = ?", (self.session_id,))
        embed = create_error_embed("Набор отменён", f"Организатор {interaction.user.mention} отменил этот набор.")
        await interaction.response.edit_message(embed=embed, view=None)


class MpTypeSelectView(discord.ui.View):
    """View для выбора типа мероприятия: VZP или ВЗХ."""
    def __init__(self, cog, session_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.session_id = session_id

    @discord.ui.button(label="ВЗП (16 карт)", style=discord.ButtonStyle.primary, emoji="⚔️", custom_id="mp_type_vzp")
    async def vzp_clicked(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_mp_organizer(interaction):
            await interaction.response.send_message("❌ Только пользователи с правами/ролью организатора могут выбирать тип мероприятия!", ephemeral=True)
            return

        await execute_query("UPDATE mp_sessions SET event_type = 'ВЗП' WHERE session_id = ?", (self.session_id,))

        embed = create_embed(
            title="⚔️ ВЫБРАНО МЕРОПРИЯТИЕ: ВЗП",
            description=(
                f"Организатор {interaction.user.mention} выбрал формат **ВЗП**.\n\n"
                f"🛡️ **Выберите карту ниже (доступно только организаторам):**\n"
                f"После выбора карты запустите `/mp position [кол-во]`, чтобы открыть табло позиций.\n"
                f"Для досрочного завершения используйте `/mp stop`."
            ),
            color=EMBED_PURPLE
        )
        map_view = MpMapSelectView(self.cog, self.session_id, VZP_MAPS)
        await interaction.response.edit_message(embed=embed, view=map_view)

    @discord.ui.button(label="ВЗХ (3 карты)", style=discord.ButtonStyle.success, emoji="🛡️", custom_id="mp_type_vzh")
    async def vzh_clicked(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_mp_organizer(interaction):
            await interaction.response.send_message("❌ Только пользователи с правами/ролью организатора могут выбирать тип мероприятия!", ephemeral=True)
            return

        await execute_query("UPDATE mp_sessions SET event_type = 'ВЗХ' WHERE session_id = ?", (self.session_id,))

        embed = create_embed(
            title="🛡️ ВЫБРАНО МЕРОПРИЯТИЕ: ВЗХ",
            description=(
                f"Организатор {interaction.user.mention} выбрал формат **ВЗХ**.\n\n"
                f"🛡️ **Выберите карту ниже (доступно только организаторам):**\n"
                f"После выбора карты запустите `/mp position [кол-во]`, чтобы открыть табло позиций.\n"
                f"Для досрочного завершения используйте `/mp stop`."
            ),
            color=EMBED_GREEN
        )
        map_view = MpMapSelectView(self.cog, self.session_id, VZH_MAPS)
        await interaction.response.edit_message(embed=embed, view=map_view)


class MpMapButton(discord.ui.Button):
    """Отдельная кнопка карты."""
    def __init__(self, map_name: str, cog, session_id: int, row: int):
        super().__init__(label=map_name, style=discord.ButtonStyle.secondary, row=row)
        self.map_name = map_name
        self.cog = cog
        self.session_id = session_id

    async def callback(self, interaction: discord.Interaction):
        # Строгая проверка: карту могут выбирать ТОЛЬКО администраторы и выборочные роли организаторов
        if not await is_mp_organizer(interaction):
            await interaction.response.send_message(
                "❌ **Доступ запрещён!** Выбирать карту могут только организаторы с назначенной ролью или администраторы.",
                ephemeral=True
            )
            return

        await execute_query("UPDATE mp_sessions SET selected_map = ? WHERE session_id = ?", (self.map_name, self.session_id))

        # Ищем изображение карты
        map_path = find_map_file(self.map_name)

        embed = create_embed(
            title=f"🗺️ Выбрана карта: {self.map_name}",
            description=(
                f"Организатор {interaction.user.mention} утвердил карту **{self.map_name}**.\n\n"
                f"👉 Запустите `/mp position [кол-во]`, чтобы участники основы могли занимать позиции!\n"
                f"👉 Для завершения МП напишите `/mp stop`."
            ),
            color=EMBED_PURPLE
        )

        if map_path and os.path.exists(map_path):
            filename = os.path.basename(map_path)
            file = discord.File(map_path, filename=filename)
            embed.set_image(url=f"attachment://{filename}")
            await interaction.response.send_message(embed=embed, file=file)
        else:
            embed.set_footer(text=f"{WATERMARK} • Файл карты {self.map_name} пока не загружен в assets/maps/")
            await interaction.response.send_message(embed=embed)


class MpEndButton(discord.ui.Button):
    """Кнопка досрочного завершения МП."""
    def __init__(self, cog, session_id: int, row: int):
        super().__init__(label="🛑 Завершить МП", style=discord.ButtonStyle.danger, row=row)
        self.cog = cog
        self.session_id = session_id

    async def callback(self, interaction: discord.Interaction):
        if not await is_mp_organizer(interaction):
            await interaction.response.send_message("❌ Только организаторы могут досрочно завершить мероприятие!", ephemeral=True)
            return

        session = await fetch_one("SELECT * FROM mp_sessions WHERE session_id = ? AND is_active = 1", (self.session_id,))
        if not session:
            await interaction.response.send_message("Мероприятие уже завершено.", ephemeral=True)
            return

        await interaction.response.send_message("Завершаю мероприятие и формирую архив...", ephemeral=True)
        await self.cog.archive_session(session)


class MpMapSelectView(discord.ui.View):
    """Сетка кнопок карт для выбранного режима + кнопка завершения."""
    def __init__(self, cog, session_id: int, maps: list):
        super().__init__(timeout=None)
        # До 4 кнопок в ряду
        for i, map_name in enumerate(maps):
            row = min(i // 4, 3)
            self.add_item(MpMapButton(map_name, cog, session_id, row=row))

        # Добавляем кнопку завершения МП на последний ряд
        self.add_item(MpEndButton(cog, session_id, row=4))


# ==============================================================================
# MAIN COG
# ==============================================================================

class MPCog(commands.Cog, name="MP"):
    def __init__(self, bot):
        self.bot = bot
        # Словарь активных сессий позиций: thread_id -> session_id
        self.active_position_sessions = {}
        self.archive_check_loop.start()

    def cog_unload(self):
        self.archive_check_loop.cancel()

    async def cog_load(self):
        try:
            sessions = await fetch_all("SELECT session_id, thread_id FROM mp_sessions WHERE is_active = 1 AND thread_id IS NOT NULL")
            for s in sessions:
                self.active_position_sessions[s['thread_id']] = s['session_id']
        except Exception as e:
            log.warning(f"Ошибка предварительной загрузки сессий МП: {e}")

    # ==================== ГРУППА КОМАНД /MP ====================
    mp = app_commands.Group(name="mp", description="Система проведения мероприятий (ВЗП / ВЗХ)")
    settings_group = app_commands.Group(name="settings", description="Настройки системы МП", parent=mp)

    # ------------------ НАСТРОЙКИ ------------------
    @settings_group.command(name="roles", description="Выбрать несколько ролей организаторов МП через удобное меню")
    @app_commands.checks.has_permissions(administrator=True)
    async def settings_roles_menu(self, interaction: discord.Interaction):
        settings = await fetch_one("SELECT * FROM mp_settings WHERE guild_id = ?", (interaction.guild_id,))
        current_roles = json_to_list(settings['organizer_roles']) if settings and settings['organizer_roles'] else []

        embed = create_embed(
            title="🛡️ Выбор ролей организаторов МП",
            description=(
                "Выберите одну или несколько ролей из выпадающего списка ниже.\n\n"
                "Люди с выбранными ролями смогут:\n"
                "• Открывать набор (`/mp nabor`)\n"
                "• Переносить людей из запаса в основу (`⬆️`)\n"
                "• Запускать ВЗП / ВЗХ (`/mp start`)\n"
                "• **Выбирать карты**\n"
                "• Запускать распределение позиций (`/mp position`)\n"
                "• Завершать мероприятие (`/mp stop`)"
            ),
            color=EMBED_COLOR
        )
        view = MpRoleSelectView(current_roles)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @settings_group.command(name="role_add", description="Добавить роль (или несколько ролей) организатора МП")
    @app_commands.describe(
        role="Первая роль организатора",
        role2="Вторая роль (необязательно)",
        role3="Третья роль (необязательно)",
        role4="Четвертая роль (необязательно)",
        role5="Пятая роль (необязательно)"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def settings_role_add(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        role2: Optional[discord.Role] = None,
        role3: Optional[discord.Role] = None,
        role4: Optional[discord.Role] = None,
        role5: Optional[discord.Role] = None
    ):
        settings = await fetch_one("SELECT * FROM mp_settings WHERE guild_id = ?", (interaction.guild_id,))
        roles = json_to_list(settings['organizer_roles']) if settings and settings['organizer_roles'] else []

        added = []
        for r in [role, role2, role3, role4, role5]:
            if r and r.id not in roles:
                roles.append(r.id)
                added.append(r.mention)

        if not added:
            embed = create_warning_embed("Внимание", "Все указанные роли уже являются организаторами.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await execute_query(
            "INSERT INTO mp_settings (guild_id, organizer_roles) VALUES (?, ?) ON CONFLICT (guild_id) DO UPDATE SET organizer_roles = EXCLUDED.organizer_roles",
            (interaction.guild_id, list_to_json(roles))
        )
        embed = create_success_embed(
            "Роли организаторов добавлены",
            f"Добавлены роли: {', '.join(added)}\nТеперь они могут выбирать карты и управлять проведением МП."
        )
        await interaction.response.send_message(embed=embed)

    @settings_group.command(name="role_remove", description="Удалить роль (или несколько ролей) организатора МП")
    @app_commands.describe(
        role="Первая роль для удаления",
        role2="Вторая роль (необязательно)",
        role3="Третья роль (необязательно)"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def settings_role_remove(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        role2: Optional[discord.Role] = None,
        role3: Optional[discord.Role] = None
    ):
        settings = await fetch_one("SELECT * FROM mp_settings WHERE guild_id = ?", (interaction.guild_id,))
        roles = json_to_list(settings['organizer_roles']) if settings and settings['organizer_roles'] else []

        removed = []
        for r in [role, role2, role3]:
            if r and r.id in roles:
                roles.remove(r.id)
                removed.append(r.mention)

        if not removed:
            embed = create_warning_embed("Внимание", "Ни одна из указанных ролей не найдена в списке организаторов.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await execute_query(
            "UPDATE mp_settings SET organizer_roles = ? WHERE guild_id = ?",
            (list_to_json(roles), interaction.guild_id)
        )
        embed = create_success_embed("Роли удалены", f"Удалены из организаторов МП: {', '.join(removed)}.")
        await interaction.response.send_message(embed=embed)

    @settings_group.command(name="archive_category", description="Установить категорию для архива МП")
    @app_commands.describe(category="Категория сервера для архивирования")
    @app_commands.checks.has_permissions(administrator=True)
    async def settings_archive_category(self, interaction: discord.Interaction, category: discord.CategoryChannel):
        await execute_query(
            "INSERT INTO mp_settings (guild_id, archive_category_id) VALUES (?, ?) ON CONFLICT (guild_id) DO UPDATE SET archive_category_id = EXCLUDED.archive_category_id",
            (interaction.guild_id, category.id)
        )
        embed = create_success_embed("Категория сохранена", f"Архив МП будет сохраняться в категорию **{category.name}**.")
        await interaction.response.send_message(embed=embed)

    @settings_group.command(name="view", description="Просмотреть текущие настройки МП")
    async def settings_view(self, interaction: discord.Interaction):
        settings = await fetch_one("SELECT * FROM mp_settings WHERE guild_id = ?", (interaction.guild_id,))
        role_mentions = "Не настроены (только Администраторы сервера)"
        archive_category = "Не настроена"
        roles = []

        if settings:
            roles = json_to_list(settings['organizer_roles'])
            if roles:
                role_mentions = "\n".join([f"• <@&{rid}>" for rid in roles])
            if settings['archive_category_id']:
                cat = interaction.guild.get_channel(settings['archive_category_id'])
                archive_category = cat.name if cat else f"ID: {settings['archive_category_id']} (не найдена)"

        embed = create_embed(title="⚙️ Настройки системы МП", color=EMBED_COLOR)
        embed.add_field(name=f"🛡️ Роли организаторов ({len(roles)})", value=role_mentions, inline=False)
        embed.add_field(name="📁 Категория архива", value=archive_category, inline=False)

        # Добавляем интерактивное меню настройки ролей
        view = MpRoleSelectView(roles)
        await interaction.response.send_message(embed=embed, view=view)

    # ------------------ /MP NABOR ------------------
    @mp.command(name="nabor", description="Открыть набор на мероприятие (ВЗП / ВЗХ)")
    @app_commands.describe(
        main_slots="Количество мест основного состава",
        reserve_slots="Количество мест запасного состава"
    )
    async def mp_nabor(self, interaction: discord.Interaction, main_slots: int, reserve_slots: int):
        if not await is_mp_organizer(interaction):
            embed = create_error_embed("Доступ запрещён", "У вас нет прав организатора МП для этой команды.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if main_slots <= 0 or reserve_slots <= 0:
            embed = create_error_embed("Ошибка", "Количество мест должно быть больше 0!")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        existing = await fetch_one(
            "SELECT * FROM mp_sessions WHERE guild_id = ? AND channel_id = ? AND is_active = 1",
            (interaction.guild_id, interaction.channel_id)
        )
        if existing:
            embed = create_warning_embed("Внимание", "В этом канале уже идёт активный набор или сессия МП! Завершите её командой `/mp stop` или используйте другой канал.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        session_id = await execute_query(
            "INSERT INTO mp_sessions (guild_id, channel_id, main_slots, reserve_slots, main_list, reserve_list, is_active) VALUES (?, ?, ?, ?, '[]', '[]', 1)",
            (interaction.guild_id, interaction.channel_id, main_slots, reserve_slots)
        )

        embed = self.build_nabor_embed(main_slots, reserve_slots, [], [], interaction.guild)
        view = MpRegistrationView(session_id, self)
        await view.update_view_items({
            'main_slots': main_slots,
            'reserve_slots': reserve_slots,
            'main_list': '[]',
            'reserve_list': '[]'
        }, interaction.guild)

        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()

        await execute_query("UPDATE mp_sessions SET message_id = ? WHERE session_id = ?", (msg.id, session_id))

    def build_nabor_embed(self, main_slots: int, reserve_slots: int, main_list: list, reserve_list: list, guild: discord.Guild) -> discord.Embed:
        embed = create_embed(
            title="⚔️ НАБОР НА МЕРОПРИЯТИЕ (ВЗП / ВЗХ)",
            description=(
                "Нажмите кнопку **«Записаться / Выписаться»** ниже, чтобы подать заявку в запасной состав.\n"
                "Организаторы перемещают проверенных бойцов из запаса в основной состав стрелочкой **⬆️**."
            ),
            color=EMBED_COLOR
        )

        if main_list:
            main_text = "\n".join([f"**{i+1}.** <@{uid}>" for i, uid in enumerate(main_list)])
        else:
            main_text = "*Список пуст*"

        if reserve_list:
            reserve_text = "\n".join([f"**{i+1}.** <@{uid}>" for i, uid in enumerate(reserve_list)])
        else:
            reserve_text = "*Список пуст*"

        embed.add_field(
            name=f"🛡️ Основной состав ({len(main_list)} / {main_slots})",
            value=main_text,
            inline=False
        )
        embed.add_field(
            name=f"🔄 Запасной состав ({len(reserve_list)} / {reserve_slots})",
            value=reserve_text,
            inline=False
        )
        return embed

    async def refresh_nabor_message(self, session_id: int, guild: discord.Guild):
        session = await fetch_one("SELECT * FROM mp_sessions WHERE session_id = ?", (session_id,))
        if not session or not session['message_id'] or not session['channel_id']:
            return

        channel = guild.get_channel(session['channel_id'])
        if not channel:
            return

        try:
            message = await channel.fetch_message(session['message_id'])
        except Exception:
            return

        main_list = json_to_list(session['main_list'])
        reserve_list = json_to_list(session['reserve_list'])

        embed = self.build_nabor_embed(session['main_slots'], session['reserve_slots'], main_list, reserve_list, guild)
        view = MpRegistrationView(session_id, self)
        await view.update_view_items(session, guild)

        await message.edit(embed=embed, view=view)

    # ------------------ /MP START ------------------
    @mp.command(name="start", description="Начать мероприятие: создать приватную ветку для основы и выбрать режим")
    @app_commands.describe(event_type="Тип мероприятия (ВЗП или ВЗХ). Если не указать — можно выбрать кнопками.")
    async def mp_start(self, interaction: discord.Interaction, event_type: Optional[Literal["ВЗП", "ВЗХ"]] = None):
        if not await is_mp_organizer(interaction):
            embed = create_error_embed("Доступ запрещён", "У вас нет прав организатора МП.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        session = await fetch_one(
            "SELECT * FROM mp_sessions WHERE guild_id = ? AND channel_id = ? AND is_active = 1",
            (interaction.guild_id, interaction.channel_id)
        )
        if not session:
            embed = create_error_embed("Ошибка", "В этом канале нет активного набора МП. Сначала откройте набор через `/mp nabor`.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if session['thread_id']:
            embed = create_warning_embed("Внимание", "Ветка для этого мероприятия уже была создана ранее!")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        main_list = json_to_list(session['main_list'])
        if not main_list:
            embed = create_warning_embed("Внимание", "В основном списке нет ни одного участника! Переместите бойцов из запаса перед стартом.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.defer()

        now = datetime.now()
        mode_prefix = event_type if event_type else "мп"
        thread_name = f"⚔️・{mode_prefix.lower()}-{now.strftime('%d-%m-%H-%M')}"

        try:
            thread = await interaction.channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=60
            )
        except Exception as e:
            embed = create_error_embed("Ошибка создания ветки", f"Не удалось создать приватную ветку: {e}")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        added_count = 0
        for uid in main_list:
            try:
                member = interaction.guild.get_member(uid)
                if member:
                    await thread.add_user(member)
                    added_count += 1
            except Exception:
                pass

        try:
            await thread.add_user(interaction.user)
        except Exception:
            pass

        await execute_query(
            "UPDATE mp_sessions SET thread_id = ?, event_type = ?, created_at = CURRENT_TIMESTAMP WHERE session_id = ?",
            (thread.id, event_type or 'ВЗП', session['session_id'])
        )
        self.active_position_sessions[thread.id] = session['session_id']

        embed_start = create_success_embed(
            "Мероприятие успешно запущено!",
            f"Создана приватная ветка: {thread.mention}\nВ ветку добавлено участников основы: **{added_count}**.\n\n"
            f"⏱️ Таймер на 40 минут запущен. Для досрочного завершения используйте `/mp stop`."
        )
        await interaction.followup.send(embed=embed_start)

        squad_mentions = " ".join([f"<@{uid}>" for uid in main_list])

        if event_type == "ВЗХ":
            thread_embed = create_embed(
                title="🛡️ ПОДГОТОВКА К МЕРОПРИЯТИЮ: ВЗХ",
                description=(
                    f"Добро пожаловать в ветку сборов на ВЗХ!\n\n"
                    f"**Участники основы:**\n{squad_mentions}\n\n"
                    f"Организаторы выбирают карту из 3 предложенных ниже.\n"
                    f"Затем используйте команду `/mp position [кол-во]`, чтобы распределить позиции!"
                ),
                color=EMBED_GREEN
            )
            map_view = MpMapSelectView(self, session['session_id'], VZH_MAPS)
            await thread.send(embed=thread_embed, view=map_view)
        elif event_type == "ВЗП":
            thread_embed = create_embed(
                title="⚔️ ПОДГОТОВКА К МЕРОПРИЯТИЮ: ВЗП",
                description=(
                    f"Добро пожаловать в ветку сборов на ВЗП!\n\n"
                    f"**Участники основы:**\n{squad_mentions}\n\n"
                    f"Организаторы выбирают карту из 16 предложенных ниже.\n"
                    f"Затем используйте команду `/mp position [кол-во]`, чтобы распределить позиции!"
                ),
                color=EMBED_PURPLE
            )
            map_view = MpMapSelectView(self, session['session_id'], VZP_MAPS)
            await thread.send(embed=thread_embed, view=map_view)
        else:
            thread_embed = create_embed(
                title="🎯 ПОДГОТОВКА К МЕРОПРИЯТИЮ",
                description=(
                    f"Добро пожаловать в ветку сборов!\n\n"
                    f"**Участники основы:**\n{squad_mentions}\n\n"
                    f"Организатор {interaction.user.mention}, выберите формат мероприятия кнопкой ниже:"
                ),
                color=EMBED_COLOR
            )
            type_view = MpTypeSelectView(self, session['session_id'])
            await thread.send(embed=thread_embed, view=type_view)

    # ------------------ /MP POSITION ------------------
    @mp.command(name="position", description="Запустить распределение позиций в ветке МП")
    @app_commands.describe(positions_count="Количество доступных позиций (например, 10)")
    async def mp_position(self, interaction: discord.Interaction, positions_count: int):
        if not await is_mp_organizer(interaction):
            embed = create_error_embed("Доступ запрещён", "У вас нет прав организатора МП.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        session_id = self.active_position_sessions.get(interaction.channel_id)
        if not session_id:
            session = await fetch_one(
                "SELECT * FROM mp_sessions WHERE thread_id = ? AND is_active = 1",
                (interaction.channel_id,)
            )
            if session:
                session_id = session['session_id']
                self.active_position_sessions[interaction.channel_id] = session_id

        if not session_id:
            embed = create_error_embed("Ошибка", "Эту команду необходимо запускать внутри активной ветки МП!")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if positions_count <= 0 or positions_count > 50:
            embed = create_error_embed("Ошибка", "Количество позиций должно быть от 1 до 50!")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        pos_dict = {str(i): None for i in range(1, positions_count + 1)}
        await execute_query(
            "UPDATE mp_sessions SET positions = ? WHERE session_id = ?",
            (json.dumps(pos_dict), session_id)
        )

        embed = self.build_positions_embed(pos_dict, positions_count)
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()

        await execute_query(
            "UPDATE mp_sessions SET pos_message_id = ? WHERE session_id = ?",
            (msg.id, session_id)
        )

    def build_positions_embed(self, pos_dict: dict, total: int) -> discord.Embed:
        embed = create_embed(
            title="📍 РАСПРЕДЕЛЕНИЕ ПОЗИЦИЙ НА КАРТЕ",
            description=(
                f"Напишите в этот чат **номер свободной позиции (от 1 до {total})**, чтобы занять её!\n"
                f"• Если напишете другой номер — автоматически перейдёте на новую позицию.\n"
                f"• Занятые позиции занять нельзя."
            ),
            color=EMBED_GREEN
        )

        occupied_count = sum(1 for uid in pos_dict.values() if uid is not None)
        embed.set_author(name=f"Занято: {occupied_count} из {total}")

        lines = []
        for i in range(1, total + 1):
            uid = pos_dict.get(str(i))
            if uid:
                lines.append(f"`{i:02d}` 🔴 <@{uid}>")
            else:
                lines.append(f"`{i:02d}` 🟢 *Свободно*")

        chunk_size = 15
        for i in range(0, len(lines), chunk_size):
            chunk = lines[i:i + chunk_size]
            embed.add_field(name="Позиции", value="\n".join(chunk), inline=True)

        return embed

    async def update_position_embed(self, channel: discord.Thread, session_id: int, pos_dict: dict):
        session = await fetch_one("SELECT pos_message_id FROM mp_sessions WHERE session_id = ?", (session_id,))
        if not session or not session['pos_message_id']:
            return

        try:
            msg = await channel.fetch_message(session['pos_message_id'])
            embed = self.build_positions_embed(pos_dict, len(pos_dict))
            await msg.edit(embed=embed)
        except Exception as e:
            log.warning(f"Не удалось обновить табло позиций: {e}")

    # ------------------ /MP STOP (КОМАНДА ЗАВЕРШИТЬ МП) ------------------
    @mp.command(name="stop", description="Досрочно завершить мероприятие и отправить отчёт в архив")
    async def mp_stop(self, interaction: discord.Interaction):
        if not await is_mp_organizer(interaction):
            embed = create_error_embed("Доступ запрещён", "У вас нет прав организатора МП.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        session = await fetch_one(
            "SELECT * FROM mp_sessions WHERE (thread_id = ? OR channel_id = ?) AND is_active = 1",
            (interaction.channel_id, interaction.channel_id)
        )
        if not session:
            # Ищем любое активное в этой гильдии
            session = await fetch_one(
                "SELECT * FROM mp_sessions WHERE guild_id = ? AND is_active = 1 ORDER BY session_id DESC LIMIT 1",
                (interaction.guild_id,)
            )

        if not session:
            embed = create_error_embed("Ошибка", "Активное мероприятие не найдено!")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.send_message("🛑 Завершаю мероприятие, формирую архив и закрываю ветку...", ephemeral=True)
        await self.archive_session(session)

    # ==================== СЛУШАТЕЛЬ ЧАТА (ПОЗИЦИИ) ====================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        session_id = self.active_position_sessions.get(message.channel.id)
        if not session_id:
            return

        content = message.content.strip()
        if not content.isdigit():
            return

        pos_num = int(content)

        session = await fetch_one("SELECT * FROM mp_sessions WHERE session_id = ?", (session_id,))
        if not session or not session['is_active'] or not session['positions']:
            return

        try:
            pos_dict = json.loads(session['positions'])
        except Exception:
            return

        total_positions = len(pos_dict)
        if pos_num < 1 or pos_num > total_positions:
            return

        main_list = json_to_list(session['main_list'])
        is_org = message.author.guild_permissions.administrator

        if not is_org and message.author.id not in main_list:
            try:
                await message.delete()
                await message.channel.send(f"{message.author.mention}, только участники основного состава могут занимать позиции!", delete_after=3)
            except Exception:
                pass
            return

        key = str(pos_num)
        current_owner = pos_dict.get(key)

        if current_owner == message.author.id:
            try:
                await message.delete()
            except Exception:
                pass
            return

        if current_owner is not None:
            try:
                await message.delete()
                await message.channel.send(f"{message.author.mention}, позиция **{pos_num}** уже занята <@{current_owner}>!", delete_after=3)
            except Exception:
                pass
            return

        for p_k, p_uid in pos_dict.items():
            if p_uid == message.author.id:
                pos_dict[p_k] = None

        pos_dict[key] = message.author.id
        await execute_query("UPDATE mp_sessions SET positions = ? WHERE session_id = ?", (json.dumps(pos_dict), session_id))

        try:
            await message.delete()
        except Exception:
            pass

        await self.update_position_embed(message.channel, session_id, pos_dict)

    # ==================== АВТО-АРХИВАЦИЯ ====================
    @tasks.loop(seconds=30)
    async def archive_check_loop(self):
        try:
            active_sessions = await fetch_all(
                "SELECT * FROM mp_sessions WHERE is_active = 1 AND thread_id IS NOT NULL"
            )
            now = datetime.utcnow()

            for session in active_sessions:
                created_at = session['created_at']
                if not created_at:
                    continue

                if isinstance(created_at, str):
                    try:
                        created_at = datetime.fromisoformat(created_at)
                    except Exception:
                        continue

                delta = (now - created_at).total_seconds()
                if delta >= 40 * 60:
                    await self.archive_session(session)
        except Exception as e:
            log.error(f"Ошибка в цикле архивации МП: {e}", exc_info=True)

    @archive_check_loop.before_loop
    async def before_archive_check_loop(self):
        await self.bot.wait_until_ready()

    async def archive_session(self, session: dict):
        """Выполняет архивацию сессии МП: выгрузка отчёта в категорию и закрытие ветки."""
        session_id = session['session_id']
        guild_id = session['guild_id']
        thread_id = session['thread_id']
        event_type = session['event_type'] if session['event_type'] else "Мероприятие"

        guild = self.bot.get_guild(guild_id)
        if not guild:
            await execute_query("UPDATE mp_sessions SET is_active = 0 WHERE session_id = ?", (session_id,))
            return

        thread = guild.get_thread(thread_id)
        if not thread:
            try:
                thread = await guild.fetch_channel(thread_id)
            except Exception:
                thread = None

        map_name = session['selected_map'] if session['selected_map'] else "Не выбрана"
        main_list = json_to_list(session['main_list'])

        report_embed = create_embed(
            title=f"📁 АРХИВ МЕРОПРИЯТИЯ: {event_type.upper()}",
            description=f"Мероприятие {event_type} успешно завершено.\nВетка: {thread.mention if thread else 'Удалена'}",
            color=EMBED_ORANGE
        )
        report_embed.add_field(name="🗺️ Выбранная карта", value=f"**{map_name}**", inline=False)

        if main_list:
            squad_str = "\n".join([f"• <@{uid}>" for uid in main_list])
        else:
            squad_str = "Нет данных"
        report_embed.add_field(name="🛡️ Основной состав", value=squad_str, inline=True)

        pos_text = "Не распределялись"
        if session['positions']:
            try:
                pos_dict = json.loads(session['positions'])
                occupied = [f"Позиция `{k}`: <@{v}>" for k, v in pos_dict.items() if v]
                if occupied:
                    pos_text = "\n".join(occupied)
                else:
                    pos_text = "Ни одна позиция не была занята"
            except Exception:
                pass
        report_embed.add_field(name="📍 Занятые позиции", value=pos_text, inline=False)

        mp_settings = await fetch_one("SELECT * FROM mp_settings WHERE guild_id = ?", (guild_id,))
        archive_category_id = mp_settings['archive_category_id'] if mp_settings else None

        if archive_category_id:
            category = guild.get_channel(archive_category_id)
            if isinstance(category, discord.CategoryChannel):
                target_channel = None
                for ch in category.text_channels:
                    if "мп" in ch.name.lower() or "архив" in ch.name.lower():
                        target_channel = ch
                        break

                if not target_channel:
                    try:
                        target_channel = await guild.create_text_channel(
                            name=f"архив-мп",
                            category=category,
                            topic="Архив проведённых мероприятий (ВЗП / ВЗХ)"
                        )
                    except Exception:
                        pass

                if target_channel:
                    try:
                        map_path = find_map_file(map_name)
                        if map_path and os.path.exists(map_path):
                            filename = os.path.basename(map_path)
                            file = discord.File(map_path, filename=filename)
                            report_embed.set_image(url=f"attachment://{filename}")
                            await target_channel.send(embed=report_embed, file=file)
                        else:
                            await target_channel.send(embed=report_embed)
                    except Exception as e:
                        log.error(f"Не удалось отправить архив в канал: {e}")

        if thread:
            try:
                notice = create_warning_embed(
                    "🛑 Мероприятие завершено",
                    "Ветка блокируется и архивируется. Сводный отчёт отправлен в архивную категорию."
                )
                await thread.send(embed=notice)
                await thread.edit(locked=True, archived=True)
            except Exception as e:
                log.warning(f"Не удалось заблокировать ветку: {e}")

        await execute_query("UPDATE mp_sessions SET is_active = 0 WHERE session_id = ?", (session_id,))
        self.active_position_sessions.pop(thread_id, None)


async def setup(bot):
    await bot.add_cog(MPCog(bot))
