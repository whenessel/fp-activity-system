from __future__ import annotations

import io
import logging
import discord
import platform
import re
import enum
import datetime

from django.db.models import Count
from enum_properties import EnumProperties, p, s
from typing import TYPE_CHECKING, Optional, List, Tuple, Union
from discord import app_commands
from discord.ext import commands, tasks
from evebot.utils.enums import EmojiEnumMIxin
from evebot.bot import EveBot, EveContext, GuildEveContext
from django.db import transaction
from activity.models import *
from activity.resources import CommonEventAttendanceResource
from evebot.utils import checks


if TYPE_CHECKING:
    ...


log = logging.getLogger(__name__)


class MemberReactions(EmojiEnumMIxin, EnumProperties, s('emoji'), s('attend_server', case_fold=True)):
    ONE = enum.auto(), '1️⃣', AttendanceServer.ONE
    TWO = enum.auto(), '2️⃣', AttendanceServer.TWO
    THREE = enum.auto(), '3️⃣', AttendanceServer.THREE
    FOUR = enum.auto(), '4️⃣', AttendanceServer.FOUR
    FIVE = enum.auto(), '5️⃣', AttendanceServer.FIVE
    SIX = enum.auto(), '6️⃣', AttendanceServer.SIX


def event_template_embed(title: str, description: str) -> discord.Embed:
    embed = discord.Embed(colour=discord.Colour.blue())
    embed.title = f'{title}'
    embed.description = f'{description}'
    return embed


def event_embed(event: EventItem) -> discord.Embed:
    colour = discord.Colour.light_gray()
    if event.status == EventStatus.STARTED:
        colour = discord.Colour.blue()
    elif event.status == EventStatus.CANCELED:
        colour = discord.Colour.red()
    elif event.status == EventStatus.FINISHED:
        colour = discord.Colour.green()

    embed = discord.Embed(colour=colour)

    embed.title = f'[{event.get_status_display().upper()}] {event.title.upper()}'

    embed.set_author(name=f'РЛ: {event.author.display_name}', icon_url=event.author.avatar.url)

    embed.add_field(name=f'Время сбора', value=f'{event.description}', inline=False)
    embed.add_field(name='', value='', inline=False)
    embed.add_field(name=f'Номер события', value=f'{event.id}', inline=True)
    embed.add_field(name=f'Время события', value=f'{event.created.strftime("%d.%m.%Y %H:%M")}', inline=True)
    embed.add_field(name='', value='', inline=False)

    embed.add_field(name=f'Призыв', value=f'@everyone')

    embed.set_footer(text=f'{MemberReactions.ONE.emoji}    {MemberReactions.ONE.attend_server.label}\n'
                          f'{MemberReactions.TWO.emoji}    {MemberReactions.TWO.attend_server.label}\n'
                          f'{MemberReactions.THREE.emoji}    {MemberReactions.THREE.attend_server.label}\n'
                          f'{MemberReactions.FOUR.emoji}    {MemberReactions.FOUR.attend_server.label}\n'
                          f'{MemberReactions.FIVE.emoji}    {MemberReactions.FIVE.attend_server.label}\n'
                          f'{MemberReactions.SIX.emoji}    {MemberReactions.SIX.attend_server.label}\n')

    if event.status == EventStatus.FINISHED:
        guild_members = list([member for member in event.guild.members])
        stats_footer = ''

        for item in event.event_attendances.values('server').annotate(Count('server')):
            mr = MemberReactions[item['server']]
            cnt = item['server__count']
            stats_footer += f'{mr.emoji}    {mr.attend_server.label}    {cnt}\n'

        embed.set_footer(text=stats_footer)

    return embed


