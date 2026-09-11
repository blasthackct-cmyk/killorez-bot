import os
import sys
import traceback
import logging
import discord
from discord.ext import commands
from dotenv import load_dotenv
from utils.database import init_db

# Настройка логирования
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot_debug.log', encoding='utf-8')
    ]
)
log = logging.getLogger('bot')

load_dotenv()

log.info('=== ЗАПУСК БОТА ===')
log.debug(f'Python version: {sys.version}')
log.debug(f'discord.py version: {discord.__version__}')

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.reactions = True
intents.dm_messages = True
log.info(f'Intents: guilds={intents.guilds}, members={intents.members}, '
         f'messages={intents.messages}, dm_messages={intents.dm_messages}')

bot = commands.Bot(command_prefix='!', intents=intents)


@bot.event
async def on_ready():
    log.info(f'=== ON_READY: Бот подключен как {bot.user} (ID: {bot.user.id}) ===')
    try:
        await init_db()
        log.info('База данных инициализирована')
    except Exception as e:
        log.error(f'Ошибка инициализации БД: {e}', exc_info=True)

    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="KILLOREZ HELPER"))
    log.info('Status установлен')

    # Load cogs
    cogs_to_load = ['afk', 'ticket', 'car', 'warnings', 'points', 'market', 'commands']
    for cog_name in cogs_to_load:
        try:
            await bot.load_extension(f'cogs.{cog_name}')
            log.info(f'[OK] Загружен ког: {cog_name}')
        except Exception as e:
            log.error(f'[ERROR] Ошибка загрузки кога {cog_name}: {e}', exc_info=True)

    # Sync commands
    try:
        synced = await bot.tree.sync()
        log.info(f'Синхронизировано {len(synced)} команд')
    except Exception as e:
        log.error(f'Ошибка синхронизации команд: {e}', exc_info=True)

    log.info('=== БОТ ГОТОВ К РАБОТЕ ===')


@bot.command(name="sync")
@commands.has_permissions(administrator=True)
async def sync_commands(ctx, action: str = "clean"):
    """
    Управление синхронизацией команд:
    !sync - очищает локальные дубликаты сервера, убирая двойные команды
    !sync guild - привязать команды локально к серверу
    """
    try:
        if action.lower() == "guild":
            bot.tree.copy_global_to(guild=ctx.guild)
            synced = await bot.tree.sync(guild=ctx.guild)
            await ctx.send(f"✅ Локально синхронизировано {len(synced)} команд для сервера **{ctx.guild.name}**.")
        else:
            # Очищаем локальные команды сервера, устраняя дублирование
            bot.tree.clear_commands(guild=ctx.guild)
            await bot.tree.sync(guild=ctx.guild)
            await ctx.send(
                f"🧹 **Дубликаты успешно удалены!**\n"
                f"Локальные копии команд для сервера **{ctx.guild.name}** очищены.\n\n"
                f"👉 Нажмите **Ctrl+R** в Discord, чтобы список команд обновился без повторов."
            )
    except Exception as e:
        await ctx.send(f"❌ Ошибка: {e}")


@bot.event
async def on_command_error(ctx, error):
    log.warning(f'Command error: {error}')
    if isinstance(error, commands.MissingPermissions):
        embed = discord.Embed(title="Ошибка", description="У вас нет прав для выполнения этой команды.", color=0xED4245)
        embed.set_footer(text="KILLOREZ HELPER")
        await ctx.send(embed=embed)
    elif isinstance(error, commands.MissingRequiredArgument):
        embed = discord.Embed(title="Ошибка", description="Укажите все необходимые аргументы.", color=0xED4245)
        embed.set_footer(text="KILLOREZ HELPER")
        await ctx.send(embed=embed)


@bot.event
async def on_error(event, *args, **kwargs):
    log.error(f'ERROR in event {event}: {sys.exc_info()}', exc_info=True)


TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    log.critical('DISCORD_TOKEN не установлен в .env!')
    print("ОШИБКА: Установите DISCORD_TOKEN в .env файле!")
    exit(1)

log.info('Запуск bot.run()...')
try:
    bot.run(TOKEN, log_handler=None)  # log_handler=None т.к. уже настроили свое логирование
except Exception as e:
    log.critical(f'КРИТИЧЕСКАЯ ОШИБКА при запуске: {e}', exc_info=True)
    raise
