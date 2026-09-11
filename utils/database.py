import asyncpg
import os
import asyncio

# PostgreSQL connection pool
_pool = None


def _convert_params(query, params):
    """Конвертирует SQLite-style ? плейсхолдеры в PostgreSQL $1, $2..."""
    if not params:
        return query, ()
    param_list = list(params)
    parts = query.split('?')
    if len(parts) == 1:
        return query, tuple(param_list)
    new_query = parts[0]
    for i, part in enumerate(parts[1:], 1):
        new_query += f'${i}' + part
    return new_query, tuple(param_list)


async def get_pool():
    """Получает или создаёт connection pool к PostgreSQL"""
    global _pool
    if _pool is None or _pool._closed:
        database_url = os.getenv('DATABASE_URL')
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL не установлен! Добавьте PostgreSQL на Railway:\n"
                "1. railway add (выберите PostgreSQL)\n"
                "2. Переменная DATABASE_URL появится автоматически"
            )
        _pool = await asyncpg.create_pool(
            database_url,
            min_size=2,
            max_size=10
        )
    return _pool


async def init_db():
    """Инициализация таблиц PostgreSQL"""
    pool = await get_pool()
    async with pool.acquire() as conn:

        # Guild settings
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id BIGINT PRIMARY KEY,
                log_channel_id BIGINT DEFAULT NULL,
                main_roles TEXT DEFAULT '[]'
            )
        ''')

        # AFK system
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS afk (
                user_id BIGINT,
                guild_id BIGINT,
                reason TEXT DEFAULT '',
                time_from TEXT DEFAULT '',
                time_to TEXT DEFAULT '',
                is_afk INTEGER DEFAULT 1,
                PRIMARY KEY (user_id, guild_id)
            )
        ''')

        # Inactive (vacation) system
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS inactive (
                user_id BIGINT,
                guild_id BIGINT,
                reason TEXT DEFAULT '',
                time_from TEXT DEFAULT '',
                time_to TEXT DEFAULT '',
                is_inactive INTEGER DEFAULT 1,
                PRIMARY KEY (user_id, guild_id)
            )
        ''')

        # Ticket settings
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS ticket_settings (
                guild_id BIGINT PRIMARY KEY,
                welcome_message TEXT DEFAULT 'Добро пожаловать в тикет!',
                call_message TEXT DEFAULT 'Обзвон начат!',
                questions TEXT DEFAULT '["Ваш вопрос 1", "Ваш вопрос 2"]',
                call_roles TEXT DEFAULT '[]',
                call_channels TEXT DEFAULT '[]',
                log_channel_id BIGINT DEFAULT NULL,
                category_id BIGINT DEFAULT NULL
            )
        ''')

        # Ticket panels (мульти-панели анкет)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS ticket_panels (
                panel_id SERIAL PRIMARY KEY,
                guild_id BIGINT,
                name TEXT DEFAULT 'Тикет',
                description TEXT DEFAULT '',
                button_label TEXT DEFAULT 'Подать заявку',
                button_emoji TEXT DEFAULT '📩',
                welcome_message TEXT DEFAULT 'Добро пожаловать в тикет!',
                call_message TEXT DEFAULT 'Обзвон начат!',
                questions TEXT DEFAULT '[]',
                call_roles TEXT DEFAULT '[]',
                call_channels TEXT DEFAULT '[]',
                admin_roles TEXT DEFAULT '[]',
                log_channel_id BIGINT DEFAULT NULL,
                category_id BIGINT DEFAULT NULL,
                banner_url TEXT DEFAULT '',
                select_placeholder TEXT DEFAULT 'Выберите семью для подачи заявки...'
            )
        ''')

        # Миграции ticket_panels
        try:
            await conn.execute("ALTER TABLE ticket_panels ADD COLUMN IF NOT EXISTS banner_url TEXT DEFAULT ''")
            await conn.execute("ALTER TABLE ticket_panels ADD COLUMN IF NOT EXISTS select_placeholder TEXT DEFAULT 'Выберите семью для подачи заявки...'")
        except Exception:
            pass

        # Active tickets
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                channel_id BIGINT PRIMARY KEY,
                guild_id BIGINT,
                user_id BIGINT,
                panel_id INTEGER DEFAULT NULL,
                answers TEXT DEFAULT '[]'
            )
        ''')

        # Миграция: добавляем колонку panel_id если её нет
        try:
            await conn.execute('ALTER TABLE tickets ADD COLUMN IF NOT EXISTS panel_id INTEGER DEFAULT NULL')
        except Exception:
            pass

        # Car system
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS cars (
                car_id SERIAL PRIMARY KEY,
                guild_id BIGINT,
                name TEXT,
                in_use INTEGER DEFAULT 0,
                used_by BIGINT DEFAULT NULL,
                used_at TEXT DEFAULT NULL
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS car_settings (
                guild_id BIGINT PRIMARY KEY,
                reset_time INTEGER DEFAULT 3600,
                activity_mode TEXT DEFAULT 'voice',
                required_roles TEXT DEFAULT '[]'
            )
        ''')

        # Warnings system
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS warnings (
                warning_id SERIAL PRIMARY KEY,
                guild_id BIGINT,
                user_id BIGINT,
                admin_id BIGINT,
                reason TEXT DEFAULT '',
                warn_role_id BIGINT DEFAULT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS warning_settings (
                guild_id BIGINT PRIMARY KEY,
                warn_roles TEXT DEFAULT '[]',
                admin_roles TEXT DEFAULT '[]'
            )
        ''')

        # Market system
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS market_products (
                product_id SERIAL PRIMARY KEY,
                guild_id BIGINT,
                name TEXT,
                price INTEGER DEFAULT 0,
                description TEXT DEFAULT ''
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS market_settings (
                guild_id BIGINT PRIMARY KEY,
                log_channel_id BIGINT DEFAULT NULL,
                roles TEXT DEFAULT '[]',
                admin_roles TEXT DEFAULT '[]',
                welcome_message TEXT DEFAULT 'В данном магазине вы можете обменять свои очки на товары.',
                exchange_rate INTEGER DEFAULT 2500,
                exchange_enabled INTEGER DEFAULT 1
            )
        ''')

        # Points system
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS points (
                user_id BIGINT,
                guild_id BIGINT,
                amount INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, guild_id)
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS point_events (
                event_id SERIAL PRIMARY KEY,
                guild_id BIGINT,
                name TEXT,
                points INTEGER DEFAULT 0
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS point_settings (
                guild_id BIGINT PRIMARY KEY,
                log_channel_id BIGINT DEFAULT NULL,
                button_channel_id BIGINT DEFAULT NULL,
                button_message_id BIGINT DEFAULT NULL
            )
        ''')