class EventItem(Event):

    bot: Optional[EveBot] = None
    cog: Optional[EventCog] = None

    message: Optional[discord.Message]
    author: Optional[discord.Member]

    _member_attendances: dict[int, AttendanceServer] = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def save(self, message_id=None, status=None,
             force_insert=False, force_update=False, using=None, update_fields=None):
        if message_id:
            self.message_id = message_id
        if status:
            self.status = status
        super().save(force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields)

    class Meta:
        proxy = True

    @property
    def guild(self) -> Optional[discord.Guild]:
        if self.guild_id is not None:
            return self.bot.get_guild(self.guild_id)
        return None

    @property
    def channel(self) -> Optional[discord.TextChannel]:
        if self.channel_id is not None:
            return self.bot.get_channel(self.channel_id)
        return None

    @property
    def author(self) -> discord.Member:
        return self.guild.get_member(self.member_id)

    async def fetch_message(self) -> Optional[discord.Message]:
        channel = self.channel
        if channel is not None and self.message_id is not None:
            self.message = await self.cog.get_event_message(channel.id, self.message_id)
        return self.message

    @property
    def member_attendances(self) -> dict:
        # Подгружаем из БД то что есть ( при рестарте )
        if not self._member_attendances:
            members = self.event_attendances.all().values('member_id', 'server')
            for member in members:
                attend_server = AttendanceServer(member['server'])
                self._member_attendances[member['member_id']] = attend_server
        return self._member_attendances

    def member_attendance(self, member_id: int):
        attend_server = self.member_attendances.get(member_id)
        return attend_server

    def add_member_attendance(self, member: discord.Member,
                              server: AttendanceServer, force: bool = True) -> Tuple[EventAttendance, bool]:
        self._member_attendances[member.id] = server
        attend_member, created = EventAttendance.objects.get_or_create(
            event=self,
            member_id=member.id,
            defaults={
                'member_name': member.name,
                'member_display_name': member.display_name,
                'server': server
            }
        )
        if not created and force:
            attend_member.server = server

        attend_member.save(update_fields=['server', ])
        return attend_member, created

    def remove_member_attendance(self, member: discord.Member) -> bool:
        try:
            attend_member = EventAttendance.objects.get(
                event=self,
                member_id=member.id
            )
            attend_member.delete()
        except EventAttendance.DoesNotExist:
            ...
        except EventAttendance.MultipleObjectsReturned:
            EventAttendance.objects.filter(
                event=self,
                member_id=member.id
            ).delete()
        self._member_attendances.pop(member.id, None)
        return True

    async def do_finish(self) -> None:

        self.status = EventStatus.FINISHED
        self.save()

    async def do_cancel(self) -> None:
        self.status = EventStatus.CANCELED
        self.save()

    async def clean_reactions(self) -> None:
        message = await self.fetch_message()
        await message.clear_reactions()