async def execute_query(query, params=None):
    """Выполняет запрос (INSERT, UPDATE, DELETE). Возвращает lastrowid для INSERT."""
    pool = await get_pool()
    pg_query, pg_params = _convert_params(query, params)

    async with pool.acquire() as conn:
        # Для INSERT добавляем RETURNING id если есть SERIAL колонка
        is_insert = query.strip().upper().startswith('INSERT')
        if is_insert and 'RETURNING' not in query.upper():
            # Определяем имя колонки SERIAL по имени таблицы
            serial_col = _get_serial_column(query)
            if serial_col:
                pg_query += f' RETURNING {serial_col}'
                if pg_params:
                    row = await conn.fetchrow(pg_query, *pg_params)
                else:
                    row = await conn.fetchrow(pg_query)
                return row[serial_col] if row else None

        if pg_params:
            result = await conn.execute(pg_query, *pg_params)
        else:
            result = await conn.execute(pg_query)
        return None


def _get_serial_column(insert_query):
    """Определяет имя SERIAL колонки для таблицы по INSERT-запросу."""
    # Маппинг: таблица → serial колонка
    serial_map = {
        'cars': 'car_id',
        'warnings': 'warning_id',
        'market_products': 'product_id',
        'point_events': 'event_id',
        'ticket_panels': 'panel_id',
    }
    query_upper = insert_query.upper()
    for table, col in serial_map.items():
        if table.upper() in query_upper:
            return col
    return None


async def fetch_one(query, params=None):
    """Возвращает одну строку как dict-like объект."""
    pool = await get_pool()
    pg_query, pg_params = _convert_params(query, params)

    async with pool.acquire() as conn:
        if pg_params:
            row = await conn.fetchrow(pg_query, *pg_params)
        else:
            row = await conn.fetchrow(pg_query)
        return row


async def fetch_all(query, params=None):
    """Возвращает все строки как список dict-like объектов."""
    pool = await get_pool()
    pg_query, pg_params = _convert_params(query, params)

    async with pool.acquire() as conn:
        if pg_params:
            rows = await conn.fetch(pg_query, *pg_params)
        else:
            rows = await conn.fetch(pg_query)
        return rows