class EventButtonsPersistentView(discord.ui.View):
    def __init__(self, cog: EventCog):
        super().__init__(timeout=None)
        self.cog: EventCog = cog
        self.event: Optional[EventItem] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        self.event = await self.cog.get_event_for_message(interaction.message.id)

        if not self.cog.is_event_moderator(self.event.guild_id, interaction.user.id):
            await interaction.response.send_message(
                f':face_with_symbols_over_mouth: Убрал руки! Это только для модераторов.', ephemeral=True)
            return False

        if self.event.status in [EventStatus.FINISHED, EventStatus.CANCELED]:
            embed = event_embed(event=self.event)
            await interaction.response.edit_message(content=None, embed=embed, view=None)
            await self.event.clean_reactions()
            await interaction.followup.send(
                f':face_with_spiral_eyes: Упс! Событие уже {self.event.get_status_display()}', ephemeral=True)
            return False

        return True

    @discord.ui.button(label='Завершить',
                       style=discord.ButtonStyle.green, custom_id='EVENT_BUTTON_PERSISTENT_VIEW:SUCCESS')
    async def success(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.event.do_finish()
        embed = event_embed(event=self.event)
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        await self.event.clean_reactions()
        return

    @discord.ui.button(label='Отменить',
                       style=discord.ButtonStyle.red, custom_id='EVENT_BUTTON_PERSISTENT_VIEW:CANCEL')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.event.do_cancel()
        embed = event_embed(event=self.event)
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        await self.event.clean_reactions()


class EventCog(commands.Cog):

    def __init__(self, bot: EveBot):
        self.bot: EveBot = bot

        self.bot.add_view(EventButtonsPersistentView(cog=self))

        self.event_class = EventItem
        self.event_class.bot = bot
        self.event_class.cog = self

        self._event_message_cache: dict[int, discord.Message] = {}
        self.cleanup_event_message_cache.start()
        self._event_moderators_cache: dict[int, dict] = {}
        self.cleanup_event_moderators_cache.start()

        self._event_channels_cache: dict[int, dict] = {}
        self.cleanup_event_channels_cache.start()

        self.ctx_menu_event_finish = app_commands.ContextMenu(name='Завершить событие',
                                                              callback=self.context_event_finish)
        self.bot.tree.add_command(self.ctx_menu_event_finish)

    async def context_event_finish(self, interaction: discord.Interaction, message: discord.Message):
        # We have to make sure the following query takes <3s in order to meet the response window
        event = await self.get_event_for_message(message_id=message.id)
        if not event:
            await interaction.response.send_message(
                f':face_with_spiral_eyes: Упс! Тут нет события!', ephemeral=True)
            return False
        if event.status in [EventStatus.FINISHED, EventStatus.CANCELED]:
            await interaction.response.send_message(
                f':face_with_spiral_eyes: Упс! Событие уже {event.get_status_display()}', ephemeral=True)
            return False

        await interaction.response.send_modal(QuantityModal(cog=self, event=event))

    @tasks.loop(hours=1.0)
    async def cleanup_event_message_cache(self):
        self._event_message_cache.clear()

    @tasks.loop(hours=1.0)
    async def cleanup_event_moderators_cache(self):
        self._event_moderators_cache.clear()

    @tasks.loop(hours=1.0)
    async def cleanup_event_channels_cache(self):
        self._event_channels_cache.clear()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        message = await self.get_event_message(payload.channel_id, message_id=payload.message_id)
        event = await self.get_event_for_message(message_id=payload.message_id)

        member = payload.member

        # Проверим канал. Есть ли он в БД
        if not self.is_event_channel(guild_id=payload.guild_id, channel_id=payload.channel_id):
            return

        # Сначала проверяем есть ли событие для этого сообщения
        if not event:
            await message.remove_reaction(payload.emoji, member)
            return

        if event.status == EventStatus.FINISHED:
            await message.remove_reaction(payload.emoji, member)
            return

        if member.bot:
            return

        if str(payload.emoji) in MemberReactions.emojis():
            current_member_attendance = MemberReactions(str(payload.emoji)).attend_server
            old_member_attendance = event.member_attendance(member_id=member.id)
            if old_member_attendance is not None and current_member_attendance != old_member_attendance:
                emoji = MemberReactions(old_member_attendance).emoji
                await message.remove_reaction(emoji, member)

            event.add_member_attendance(member=member, server=current_member_attendance)
        else:
            await message.remove_reaction(str(payload.emoji), member)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        message = await self.get_event_message(payload.channel_id, message_id=payload.message_id)
        event = await self.get_event_for_message(message_id=payload.message_id)

        # Сначала проверяем есть ли событие для этого сообщения
        if not event:
            return

        member = payload.member or event.guild.get_member(payload.user_id)

        if event.status == EventStatus.FINISHED:
            return

        if member.bot:
            return

        if str(payload.emoji) in MemberReactions.emojis():
            event.remove_member_attendance(member=member)

    async def cog_app_command_error(self, inter: discord.Interaction,
                                    error: app_commands.AppCommandError) -> None:
        log.error(f'Error handled by "cog_app_command_error": {str(error)}')
        await inter.response.send_message(f'\N{SKULL AND CROSSBONES} Что-то пошло не так\n\n'
                                          f'> {str(error)}', ephemeral=True)

    async def cog_command_error(self, ctx: EveContext, error: Exception) -> None:
        log.error(f'Error handled by "cog_command_error": {str(error)}')
        await ctx.send(f'\N{SKULL AND CROSSBONES} Что-то пошло не так\n\n'
                       f'> {str(error)}', ephemeral=True)

    def is_event_moderator(self, guild_id: int, member_id: int) -> bool:
        try:
            _ = self._event_moderators_cache[guild_id][member_id]
            return True
        except KeyError:
            try:
                _ = EventModerator.objects.get(guild_id=guild_id, member_id=member_id)
            except EventModerator.DoesNotExist:
                return False
            else:
                if guild_id not in self._event_moderators_cache:
                    self._event_moderators_cache[guild_id] = {}
                self._event_moderators_cache[guild_id].update({member_id: True})
                return True

    def is_event_channel(self, guild_id: int, channel_id: int) -> bool:
        try:
            _ = self._event_channels_cache[guild_id][channel_id]
            return True
        except KeyError:
            try:
                _ = EventChannel.objects.get(guild_id=guild_id, channel_id=channel_id)
            except EventChannel.DoesNotExist:
                return False
            else:
                if guild_id not in self._event_channels_cache:
                    self._event_channels_cache[guild_id] = {}
                self._event_channels_cache[guild_id].update({channel_id: True})
                return True

    async def get_event_message(self, channel_id: int, message_id: int) -> Optional[discord.Message]:
        try:
            return self._event_message_cache[message_id]
        except KeyError:
            try:
                channel = self.bot.get_channel(channel_id)
                msg = await channel.fetch_message(message_id)
            except discord.HTTPException:
                return None
            else:
                self._event_message_cache[message_id] = msg
                return msg

    async def get_event_for_message(self, message_id: int) -> Optional[EventItem]:
        try:
            event = self.event_class.objects.get(message_id=message_id)
            return event
        except (EventItem.DoesNotExist, Event.DoesNotExist):
            return None

    @commands.hybrid_group(
        name='eventadmin',
        description='Группа команд для администрирования событий',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.is_owner()
    @commands.guild_only()
    @app_commands.guild_only()
    async def event_admin(self, ctx: GuildEveContext) -> None:
        ...

    @event_admin.group(
        name='channel',
        description='Добавление или удаление каналов для событий',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.is_owner()
    @commands.guild_only()
    @app_commands.guild_only()
    async def event_admin_channel(self, ctx: GuildEveContext) -> None:
        ...

    @event_admin.group(
        name='moderator',
        description='Добавление или удаление модераторов событий',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.is_owner()
    @commands.guild_only()
    @app_commands.guild_only()
    async def event_admin_moderator(self, ctx: GuildEveContext) -> None:
        ...

    @event_admin_channel.command(
        name='add',
        description='Добавление текстового канала для событий',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.is_owner()
    @commands.guild_only()
    @app_commands.guild_only()
    @app_commands.describe(channel='Текстовый канал для событий')
    async def event_admin_channel_add(self, ctx: GuildEveContext, channel: discord.TextChannel) -> None:
        event_channel, _ = EventChannel.objects.get_or_create(guild_id=ctx.guild.id, channel_id=channel.id)
        await ctx.send(f'Канал, {channel.mention}, настроен для использования событий!', reference=ctx.message)

    @event_admin_channel.command(
        name='del',
        description='Удаление текстового канала для событий',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.is_owner()
    @commands.guild_only()
    @app_commands.guild_only()
    @app_commands.describe(channel='Текстовый канал для событий')
    async def event_admin_channel_del(self, ctx: GuildEveContext, channel: discord.TextChannel) -> None:
        event_channel = EventChannel.objects.get(guild_id=ctx.guild.id, channel_id=channel.id)
        event_channel.delete()
        await ctx.send(f'Канал, {channel.mention}, больше не используется для событий!', reference=ctx.message)

    @event_admin_moderator.command(
        name='add',
        description='Добавление модератора событий',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.is_owner()
    @commands.guild_only()
    @app_commands.guild_only()
    @app_commands.describe(member='Участник, которому предоставить права модерации')
    async def event_admin_moderator_add(self, ctx: GuildEveContext, member: discord.Member) -> None:
        event_moderator, _ = EventModerator.objects.get_or_create(guild_id=ctx.guild.id, member_id=member.id)
        await ctx.send(f'Пользователь, {member.mention}, теперь является модератором!', reference=ctx.message)

    @event_admin_moderator.command(
        name='del',
        description='Удаление модератора событий',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.is_owner()
    @commands.guild_only()
    @app_commands.guild_only()
    @app_commands.describe(member='Участник, у которого отозвать права модератора')
    async def event_admin_moderator_del(self, ctx: GuildEveContext, member: discord.Member) -> None:
        event_moderator = EventModerator.objects.get(guild_id=ctx.guild.id, member_id=member.id)
        event_moderator.delete()
        await ctx.send(f'Пользователь, {member.mention}, теперь не является модератором!', reference=ctx.message)

    @event_admin_moderator.command(
        name='show',
        description='Показать всех модераторов',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.guild_only()
    @app_commands.guild_only()
    async def event_admin_moderator_show(self, ctx: GuildEveContext) -> None:
        event_moderators = EventModerator.objects.filter(guild_id=ctx.guild.id).values_list('member_id', flat=True)
        members = ctx.guild.members
        moderators = filter(lambda member: member.id in event_moderators, members)
        await ctx.send(f'Наши любимчики :heart_exclamation:\n{", ".join([member.mention for member in moderators])}',
                       reference=ctx.message)

    @commands.hybrid_group(
        name='eventmod',
        description='Группа команд для модерации событий',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.is_owner()
    @commands.guild_only()
    @app_commands.guild_only()
    async def event_mod(self, ctx: GuildEveContext) -> None:
        ...

    @event_mod.group(
        name='member',
        description='Добавление или удаление участника из события',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.guild_only()
    @app_commands.guild_only()
    @checks.event_channel_only()
    @checks.event_moderator_only()
    async def event_mod_member(self, ctx: GuildEveContext) -> None:
        ...

    @event_mod_member.command(
        name='add',
        description='Добавить участника к событию',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.guild_only()
    @app_commands.guild_only()
    @checks.event_channel_only()
    @checks.event_moderator_only()
    @app_commands.describe(
        member='Участник, которого надо добавить к событию',
        server='Сервер участника',
        event='Номер события'
    )
    @app_commands.choices(
        server=[app_commands.Choice(name=label, value=value) for value, label in AttendanceServer.choices]
    )
    async def event_mod_member_add(self, ctx: GuildEveContext,
                               member: discord.Member, server: str, event: int) -> None:
        try:
            event = self.event_class.objects.get(id=event)
            event.add_member_attendance(member=member, server=server)

            event_message = await event.fetch_message()
            embed = event_embed(event=event)
            await event_message.edit(content=None, embed=embed, view=None)

            # await ctx.send(f'{member.mention} '
            #                f'добавлен к событию **{event.id}** '
            #                f'как **{AttendanceType[type].label.upper()}**\n'
            #                f'Событие: {event_message.jump_url}', reference=event_message)
            await ctx.send(f'Добавлен участник {member.mention} к событию **{event.id} ({event.title})**\n'
                           f'Перейти к событию: {event_message.jump_url}', reference=event_message)
        except (EventItem.DoesNotExist, Event.DoesNotExist):
            await ctx.send(f'\N{SKULL AND CROSSBONES} Событие с номером **{event}** не найдено.', ephemeral=True)

    @event_mod_member.command(
        name='del',
        description='Удалить участника из события',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.guild_only()
    @app_commands.guild_only()
    @checks.event_channel_only()
    @checks.event_moderator_only()
    @app_commands.describe(
        member='Участник, которого надо удалить из события',
        event='Номер события'
    )
    async def event_mod_member_del(self, ctx: GuildEveContext, member: discord.Member, event: int) -> None:
        try:
            event = self.event_class.objects.get(id=event)
            event.remove_member_attendance(member=member)

            event_message = await event.fetch_message()
            embed = event_embed(event=event)
            await event_message.edit(content=None, embed=embed, view=None)

            # await ctx.send(f'{member.mention} '
            #                f'удален из события **{event.id}**\n'
            #                f'Событие: {event_message.jump_url}', reference=event_message)
            await ctx.send(f'Удален участник {member.mention} из события **{event.id} ({event.title})**\n'
                           f'Перейти к событию: {event_message.jump_url}', reference=event_message)

        except (EventItem.DoesNotExist, Event.DoesNotExist):
            await ctx.send(f'\N{SKULL AND CROSSBONES} Событие с номером **{event}** не найдено.', ephemeral=True)

    @event_mod.command(
        name='statistic',
        description='Статистика посещаемости',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.guild_only()
    @app_commands.guild_only()
    @checks.event_channel_only()
    @checks.event_moderator_only()
    @app_commands.describe(
        start='Пример: 2023-02-01 (YYYY-MM-DD)',
        end='Пример: 2023-02-09 (YYYY-MM-DD)'
    )
    async def event_statistic(self, ctx: GuildEveContext,
                              start: str, end: str) -> None:

        start_date = datetime.datetime.strptime(start, "%Y-%m-%d")
        end_date = datetime.datetime.strptime(end, "%Y-%m-%d")

        filter = {
            'event__created__gte': start_date,
            'event__created__lte': end_date,
            'event__status': EventStatus.FINISHED
        }

        filename = f'report_{start}_{end}'

        await ctx.defer(ephemeral=True)

        queryset = EventAttendance.objects.filter(**filter)
        resource = CommonEventAttendanceResource()
        result = resource.export(queryset=queryset)
        stat_data = io.BytesIO(result.xlsx)
        stat_file = discord.File(stat_data, filename=f'{filename}.xlsx',
                                 description=f'Статистика за период с {start_date} по {end_date}')

        await ctx.send(f'Все готово!', file=stat_file, ephemeral=True)

    @event_mod.command(
        name='sync',
        description='Синхронизация данных события с базой',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.guild_only()
    @app_commands.guild_only()
    @checks.event_channel_only()
    @checks.event_moderator_only()
    async def event_sync(self, ctx: GuildEveContext, event: int) -> None:
        try:
            event: EventItem = EventItem.objects.get(id=event)
            event.perform_attendance_reward()

            event_message = await event.fetch_message()
            embed = event_embed(event=event)

            await event_message.edit(content=None, embed=embed, view=None)

            await ctx.send(f'**Синхронизировано**\n'
                           f'Событие: **{event.title}**\n'
                           f'Перейти к событию: {event_message.jump_url}', reference=event_message, ephemeral=True)

        except (EventItem.DoesNotExist, Event.DoesNotExist):
            await ctx.send(f'\N{SKULL AND CROSSBONES} Событие с номером **{event}** не найдено.', ephemeral=True)

    @commands.hybrid_group(
        name='event',
        description='Группа команд для управления событиями',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.guild_only()
    @app_commands.guild_only()
    @checks.event_channel_only()
    @checks.event_moderator_only()
    async def event(self, ctx: GuildEveContext) -> None:
        ...

    @event.command(
        name='start',
        description='Запуск события',
        with_app_command=True,
        invoke_without_command=False
    )
    @commands.guild_only()
    @app_commands.guild_only()
    @checks.event_channel_only()
    @checks.event_moderator_only()
    @app_commands.describe(title='Название события. По умолчанию "Сбор Арена"',
                           schedule='Запланированное время сбора. Формат: 14:00')
    async def event_start(self, ctx: GuildEveContext,
                          title: Optional[str],
                          schedule: str) -> None:

        if not title:
            title = "Сбор Арена"

        event = self.event_class.objects.create(
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            member_id=ctx.author.id,
            member_name=ctx.author.name,
            member_display_name=ctx.author.display_name,
            title=title,
            description=schedule,
            status=EventStatus.STARTED
        )

        embed = event_embed(event=event)
        event_buttons_view = EventButtonsPersistentView(cog=self)

        message = await ctx.send(embed=embed, view=event_buttons_view)

        event.save(message_id=message.id)
        for event_reaction in MemberReactions.emojis():
            await message.add_reaction(event_reaction)


async def setup(bot):
    await bot.add_cog(EventCog(bot))
