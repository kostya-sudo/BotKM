import asyncio
import json
import logging
import math
import os
import random
import re
import secrets
import string
import time
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
from vkbottle import API, Bot, CtxStorage, GroupEventType
from vkbottle.http import AiohttpClient
import aiohttp
from vkbottle.bot import Blueprint, Message, rules
from vkbottle.tools import Keyboard, KeyboardButtonColor, Callback


# ==================== Владельцы бота ====================
# Основные владельцы бота
BOT_OWNER_IDS = (662204206, 1054782531)
BOT_OWNER_ID = BOT_OWNER_IDS[0]

# Токен группы для прямых API запросов (будет загружен из config)
VK_GROUP_TOKEN: str = ""


async def load_vk_token() -> str:
    """Загрузить VK токен из конфига"""
    global VK_GROUP_TOKEN
    if VK_GROUP_TOKEN:
        return VK_GROUP_TOKEN
    # Пробуем разные варианты расположения/названия файла конфига.
    # Частая проблема: бот запускают не из папки проекта или файл назван с опечаткой.
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_paths = [
        os.path.join(base_dir, "config.json"),
        os.path.join(os.getcwd(), "config.json"),
        os.path.join(base_dir, "config.jsomn"),  # fallback для опечатки в имени файла
        os.path.join(os.getcwd(), "config.jsomn"),
    ]

    last_error = None
    for cfg_path in config_paths:
        try:
            if not os.path.exists(cfg_path):
                continue
            # utf-8-sig корректно читает UTF-8 с BOM и без BOM
            with open(cfg_path, "r", encoding="utf-8-sig") as f:
                config = json.load(f)
            VK_GROUP_TOKEN = str(config.get("vk_token", "")).strip()
            if VK_GROUP_TOKEN:
                logger.info(f"VK токен загружен из: {cfg_path}")
                return VK_GROUP_TOKEN
        except Exception as exc:
            last_error = exc

    if last_error:
        logger.error(f"Не удалось прочитать файл конфига: {last_error}")
    else:
        logger.error("Файл конфигурации не найден (ожидался config.json или config.jsomn)")
    return ""


async def vk_api_request(method: str, **params) -> dict:
    """Прямой запрос к VK API через HTTP"""
    token = await load_vk_token()
    if not token:
        raise Exception("VK токен не загружен")

    url = "https://api.vk.com/method/" + method
    params["access_token"] = token
    # Для messages.changeConversationMemberRestrictions нужна более новая версия API
    params["v"] = "5.199"

    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=params) as resp:
            result = await resp.json()

    if "error" in result:
        error_msg = result["error"].get("error_msg", "Unknown error")
        raise Exception(f"VK API error: {error_msg}")

    return result.get("response", {})


# ==================== Лимиты на выдачу денег ====================
MONEY_DAILY_LIMIT = 5_000_000  # 5 миллионов в день
money_daily_limits: dict[int, tuple[int, str]] = {}  # user_id -> (used_amount, date_string)


def get_today_date_string() -> str:
    """Получить текущую дату в формате YYYY-MM-DD"""
    return datetime.now().strftime("%Y-%m-%d")


def check_money_limit(user_id: int, amount: int) -> tuple[bool, str, int]:
    """
    Проверить лимит на выдачу денег.
    Возвращает (достаточно_ли_лимита, причина_если_нет, оставшийся_лимит).
    Для владельцев бота лимит не ограничен.
    """
    # Владельцы бота не имеют ограничений
    if user_id in BOT_OWNER_IDS:
        return True, "", MONEY_DAILY_LIMIT
    
    today = get_today_date_string()
    current = money_daily_limits.get(user_id)
    
    if current is None:
        # Первый раз сегодня
        return True, "", MONEY_DAILY_LIMIT
    
    used_amount, date_str = current
    if date_str != today:
        # Новый день - сбрасываем
        money_daily_limits[user_id] = (0, today)
        return True, "", MONEY_DAILY_LIMIT
    
    remaining = MONEY_DAILY_LIMIT - used_amount
    if amount <= remaining:
        return True, "", remaining
    return False, f"❌ Достигнут дневной лимит! Осталось: {remaining}$ из {MONEY_DAILY_LIMIT}$", remaining


def add_money_to_limit(user_id: int, amount: int) -> None:
    """Добавить сумму к использованному лимиту за день."""
    # Владельцы бота не учитываются в лимите
    if user_id in BOT_OWNER_IDS:
        return
    
    today = get_today_date_string()
    current = money_daily_limits.get(user_id)
    
    if current is None:
        money_daily_limits[user_id] = (amount, today)
    else:
        used_amount, date_str = current
        if date_str != today:
            money_daily_limits[user_id] = (amount, today)
        else:
            money_daily_limits[user_id] = (used_amount + amount, today)


async def check_is_leader_or_owner(user_id: int) -> bool:
    """Проверить, является ли пользователь владельцем или руководством бота."""
    if user_id in BOT_OWNER_IDS:
        return True
    return await db.is_bot_leader(user_id)


# ==================== Класс Database ====================
class Database:
    def __init__(self, db_name: str = "database.db"):
        self.db_name = db_name

    async def init_db(self):
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, is_admin INTEGER DEFAULT 0, notify INTEGER DEFAULT 1, silent INTEGER DEFAULT 0, allow_games INTEGER DEFAULT 1, allow_community_add INTEGER DEFAULT 1, auto_kick_on_leave INTEGER DEFAULT 0)")
            await db.execute("CREATE TABLE IF NOT EXISTS warnings (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, reason TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            await db.execute("CREATE TABLE IF NOT EXISTS mutes (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, reason TEXT, duration INTEGER DEFAULT -1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, end_time TIMESTAMP)")
            await db.execute("CREATE TABLE IF NOT EXISTS roles (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, name TEXT NOT NULL, priority INTEGER NOT NULL)")
            await db.execute("CREATE TABLE IF NOT EXISTS user_roles (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role_id INTEGER NOT NULL)")
            await db.execute("CREATE TABLE IF NOT EXISTS bans (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, reason TEXT, duration INTEGER DEFAULT -1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, end_time TIMESTAMP)")
            await db.execute("CREATE TABLE IF NOT EXISTS nicknames (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, nickname TEXT NOT NULL)")
            await db.execute("CREATE TABLE IF NOT EXISTS sysbans (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, reason TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            await db.execute("CREATE TABLE IF NOT EXISTS bot_owners (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            await db.execute("CREATE TABLE IF NOT EXISTS cmd_priorities (id INTEGER PRIMARY KEY AUTOINCREMENT, command TEXT NOT NULL UNIQUE, priority INTEGER NOT NULL)")
            await db.execute("""CREATE TABLE IF NOT EXISTS user_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moder TEXT,
                action TEXT NOT NULL,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            await db.execute("""CREATE TABLE IF NOT EXISTS chat_welcome (
                chat_id INTEGER PRIMARY KEY,
                welcome_text TEXT NOT NULL
            )""")
            await db.execute("""CREATE TABLE IF NOT EXISTS roulette_users (
                user_id INTEGER PRIMARY KEY,
                spins INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            await db.execute("""CREATE TABLE IF NOT EXISTS user_system_cmd_access (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                command TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, command)
            )""")
            
            # Таблицы для ролей бота
            await db.execute("CREATE TABLE IF NOT EXISTS bot_leaders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            await db.execute("CREATE TABLE IF NOT EXISTS bot_admins (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            await db.execute("CREATE TABLE IF NOT EXISTS bot_moderators (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            await db.execute("CREATE TABLE IF NOT EXISTS bot_helpers (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, level INTEGER DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            
            # Таблица для тикетов
            await db.execute("""CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                peer_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            await db.execute("""CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT UNIQUE,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                report_text TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                answered_by INTEGER,
                answer_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                answered_at TIMESTAMP
            )""")
            
            await db.commit()

            # Миграция: добавляем колонку notify если её нет
            try:
                await db.execute("ALTER TABLE chats ADD COLUMN notify INTEGER DEFAULT 1")
                await db.commit()
            except aiosqlite.OperationalError:
                pass  # Колонка уже существует

            # Миграция: добавляем колонку sysbans если таблицы нет
            try:
                await db.execute("CREATE TABLE IF NOT EXISTS sysbans (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, reason TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
                await db.commit()
            except:
                pass
            
            # Миграция: добавляем таблицу bot_owners если её нет
            try:
                await db.execute("CREATE TABLE IF NOT EXISTS bot_owners (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
                await db.commit()
            except:
                pass
            
            # Добавляем второго владельца бота
            try:
                await db.execute("INSERT OR IGNORE INTO bot_owners (user_id) VALUES (1054782531)")
                await db.commit()
            except:
                pass
            
            # Миграция: добавляем колонку silent если её нет
            try:
                await db.execute("ALTER TABLE chats ADD COLUMN silent INTEGER DEFAULT 0")
                await db.commit()
            except aiosqlite.OperationalError:
                pass  # Колонка уже существует

            # Миграция: добавляем колонку sub_community если её нет
            try:
                await db.execute("ALTER TABLE chats ADD COLUMN sub_community INTEGER DEFAULT 0")
                await db.commit()
            except aiosqlite.OperationalError:
                pass  # Колонка уже существует

            # Миграция: добавляем колонки настроек беседы если их нет
            try:
                await db.execute("ALTER TABLE chats ADD COLUMN allow_games INTEGER DEFAULT 1")
                await db.commit()
            except aiosqlite.OperationalError:
                pass
            try:
                await db.execute("ALTER TABLE chats ADD COLUMN allow_community_add INTEGER DEFAULT 1")
                await db.commit()
            except aiosqlite.OperationalError:
                pass
            try:
                await db.execute("ALTER TABLE chats ADD COLUMN auto_kick_on_leave INTEGER DEFAULT 0")
                await db.commit()
            except aiosqlite.OperationalError:
                pass
            
            # Таблица для банов репортов
            await db.execute("""CREATE TABLE IF NOT EXISTS report_bans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                reason TEXT,
                duration_minutes INTEGER,
                banned_until TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")

            # Таблица для объединений бесед (unity)
            await db.execute("""CREATE TABLE IF NOT EXISTS unions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                union_code TEXT NOT NULL UNIQUE,
                owner_chat_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            await db.execute("""CREATE TABLE IF NOT EXISTS union_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                union_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(union_id, chat_id)
            )""")

            # Миграции для report_bans
            try:
                await db.execute("ALTER TABLE report_bans ADD COLUMN duration_minutes INTEGER")
                await db.commit()
            except aiosqlite.OperationalError:
                pass
            try:
                await db.execute("ALTER TABLE report_bans ADD COLUMN banned_until TIMESTAMP")
                await db.commit()
            except aiosqlite.OperationalError:
                pass
            
            # Таблицы для экономики / браков / бизнеса
            await db.execute("""CREATE TABLE IF NOT EXISTS economy_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                balance INTEGER NOT NULL DEFAULT 1000,
                last_job_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            await db.execute("""CREATE TABLE IF NOT EXISTS marriages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user1_id INTEGER NOT NULL UNIQUE,
                user2_id INTEGER NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            await db.execute("""CREATE TABLE IF NOT EXISTS marriage_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user_id INTEGER NOT NULL,
                to_user_id INTEGER NOT NULL,
                peer_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            await db.execute("""CREATE TABLE IF NOT EXISTS businesses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                level INTEGER NOT NULL DEFAULT 0,
                total_earned INTEGER NOT NULL DEFAULT 0,
                last_collect_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            await db.execute("""CREATE TABLE IF NOT EXISTS my_businesses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                raw_material INTEGER NOT NULL DEFAULT 0,
                workers INTEGER NOT NULL DEFAULT 0,
                ad_level INTEGER NOT NULL DEFAULT 0,
                cashbox INTEGER NOT NULL DEFAULT 0,
                total_profit INTEGER NOT NULL DEFAULT 0,
                tax_debt INTEGER NOT NULL DEFAULT 0,
                last_profit_at TIMESTAMP,
                last_tax_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            
            # Таблица для ВИП-пользователей
            await db.execute("""CREATE TABLE IF NOT EXISTS vip_users (
                user_id INTEGER PRIMARY KEY,
                days INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP
            )""")
            
            await db.commit()

    async def add_chat(self, chat_id: int) -> bool:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT chat_id FROM chats WHERE chat_id = ?", (chat_id,))
            existing = await cursor.fetchone()
            if existing is None:
                await db.execute("INSERT INTO chats (chat_id, is_admin) VALUES (?,0)", (chat_id,))
                # Создаём базовые роли
                base_roles = [
                    (chat_id, "Владелец беседы", 100),
                    (chat_id, "Зам. владельца", 90),
                    (chat_id, "Главный админ", 75),
                    (chat_id, "Зам. главного админа", 65),
                    (chat_id, "Старший админ", 40),
                    (chat_id, "Админ", 30),
                    (chat_id, "Младший админ", 20),
                    (chat_id, "Модератор", 10),
                    (chat_id, "Пользователь", 0),
                ]
                for chat_id, name, priority in base_roles:
                    await db.execute("INSERT INTO roles (chat_id, name, priority) VALUES (?, ?, ?)", (chat_id, name, priority))
                await db.commit()
                return True
            return False

    async def get_chat_status(self, chat_id: int) -> Optional[int]:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT is_admin FROM chats WHERE chat_id = ?", (chat_id,))
            result = await cursor.fetchone()
            return result[0] if result else None

    async def set_admin_status(self, chat_id: int, is_admin: bool) -> bool:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("UPDATE chats SET is_admin = ? WHERE chat_id = ?", (1 if is_admin else 0, chat_id))
            await db.commit()
            return True

    # ==================== Welcome ====================
    async def set_welcome(self, chat_id: int, welcome_text: str) -> None:
        welcome_text = (welcome_text or "").strip()
        async with aiosqlite.connect(self.db_name) as db:
            # SQLite UPSERT
            await db.execute(
                """INSERT INTO chat_welcome (chat_id, welcome_text)
                   VALUES (?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET welcome_text = excluded.welcome_text""",
                (int(chat_id), welcome_text)
            )
            await db.commit()

    async def get_welcome(self, chat_id: int) -> Optional[str]:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT welcome_text FROM chat_welcome WHERE chat_id = ?", (int(chat_id),))
            row = await cursor.fetchone()
            return row[0] if row else None

    # ==================== Рулетка ====================
    async def ensure_roulette_user(self, user_id: int) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("INSERT OR IGNORE INTO roulette_users (user_id) VALUES (?)", (int(user_id),))
            await db.commit()

    async def add_roulette_spins(self, user_id: int, amount: int) -> int:
        await self.ensure_roulette_user(user_id)
        value = max(0, int(amount))
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute(
                "UPDATE roulette_users SET spins = spins + ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (value, int(user_id))
            )
            await db.commit()
            cursor = await db.execute("SELECT spins FROM roulette_users WHERE user_id = ?", (int(user_id),))
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def get_roulette_spins(self, user_id: int) -> int:
        await self.ensure_roulette_user(user_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT spins FROM roulette_users WHERE user_id = ?", (int(user_id),))
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def consume_roulette_spin(self, user_id: int) -> bool:
        await self.ensure_roulette_user(user_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "UPDATE roulette_users SET spins = spins - 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND spins > 0",
                (int(user_id),)
            )
            await db.commit()
            return cursor.rowcount > 0

    # ==================== Доступ к системным командам ====================
    async def grant_system_cmd_access(self, user_id: int, command: str) -> None:
        cmd = (command or "").strip().lower().lstrip("/").lstrip("!")
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute(
                "INSERT OR IGNORE INTO user_system_cmd_access (user_id, command) VALUES (?, ?)",
                (int(user_id), cmd)
            )
            await db.commit()

    async def has_system_cmd_access(self, user_id: int, command: str) -> bool:
        cmd = (command or "").strip().lower().lstrip("/").lstrip("!")
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT 1 FROM user_system_cmd_access WHERE user_id = ? AND command = ?",
                (int(user_id), cmd)
            )
            return await cursor.fetchone() is not None

    async def revoke_system_cmd_access(self, user_id: int, command: str) -> bool:
        cmd = (command or "").strip().lower().lstrip("/").lstrip("!")
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "DELETE FROM user_system_cmd_access WHERE user_id = ? AND command = ?",
                (int(user_id), cmd)
            )
            await db.commit()
            return cursor.rowcount > 0
        
    async def add_warning(self, chat_id: int, user_id: int, reason: str = "") -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("INSERT INTO warnings (chat_id, user_id, reason) VALUES (?, ?, ?)", (chat_id, user_id, reason))
            await db.commit()

    async def get_warnings_count(self, chat_id: int, user_id: int) -> int:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            result = await cursor.fetchone()
            return result[0] if result else 0

    async def get_warnings(self, chat_id: int, user_id: int) -> list:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT id, reason, created_at FROM warnings WHERE chat_id = ? AND user_id = ? ORDER BY created_at ASC", (chat_id, user_id))
            return await cursor.fetchall()

    async def remove_warnings_count(self, chat_id: int, user_id: int, count: int) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT id FROM warnings WHERE chat_id = ? AND user_id = ? ORDER BY created_at ASC LIMIT ?", (chat_id, user_id, count))
            ids = await cursor.fetchall()
            for (wid,) in ids:
                await db.execute("DELETE FROM warnings WHERE id = ?", (wid,))
            await db.commit()

    async def clear_warnings(self, chat_id: int, user_id: int) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("DELETE FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            await db.commit()


    # ==================== Роли ====================
    async def ensure_base_roles(self, chat_id: int) -> None:
        """Создаёт базовые роли если их нет"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM roles WHERE chat_id = ?", (chat_id,))
            count = await cursor.fetchone()
            if count[0] == 0:
                base_roles = [
                    (chat_id, "Владелец беседы", 100),
                    (chat_id, "Зам. владельца", 90),
                    (chat_id, "Главный админ", 75),
                    (chat_id, "Зам. главного админа", 65),
                    (chat_id, "Старший админ", 40),
                    (chat_id, "Админ", 30),
                    (chat_id, "Младший админ", 20),
                    (chat_id, "Модератор", 10),
                    (chat_id, "Пользователь", 0),
                ]
                for cid, name, priority in base_roles:
                    await db.execute("INSERT INTO roles (chat_id, name, priority) VALUES (?, ?, ?)", (cid, name, priority))
                await db.commit()

    async def add_role(self, chat_id: int, name: str, priority: int) -> None:
        # Сначала убедимся, что базовые роли есть
        await self.ensure_base_roles(chat_id)
        
        async with aiosqlite.connect(self.db_name) as db:
            # Проверяем, есть ли роль с таким приоритетом
            cursor = await db.execute("SELECT id FROM roles WHERE chat_id = ? AND priority = ?", (chat_id, priority))
            existing = await cursor.fetchone()
            if existing:
                # Обновляем существующую роль
                await db.execute("UPDATE roles SET name = ? WHERE chat_id = ? AND priority = ?", (name, chat_id, priority))
            else:
                # Создаём новую роль
                await db.execute("INSERT INTO roles (chat_id, name, priority) VALUES (?, ?, ?)", (chat_id, name, priority))
            await db.commit()

    async def delete_role(self, chat_id: int, priority: int) -> bool:
        """Удалить роль по приоритету"""
        async with aiosqlite.connect(self.db_name) as db:
            # Проверяем, есть ли роль
            cursor = await db.execute("SELECT id FROM roles WHERE chat_id = ? AND priority = ?", (chat_id, priority))
            existing = await cursor.fetchone()
            if not existing:
                return False
            # Удаляем роль
            await db.execute("DELETE FROM roles WHERE chat_id = ? AND priority = ?", (chat_id, priority))
            # Также удаляем у пользователей эту роль
            await db.execute("""
                DELETE FROM user_roles 
                WHERE chat_id = ? AND role_id IN (
                    SELECT id FROM roles WHERE chat_id = ? AND priority = ?
                )
            """, (chat_id, chat_id, priority))
            await db.commit()
            return True
        
    async def get_roles(self, chat_id: int) -> list:
        # Сначала убедимся, что базовые роли есть
        await self.ensure_base_roles(chat_id)
        
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT name, priority FROM roles WHERE chat_id = ? ORDER BY priority DESC", (chat_id,))
            return await cursor.fetchall()

    async def set_user_role(self, chat_id: int, user_id: int, priority: int) -> bool:
        async with aiosqlite.connect(self.db_name) as db:
            # Находим роль с данным приоритетом
            cursor = await db.execute("SELECT id FROM roles WHERE chat_id = ? AND priority = ?", (chat_id, priority))
            role = await cursor.fetchone()
            if not role:
                return False
            role_id = role[0]
            # Удаляем старые роли пользователя
            await db.execute("DELETE FROM user_roles WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            await db.execute("INSERT INTO user_roles (chat_id, user_id, role_id) VALUES (?, ?, ?)", (chat_id, user_id, role_id))
            await db.commit()
            return True
        
    async def set_user_role_by_name(self, chat_id: int, user_id: int, role_name: str) -> bool:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT id FROM roles WHERE chat_id = ? AND name = ?", (chat_id, role_name))
            role = await cursor.fetchone()
            if not role:
                return False
            role_id = role[0]
            await db.execute("DELETE FROM user_roles WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            await db.execute("INSERT INTO user_roles (chat_id, user_id, role_id) VALUES (?, ?, ?)", (chat_id, user_id, role_id))
            await db.commit()
            return True

    async def get_user_role(self, chat_id: int, user_id: int) -> tuple:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("""
                SELECT r.name, r.priority FROM user_roles ur
                JOIN roles r ON ur.role_id = r.id
                WHERE ur.chat_id = ? AND ur.user_id = ?
            """, (chat_id, user_id))
            return await cursor.fetchone()

    # ==================== Баны ====================
    async def add_ban(self, chat_id: int, user_id: int, days: int = -1, reason: str = "") -> None:
        async with aiosqlite.connect(self.db_name) as db:
            if days == -1:
                await db.execute("INSERT INTO bans (chat_id, user_id, reason, duration, end_time) VALUES (?, ?, ?, ?, NULL)", (chat_id, user_id, reason, days))
            else:
                end_time = datetime.now() + timedelta(days=days)
                await db.execute("INSERT INTO bans (chat_id, user_id, reason, duration, end_time) VALUES (?, ?, ?, ?, ?)", (chat_id, user_id, reason, days, end_time.isoformat()))
            await db.commit()

    async def is_banned(self, chat_id: int, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT end_time FROM bans WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            result = await cursor.fetchone()
            if not result:
                return False
            end_time = result[0]
            if end_time is None:
                return True
            try:
                end_dt = datetime.fromisoformat(end_time)
                if end_dt > datetime.now():
                    return True
                else:
                    await db.execute("DELETE FROM bans WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
                    await db.commit()
                    return False
            except Exception:
                return False

    async def remove_ban(self, chat_id: int, user_id: int) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("DELETE FROM bans WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            await db.commit()

    async def get_banned_users(self, chat_id: int) -> list:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT user_id, reason, duration, end_time FROM bans WHERE chat_id = ?", (chat_id,))
            return await cursor.fetchall()

    async def get_admins(self, chat_id: int) -> list:
        """Получить список администраторов с приоритетом 1-100"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("""
                SELECT u.user_id, r.name, r.priority 
                FROM user_roles u
                JOIN roles r ON u.role_id = r.id
                WHERE u.chat_id = ? AND r.priority >= 1 AND r.priority <= 100
                ORDER BY r.priority DESC
            """, (chat_id,))
            return await cursor.fetchall()

    # ==================== Ники ====================
    async def set_nickname(self, chat_id: int, user_id: int, nickname: str) -> None:
        """Установить ник пользователю"""
        async with aiosqlite.connect(self.db_name) as db:
            # Удаляем старый ник
            await db.execute("DELETE FROM nicknames WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            # Добавляем новый ник
            await db.execute("INSERT INTO nicknames (chat_id, user_id, nickname) VALUES (?, ?, ?)", (chat_id, user_id, nickname))
            await db.commit()

    async def get_nickname(self, chat_id: int, user_id: int) -> Optional[str]:
        """Получить ник пользователя"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT nickname FROM nicknames WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            result = await cursor.fetchone()
            return result[0] if result else None

    async def remove_nickname(self, chat_id: int, user_id: int) -> None:
        """Удалить ник пользователя"""
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("DELETE FROM nicknames WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            await db.commit()

    async def get_all_nicknames(self, chat_id: int) -> list:
        """Получить все ники в чате"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT user_id, nickname FROM nicknames WHERE chat_id = ?", (chat_id,))
            return await cursor.fetchall()

    # ==================== Системный бан ====================
    async def add_sysban(self, user_id: int, reason: str = "") -> None:
        """Забанить пользователя в боте глобально"""
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("INSERT INTO sysbans (user_id, reason) VALUES (?, ?)", (user_id, reason))
            await db.commit()

    async def is_sysbanned(self, user_id: int) -> bool:
        """Проверить, забанен ли пользователь в боте"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT id FROM sysbans WHERE user_id = ?", (user_id,))
            result = await cursor.fetchone()
            return result is not None

    async def remove_sysban(self, user_id: int) -> None:
        """Разбанить пользователя в боте"""
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("DELETE FROM sysbans WHERE user_id = ?", (user_id,))
            await db.commit()

    async def get_sysbanned_users(self) -> list:
        """Получить всех забаненных пользователей"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT user_id, reason, created_at FROM sysbans")
            return await cursor.fetchall()

    # ==================== Уведомления ====================
    async def set_notify(self, chat_id: int, notify: bool) -> None:
        """Включить/выключить уведомления для чата"""
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("UPDATE chats SET notify = ? WHERE chat_id = ?", (1 if notify else 0, chat_id))
            await db.commit()

    async def get_notify(self, chat_id: int) -> bool:
        """Получить статус уведомлений для чата"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT notify FROM chats WHERE chat_id = ?", (chat_id,))
            result = await cursor.fetchone()
            return result[0] == 1 if result else True

    async def get_all_chats_with_notify(self) -> list:
        """Получить все чаты с уведомлениями"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT chat_id FROM chats WHERE notify = 1")
            return [row[0] for row in await cursor.fetchall()]

    # ==================== Режим тишины ====================
    async def set_silent_mode(self, chat_id: int, silent: bool) -> None:
        """Включить/выключить режим тишины"""
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("UPDATE chats SET silent = ? WHERE chat_id = ?", (1 if silent else 0, chat_id))
            await db.commit()

    async def get_silent_mode(self, chat_id: int) -> bool:
        """Получить статус режима тишины"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT silent FROM chats WHERE chat_id = ?", (chat_id,))
            result = await cursor.fetchone()
            return result[0] == 1 if result else False

    # ==================== Проверка подписки на сообщество ====================
    async def set_sub_community(self, chat_id: int, community_id: int) -> None:
        """Установить ID сообщества для проверки подписки"""
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("UPDATE chats SET sub_community = ? WHERE chat_id = ?", (community_id, chat_id))
            await db.commit()

    async def get_sub_community(self, chat_id: int) -> int:
        """Получить ID сообщества для проверки подписки"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT sub_community FROM chats WHERE chat_id = ?", (chat_id,))
            result = await cursor.fetchone()
            return int(result[0]) if result and result[0] else 0

    # ==================== Настройки беседы ====================
    async def get_chat_settings(self, chat_id: int) -> tuple[bool, bool, bool]:
        """Вернёт (allow_games, allow_community_add, auto_kick_on_leave)."""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT allow_games, allow_community_add, auto_kick_on_leave FROM chats WHERE chat_id = ?",
                (int(chat_id),)
            )
            row = await cursor.fetchone()
            if not row:
                return True, True, False
            return bool(row[0]), bool(row[1]), bool(row[2])

    async def get_allow_games(self, chat_id: int) -> bool:
        allow_games, _, _ = await self.get_chat_settings(chat_id)
        return allow_games

    async def set_allow_games(self, chat_id: int, enabled: bool) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute(
                "UPDATE chats SET allow_games = ? WHERE chat_id = ?",
                (1 if enabled else 0, int(chat_id))
            )
            await db.commit()

    async def get_allow_community_add(self, chat_id: int) -> bool:
        _, allow_community_add, _ = await self.get_chat_settings(chat_id)
        return allow_community_add

    async def set_allow_community_add(self, chat_id: int, enabled: bool) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute(
                "UPDATE chats SET allow_community_add = ? WHERE chat_id = ?",
                (1 if enabled else 0, int(chat_id))
            )
            await db.commit()

    async def get_auto_kick_on_leave(self, chat_id: int) -> bool:
        _, _, auto_kick_on_leave = await self.get_chat_settings(chat_id)
        return auto_kick_on_leave

    async def set_auto_kick_on_leave(self, chat_id: int, enabled: bool) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute(
                "UPDATE chats SET auto_kick_on_leave = ? WHERE chat_id = ?",
                (1 if enabled else 0, int(chat_id))
            )
            await db.commit()

    async def get_all_chats(self) -> list:
        """Получить все чаты"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT chat_id FROM chats")
            return [row[0] for row in await cursor.fetchall()]

    # ==================== Дополнительные владельцы бота ====================
    async def add_bot_owner(self, user_id: int) -> bool:
        """Добавить дополнительного владельца бота"""
        async with aiosqlite.connect(self.db_name) as db:
            try:
                await db.execute("INSERT INTO bot_owners (user_id) VALUES (?)", (user_id,))
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False  # Уже существует

    async def remove_bot_owner(self, user_id: int) -> bool:
        """Удалить дополнительного владельца бота"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("DELETE FROM bot_owners WHERE user_id = ?", (user_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def is_bot_owner(self, user_id: int) -> bool:
        """Проверить, является ли пользователь владельцем бота (включая дополнительных)"""
        # Проверяем основного владельца
        if user_id in BOT_OWNER_IDS:
            return True
        # Проверяем дополнительных владельцев
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT id FROM bot_owners WHERE user_id = ?", (user_id,))
            result = await cursor.fetchone()
            return result is not None
        
    async def get_bot_owners(self) -> list:
        """Получить всех дополнительных владельцев бота"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT user_id, created_at FROM bot_owners ORDER BY created_at")
            return await cursor.fetchall()

    # ==================== Приоритеты команд ====================
    async def set_cmd_priority(self, command: str, priority: int) -> None:
        """Установить приоритет для команды"""
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("""
                INSERT INTO cmd_priorities (command, priority) VALUES (?, ?)
                ON CONFLICT(command) DO UPDATE SET priority = ?
            """, (command, priority, priority))
            await db.commit()

    async def get_cmd_priority(self, command: str) -> Optional[int]:
        """Получить приоритет команды"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT priority FROM cmd_priorities WHERE command = ?", (command,))
            result = await cursor.fetchone()
            return result[0] if result else None

    async def get_all_cmd_priorities(self) -> list:
        """Получить все приоритеты команд"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT command, priority FROM cmd_priorities ORDER BY priority DESC")
            return await cursor.fetchall()

    async def delete_cmd_priority(self, command: str) -> bool:
        """Удалить приоритет команды"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("DELETE FROM cmd_priorities WHERE command = ?", (command,))
            await db.commit()
            return cursor.rowcount > 0

    # ==================== Руководство бота ====================
    async def add_bot_leader(self, user_id: int) -> bool:
        """Добавить руководство бота"""
        async with aiosqlite.connect(self.db_name) as db:
            try:
                await db.execute("INSERT INTO bot_leaders (user_id) VALUES (?)", (user_id,))
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def remove_bot_leader(self, user_id: int) -> bool:
        """Удалить руководство бота"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("DELETE FROM bot_leaders WHERE user_id = ?", (user_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def is_bot_leader(self, user_id: int) -> bool:
        """Проверить, является ли пользователь руководством бота"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT id FROM bot_leaders WHERE user_id = ?", (user_id,))
            return await cursor.fetchone() is not None

    # ==================== Админы бота ====================
    async def add_bot_admin(self, user_id: int) -> bool:
        """Добавить админа бота"""
        async with aiosqlite.connect(self.db_name) as db:
            try:
                await db.execute("INSERT INTO bot_admins (user_id) VALUES (?)", (user_id,))
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def remove_bot_admin(self, user_id: int) -> bool:
        """Удалить админа бота"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("DELETE FROM bot_admins WHERE user_id = ?", (user_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def is_bot_admin(self, user_id: int) -> bool:
        """Проверить, является ли пользователь админом бота"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT id FROM bot_admins WHERE user_id = ?", (user_id,))
            return await cursor.fetchone() is not None

    # ==================== Модераторы бота ====================
    async def add_bot_moderator(self, user_id: int) -> bool:
        """Добавить модератора бота"""
        async with aiosqlite.connect(self.db_name) as db:
            try:
                await db.execute("INSERT INTO bot_moderators (user_id) VALUES (?)", (user_id,))
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def remove_bot_moderator(self, user_id: int) -> bool:
        """Удалить модератора бота"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("DELETE FROM bot_moderators WHERE user_id = ?", (user_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def is_bot_moderator(self, user_id: int) -> bool:
        """Проверить, является ли пользователь модератором бота"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT id FROM bot_moderators WHERE user_id = ?", (user_id,))
            return await cursor.fetchone() is not None

    # ==================== Хелперы бота ====================
    async def add_bot_helper(self, user_id: int, level: int = 1) -> bool:
        """Добавить хелпера бота"""
        async with aiosqlite.connect(self.db_name) as db:
            try:
                await db.execute("INSERT INTO bot_helpers (user_id, level) VALUES (?, ?)", (user_id, level))
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                # Обновляем уровень если уже существует
                await db.execute("UPDATE bot_helpers SET level = ? WHERE user_id = ?", (level, user_id))
                await db.commit()
                return True

    async def remove_bot_helper(self, user_id: int) -> bool:
        """Удалить хелпера бота"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("DELETE FROM bot_helpers WHERE user_id = ?", (user_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def is_bot_helper(self, user_id: int) -> bool:
        """Проверить, является ли пользователь хелпером бота"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT id FROM bot_helpers WHERE user_id = ?", (user_id,))
            return await cursor.fetchone() is not None

    async def get_bot_helper_level(self, user_id: int) -> int:
        """Получить уровень хелпера"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT level FROM bot_helpers WHERE user_id = ?", (user_id,))
            result = await cursor.fetchone()
            return result[0] if result else 0

    async def get_all_helpers(self) -> list:
        """Получить всех хелперов"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT user_id, level FROM bot_helpers ORDER BY level DESC")
            return await cursor.fetchall()

    # ==================== Объединения бесед (Unity) ====================
    async def create_union(self, owner_chat_id: int, union_code: str) -> bool:
        """Создать объединение бесед"""
        async with aiosqlite.connect(self.db_name) as db:
            try:
                cursor = await db.execute(
                    "INSERT INTO unions (union_code, owner_chat_id) VALUES (?, ?)",
                    (union_code, owner_chat_id)
                )
                await db.commit()
                union_id = cursor.lastrowid
                # Добавляем создающий чат в объединение
                await db.execute(
                    "INSERT INTO union_chats (union_id, chat_id) VALUES (?, ?)",
                    (union_id, owner_chat_id)
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def get_union_by_code(self, union_code: str) -> Optional[tuple]:
        """Получить объединение по коду"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT id, union_code, owner_chat_id, created_at FROM unions WHERE union_code = ?",
                (union_code,)
            )
            return await cursor.fetchone()

    async def get_union_by_chat(self, chat_id: int) -> Optional[tuple]:
        """Получить объединение, к которому принадлежит чат"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("""
                SELECT u.id, u.union_code, u.owner_chat_id, u.created_at
                FROM unions u
                JOIN union_chats uc ON u.id = uc.union_id
                WHERE uc.chat_id = ?
            """, (chat_id,))
            return await cursor.fetchone()

    async def join_union(self, union_code: str, chat_id: int) -> tuple[bool, str]:
        """Присоединить чат к объединению"""
        union = await self.get_union_by_code(union_code)
        if not union:
            return False, "Объединение не найдено"

        union_id = union[0]
        async with aiosqlite.connect(self.db_name) as db:
            try:
                await db.execute(
                    "INSERT INTO union_chats (union_id, chat_id) VALUES (?, ?)",
                    (union_id, chat_id)
                )
                await db.commit()
                return True, "Чат присоединён к объединению"
            except aiosqlite.IntegrityError:
                return False, "Этот чат уже в объединении"

    async def leave_union(self, chat_id: int) -> tuple[bool, str]:
        """Покинуть объединение"""
        union = await self.get_union_by_chat(chat_id)
        if not union:
            return False, "Этот чат не состоит в объединении"

        union_id, owner_chat_id = union[0], union[2]
        if chat_id == owner_chat_id:
            return False, "Владелец не может покинуть объединение"

        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("DELETE FROM union_chats WHERE union_id = ? AND chat_id = ?", (union_id, chat_id))
            await db.commit()
        return True, "Чат покинул объединение"

    async def get_union_chats(self, union_id: int) -> list:
        """Получить все чаты в объединении"""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT chat_id FROM union_chats WHERE union_id = ?",
                (union_id,)
            )
            return [row[0] for row in await cursor.fetchall()]

    async def is_union_admin(self, chat_id: int, user_id: int) -> bool:
        """Проверить, является ли пользователь админом объединения (владелец любого чата в юнионе)"""
        union = await self.get_union_by_chat(chat_id)
        if not union:
            return False
        union_id, owner_chat_id = union[0], union[2]
        # Владелец объединения - владелец создавшего чата
        user_role = await self.get_user_role(owner_chat_id, user_id)
        if user_role and user_role[1] == 100:
            return True
        return False

    # ==================== Тикеты ====================
    async def create_ticket(self, user_id: int, peer_id: int, text: str) -> int:
        """Создать тикет."""
        clean_text = (text or "").strip()
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "INSERT INTO tickets (user_id, peer_id, text, status) VALUES (?, ?, ?, 'open')",
                (user_id, peer_id, clean_text)
            )
            await db.commit()
            return cursor.lastrowid

    async def get_ticket(self, ticket_id: int) -> Optional[tuple]:
        """Получить тикет по ID."""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT id, user_id, peer_id, text, status, created_at FROM tickets WHERE id = ?",
                (ticket_id,)
            )
            return await cursor.fetchone()

    async def get_tickets(self, status: Optional[str] = None, limit: int = 30) -> list:
        """Получить список тикетов (с фильтром по статусу)."""
        async with aiosqlite.connect(self.db_name) as db:
            if status:
                cursor = await db.execute(
                    """SELECT id, user_id, peer_id, text, status, created_at
                       FROM tickets
                       WHERE status = ?
                       ORDER BY id DESC
                       LIMIT ?""",
                    (status, limit)
                )
            else:
                cursor = await db.execute(
                    """SELECT id, user_id, peer_id, text, status, created_at
                       FROM tickets
                       ORDER BY id DESC
                       LIMIT ?""",
                    (limit,)
                )
            return await cursor.fetchall()

    async def set_ticket_status(self, ticket_id: int, status: str) -> bool:
        """Установить статус тикета."""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))
            await db.commit()
            return cursor.rowcount > 0

    async def get_open_tickets(self) -> list:
        """Совместимость со старым кодом."""
        return await self.get_tickets(status="open", limit=50)

    async def close_ticket(self, ticket_id: int) -> bool:
        """Закрыть тикет (совместимость)."""
        return await self.set_ticket_status(ticket_id, "closed")

    # ==================== Репорты ====================
    def _generate_report_id(self) -> str:
        first = secrets.choice("123456789")
        other = "".join(secrets.choice(string.digits) for _ in range(4))
        return f"{first}{other}"

    def _normalize_report_id(self, report_id: str) -> str:
        return str(report_id).lstrip("#").upper().strip()

    def _report_id_variants(self, report_id: str) -> tuple[str, str]:
        clean_id = self._normalize_report_id(report_id)
        return clean_id, f"#{clean_id}"

    async def create_report(self, user_id: int, chat_id: int, report_text: str) -> str:
        report_id = self._generate_report_id()
        attempts = 0

        async with aiosqlite.connect(self.db_name) as db:
            while attempts < 20:
                clean_id, hash_id = self._report_id_variants(report_id)
                cursor = await db.execute(
                    "SELECT report_id FROM reports WHERE report_id IN (?, ?) LIMIT 1",
                    (clean_id, hash_id)
                )
                exists = await cursor.fetchone()
                if not exists:
                    report_id = clean_id
                    break
                report_id = self._generate_report_id()
                attempts += 1

            await db.execute(
                """INSERT INTO reports (report_id, user_id, chat_id, report_text, status)
                   VALUES (?, ?, ?, ?, 'pending')""",
                (report_id, user_id, chat_id, str(report_text))
            )
            await db.commit()

        return report_id

    async def get_report_by_id(self, report_id: str) -> Optional[tuple]:
        clean_id, hash_id = self._report_id_variants(report_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT * FROM reports WHERE report_id IN (?, ?) ORDER BY id DESC LIMIT 1",
                (clean_id, hash_id)
            )
            return await cursor.fetchone()

    async def get_active_report(self, user_id: int, chat_id: int) -> Optional[str]:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                """SELECT report_id FROM reports
                   WHERE user_id = ? AND chat_id = ?
                   AND status IN ('pending', 'in_progress')
                   ORDER BY created_at DESC
                   LIMIT 1""",
                (user_id, chat_id)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def take_report(self, report_id: str, taken_by: int) -> bool:
        clean_id, hash_id = self._report_id_variants(report_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                """UPDATE reports
                   SET status = 'in_progress', answered_by = ?
                   WHERE report_id IN (?, ?) AND status = 'pending'""",
                (taken_by, clean_id, hash_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def answer_report(self, report_id: str, answer_text: str, answered_by: int) -> tuple[bool, str]:
        clean_id, hash_id = self._report_id_variants(report_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                """SELECT report_id, status, answered_by
                   FROM reports
                   WHERE report_id IN (?, ?)
                   ORDER BY id DESC
                   LIMIT 1""",
                (clean_id, hash_id)
            )
            report = await cursor.fetchone()
            if not report:
                return False, "Репорт не найден"

            matched_report_id, status, current_agent = report
            is_owner = answered_by in BOT_OWNER_IDS or await self.is_bot_owner(answered_by)
            if not is_owner and status == "in_progress" and current_agent and int(current_agent) != answered_by:
                return False, "Этот репорт взят другим агентом"
            if status == "closed":
                return False, "Репорт уже закрыт"

            await db.execute(
                """UPDATE reports
                   SET status = 'answered',
                       answer_text = ?,
                       answered_by = ?,
                       answered_at = CURRENT_TIMESTAMP
                   WHERE report_id = ?""",
                (str(answer_text), answered_by, matched_report_id)
            )
            await db.commit()
            return True, "Ответ сохранен"

    async def append_to_report(self, report_id: str, additional_text: str) -> bool:
        clean_id, hash_id = self._report_id_variants(report_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                """SELECT report_id
                   FROM reports
                   WHERE report_id IN (?, ?)
                   AND status IN ('pending', 'in_progress', 'answered')
                   ORDER BY id DESC
                   LIMIT 1""",
                (clean_id, hash_id)
            )
            row = await cursor.fetchone()
            if not row:
                return False

            matched_report_id = row[0]
            update_cursor = await db.execute(
                """UPDATE reports
                   SET report_text = report_text || '\n\n' || ?,
                       status = 'pending',
                       answered_by = NULL,
                       answer_text = NULL,
                       answered_at = NULL
                   WHERE report_id = ?""",
                (str(additional_text), matched_report_id)
            )
            await db.commit()
            return update_cursor.rowcount > 0

    async def close_report(self, report_id: str, closed_by: int) -> tuple[bool, str]:
        clean_id, hash_id = self._report_id_variants(report_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                """SELECT report_id, status, answered_by
                   FROM reports
                   WHERE report_id IN (?, ?)
                   ORDER BY id DESC
                   LIMIT 1""",
                (clean_id, hash_id)
            )
            report = await cursor.fetchone()
            if not report:
                return False, "Репорт не найден"

            matched_report_id, status, current_agent = report
            is_owner = closed_by == BOT_OWNER_ID or await self.is_bot_owner(closed_by)
            if not is_owner and status == "in_progress" and current_agent and int(current_agent) != closed_by:
                return False, "Этот репорт взят другим агентом"
            if status == "closed":
                return False, "Репорт уже закрыт"

            update_cursor = await db.execute(
                """UPDATE reports
                   SET status = 'closed',
                       answered_by = ?,
                       answered_at = COALESCE(answered_at, CURRENT_TIMESTAMP)
                   WHERE report_id = ?
                   AND status IN ('pending', 'in_progress', 'answered')""",
                (closed_by, matched_report_id)
            )
            await db.commit()
            if update_cursor.rowcount <= 0:
                return False, "Не удалось закрыть репорт"
            return True, "Репорт закрыт"

    async def get_reports(self, status: Optional[str] = None, limit: int = 50) -> list:
        async with aiosqlite.connect(self.db_name) as db:
            if status:
                cursor = await db.execute(
                    """SELECT * FROM reports
                       WHERE status = ?
                       ORDER BY created_at DESC
                       LIMIT ?""",
                    (status, limit)
                )
            else:
                cursor = await db.execute(
                    """SELECT * FROM reports
                       ORDER BY created_at DESC
                       LIMIT ?""",
                    (limit,)
                )
            return await cursor.fetchall()
    
    async def get_active_reports(self) -> list:
        """Получить все активные тикеты."""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                """SELECT * FROM reports
                   WHERE status IN ('pending', 'in_progress')
                   ORDER BY created_at DESC
                   LIMIT 50"""
            )
            return await cursor.fetchall()

    async def get_report(self, report_id: str) -> Optional[tuple]:
        """Получить тикет по report_id (строковому идентификатору)."""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT * FROM reports WHERE report_id = ? LIMIT 1",
                (str(report_id),)
            )
            return await cursor.fetchone()

    async def get_report_by_report_id(self, report_id: str) -> Optional[tuple]:
        """Получить тикет по report_id (строковому идентификатору тикета)."""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT * FROM reports WHERE report_id = ? LIMIT 1",
                (str(report_id),)
            )
            return await cursor.fetchone()

    # ==================== Экономика ====================
    async def ensure_economy_user(self, user_id: int) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("INSERT OR IGNORE INTO economy_users (user_id) VALUES (?)", (user_id,))
            await db.commit()

    async def get_balance(self, user_id: int) -> int:
        await self.ensure_economy_user(user_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT balance FROM economy_users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def add_balance(self, user_id: int, amount: int) -> int:
        await self.ensure_economy_user(user_id)
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute(
                "UPDATE economy_users SET balance = MAX(0, balance + ?) WHERE user_id = ?",
                (amount, user_id)
            )
            await db.commit()
        return await self.get_balance(user_id)
            
    async def set_balance(self, user_id: int, amount: int) -> int:
        """Установить баланс точно в указанное значение."""
        await self.ensure_economy_user(user_id)
        value = max(0, int(amount))
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("UPDATE economy_users SET balance = ? WHERE user_id = ?", (value, int(user_id)))
            await db.commit()
        return value

    async def transfer_balance(self, from_user_id: int, to_user_id: int, amount: int) -> tuple[bool, str]:
        if amount <= 0:
            return False, "Сумма должна быть больше нуля"
        if from_user_id == to_user_id:
            return False, "Нельзя переводить деньги самому себе"

        await self.ensure_economy_user(from_user_id)
        await self.ensure_economy_user(to_user_id)

        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT balance FROM economy_users WHERE user_id = ?", (from_user_id,))
            row = await cursor.fetchone()
            current = row[0] if row else 0
            if current < amount:
                return False, "Недостаточно средств"

            await db.execute("UPDATE economy_users SET balance = balance - ? WHERE user_id = ?", (amount, from_user_id))
            await db.execute("UPDATE economy_users SET balance = balance + ? WHERE user_id = ?", (amount, to_user_id))
            await db.commit()
            return True, "ok"

    async def do_job(self, user_id: int) -> tuple[bool, int, int]:
        """Вернет (успех, заработок, секунд_до_отката)."""
        await self.ensure_economy_user(user_id)
        cooldown_seconds = 1800
        now = datetime.utcnow()
        now_iso = now.isoformat()

        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT last_job_at FROM economy_users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            last_job_at = row[0] if row else None

            if last_job_at:
                try:
                    last_dt = datetime.fromisoformat(last_job_at)
                except ValueError:
                    last_dt = None
                if last_dt:
                    diff = (now - last_dt).total_seconds()
                    if diff < cooldown_seconds:
                        return False, 0, int(cooldown_seconds - diff)

            # Проверяем ВИП-статус для буста
            is_vip = await self.is_vip(user_id)
            if is_vip:
                earned = random.randint(2500, 5000)
            else:
                earned = random.randint(150, 650)
            
            await db.execute(
                "UPDATE economy_users SET balance = balance + ?, last_job_at = ? WHERE user_id = ?",
                (earned, now_iso, user_id)
            )
            await db.commit()
            return True, earned, 0

    # ==================== Бизнес ====================
    async def ensure_business(self, user_id: int) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("INSERT OR IGNORE INTO businesses (user_id) VALUES (?)", (user_id,))
            await db.commit()

    async def get_business(self, user_id: int) -> tuple[int, int, Optional[str]]:
        await self.ensure_business(user_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT level, total_earned, last_collect_at FROM businesses WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return 0, 0, None
            return row[0], row[1], row[2]

    async def upgrade_business(self, user_id: int, cost: int) -> tuple[bool, str, int]:
        await self.ensure_economy_user(user_id)
        await self.ensure_business(user_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT balance FROM economy_users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            balance = row[0] if row else 0
            if balance < cost:
                return False, "Недостаточно денег для покупки бизнеса", balance

            cursor = await db.execute("SELECT level FROM businesses WHERE user_id = ?", (user_id,))
            business_row = await cursor.fetchone()
            level = business_row[0] if business_row else 0

            await db.execute("UPDATE economy_users SET balance = balance - ? WHERE user_id = ?", (cost, user_id))
            await db.execute(
                "UPDATE businesses SET level = ?, last_collect_at = COALESCE(last_collect_at, ?) WHERE user_id = ?",
                (level + 1, datetime.utcnow().isoformat(), user_id)
            )
            await db.commit()
            return True, "ok", balance - cost

    async def collect_business_income(self, user_id: int, income_per_hour: int) -> tuple[int, int]:
        """Вернет (доход, часы_начисления)."""
        await self.ensure_business(user_id)
        now = datetime.utcnow()
        now_iso = now.isoformat()
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT level, last_collect_at FROM businesses WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return 0, 0
                
            level, last_collect_at = row[0], row[1]
            if level <= 0:
                return 0, 0

            if not last_collect_at:
                await db.execute("UPDATE businesses SET last_collect_at = ? WHERE user_id = ?", (now_iso, user_id))
                await db.commit()
                return 0, 0

            try:
                last_dt = datetime.fromisoformat(last_collect_at)
            except ValueError:
                last_dt = now

            elapsed_hours = int((now - last_dt).total_seconds() // 3600)
            if elapsed_hours <= 0:
                return 0, 0

            elapsed_hours = min(elapsed_hours, 24)
            earned = elapsed_hours * income_per_hour

            await db.execute("UPDATE economy_users SET balance = balance + ? WHERE user_id = ?", (earned, user_id))
            await db.execute(
                "UPDATE businesses SET total_earned = total_earned + ?, last_collect_at = ? WHERE user_id = ?",
                (earned, now_iso, user_id)
            )
            await db.commit()
            return earned, elapsed_hours

    # ==================== Брак ====================
    async def get_spouse(self, user_id: int) -> Optional[int]:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT user1_id, user2_id FROM marriages WHERE user1_id = ? OR user2_id = ?",
                (user_id, user_id)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return row[1] if row[0] == user_id else row[0]
            
    async def create_marriage(self, user1_id: int, user2_id: int) -> tuple[bool, str]:
        if user1_id == user2_id:
            return False, "Нельзя вступить в брак с самим собой"

        spouse1 = await self.get_spouse(user1_id)
        spouse2 = await self.get_spouse(user2_id)
        if spouse1 is not None:
            return False, "Вы уже состоите в браке"
        if spouse2 is not None:
            return False, "Этот пользователь уже состоит в браке"

        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("INSERT INTO marriages (user1_id, user2_id) VALUES (?, ?)", (user1_id, user2_id))
            await db.commit()
            return True, "ok"

    async def remove_marriage(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("DELETE FROM marriages WHERE user1_id = ? OR user2_id = ?", (user_id, user_id))
            await db.commit()
            return cursor.rowcount > 0

    async def create_marriage_proposal(self, from_user_id: int, to_user_id: int, peer_id: int) -> tuple[bool, str, Optional[int]]:
        if from_user_id == to_user_id:
            return False, "Нельзя вступить в брак с самим собой", None

        spouse1 = await self.get_spouse(from_user_id)
        spouse2 = await self.get_spouse(to_user_id)
        if spouse1 is not None:
            return False, "Вы уже состоите в браке", None
        if spouse2 is not None:
            return False, "Этот пользователь уже состоит в браке", None

        async with aiosqlite.connect(self.db_name) as db:
            await db.execute(
                "UPDATE marriage_proposals SET status = 'cancelled' WHERE from_user_id = ? AND status = 'pending'",
                (from_user_id,)
            )
            cursor = await db.execute(
                "INSERT INTO marriage_proposals (from_user_id, to_user_id, peer_id, status) VALUES (?, ?, ?, 'pending')",
                (from_user_id, to_user_id, peer_id)
            )
            await db.commit()
            return True, "ok", cursor.lastrowid
            
    async def get_marriage_proposal(self, proposal_id: int) -> Optional[tuple]:
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT id, from_user_id, to_user_id, peer_id, status FROM marriage_proposals WHERE id = ?",
                (proposal_id,)
            )
            return await cursor.fetchone()

    async def close_marriage_proposal(self, proposal_id: int, status: str) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("UPDATE marriage_proposals SET status = ? WHERE id = ?", (status, proposal_id))
            await db.commit()

    # ==================== Мой бизнес ====================
    async def ensure_my_business(self, user_id: int) -> None:
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("INSERT OR IGNORE INTO my_businesses (user_id) VALUES (?)", (user_id,))
            await db.commit()

    async def get_my_business(self, user_id: int) -> tuple[int, int, int, int, int]:
        await self.ensure_my_business(user_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT raw_material, workers, ad_level, cashbox, tax_debt FROM my_businesses WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return 0, 0, 0, 0, 0
            return row[0], row[1], row[2], row[3], row[4]

    async def process_my_business(self, user_id: int) -> tuple[int, int]:
        """Начисляет прибыль и налоги. Вернет (earned, hours)."""
        await self.ensure_my_business(user_id)
        now = datetime.utcnow()
        now_iso = now.isoformat()
        earned_total = 0
        worked_hours = 0

        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT raw_material, workers, ad_level, cashbox, tax_debt, last_profit_at, last_tax_at FROM my_businesses WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return 0, 0

            raw_material, workers, ad_level, cashbox, tax_debt, last_profit_at, last_tax_at = row
            if not last_profit_at:
                await db.execute(
                    "UPDATE my_businesses SET last_profit_at = ?, last_tax_at = COALESCE(last_tax_at, ?) WHERE user_id = ?",
                    (now_iso, now_iso, user_id)
                )
                await db.commit()
                return 0, 0
                
            try:
                last_profit_dt = datetime.fromisoformat(last_profit_at)
            except ValueError:
                last_profit_dt = now

            elapsed_hours = int((now - last_profit_dt).total_seconds() // 3600)
            elapsed_hours = min(max(elapsed_hours, 0), 24)
            if elapsed_hours > 0 and workers > 0:
                for _ in range(elapsed_hours):
                    need_raw = workers * 3
                    if raw_material < need_raw:
                        break
                    raw_material -= need_raw
                    income = workers * 180 + ad_level * 120
                    salaries = workers * 50
                    hour_profit = max(0, income - salaries)
                    cashbox += hour_profit
                    earned_total += hour_profit
                    worked_hours += 1

            if not last_tax_at:
                last_tax_dt = now
            else:
                try:
                    last_tax_dt = datetime.fromisoformat(last_tax_at)
                except ValueError:
                    last_tax_dt = now
            tax_cycles = int((now - last_tax_dt).total_seconds() // 86400)
            if tax_cycles > 0:
                daily_tax = 350 + workers * 120 + ad_level * 180
                tax_debt += daily_tax * tax_cycles
                last_tax_dt = last_tax_dt + timedelta(days=tax_cycles)

            await db.execute(
                """UPDATE my_businesses
                   SET raw_material = ?, cashbox = ?, tax_debt = ?, total_profit = total_profit + ?,
                       last_profit_at = ?, last_tax_at = ?
                   WHERE user_id = ?""",
                (raw_material, cashbox, tax_debt, earned_total, now_iso, last_tax_dt.isoformat(), user_id)
            )
            await db.commit()
            return earned_total, worked_hours

    async def my_business_buy_raw(self, user_id: int, packs: int) -> tuple[bool, str]:
        if packs <= 0:
            return False, "Количество должно быть больше 0"
        cost_per_pack = 250
        raw_per_pack = 30
        total_cost = packs * cost_per_pack
        await self.ensure_economy_user(user_id)
        await self.ensure_my_business(user_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT balance FROM economy_users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            balance = row[0] if row else 0
            if balance < total_cost:
                return False, f"Нужно {total_cost}$, у вас {balance}$"
            await db.execute("UPDATE economy_users SET balance = balance - ? WHERE user_id = ?", (total_cost, user_id))
            await db.execute(
                "UPDATE my_businesses SET raw_material = raw_material + ? WHERE user_id = ?",
                (packs * raw_per_pack, user_id)
            )
            await db.commit()
            return True, f"Куплено сырья: +{packs * raw_per_pack} (за {total_cost}$)"

    async def my_business_hire(self, user_id: int, count: int) -> tuple[bool, str]:
        if count <= 0:
            return False, "Количество должно быть больше 0"
        hire_cost = count * 1200
        await self.ensure_economy_user(user_id)
        await self.ensure_my_business(user_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT balance FROM economy_users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            balance = row[0] if row else 0
            if balance < hire_cost:
                return False, f"Нужно {hire_cost}$, у вас {balance}$"
            await db.execute("UPDATE economy_users SET balance = balance - ? WHERE user_id = ?", (hire_cost, user_id))
            await db.execute("UPDATE my_businesses SET workers = workers + ? WHERE user_id = ?", (count, user_id))
            await db.commit()
            return True, f"Нанято работников: +{count} (за {hire_cost}$)"

    async def my_business_advertise(self, user_id: int, levels: int) -> tuple[bool, str]:
        if levels <= 0:
            return False, "Количество должно быть больше 0"
        await self.ensure_economy_user(user_id)
        await self.ensure_my_business(user_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT ad_level FROM my_businesses WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            ad_level = row[0] if row else 0
            new_level = min(ad_level + levels, 30)
            real_levels = new_level - ad_level
            if real_levels <= 0:
                return False, "Реклама уже на максимуме"
            cost = real_levels * 900

            cursor = await db.execute("SELECT balance FROM economy_users WHERE user_id = ?", (user_id,))
            money_row = await cursor.fetchone()
            balance = money_row[0] if money_row else 0
            if balance < cost:
                return False, f"Нужно {cost}$, у вас {balance}$"

            await db.execute("UPDATE economy_users SET balance = balance - ? WHERE user_id = ?", (cost, user_id))
            await db.execute("UPDATE my_businesses SET ad_level = ? WHERE user_id = ?", (new_level, user_id))
            await db.commit()
            return True, f"Реклама улучшена до {new_level} (затраты {cost}$)"

    async def my_business_pay_tax(self, user_id: int) -> tuple[bool, str]:
        await self.ensure_economy_user(user_id)
        await self.ensure_my_business(user_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT tax_debt FROM my_businesses WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            debt = row[0] if row else 0
            if debt <= 0:
                return False, "У вас нет задолженности по налогам"

            cursor = await db.execute("SELECT balance FROM economy_users WHERE user_id = ?", (user_id,))
            money_row = await cursor.fetchone()
            balance = money_row[0] if money_row else 0
            if balance < debt:
                return False, f"Нужно {debt}$, у вас {balance}$"

            await db.execute("UPDATE economy_users SET balance = balance - ? WHERE user_id = ?", (debt, user_id))
            await db.execute("UPDATE my_businesses SET tax_debt = 0 WHERE user_id = ?", (user_id,))
            await db.commit()
            return True, f"Налоги оплачены: {debt}$"

    async def my_business_withdraw(self, user_id: int, amount: int) -> tuple[bool, str]:
        if amount <= 0:
            return False, "Сумма должна быть больше 0"
        await self.ensure_economy_user(user_id)
        await self.ensure_my_business(user_id)
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT cashbox, tax_debt FROM my_businesses WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if not row:
                return False, "Бизнес не найден"
            cashbox, tax_debt = row
            if tax_debt > 0:
                return False, "Сначала оплатите налоги через /mybusiness paytax"
            if cashbox < amount:
                return False, f"В кассе только {cashbox}$"
            await db.execute("UPDATE my_businesses SET cashbox = cashbox - ? WHERE user_id = ?", (amount, user_id))
            await db.execute("UPDATE economy_users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
            await db.commit()
            return True, f"Выведено из кассы: {amount}$"

    # ==================== ВИП-пользователи ====================
    async def add_vip(self, user_id: int, days: int) -> bool:
        """Добавить/обновить ВИП-статус пользователю. Возвращает True при успехе."""
        if days <= 0:
            return False
        async with aiosqlite.connect(self.db_name) as db:
            # Проверяем, есть ли уже ВИП
            cursor = await db.execute("SELECT expires_at FROM vip_users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            
            now = datetime.utcnow()
            if row and row[0]:
                try:
                    current_expires = datetime.fromisoformat(row[0])
                    if current_expires > now:
                        # ВИП уже активен - продлеваем
                        new_expires = current_expires + timedelta(days=days)
                    else:
                        # ВИП истёк - новый
                        new_expires = now + timedelta(days=days)
                except ValueError:
                    new_expires = now + timedelta(days=days)
            else:
                # Новый ВИП
                new_expires = now + timedelta(days=days)
            
            await db.execute(
                """INSERT INTO vip_users (user_id, days, created_at, expires_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP, ?)
                   ON CONFLICT(user_id) DO UPDATE SET 
                       days = days + excluded.days,
                       expires_at = excluded.expires_at""",
                (user_id, days, new_expires.isoformat())
            )
            await db.commit()
            return True

    async def remove_vip(self, user_id: int) -> bool:
        """Удалить ВИП-статус пользователя."""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("DELETE FROM vip_users WHERE user_id = ?", (user_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def is_vip(self, user_id: int) -> bool:
        """Проверить, является ли пользователь ВИПом (и активен ли ВИП)."""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute("SELECT expires_at FROM vip_users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if not row or not row[0]:
                return False
            try:
                expires_at = datetime.fromisoformat(row[0])
                return expires_at > datetime.utcnow()
            except ValueError:
                return False

    async def get_vip_info(self, user_id: int) -> Optional[dict]:
        """Получить информацию о ВИП-статусе пользователя."""
        async with aiosqlite.connect(self.db_name) as db:
            cursor = await db.execute(
                "SELECT days, created_at, expires_at FROM vip_users WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            days, created_at, expires_at = row
            is_active = False
            if expires_at:
                try:
                    is_active = datetime.fromisoformat(expires_at) > datetime.utcnow()
                except ValueError:
                    is_active = False
            return {
                "days": days,
                "created_at": created_at,
                "expires_at": expires_at,
                "is_active": is_active
            }

    # ==================== Проверка роли в боте ====================
    async def get_bot_role(self, user_id: int) -> str:
        """Получить роль пользователя в боте (владелец, руководство, админ, модератор, хелпер)"""
        if user_id in BOT_OWNER_IDS:
            return "owner"
        if await self.is_bot_owner(user_id):
            return "owner"
        if await self.is_bot_leader(user_id):
            return "leader"
        if await self.is_bot_admin(user_id):
            return "admin"
        if await self.is_bot_moderator(user_id):
            return "moderator"
        if await self.is_bot_helper(user_id):
            level = await self.get_bot_helper_level(user_id)
            return f"helper_{level}"
        return "user"


# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Владельцы бота (основные)

BOT_OWNER_IDS = (662204206, 105478253, 882530249)

# Для обратной совместимости - основной владелец
BOT_OWNER_ID = BOT_OWNER_IDS[0]

def is_bot_owner(user_id: int) -> bool:
    """Проверить, является ли пользователь основным владельцем бота"""
    return user_id in BOT_OWNER_IDS

# ==================== Лимиты на выдачу денег ====================
MONEY_DAILY_LIMIT = 5_000_000  # 5 миллионов в день
money_daily_limits: dict[int, tuple[int, str]] = {}  # user_id -> (used_amount, date_string)


def get_today_date_string() -> str:
    """Получить текущую дату в формате YYYY-MM-DD"""
    return datetime.now().strftime("%Y-%m-%d")


def check_money_limit(user_id: int, amount: int) -> tuple[bool, str, int]:
    """
    Проверить лимит на выдачу денег.
    Возвращает (достаточно_ли_лимита, причина_если_нет, оставшийся_лимит).
    Для владельцев бота лимит не ограничен.
    """
    # Владельцы бота не имеют ограничений
    if user_id in BOT_OWNER_IDS:
        return True, "", MONEY_DAILY_LIMIT
    
    today = get_today_date_string()
    current = money_daily_limits.get(user_id)
    
    if current is None:
        # Первый раз сегодня
        return True, "", MONEY_DAILY_LIMIT
    
    used_amount, date_str = current
    if date_str != today:
        # Новый день - сбрасываем
        money_daily_limits[user_id] = (0, today)
        return True, "", MONEY_DAILY_LIMIT
    
    remaining = MONEY_DAILY_LIMIT - used_amount
    if amount <= remaining:
        return True, "", remaining
    return False, f"❌ Достигнут дневной лимит! Осталось: {remaining}$ из {MONEY_DAILY_LIMIT}$", remaining


def add_money_to_limit(user_id: int, amount: int) -> None:
    """Добавить сумму к использованному лимиту за день."""
    # Владельцы бота не учитываются в лимите
    if user_id in BOT_OWNER_IDS:
        return
    
    today = get_today_date_string()
    current = money_daily_limits.get(user_id)
    
    if current is None:
        money_daily_limits[user_id] = (amount, today)
    else:
        used_amount, date_str = current
        if date_str != today:
            money_daily_limits[user_id] = (amount, today)
        else:
            money_daily_limits[user_id] = (used_amount + amount, today)


async def check_is_leader_or_owner(user_id: int) -> bool:
    """Проверить, является ли пользователь владельцем или руководством бота."""
    if user_id in BOT_OWNER_IDS:
        return True
    return await db.is_bot_leader(user_id)

# Peer ID для тикетов/репортов поддержки
HELP_GROUP_ID = 2000000001

# Инициализация базы данных
db = Database()

# Blueprint для группировки хэндлеров
bp = Blueprint(name="main_blueprint")

#
# ==================== Рулетка (wall replies) ====================
#
# Событие: пользователь пишет "Рулетка" в ответ на комментарий/пост на стене.
# Бот отвечает комментарием с результатом.
ROULETTE_GROUP_ID = 237041509
ROULETTE_EMPTY_WORDS = ["Пусто (повезёт в следующий раз)", "Ничего не выпало", "Пусто"]
ROULETTE_EMPTY_PROBABILITY = 0.20

zov_dedupe: dict[tuple[int, int], float] = {}

# Хранилище для ожидающих ввода страницы в /groupall
# {peer_id: {"user_id": user_id, "message_id": cmid, "timeout_at": timestamp}}
groupall_pending_input: dict[int, dict] = {}

def roulette_roll() -> tuple[str, int]:
    """Синхронная часть: крутит рулетку (без ограничений/кд)."""
    if random.random() < ROULETTE_EMPTY_PROBABILITY:
        prize = random.choice(ROULETTE_EMPTY_WORDS)
    else:
        prize = random.randint(100, 5000)

    result_message = "Вы успешно прокрутили рулетку 🎁\n\n"
    if isinstance(prize, int):
        result_message += f"Поздравляем! Вам выпало: {prize}$\n\n"
    else:
        result_message += f"В этот раз ничего не выпало :( \n\n{prize}\n\n"
    return result_message, prize if isinstance(prize, int) else 0


@bp.on.raw_event(GroupEventType.WALL_REPLY_NEW)
async def roulette_wall_reply_handler(event: dict) -> None:
    obj = event.get("object", {}) or {}

    # Ожидаем данные комментария на стене
    text = (obj.get("text") or "").strip().lower()
    if text != "рулетка" and text != "roulette":
        return
    
    from_id = obj.get("from_id")
    comment_id = obj.get("id")
    post_id = obj.get("post_id")
    owner_id = obj.get("owner_id")

    if not all([from_id, comment_id, post_id, owner_id]):
        return
    
    # VK часто хранит owner_id отрицательным для сообществ
    if abs(int(owner_id)) != ROULETTE_GROUP_ID:
        return

    # Тратим 1 прокрутку из БД
    has_spin = await db.consume_roulette_spin(int(from_id))
    if not has_spin:
        spins = await db.get_roulette_spins(int(from_id))
        response = f"🎰 У вас нет рулеток. Доступно: {spins}"
        win_amount = 0
    else:
        response, win_amount = roulette_roll()
        spins_left = await db.get_roulette_spins(int(from_id))
        response += f"Осталось рулеток: {spins_left}"
        if win_amount > 0:
            try:
                await db.add_balance(int(from_id), int(win_amount))
            except Exception as e:
                logger.error(f"Ошибка начисления денег в рулетке: {e}")

    try:
        # Ответ в виде комментария к комментарию
        await bp.api.wall.create_comment(
            owner_id=int(owner_id),
            post_id=int(post_id),
            reply_to_comment=int(comment_id),
            message=response,
            from_group=ROULETTE_GROUP_ID,
            guid=f"roulette-{int(time.time())}-{int(from_id)}-{int(comment_id)}",
        )
    except Exception as e:
        logger.error(f"Ошибка рулетки: {e}")

def normalize_command_text(text: Optional[str]) -> str:
    """Нормализует текст команды: убирает упоминание бота в начале."""
    value = (text or "").strip()
    if not value:
        return ""

    value = value.lstrip(", ")
    while value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            break
        mention = value[:closing + 1]
        if mention.startswith("[id") or mention.startswith("[club"):
            value = value[closing + 1:].lstrip(" ,")
            continue
        break

    # Форматы: @club123, @public123, club123, public123
    value = re.sub(r"^@?(club|public)\d+\s*,?\s*", "", value, flags=re.IGNORECASE)
    return value.strip()


def extract_command_payload(text: Optional[str], command: str) -> Optional[str]:
    """Возвращает аргументы команды или None, если это не та команда."""
    normalized = normalize_command_text(text)
    if not normalized:
        return None

    lowered = normalized.lower()
    cmd_slash = f"/{command}"
    cmd_bang = f"!{command}"

    for prefix in (cmd_slash, cmd_bang):
        if lowered == prefix:
            return ""
        if lowered.startswith(prefix + " "):
            return normalized[len(prefix):].strip()

    return None


def parse_target_user_id(message: Message, payload: str) -> Optional[int]:
    """Пытается получить target user_id из reply/mention/id."""
    if getattr(message, "reply_message", None):
        return message.reply_message.from_id

    mention_match = re.search(r"\[id(\d+)\|", payload)
    if mention_match:
        return int(mention_match.group(1))

    direct_id = re.search(r"\b(\d{5,12})\b", payload)
    if direct_id:
        return int(direct_id.group(1))
    return None


async def delete_by_cmid(peer_id: int, cmid: Optional[int]) -> None:
    """Удаляет сообщение в беседе через messages.delete по conversation_message_id."""
    if not cmid:
        return
    try:
        await bp.api.messages.delete(
            peer_id=peer_id,
            delete_for_all=True,
            cmids=[cmid]
        )
    except Exception:
        pass


def sanitize_plain_name(value: str) -> str:
    """Убирает VK-упоминания из отображаемого имени (без кликабельных ссылок)."""
    text = (value or "").strip()
    if not text:
        return "Пользователь"
    # [id123|Name] / [club123|Name] -> Name
    text = re.sub(r"\[(id|club)\d+\|([^\]]+)\]", r"\2", text, flags=re.IGNORECASE)
    # @id123, @club123, id123, club123 в начале
    text = re.sub(r"^@?(id|club)\d+\s*", "", text, flags=re.IGNORECASE)
    # лишние пробелы
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text or "Пользователь"


def build_settings_keyboard(allow_games: bool, allow_community_add: bool, auto_kick_on_leave: bool) -> str:
    games_text = "✅ Игровые команды: разрешены" if allow_games else "⛔ Игровые команды: запрещены"
    community_text = "✅ Добавление ботов: разрешено" if allow_community_add else "⛔ Добавление ботов: запрещено"
    leave_text = "✅ Авто-кик при выходе: включён" if auto_kick_on_leave else "⛔ Авто-кик при выходе: выключен"

    keyboard = (
        Keyboard(inline=True)
        .row()
        .add(Callback(games_text, payload={"action": "settings_toggle_games"}), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Callback(community_text, payload={"action": "settings_toggle_community_add"}), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Callback(leave_text, payload={"action": "settings_toggle_auto_kick_leave"}), color=KeyboardButtonColor.PRIMARY)
    )
    return keyboard.get_json()


async def games_allowed_in_message(message: Message) -> bool:
    if message.peer_id < 2000000000:
        return True
    return await db.get_allow_games(message.peer_id)


async def ensure_games_enabled_for_message(message: Message) -> bool:
    if await games_allowed_in_message(message):
        return True
    await message.answer("⛔ Игровые команды отключены владельцем беседы через /settings")
    return False


async def is_silent_blocked_message(message: Message) -> bool:
    """Проверяет, должен ли режим тишины удалить сообщение (приоритет 0..10)."""
    peer_id = message.peer_id
    if peer_id < 2000000000:
        return False

    if not await db.get_silent_mode(peer_id):
        return False

    user_role = await db.get_user_role(peer_id, message.from_id)
    user_priority = user_role[1] if user_role else 0
    if user_priority > 10:
        return False

    cmid = getattr(message, "conversation_message_id", None)
    if isinstance(cmid, int):
        await delete_by_cmid(peer_id, cmid)
    return True


class MessageRule(rules.ABCRule[Message]):
    """Правило для обработки текстовых команд (/cmd и !cmd)."""
    
    # Локальные алиасы команд (русские/альтернативные названия)
    COMMAND_ALIASES: dict[str, tuple[str, ...]] = {
        "kick": ("кик",),
        "ban": ("бан",),
        "unban": ("унбан",),
        "admins": ("админы",),
        "warn": ("варн",),
        "unwarn": ("унварн",),
        "settings": ("настройки",),
    }
    
    def __init__(self, command: str):
        self.command = command.lower()
    
    async def check(self, message: Message) -> bool:
        text = normalize_command_text(message.text).strip().lower()
        if not text:
            return False

        first, sep, tail = text.partition(" ")

        # Для команд вида /cmd или !cmd считаем префиксы взаимозаменяемыми.
        if self.command.startswith(("/", "!")):
            command_name = self.command[1:]
            aliases = self.COMMAND_ALIASES.get(command_name, ())

            accepted_tokens = {f"/{command_name}", f"!{command_name}"}
            for alias in aliases:
                accepted_tokens.add(f"/{alias}")
                accepted_tokens.add(f"!{alias}")

            if first not in accepted_tokens:
                return False

            # Нормализуем текст к базовому виду команды, чтобы обработчики
            # с message.text.replace("/cmd", ...) работали и для алиасов.
            message.text = f"{self.command}{sep}{tail}".strip()
            return True
        
        return first == self.command


class CommandNameRule(rules.ABCRule[Message]):
    """Проверяет команду по имени: /cmd и !cmd."""

    def __init__(self, command: str):
        self.command = command

    async def check(self, message: Message) -> bool:
        return extract_command_payload(message.text, self.command) is not None


class IsAdminRule(rules.ABCRule[Message]):
    """Правило для проверки прав администратора (любой приоритет > 0)"""
    
    async def check(self, message: Message) -> bool:
        # Проверяем, не заблокирован ли пользователь в боте
        if await db.is_sysbanned(message.from_id):
            return False
        
        # Если это владелец бота - пропускаем (в том числе в ЛС)
        if message.from_id in BOT_OWNER_IDS:
            return True
        
        # Получаем peer_id (для бесед) или user_id (для лс)
        peer_id = message.peer_id
        
        # Если это личные сообщения - проверяем роль в боте
        if peer_id < 2000000000:
            # В ЛС пропускаем только staff бота
            if await db.is_bot_leader(message.from_id):
                return True
            if await db.is_bot_admin(message.from_id):
                return True
            if await db.is_bot_moderator(message.from_id):
                return True
            if await db.is_bot_helper(message.from_id):
                return True
            return False

        # Проверяем статус в базе данных
        status = await db.get_chat_status(peer_id)
        has_admin = bool(status) if status is not None else False
        return has_admin


class PriorityRule(rules.ABCRule[Message]):
    """Правило для проверки минимального приоритета роли"""
    
    def __init__(self, min_priority: int):
        self.default_priority = min_priority
    
    async def check(self, message: Message) -> bool:
        # Проверяем, не заблокирован ли пользователь в боте
        if await db.is_sysbanned(message.from_id):
            return False

        # Если это владелец бота - всегда пропускаем (в том числе в ЛС)
        if message.from_id in BOT_OWNER_IDS:
            return True

        # Получаем peer_id
        peer_id = message.peer_id
        
        # Если это личные сообщения - проверяем роль в боте
        if peer_id < 2000000000:
            # В ЛС пропускаем только если это staff бота
            if await db.is_bot_leader(message.from_id):
                return True
            if await db.is_bot_admin(message.from_id):
                return True
            if await db.is_bot_moderator(message.from_id):
                return True
            if await db.is_bot_helper(message.from_id):
                return True
            # Обычные пользователи не могут использовать команды в ЛС
            return False

        # Проверяем статус админа в базе данных
        status = await db.get_chat_status(peer_id)
        has_admin = bool(status) if status is not None else False
        if not has_admin:
            await message.answer("⚠️ Бот не имеет прав администратора в этом чате.")
            return False
        
        # Получаем роль пользователя
        user_role = await db.get_user_role(peer_id, message.from_id)
        if not user_role:
            return False
        
        user_priority = user_role[1]
        
        # Извлекаем имя команды из текста сообщения
        text = message.text.strip().lower()
        if text.startswith('/'):
            cmd = text.split()[0][1:]  # убираем /
        elif text.startswith('!'):
            cmd = text.split()[0][1:]  # убираем !
        else:
            cmd = None
        
        # Получаем приоритет команды из БД если он установлен
        if cmd:
            cmd_priority = await db.get_cmd_priority(cmd)
            if cmd_priority is not None:
                if user_priority < cmd_priority:
                    await message.answer(f"❌ Нет доступа! Требуется приоритет: {cmd_priority}")
                    return False
                return True
        
        if user_priority < self.default_priority:
            await message.answer(f"❌ Нет доступа! Требуется приоритет: {self.default_priority}")
            return False

        return True


class IsOwnerRule(rules.ABCRule[Message]):
    """Правило для проверки владельца бота (включая дополнительных)"""
    
    async def check(self, message: Message) -> bool:
        # Владелец бота всегда имеет доступ (в том числе в ЛС)
        if message.from_id in BOT_OWNER_IDS:
            return True
        # Проверяем дополнительных владельцев
        return await db.is_bot_owner(message.from_id)


class IsMainOwnerRule(rules.ABCRule[Message]):
    """Правило для проверки владельца бота (включая дополнительных)"""
    
    async def check(self, message: Message) -> bool:
        # Работает в ЛС и беседах - проверяем всех владельцев бота
        return await db.is_bot_owner(message.from_id)


class IsLeaderRule(rules.ABCRule[Message]):
    """Правило для проверки руководства бота"""
    
    async def check(self, message: Message) -> bool:
        # Владелец бота всегда имеет доступ
        if message.from_id in BOT_OWNER_IDS:
            return True
        # Проверяем роль в боте
        return await db.is_bot_leader(message.from_id)
        

class IsBotAdminRule(rules.ABCRule[Message]):
    """Правило для проверки админа бота"""
    
    async def check(self, message: Message) -> bool:
        # Владелец бота всегда имеет доступ
        if message.from_id in BOT_OWNER_IDS:
            return True
        # Проверяем руководство
        if await db.is_bot_leader(message.from_id):
            return True
        # Проверяем админа бота
        return await db.is_bot_admin(message.from_id)


class IsBotModeratorRule(rules.ABCRule[Message]):
    """Правило для проверки модератора бота"""
    
    async def check(self, message: Message) -> bool:
        # Владелец бота всегда имеет доступ
        if message.from_id in BOT_OWNER_IDS:
            return True
        # Проверяем руководство
        if await db.is_bot_leader(message.from_id):
            return True
        # Проверяем админа
        if await db.is_bot_admin(message.from_id):
            return True
        # Проверяем модератора бота
        return await db.is_bot_moderator(message.from_id)


class IsBotHelperRule(rules.ABCRule[Message]):
    """Правило для проверки хелпера бота (любой уровень)"""
    
    async def check(self, message: Message) -> bool:
        # Владелец бота всегда имеет доступ
        if message.from_id in BOT_OWNER_IDS:
            return True
        # Проверяем руководство
        if await db.is_bot_leader(message.from_id):
            return True
        # Проверяем админа
        if await db.is_bot_admin(message.from_id):
            return True
        # Проверяем модератора
        if await db.is_bot_moderator(message.from_id):
            return True
        # Проверяем хелпера
        return await db.is_bot_helper(message.from_id)
        

class IsBotHelperLevelRule(rules.ABCRule[Message]):
    """Правило для проверки минимального уровня хелпера"""
    
    def __init__(self, min_level: int):
        self.min_level = min_level
    
    async def check(self, message: Message) -> bool:
        # Владелец бота всегда имеет доступ
        if message.from_id in BOT_OWNER_IDS:
            return True
        # Проверяем руководство
        if await db.is_bot_leader(message.from_id):
            return True
        # Проверяем админа
        if await db.is_bot_admin(message.from_id):
            return True
        # Проверяем модератора
        if await db.is_bot_moderator(message.from_id):
            return True
        # Проверяем хелпера
        if not await db.is_bot_helper(message.from_id):
            return False
        level = await db.get_bot_helper_level(message.from_id)
        return level >= self.min_level
        

# ==================== Хэндлер команды /help ====================
@bp.on.message(MessageRule("/help"), IsAdminRule())
async def help_handler(message: Message) -> None:
    """Обработчик команды /help"""
    help_text = """📘 Отображена малая часть необходимых команд:

!кик — исключить пользователя
!бан — заблокировать пользователя
!унбан — разблокировать участника
!мут — запретить писать в чат
!унмут — разрешить писать в чат
!warn — выдать предупреждение
!unwarn — снять предупреждение
!настройки — детальная настройка конференции
!админы — отобразить список участников с ролью
!role — выдать роль участнику"""
    await message.answer(help_text)


# ==================== Команда /ping ====================
@bp.on.message(MessageRule("/ping"), PriorityRule(100))
async def ping_handler(message: Message) -> None:
    """Проверка доступности бота (только владелец беседы)."""
    peer_id = message.peer_id
    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return
    
    role = await db.get_user_role(peer_id, message.from_id)
    if not role or role[1] != 100:
        await message.answer("❌ Команда доступна только владельцу беседы (приоритет 100)")
        return

    await message.answer("Понг!")


# ==================== Команда /stats ====================
@bp.on.message(CommandNameRule("stats"))
async def stats_handler(message: Message) -> None:
    """Показать свою статистику всегда; другие - только при приоритете 10+."""
    peer_id = message.peer_id
    from_id = message.from_id
    
    # Кого показываем
    target_id: int | None = None
    if message.reply_message:
        target_id = message.reply_message.from_id
    else:
        normalized = normalize_command_text(message.text)
        payload = extract_command_payload(message.text, "stats") or ""
        mention_match = re.search(r"\[id(\d+)\|", normalized)
        if mention_match:
            target_id = int(mention_match.group(1))
        else:
            # Если указали id как число
            direct_id = re.search(r"\b(\d{5,12})\b", payload)
            if direct_id:
                target_id = int(direct_id.group(1))
            else:
                target_id = from_id

    if not target_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    # Права: для других пользователей нужна роль приоритет 10+
    if target_id != from_id and peer_id >= 2000000000:
        actor_role = await db.get_user_role(peer_id, from_id)
        actor_priority = actor_role[1] if actor_role else 0
        if actor_priority < 10:
            await message.answer("❌ Нет доступа: требуется приоритет 10+")
            return

    if target_id != from_id and peer_id < 2000000000:
        await message.answer("❌ В ЛС можно смотреть только свою статистику")
        return
    
    # Экономика всегда доступна (по user_id)
    balance = await db.get_balance(target_id)
    spouse_id = await db.get_spouse(target_id)
    biz_level, biz_total_earned, _ = await db.get_business(target_id)

    # Получаем название бизнеса (упрощенно)
    if biz_level > 0:
        biz_name = f"Бизнес уровень {biz_level}"
    else:
        biz_name = "Нет"

    text = "📊 Статистика\n\n"
    text += f"👤 Пользователь: [id{target_id}|пользователь]\n"
    
    # ВИП-статус
    is_vip = await db.is_vip(target_id)
    text += f"⭐ ВИП: {'да' if is_vip else 'нет'}\n"
    
    text += f"💰 Баланс: {balance}$\n"
    text += f"🏢 Бизнес: {biz_name}\n"
    text += f"💵 Доход с бизнеса (всего): {biz_total_earned}$\n"
    text += f"💍 Брак: [id{spouse_id}|пользователь]\n" if spouse_id else "💍 Брак: отсутствует\n"

    # Статусы в чате (только для бесед)
    if peer_id >= 2000000000:
        nickname = await db.get_nickname(peer_id, target_id)
        warnings_count = await db.get_warnings_count(peer_id, target_id)
        user_role = await db.get_user_role(peer_id, target_id)
        is_banned = await db.is_banned(peer_id, target_id)

        user_mention = await get_user_mention(peer_id, target_id)
        text += "\n"
        text += f"👤 Ник: {nickname or 'не установлен'}\n"
        text += f"⚠️ Предупреждения: {warnings_count}/3\n"
        if user_role:
            role_name, priority = user_role
            text += f"🏅 Роль: {role_name} (приоритет: {priority})\n"
        else:
            text += "🏅 Роль: Пользователь (приоритет: 0)\n"
        text += f"🚫 Бан: {'да' if is_banned else 'нет'}\n"

    await message.answer(text)


# ==================== Команда /warn ====================
@bp.on.message(MessageRule("/warn"), PriorityRule(10))
async def warn_handler(message: Message) -> None:
    """Выдать предупреждение"""
    peer_id = message.peer_id
    user_id = None
    reason = ""
    
    # Получаем ID того, кто выдал команду
    from_id = message.from_id
    
    # Проверяем reply
    if message.reply_message:
        user_id = message.reply_message.from_id
        reason = message.text.replace("/warn", "").strip()
    else:
        # Пробуем найти @user или упоминание
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
            reason = re.sub(r'\[id\d+\|[^\]]+\]', '', text).replace("/warn", "").strip()
        else:
            await message.answer("Использование: /warn [reply/@user] [причина]")
            return
    
    if not user_id:
        await message.answer("Не удалось определить пользователя")
        return

    if user_id == from_id:
        await message.answer("❌ Нельзя выдавать предупреждение самому себе")
        return

    sender_role = await db.get_user_role(peer_id, from_id)
    target_role = await db.get_user_role(peer_id, user_id)
    sender_priority = sender_role[1] if sender_role else 0
    target_priority = target_role[1] if target_role else 0

    if target_priority == 100:
        await message.answer("❌ Нельзя выдавать предупреждение владельцу беседы")
        return

    if target_priority >= sender_priority and from_id not in BOT_OWNER_IDS:
        await message.answer("❌ Нельзя выдавать предупреждение пользователю с равным или более высоким приоритетом")
        return
    
    # Добавляем предупреждение
    await db.add_warning(peer_id, user_id, reason)
    
    # Проверяем количество предупреждений
    warnings_count = await db.get_warnings_count(peer_id, user_id)
    
    # Формируем mention для пользователя, который выдал варн
    admin_mention = f"[id{from_id}|Администратор]"
    user_mention = await get_user_mention(peer_id, user_id)
    
    if warnings_count >= 3:
        # Кикаем пользователя
        try:
            await bp.api.messages.remove_chat_user(
                chat_id=peer_id - 2000000000,
                member_id=user_id
            )
            await db.clear_warnings(peer_id, user_id)
            await message.answer(f"⚠️ {admin_mention} кикнул пользователя {user_mention} за 3 предупреждения")
        except Exception as e:
            logger.error(f"Ошибка при кике: {e}")
            await message.answer(f"⚠️ {admin_mention} выдал предупреждение #{warnings_count}/3 пользователю {user_mention}\n📝 Причина: {reason or 'не указана'}\n⚠️ При 3-х предупреждениях пользователь будет кикнут.")
    else:
        # Создаём клавиатуру с кнопкой снятия
        keyboard = (
            Keyboard(inline=True)
            .row()
            .add(
                Callback("Снять варн", payload={"action": "unwarn", "user_id": user_id}),
                color=KeyboardButtonColor.NEGATIVE
            )
        )
        
        await message.answer(
            f"⚠️ {admin_mention} выдал предупреждение #{warnings_count}/3 пользователю {user_mention}\n📝 Причина: {reason or 'не указана'}",
            keyboard=keyboard.get_json()
        )
        

# ==================== Команда /unwarn ====================
@bp.on.message(MessageRule("/unwarn"), PriorityRule(30))
async def unwarn_handler(message: Message) -> None:
    """Снять предупреждения"""
    peer_id = message.peer_id
    user_id = None
    
    # Получаем ID того, кто снимает
    from_id = message.from_id
    
    # Проверяем reply
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        # Пробуем найти @user
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            await message.answer("📋 Использование: /unwarn [reply/@user]")
            return
    
    if not user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    if user_id == from_id:
        await message.answer("❌ Нельзя выдавать бан самому себе")
        return
    
    sender_role = await db.get_user_role(peer_id, from_id)
    target_role = await db.get_user_role(peer_id, user_id)
    sender_priority = sender_role[1] if sender_role else 0
    target_priority = target_role[1] if target_role else 0

    if target_priority == 100:
        await message.answer("❌ Нельзя банить владельца беседы")
        return
    
    if target_priority >= sender_priority and from_id not in BOT_OWNER_IDS:
        await message.answer("❌ Нельзя банить пользователя с равным или более высоким приоритетом")
        return
    
    # Получаем количество варнов
    warnings_count = await db.get_warnings_count(peer_id, user_id)
    
    if warnings_count == 0:
        await message.answer("✅ У пользователя нет предупреждений")
        return
    
    user_mention = await get_user_mention(peer_id, user_id)
    
    if warnings_count == 1:
        # Если всего 1 варн - снимаем сразу
        await db.clear_warnings(peer_id, user_id)
        admin_mention = f"[id{from_id}|Администратор]"
        await message.answer(f"✅ {admin_mention} снял предупреждение с {user_mention}")
    else:
        # Если больше 1 - показываем клавиатуру с выбором
        keyboard = Keyboard(inline=True)
        
        # Кнопки: 1, 2, ... , все
        keyboard.row()
        for i in range(1, min(warnings_count + 1, 4)):  # максимум 3 кнопки
            keyboard.add(Callback(f"-{i}", payload={"action": "unwarn", "user_id": user_id, "count": i}))
        
        # Кнопка "Все"
        keyboard.add(Callback("Все", payload={"action": "unwarn", "user_id": user_id, "count": warnings_count}))
        
        await message.answer(
            f"❓ Сколько варнов снять с {user_mention}? (всего: {warnings_count})",
            keyboard=keyboard.get_json()
        )


# ==================== Команда /ban ====================
@bp.on.message(MessageRule("/ban"), PriorityRule(40))
async def ban_handler(message: Message) -> None:
    """Забанить пользователя"""
    peer_id = message.peer_id
    user_id = None
    days = -1  # по умолчанию навсегда
    reason = ""
    
    from_id = message.from_id
    
    if message.reply_message:
        user_id = message.reply_message.from_id
        args = message.text.replace("/ban", "").strip().split()
        if args:
            try:
                days = int(args[0])
                reason = " ".join(args[1:])
            except ValueError:
                reason = " ".join(args)
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
            args = re.sub(r'\[id\d+\|[^\]]+\]', '', text).replace("/ban", "").strip().split()
            if args:
                try:
                    days = int(args[0])
                    reason = " ".join(args[1:])
                except ValueError:
                    reason = " ".join(args)
        else:
            await message.answer("📋 Использование: /ban [reply/@user] [дни/-1] [причина]\n⏱️ -1 = навсегда")
            return
    
    if not user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    await db.add_ban(peer_id, user_id, days, reason)
    
    # Кикаем пользователя из чата
    try:
        await bp.api.messages.remove_chat_user(
            chat_id=peer_id - 2000000000,
            member_id=user_id
        )
    except Exception as e:
        logger.error(f"Ошибка при кике: {e}")
    
    admin_mention = f"[id{from_id}|Администратор]"
    user_mention = await get_user_mention(peer_id, user_id)
    
    keyboard = (
        Keyboard(inline=True)
        .row()
        .add(
            Callback("Разбанить", payload={"action": "unban", "user_id": user_id}),
            color=KeyboardButtonColor.POSITIVE
        )
    )
        
    if days == -1:
        await message.answer(
            f"🚫 {admin_mention} забанил пользователя {user_mention} навсегда\n📝 Причина: {reason or 'не указана'}",
            keyboard=keyboard.get_json()
        )
    else:
        await message.answer(
            f"🚫 {admin_mention} забанил пользователя {user_mention} на {days} дней\n📝 Причина: {reason or 'не указана'}",
            keyboard=keyboard.get_json()
        )


# ==================== Команда /unban ====================
@bp.on.message(MessageRule("/unban"), PriorityRule(40))
async def unban_handler(message: Message) -> None:
    """Разбанить пользователя"""
    peer_id = message.peer_id
    user_id = None
    
    from_id = message.from_id
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            await message.answer("📋 Использование: /unban [reply/@user]")
            return
    
    if not user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    if not await db.is_banned(peer_id, user_id):
        user_mention = await get_user_mention(peer_id, user_id)
        await message.answer(f"ℹ️ Пользователь {user_mention} не забанен")
        return
    
    await db.remove_ban(peer_id, user_id)
    
    admin_mention = f"[id{from_id}|Администратор]"
    user_mention = await get_user_mention(peer_id, user_id)
    
    await message.answer(f"✅ {admin_mention} разбанил пользователя {user_mention}")


# ==================== Команда /kick ====================
@bp.on.message(MessageRule("/kick"), PriorityRule(30))
async def kick_handler(message: Message) -> None:
    """Кикнуть пользователя из беседы"""
    peer_id = message.peer_id
    user_id = None
    reason = ""
    
    from_id = message.from_id
    
    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return

    payload = extract_command_payload(message.text, "kick")
    payload = (payload or "").strip()

    if message.reply_message:
        user_id = message.reply_message.from_id
        reason = payload
    else:
        user_id = parse_target_user_id(message, payload)
        if user_id:
            reason = re.sub(r'\[id\d+\|[^\]]+\]', '', payload).strip()
            reason = re.sub(r'^\d{5,12}\s*', '', reason).strip()
        else:
            await message.answer("📋 Использование: /kick [reply/@user] [причина]")
            return
    
    if not user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    if user_id == from_id:
        await message.answer("❌ Нельзя кикнуть самого себя")
        return
    
    sender_role = await db.get_user_role(peer_id, from_id)
    target_role = await db.get_user_role(peer_id, user_id)
    sender_priority = sender_role[1] if sender_role else 0
    target_priority = target_role[1] if target_role else 0

    if target_priority == 100:
        await message.answer("❌ Нельзя кикнуть владельца беседы")
        return
    
    if target_priority >= sender_priority and from_id not in BOT_OWNER_IDS:
        await message.answer("❌ Нельзя кикнуть пользователя с равным или более высоким приоритетом")
        return
    
    try:
        await bp.api.messages.remove_chat_user(
            chat_id=peer_id - 2000000000,
            member_id=user_id
        )
    except Exception as e:
        logger.error(f"Ошибка при кике: {e}")
        await message.answer("❌ Не удалось кикнуть пользователя")
        return
    
    try:
        await db.log_action(peer_id, user_id, str(from_id), "kick", reason)
    except Exception:
        pass

    admin_mention = f"[id{from_id}|Администратор]"
    user_mention = await get_user_mention(peer_id, user_id)
    if reason:
        await message.answer(f"👢 {admin_mention} кикнул пользователя {user_mention}\n📝 Причина: {reason}")
    else:
        await message.answer(f"👢 {admin_mention} кикнул пользователя {user_mention}")


# ==================== Команда /mute ====================
@bp.on.message(MessageRule("/mute"), PriorityRule(30))
async def mute_handler(message: Message) -> None:
    """Выдать мут пользователю(ям) в беседе."""
    peer_id = message.peer_id
    from_id = message.from_id

    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return

    payload = (extract_command_payload(message.text, "mute") or "").strip()

    member_ids: list[int] = []
    mute_for: Optional[int] = None

    if message.reply_message:
        member_ids = [message.reply_message.from_id]
        if payload:
            try:
                mute_for = int(payload.split()[0])
            except ValueError:
                await message.answer("📋 Использование: /mute [reply/@user/id,id] [время_в_секундах]")
                return
    else:
        mention_ids = [int(uid) for uid in re.findall(r"\[id(\d+)\|[^\]]+\]", payload)]
        if mention_ids:
            member_ids = mention_ids
            rest = re.sub(r"\[id\d+\|[^\]]+\]", "", payload).strip()
            if rest:
                try:
                    mute_for = int(rest.split()[0])
                except ValueError:
                    await message.answer("📋 Использование: /mute [reply/@user/id,id] [время_в_секундах]")
                    return
        else:
            args = payload.split()
            if not args:
                await message.answer("📋 Использование: /mute [reply/@user/id,id] [время_в_секундах]")
                return

            raw_ids = args[0].replace(" ", "")
            if not re.fullmatch(r"\d{1,12}(,\d{1,12})*", raw_ids):
                await message.answer("📋 Использование: /mute [reply/@user/id,id] [время_в_секундах]")
                return
            member_ids = [int(uid) for uid in raw_ids.split(",") if uid]
            if len(args) > 1:
                try:
                    mute_for = int(args[1])
                except ValueError:
                    await message.answer("📋 Использование: /mute [reply/@user/id,id] [время_в_секундах]")
                    return

    member_ids = list(dict.fromkeys(member_ids))
    if not member_ids:
        await message.answer("❌ Не удалось определить пользователя")
        return

    if any(uid == from_id for uid in member_ids):
        await message.answer("❌ Нельзя выдать мут самому себе")
        return

    if mute_for is not None and mute_for <= 0:
        await message.answer("❌ Время мута должно быть больше 0 секунд")
        return

    sender_role = await db.get_user_role(peer_id, from_id)
    sender_priority = sender_role[1] if sender_role else 0
    for uid in member_ids:
        target_role = await db.get_user_role(peer_id, uid)
        target_priority = target_role[1] if target_role else 0
        if target_priority == 100:
            await message.answer("❌ Нельзя выдать мут владельцу беседы")
            return
        if target_priority >= sender_priority and from_id not in BOT_OWNER_IDS:
            await message.answer("❌ Нельзя выдать мут пользователю с равным или более высоким приоритетом")
            return

    params = {
        "peer_id": peer_id,
        "member_ids": ",".join(str(uid) for uid in member_ids),
        "action": "ro",
    }
    if mute_for is not None:
        params["for"] = mute_for

    try:
        await vk_api_request("messages.changeConversationMemberRestrictions", **params)
    except Exception as e:
        logger.error(f"Ошибка при выдаче мута: {e}")
        await message.answer("❌ Не удалось выдать мут")
        return

    for uid in member_ids:
        try:
            await db.log_action(peer_id, uid, str(from_id), "mute", f"{mute_for or -1}")
        except Exception:
            pass

    admin_mention = f"[id{from_id}|Администратор]"
    users_mention = ", ".join([await get_user_mention(peer_id, uid) for uid in member_ids])
    if mute_for is None:
        await message.answer(f"🔇 {admin_mention} выдал мут пользователю(ям): {users_mention}\n⏱️ Срок: навсегда")
    else:
        await message.answer(f"🔇 {admin_mention} выдал мут пользователю(ям): {users_mention}\n⏱️ Срок: {mute_for} сек.")


# ==================== Команда /unmute ====================
@bp.on.message(MessageRule("/unmute"), PriorityRule(30))
async def unmute_handler(message: Message) -> None:
    """Снять мут с пользователя(ей) в беседе."""
    peer_id = message.peer_id
    from_id = message.from_id

    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return

    payload = (extract_command_payload(message.text, "unmute") or "").strip()
    member_ids: list[int] = []

    if message.reply_message:
        member_ids = [message.reply_message.from_id]
    else:
        mention_ids = [int(uid) for uid in re.findall(r"\[id(\d+)\|[^\]]+\]", payload)]
        if mention_ids:
            member_ids = mention_ids
        else:
            raw_ids = payload.replace(" ", "")
            if not raw_ids or not re.fullmatch(r"\d{1,12}(,\d{1,12})*", raw_ids):
                await message.answer("📋 Использование: /unmute [reply/@user/id,id]")
                return
            member_ids = [int(uid) for uid in raw_ids.split(",") if uid]

    member_ids = list(dict.fromkeys(member_ids))
    if not member_ids:
        await message.answer("❌ Не удалось определить пользователя")
        return

    params = {
        "peer_id": peer_id,
        "member_ids": ",".join(str(uid) for uid in member_ids),
        "action": "rw",
    }

    try:
        await vk_api_request("messages.changeConversationMemberRestrictions", **params)
    except Exception as e:
        logger.error(f"Ошибка при снятии мута: {e}")
        await message.answer("❌ Не удалось снять мут")
        return

    for uid in member_ids:
        try:
            await db.log_action(peer_id, uid, str(from_id), "unmute", "")
        except Exception:
            pass

    admin_mention = f"[id{from_id}|Администратор]"
    users_mention = ", ".join([await get_user_mention(peer_id, uid) for uid in member_ids])
    await message.answer(f"🔊 {admin_mention} снял мут с пользователя(ей): {users_mention}")


# ==================== Команда /roles ====================
@bp.on.message(MessageRule("/roles"), PriorityRule(40))
async def roles_handler(message: Message) -> None:
    """Показать список ролей или импортировать набор ролей"""
    peer_id = message.peer_id
    from_id = message.from_id
    
    # Проверяем, есть ли аргументы после /roles
    args = message.text.replace("/roles", "").strip()

    # Если есть "import" - это команда импорта
    if args.lower().startswith("import"):
        # Только беседы
        if peer_id < 2000000000:
            await message.answer("❌ Команда работает только в беседах")
            return
    
        # Только владелец беседы (приоритет 100)
        role = await db.get_user_role(peer_id, from_id)
        if not role or role[1] != 100:
            await message.answer("❌ /roles import доступна только владельцу беседы (приоритет 100)")
            return
    
        arg = args.replace("import", "").strip().upper()

        if arg not in ("SAMP", "CRMP", "ARIZONA"):
            await message.answer(
                "📋 Использование: /roles import [SAMP|CRMP|ARIZONA]\n\n"
                "Доступные наборы ролей:\n"
                "• SAMP — роли для SA:MP сервера\n"
                "• CRMP — роли для CR:MP сервера\n"
                "• ARIZONA — роли для Arizona RP\n\n"
                "Пример: /roles import SAMP"
            )
            return
        
        # Наборы ролей (без префикса SAMP/CRMP)
        SAMP_ROLES = [
            (0, "Администратор"),
            (40, "Заместитель ГА"),
            (50, "Главный Администратор"),
            (60, "Заместитель КС"),
            (70, "Куратор Сервера"),
            (80, "Помощник Основателя"),
            (90, "Заместитель Основателя"),
            (95, "И.О Основателя"),
            (96, "Куратор И.О"),
            (97, "Основатель Сервера"),
            (99, "Старшее Руководство"),
            (100, "Верховная Администрация"),
        ]

        CRMP_ROLES = [
            (0, "Младший модератор"),
            (5, "Модератор"),
            (10, "Старший модератор"),
            (30, "Администратор"),
            (45, "Старший администратор"),
            (55, "Куратор администрации"),
            (65, "Технический специалист"),
            (70, "Зам главного администратора"),
            (80, "Главный администратор"),
            (85, "Разработчик"),
            (90, "Зам. основателя"),
            (100, "Владелец"),
        ]

        ARIZONA_ROLES = [
            (0, "Администраторы 1-3 уровня"),
            (10, "Следящий (4 уровень adm)"),
            (30, "ЗГС Гетто, Госс, Мафий"),
            (40, "ГС Гетто, Госс, Мафий"),
            (41, "Следящий за Хелперами"),
            (60, "Куратор"),
            (75, "Заместитель главного администратора"),
            (80, "Главный Администратор"),
            (90, "Спец. Администратор"),
            (95, "Заместитель основателя"),
            (100, "Основатель"),
        ]

        if arg == "SAMP":
            roles_to_import = SAMP_ROLES
            game_name = "SA:MP"
        elif arg == "CRMP":
            roles_to_import = CRMP_ROLES
            game_name = "CR:MP"
        else:  # ARIZONA
            roles_to_import = ARIZONA_ROLES
            game_name = "Arizona RP"

        # Удаляем старые роли (кроме базовых 0 и 100)
        existing_roles = await db.get_roles(peer_id)
        for role_name, priority in existing_roles:
            if priority not in [0, 100]:
                await db.delete_role(peer_id, priority)

        # Импортируем роли
        for priority, name in roles_to_import:
            await db.add_role(peer_id, name, priority)

        roles_list = "\n".join([f"  🔹 {name} ({priority})" for priority, name in roles_to_import])

        admin_mention = f"[id{from_id}|Владелец]"
        await message.answer(
            f"✅ {admin_mention} импортировал набор ролей для {game_name}:\n\n{roles_list}\n\n"
            f"💡 Используйте /roles для просмотра ролей"
        )
        return
    
    # Иначе - просто показываем список ролей
    roles = await db.get_roles(peer_id)

    if not roles:
        roles_text = "📋 Роли в беседе:\n\n🔹 Владелец беседы (100)\n🔹 Зам. владельца (90)\n🔹 Главный админ (75)\n🔹 Зам. главного админа (65)\n🔹 Старший админ (40)\n🔹 Админ (30)\n🔹 Младший админ (20)\n🔹 Модератор (10)\n🔹 Пользователь (0)\n\n💡 Используйте /newrole [приоритет] [название] для создания своей роли"
    else:
        roles_text = "📋 Роли в беседе:\n\n"
        for name, priority in roles:
            roles_text += f"🔹 {name} ({priority})\n"
        roles_text += "\n💡 Используйте /newrole [приоритет] [название] для создания своей роли"

    await message.answer(roles_text)


# ==================== Команда /admins ====================
@bp.on.message(MessageRule("/admins"), PriorityRule(0))
async def admins_handler(message: Message) -> None:
    """Показать список администраторов с кнопками"""
    peer_id = message.peer_id
    admins = await db.get_admins(peer_id)
    
    if not admins:
        admins_text = "👥 Администраторы беседы:\n\n⚠️ Нет пользователей с правами администратора (приоритет 1-100)"
        keyboard = Keyboard(inline=True).row().add(Callback("🔄 Обновить", payload={"action": "admins_refresh"}))
        await message.answer(admins_text, keyboard=keyboard.get_json())
        return
    
    # По умолчанию показываем ники
    await send_admins_list(message, admins, view_type="nicknames")


async def send_admins_list(message: Message, admins: list, view_type: str = "nicknames", edit: bool = False) -> None:
    """Отправить или обновить список администраторов"""
    peer_id = message.peer_id

    # Получаем информацию о пользователях через VK API
    user_ids = [user_id for user_id, _, _ in admins]
    try:
        users_info = await bp.api.users.get(user_ids=user_ids)
        users_map = {u.id: f"{u.first_name} {u.last_name}" for u in users_info}
    except Exception:
        users_map = {uid: "Пользователь" for uid in user_ids}

    current_priority = None
    admins_text = f"👥 Администраторы беседы ({'Ники' if view_type == 'nicknames' else 'Имена'}):\n\n"

    for user_id, role_name, priority in admins:
        if priority != current_priority:
            admins_text += f"\n🏆 {role_name} (приоритет: {priority}):\n"
            current_priority = priority

        if view_type == "names":
            # Показываем только VK имена (ссылка без упоминания)
            vk_name = users_map.get(user_id, "Пользователь")
            display_name = sanitize_plain_name(vk_name)
            admins_text += f"▪️ [vk.com/id{user_id}|{display_name}]\n"
        else:
            # Показываем ники или VK имена (ссылка без упоминания)
            custom_nick = await db.get_nickname(peer_id, user_id)
            display_name = sanitize_plain_name(custom_nick) if custom_nick else sanitize_plain_name(users_map.get(user_id, "Пользователь"))
            admins_text += f"▪️ [vk.com/id{user_id}|{display_name}]\n"

    # Кнопки
    keyboard = Keyboard(inline=True).row()
    keyboard.add(
        Callback("👤 Имена", payload={"action": "admins_view", "view": "names"}),
        color=KeyboardButtonColor.SECONDARY if view_type == "nicknames" else KeyboardButtonColor.PRIMARY
    )
    keyboard.add(
        Callback("🔖 Ники", payload={"action": "admins_view", "view": "nicknames"}),
        color=KeyboardButtonColor.PRIMARY if view_type == "nicknames" else KeyboardButtonColor.SECONDARY
    )
    keyboard.row()
    keyboard.add(Callback("🔄 Обновить", payload={"action": "admins_refresh"}))

    keyboard_json = keyboard.get_json()

    if edit and hasattr(message, 'conversation_message_id'):
        try:
            await bp.api.messages.edit(
                peer_id=peer_id,
                conversation_message_id=message.conversation_message_id,
                message=admins_text,
                keyboard=keyboard_json
            )
            return
        except Exception:
            pass
    
    await message.answer(admins_text, keyboard=keyboard_json)


# ==================== Команда /newrole ====================
@bp.on.message(MessageRule("/newrole"), PriorityRule(75))
async def newrole_handler(message: Message) -> None:
    """Создать или изменить роль"""
    peer_id = message.peer_id
    from_id = message.from_id
    
    args = message.text.replace("/newrole", "").strip().split(maxsplit=1)
    
    if len(args) < 2:
        await message.answer("📋 Использование: /newrole [приоритет] [название]\nПример: /newrole 55 Тест")
        return
    
    try:
        priority = int(args[0])
        name = args[1]
    except ValueError:
        await message.answer("❌ Приоритет должен быть числом!")
        return
    
    if priority < 0 or priority > 100:
        await message.answer("❌ Приоритет должен быть от 0 до 100")
        return
    
    await db.add_role(peer_id, name, priority)
    
    admin_mention = f"[id{from_id}|Администратор]"
    await message.answer(f"✅ {admin_mention} создал/изменил роль «{name}» с приоритетом {priority}")


# ==================== Команда /delrole ====================
@bp.on.message(MessageRule("/delrole"), PriorityRule(75))
async def delrole_handler(message: Message) -> None:
    """Удалить роль по приоритету"""
    peer_id = message.peer_id
    from_id = message.from_id
    
    args = message.text.replace("/delrole", "").strip()
    
    if not args:
        await message.answer("📋 Использование: /delrole [приоритет]\nПример: /delrole 55")
        return
    
    try:
        priority = int(args)
    except ValueError:
        await message.answer("❌ Приоритет должен быть числом!")
        return
    
    if priority < 0 or priority > 100:
        await message.answer("❌ Приоритет должен быть от 0 до 100")
        return
    
    # Проверяем, что роль существует
    roles = await db.get_roles(peer_id)
    role_exists = any(r[1] == priority for r in roles)
    if not role_exists:
        await message.answer(f"❌ Роль с приоритетом {priority} не найдена")
        return
    
    # Нельзя удалять базовые роли 0 и 100
    if priority in [100, 0]:
        await message.answer("❌ Нельзя удалить базовую роль (0 или 100)")
        return
    
    success = await db.delete_role(peer_id, priority)
    
    if success:
        admin_mention = f"[id{from_id}|Администратор]"
        await message.answer(f"✅ {admin_mention} удалил роль с приоритетом {priority}")
    else:
        await message.answer(f"❌ Не удалось удалить роль с приоритетом {priority}")


# ==================== Команда /role ====================
@bp.on.message(MessageRule("/role"), PriorityRule(40))
async def role_handler(message: Message) -> None:
    """Выдать роль пользователю"""
    peer_id = message.peer_id
    user_id = None
    role_arg = ""
    
    from_id = message.from_id
    
    if message.reply_message:
        user_id = message.reply_message.from_id
        role_arg = message.text.replace("/role", "").strip()
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
            role_arg = re.sub(r'\[id\d+\|[^\]]+\]', '', text).replace("/role", "").strip()
        else:
            await message.answer("📋 Использование: /role [reply/@user] [приоритет/название]\nПример: /role @user 55 или /role @user Модератор")
            return
    
    if not user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    # Определяем максимум, который можно выдать:
    # - приоритет 100 может выдавать максимум 99
    # - любой другой может выдавать только ниже своего (strictly less)
    sender_role = await db.get_user_role(peer_id, from_id)
    sender_priority = sender_role[1] if sender_role else 0
    if sender_priority >= 100:
        max_assignable_priority = 99
    else:
        max_assignable_priority = sender_priority - 1
    
    if not role_arg:
        # Показываем текущую роль
        user_role = await db.get_user_role(peer_id, user_id)
        user_mention = f"[id{user_id}|Пользователь]"
        if user_role:
            role_name, priority = user_role
            await message.answer(f"ℹ️ Роль пользователя {user_mention}: {role_name} (приоритет: {priority})")
        else:
            await message.answer(f"ℹ️ Роль пользователя {user_mention}: Пользователь (приоритет: 0)")
        return
    
    # Пробуем как число (приоритет)
    try:
        priority = int(role_arg)
        if priority > max_assignable_priority:
            await message.answer(
                f"❌ Нельзя выдать роль с приоритетом {priority}. "
                f"Ваш максимум для выдачи: {max_assignable_priority}"
            )
            return
        success = await db.set_user_role(peer_id, user_id, priority)
    except ValueError:
        # Пробуем как название роли
        roles = await db.get_roles(peer_id)
        role_priority = None
        for role_name, role_prio in roles:
            if role_name.lower() == role_arg.lower():
                role_priority = role_prio
                break
        if role_priority is not None and role_priority > max_assignable_priority:
            await message.answer(
                f"❌ Нельзя выдать роль «{role_arg}». "
                f"Её приоритет {role_priority}, а ваш максимум: {max_assignable_priority}"
            )
            return
        success = await db.set_user_role_by_name(peer_id, user_id, role_arg)
        priority = None
    
    user_mention = f"[id{user_id}|Пользователь]"
    admin_mention = f"[id{from_id}|Администратор]"
    
    if success:
        if priority is not None:
            await message.answer(f"✅ {admin_mention} выдал пользователю {user_mention} роль с приоритетом {priority}")
        else:
            await message.answer(f"✅ {admin_mention} выдал пользователю {user_mention} роль «{role_arg}»")
    else:
        if priority is not None:
            await message.answer(f"❌ Роль с приоритетом {priority} не найдена. Используйте /roles для просмотра ролей")
        else:
            await message.answer(f"❌ Роль «{role_arg}» не найдена. Используйте /roles для просмотра ролей")


# ==================== Вспомогательная функция для получения пользователя с ником ====================
async def get_user_mention(peer_id: int, user_id: int) -> str:
    """Получить mention пользователя с ником"""
    nickname = await db.get_nickname(peer_id, user_id)
    if nickname:
        return nickname
    return f"[id{user_id}|Пользователь]"


# ==================== Команда /snick ====================
@bp.on.message(MessageRule("/snick"), PriorityRule(30))
async def snick_handler(message: Message) -> None:
    """Установить ник пользователю"""
    peer_id = message.peer_id
    user_id = None
    nickname = ""
    
    from_id = message.from_id
    
    if message.reply_message:
        user_id = message.reply_message.from_id
        # Ник берем из всего текста после команды
        nickname = message.text.replace("/snick", "").strip()
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
            # Убираем упоминание и получаем ник
            nickname = re.sub(r'\[id\d+\|[^\]]+\]', '', text).replace("/snick", "").strip()
        else:
            await message.answer("📋 Использование: /snick [reply/@user] [ник]\nПример: /snick @user SuperNick")
            return
    
    if not user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    if not nickname:
        await message.answer("❌ Укажите ник")
        return
    
    await db.set_nickname(peer_id, user_id, nickname)
    
    user_mention = await get_user_mention(peer_id, user_id)
    admin_mention = f"[id{from_id}|Администратор]"
    
    await message.answer(f"✅ {admin_mention} установил ник «{nickname}» для пользователя {user_mention}")


# ==================== Команда /rnick ====================
@bp.on.message(MessageRule("/rnick"), PriorityRule(30))
async def rnick_handler(message: Message) -> None:
    """Удалить ник пользователя"""
    peer_id = message.peer_id
    user_id = None
    
    from_id = message.from_id
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            await message.answer("📋 Использование: /rnick [reply/@user]")
            return
    
    if not user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    # Проверяем, есть ли ник
    nickname = await db.get_nickname(peer_id, user_id)
    if not nickname:
        user_mention = await get_user_mention(peer_id, user_id)
        await message.answer(f"ℹ️ У пользователя {user_mention} нет ника")
        return
    
    await db.remove_nickname(peer_id, user_id)
    
    admin_mention = f"[id{from_id}|Администратор]"
    user_mention = f"[id{user_id}|Пользователь]"
    
    await message.answer(f"✅ {admin_mention} удалил ник «{nickname}» у пользователя {user_mention}")


# ==================== Объединения бесед (Unity) ====================

def generate_union_code() -> str:
    """Генерирует уникальный код для объединения"""
    import secrets
    import string
    chars = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(secrets.choice(chars) for _ in range(6))
        # Не начинаем с 0
        if code[0] != '0':
            return code


@bp.on.message(MessageRule("/unity"), PriorityRule(100))
async def unity_handler(message: Message) -> None:
    """Создать объединение бесед"""
    peer_id = message.peer_id
    from_id = message.from_id
    
    # Только беседы
    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return

    # Проверяем, не состоит ли уже в объединении
    existing_union = await db.get_union_by_chat(peer_id)
    if existing_union:
        await message.answer("⚠️ Этот чат уже состоит в объединении")
        return

    # Генерируем код
    union_code = generate_union_code()
    success = await db.create_union(peer_id, union_code)

    if success:
        admin_mention = f"[id{from_id}|Владелец]"
        await message.answer(
            f"✅ {admin_mention} создал объединение бесед!\n\n"
            f"🔑 Код объединения: {union_code}\n\n"
            f"📝 Другие беседы могут присоединиться с помощью:\n"
            f"/unityjoin {union_code}"
        )
    else:
        await message.answer("❌ Не удалось создать объединение")


@bp.on.message(MessageRule("/unityjoin"), PriorityRule(100))
async def unityjoin_handler(message: Message) -> None:
    """Присоединить беседу к объединению"""
    peer_id = message.peer_id
    from_id = message.from_id

    # Только беседы
    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return
    
    # Получаем код из сообщения
    args = message.text.replace("/unityjoin", "").strip().upper()
    if not args:
        await message.answer("📋 Использование: /unityjoin [код]\nПример: /unityjoin ABC123")
        return
    
    # Проверяем, не состоит ли уже в объединении
    existing_union = await db.get_union_by_chat(peer_id)
    if existing_union:
        await message.answer("⚠️ Этот чат уже состоит в объединении")
        return
    
    # Пытаемся присоединиться
    success, msg = await db.join_union(args, peer_id)

    if success:
        admin_mention = f"[id{from_id}|Владелец]"
        await message.answer(f"✅ {admin_mention} присоединил беседу к объединению!\n\n{msg}")
    else:
        await message.answer(f"❌ {msg}")


# Вспомогательная функция для проверки доступа к u-командам
async def check_unity_access(peer_id: int, user_id: int) -> bool:
    """Проверяет, есть ли доступ к командам объединения"""
    # Владелец объединения
    if await db.is_union_admin(peer_id, user_id):
        return True
    return False


async def get_target_user_from_message(message: Message) -> Optional[int]:
    """Получить ID пользователя из reply или mention"""
    if message.reply_message:
        return message.reply_message.from_id

    text = message.text
    mention_match = re.search(r'\[id(\d+)\|', text)
    if mention_match:
        return int(mention_match.group(1))
    return None


@bp.on.message(MessageRule("/urole"), PriorityRule(100))
async def urole_handler(message: Message) -> None:
    """Выдать роль во всех чатах объединения"""
    peer_id = message.peer_id
    from_id = message.from_id

    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return
    
    # Проверяем доступ
    if not await check_unity_access(peer_id, from_id):
        await message.answer("❌ Нет доступа к объединению")
        return
    
    # Получаем объединение
    union = await db.get_union_by_chat(peer_id)
    if not union:
        await message.answer("❌ Этот чат не состоит в объединении")
        return

    union_id = union[0]
    chats = await db.get_union_chats(union_id)

    # Получаем пользователя и приоритет
    user_id = await get_target_user_from_message(message)
    if not user_id:
        await message.answer("📋 Использование: /urole [reply/@user] [приоритет]")
        return

    args = message.text.replace("/urole", "").strip()
    # Убираем mention если есть
    args = re.sub(r'\[id\d+\|[^\]]+\]', '', args).strip()

    try:
        priority = int(args.split()[0] if args else "")
    except (ValueError, IndexError):
        await message.answer("📋 Использование: /urole [reply/@user] [приоритет]\nПример: /urole @user 50")
        return

    # Применяем роль во всех чатах
    success_count = 0
    for chat_id in chats:
        if await db.set_user_role(chat_id, user_id, priority):
            success_count += 1

    user_mention = f"[id{user_id}|Пользователь]"
    admin_mention = f"[id{from_id}|Владелец]"
    await message.answer(
        f"✅ {admin_mention} выдал роль с приоритетом {priority} пользователю {user_mention}\n"
        f"📍 Применено в {success_count}/{len(chats)} беседах объединения"
    )


@bp.on.message(MessageRule("/ukick"), PriorityRule(100))
async def ukick_handler(message: Message) -> None:
    """Кикнуть пользователя из всех чатов объединения"""
    peer_id = message.peer_id
    from_id = message.from_id

    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return

    if not await check_unity_access(peer_id, from_id):
        await message.answer("❌ Нет доступа к объединению")
        return

    union = await db.get_union_by_chat(peer_id)
    if not union:
        await message.answer("❌ Этот чат не состоит в объединении")
        return

    union_id = union[0]
    chats = await db.get_union_chats(union_id)

    user_id = await get_target_user_from_message(message)
    if not user_id:
        await message.answer("📋 Использование: /ukick [reply/@user]")
        return

    # Кикаем из всех чатов
    success_count = 0
    for chat_id in chats:
        try:
            await bp.api.messages.remove_chat_user(
                chat_id=chat_id - 2000000000,
                member_id=user_id
            )
            success_count += 1
        except Exception:
            pass

    user_mention = f"[id{user_id}|Пользователь]"
    admin_mention = f"[id{from_id}|Владелец]"
    await message.answer(
        f"✅ {admin_mention} исключил пользователя {user_mention}\n"
        f"📍 Исключён из {success_count}/{len(chats)} бесед объединения"
    )


@bp.on.message(MessageRule("/uban"), PriorityRule(100))
async def uban_handler(message: Message) -> None:
    """Забанить пользователя во всех чатах объединения"""
    peer_id = message.peer_id
    from_id = message.from_id

    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return

    if not await check_unity_access(peer_id, from_id):
        await message.answer("❌ Нет доступа к объединению")
        return

    union = await db.get_union_by_chat(peer_id)
    if not union:
        await message.answer("❌ Этот чат не состоит в объединении")
        return

    union_id = union[0]
    chats = await db.get_union_chats(union_id)

    user_id = await get_target_user_from_message(message)
    if not user_id:
        await message.answer("📋 Использование: /uban [reply/@user]")
        return

    reason = message.text.replace("/uban", "").strip()
    reason = re.sub(r'\[id\d+\|[^\]]+\]', '', reason).strip()

    # Баним во всех чатах
    success_count = 0
    for chat_id in chats:
        await db.add_ban(chat_id, user_id, -1, reason)
        try:
            await bp.api.messages.remove_chat_user(
                chat_id=chat_id - 2000000000,
                member_id=user_id
            )
        except Exception:
            pass
        success_count += 1

    user_mention = f"[id{user_id}|Пользователь]"
    admin_mention = f"[id{from_id}|Владелец]"
    await message.answer(
        f"✅ {admin_mention} забанил пользователя {user_mention}\n"
        f"📍 Забанен в {success_count}/{len(chats)} беседах объединения"
    )


@bp.on.message(MessageRule("/usnick"), PriorityRule(100))
async def usnick_handler(message: Message) -> None:
    """Установить ник во всех чатах объединения"""
    peer_id = message.peer_id
    from_id = message.from_id

    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return

    if not await check_unity_access(peer_id, from_id):
        await message.answer("❌ Нет доступа к объединению")
        return

    union = await db.get_union_by_chat(peer_id)
    if not union:
        await message.answer("❌ Этот чат не состоит в объединении")
        return

    union_id = union[0]
    chats = await db.get_union_chats(union_id)

    user_id = await get_target_user_from_message(message)
    if not user_id:
        await message.answer("📋 Использование: /usnick [reply/@user] [ник]")
        return

    nickname = message.text.replace("/usnick", "").strip()
    nickname = re.sub(r'\[id\d+\|[^\]]+\]', '', nickname).strip()

    if not nickname:
        await message.answer("📋 Использование: /usnick [reply/@user] [ник]")
        return

    # Устанавливаем ник во всех чатах
    success_count = 0
    for chat_id in chats:
        await db.set_nickname(chat_id, user_id, nickname)
        success_count += 1

    user_mention = f"[id{user_id}|Пользователь]"
    admin_mention = f"[id{from_id}|Владелец]"
    await message.answer(
        f"✅ {admin_mention} установил ник «{nickname}» для {user_mention}\n"
        f"📍 Установлен в {success_count}/{len(chats)} беседах объединения"
    )


@bp.on.message(MessageRule("/urnick"), PriorityRule(100))
async def urnick_handler(message: Message) -> None:
    """Удалить ник во всех чатах объединения"""
    peer_id = message.peer_id
    from_id = message.from_id

    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return

    if not await check_unity_access(peer_id, from_id):
        await message.answer("❌ Нет доступа к объединению")
        return

    union = await db.get_union_by_chat(peer_id)
    if not union:
        await message.answer("❌ Этот чат не состоит в объединении")
        return

    union_id = union[0]
    chats = await db.get_union_chats(union_id)

    user_id = await get_target_user_from_message(message)
    if not user_id:
        await message.answer("📋 Использование: /urnick [reply/@user]")
        return

    # Удаляем ник во всех чатах
    success_count = 0
    for chat_id in chats:
        await db.remove_nickname(chat_id, user_id)
        success_count += 1

    user_mention = f"[id{user_id}|Пользователь]"
    admin_mention = f"[id{from_id}|Владелец]"
    await message.answer(
        f"✅ {admin_mention} удалил ник у пользователя {user_mention}\n"
        f"📍 Удалён в {success_count}/{len(chats)} беседах объединения"
    )


@bp.on.message(MessageRule("/uwarn"), PriorityRule(100))
async def uwarn_handler(message: Message) -> None:
    """Выдать предупреждение во всех чатах объединения"""
    peer_id = message.peer_id
    from_id = message.from_id

    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return

    if not await check_unity_access(peer_id, from_id):
        await message.answer("❌ Нет доступа к объединению")
        return

    union = await db.get_union_by_chat(peer_id)
    if not union:
        await message.answer("❌ Этот чат не состоит в объединении")
        return

    union_id = union[0]
    chats = await db.get_union_chats(union_id)

    user_id = await get_target_user_from_message(message)
    if not user_id:
        await message.answer("📋 Использование: /uwarn [reply/@user] [причина]")
        return

    reason = message.text.replace("/uwarn", "").strip()
    reason = re.sub(r'\[id\d+\|[^\]]+\]', '', reason).strip()

    # Выдаём предупреждение во всех чатах
    success_count = 0
    for chat_id in chats:
        await db.add_warning(chat_id, user_id, reason)
        success_count += 1

    user_mention = f"[id{user_id}|Пользователь]"
    admin_mention = f"[id{from_id}|Владелец]"
    await message.answer(
        f"✅ {admin_mention} выдал предупреждение пользователю {user_mention}\n"
        f"📍 Выдано в {success_count}/{len(chats)} беседах объединения"
    )


@bp.on.message(MessageRule("/uunwarn"), PriorityRule(100))
async def uunwarn_handler(message: Message) -> None:
    """Снять все предупреждения во всех чатах объединения"""
    peer_id = message.peer_id
    from_id = message.from_id

    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return

    if not await check_unity_access(peer_id, from_id):
        await message.answer("❌ Нет доступа к объединению")
        return

    union = await db.get_union_by_chat(peer_id)
    if not union:
        await message.answer("❌ Этот чат не состоит в объединении")
        return

    union_id = union[0]
    chats = await db.get_union_chats(union_id)

    user_id = await get_target_user_from_message(message)
    if not user_id:
        await message.answer("📋 Использование: /uunwarn [reply/@user]")
        return

    # Снимаем предупреждения во всех чатах
    success_count = 0
    for chat_id in chats:
        await db.clear_warnings(chat_id, user_id)
        success_count += 1

    user_mention = f"[id{user_id}|Пользователь]"
    admin_mention = f"[id{from_id}|Владелец]"
    await message.answer(
        f"✅ {admin_mention} снял все предупреждения с пользователя {user_mention}\n"
        f"📍 Снято в {success_count}/{len(chats)} беседах объединения"
    )


# ==================== Команды для владельца бота ====================

# ==================== Команда /sysban ====================
@bp.on.message(MessageRule("/sysban"), IsOwnerRule())
async def sysban_handler(message: Message) -> None:
    """Забанить пользователя в боте глобально"""
    user_id = None
    reason = ""
    
    if message.reply_message:
        user_id = message.reply_message.from_id
        reason = message.text.replace("/sysban", "").strip()
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
            reason = re.sub(r'\[id\d+\|[^\]]+\]', '', text).replace("/sysban", "").strip()
        else:
            await message.answer("📋 Использование: /sysban [reply/@user] [причина]")
            return
    
    if not user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    # Проверяем, не является ли пользователь владельцем бота
    if user_id in BOT_OWNER_IDS:
        await message.answer("❌ Нельзя заблокировать владельца бота")
        return
    
    await db.add_sysban(user_id, reason)
    
    user_mention = f"[id{user_id}|Пользователь]"
    
    if reason:
        await message.answer(f"✅ Пользователь {user_id} заблокирован в боте\n📝 Причина: {reason}")
    else:
        await message.answer(f"✅ Пользователь {user_id} заблокирован в боте")


# ==================== Команда /unsysban ====================
@bp.on.message(MessageRule("/unsysban"), IsOwnerRule())
async def unsysban_handler(message: Message) -> None:
    """Разбанить пользователя в боте"""
    user_id = None
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            await message.answer("📋 Использование: /unsysban [reply/@user]")
            return
    
    if not user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    if not await db.is_sysbanned(user_id):
        await message.answer(f"ℹ️ Пользователь {user_id} не заблокирован в боте")
        return
    
    await db.remove_sysban(user_id)
    
    await message.answer(f"✅ Пользователь {user_id} разблокирован в боте")





# ==================== Команда /notify ====================
@bp.on.message(MessageRule("/notify"), PriorityRule(100))
async def notify_enable_handler(message: Message) -> None:
    """Включить уведомления для чата"""
    peer_id = message.peer_id

    # Только беседы
    if peer_id < 2000000000:
        await message.answer("❌ /notify работает только в беседах")
        return

    # Только владелец беседы (приоритет 100)
    role = await db.get_user_role(peer_id, message.from_id)
    if not role or role[1] != 100:
        await message.answer("❌ Эта команда доступна только владельцу беседы (приоритет 100)")
        return
    
    await db.set_notify(peer_id, True)
    
    await message.answer("✅ Уведомления включены для этого чата")


# ==================== Команда /setsubpub ====================
@bp.on.message(MessageRule("/setsubpub"), PriorityRule(100))
async def setsubpub_handler(message: Message) -> None:
    """Установить сообщество для проверки подписки"""
    peer_id = message.peer_id
    from_id = message.from_id
    
    # Только беседы
    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return

    # Только владелец беседы (приоритет 100)
    role = await db.get_user_role(peer_id, from_id)
    if not role or role[1] != 100:
        await message.answer("❌ Эта команда доступна только владельцу беседы (приоритет 100)")
        return

    args = message.text.replace("/setsubpub", "").strip()
    if not args:
        # Показать текущее значение
        current = await db.get_sub_community(peer_id)
        if current:
            await message.answer(f"📌 Текущее сообщество для проверки подписки: {current}\n\nЧтобы изменить: /setsubpub [id сообщества]\nЧтобы отключить: /setsubpub off")
        else:
            await message.answer("📌 Проверка подписки отключена\n\nЧтобы включить: /setsubpub [id сообщества]")
        return
    
    if args.lower() == "off" or args == "0":
        await db.set_sub_community(peer_id, 0)
        await message.answer("✅ Проверка подписки отключена")
        return
    
    # Пробуем получить ID сообщества
    try:
        community_id = int(args.replace("-", "").replace("club", ""))
    except ValueError:
        await message.answer("❌ Неверный формат ID сообщества\nПример: /setsubpub 123456789")
        return
    
    # Проверяем, что сообщество существует
    try:
        community_info = await bp.api.groups.get_by_id(group_ids=[community_id])
        if not community_info or not community_info.groups:
            await message.answer("❌ Сообщество не найдено")
            return
        community_name = community_info.groups[0].name
    except Exception as e:
        logger.error(f"Ошибка при проверке сообщества: {e}")
        await message.answer("❌ Не удалось проверить сообщество. Проверьте ID.")
        return
    
    await db.set_sub_community(peer_id, community_id)
    
    admin_mention = f"[id{from_id}|Владелец]"
    await message.answer(
        f"✅ {admin_mention} установил сообщество для проверки подписки:\n\n"
        f"🏠 {community_name}\n"
        f"🆔 ID: {community_id}\n\n"
        f"Теперь при входе в беседу будет проверяться подписка на это сообщество."
    )
    

# ==================== Команда /groupall ====================
@bp.on.message(MessageRule("/groupall"), IsLeaderRule())
async def groupall_handler(message: Message) -> None:
    """Показать список всех бесед с ботом"""
    import math
    
    # Получаем номер страницы из аргументов
    args = message.text.replace("/groupall", "").strip()
    page = 1
    if args:
        try:
            page = int(args)
        except ValueError:
            page = 1
    
    # Получаем все чаты
    all_chats = await db.get_all_chats()
    
    if not all_chats:
        await message.answer("📋 Бот не состоит ни в одной беседе")
        return

    # Пагинация: 5 чатов на страницу
    per_page = 5
    total_pages = math.ceil(len(all_chats) / per_page)

    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages
    
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    chats_page = all_chats[start_idx:end_idx]
    
    result_text = f"📋 Список бесед с ботом ({len(all_chats)} всего, стр. {page}/{total_pages}):\n\n"
    
    for chat_id in chats_page:
        try:
            # Получаем название беседы
            conv = await bp.api.messages.get_conversations_by_id(peer_ids=[chat_id])
            if conv.items:
                chat_title = conv.items[0].chat_settings.title if hasattr(conv.items[0], 'chat_settings') else "Беседа"
            else:
                chat_title = "Беседа"
            
            # Получаем количество участников
            members = await bp.api.messages.get_conversation_members(peer_id=chat_id)
            member_count = len(members.items) if members.items else 0
            
            # Получаем ссылку-приглашение
            invite_link = "нет"
            try:
                link_info = await bp.api.messages.get_invite_link(peer_id=chat_id, reset=0)
                if link_info:
                    invite_link = link_info.link
            except Exception:
                pass
            
            result_text += f"📌 {chat_title}\n"
            result_text += f"   🔗 Ссылка: {invite_link}\n"
            result_text += f"   🆔 Peer ID: {chat_id}\n"
            result_text += f"   👥 Участников: {member_count}\n\n"
        except Exception as e:
            logger.error(f"Ошибка при получении инфы о чате {chat_id}: {e}")
            result_text += f"📌 Беседа {chat_id}\n   🆔 Peer ID: {chat_id}\n   ⚠️ Ошибка получения данных\n\n"
    
    # Кнопки навигации если нужно + кнопка ввода страницы
    keyboard = Keyboard(inline=True)
    if total_pages > 1:
        if page > 1:
            keyboard.add(Callback("◀ Назад", payload={"action": "groupall_page", "page": page - 1}))
        if page < total_pages:
            keyboard.add(Callback("Вперёд ▶", payload={"action": "groupall_page", "page": page + 1}))
        # Кнопка ввода страницы
        keyboard.row()
        keyboard.add(Callback("🔢 Ввести страницу", payload={"action": "groupall_input_page"}))
    
    keyboard_json = keyboard.get_json()
    await message.answer(result_text, keyboard=keyboard_json if total_pages > 1 else None)


# ==================== Команда /settings ====================
@bp.on.message(MessageRule("/settings"), PriorityRule(90))
async def settings_handler(message: Message) -> None:
    """Показать настройки беседы"""
    peer_id = message.peer_id
    
    # Проверяем что это беседа
    if peer_id < 2000000000:
        await message.answer("❌ Команда работает только в беседах")
        return
    
    # Получаем текущие настройки
    allow_games, allow_community_add, auto_kick_on_leave = await db.get_chat_settings(peer_id)
    
    # Формируем текст настроек
    settings_text = "⚙️ Настройки беседы\n\n"
    settings_text += "🎮 Игровые команды: " + ("включены ✅" if allow_games else "отключены ⛔") + "\n"
    settings_text += "🤖 Добавление ботов/сообществ: " + ("разрешено ✅" if allow_community_add else "запрещено ⛔") + "\n"
    settings_text += "🚪 Авто-кик при выходе: " + ("включён ✅" if auto_kick_on_leave else "выключен ⛔")
    
    # Получаем клавиатуру
    keyboard_json = build_settings_keyboard(allow_games, allow_community_add, auto_kick_on_leave)
    
    await message.answer(settings_text, keyboard=keyboard_json)


# ==================== Команда /sysrole ====================
@bp.on.message(MessageRule("/sysrole"), IsOwnerRule())
async def sysrole_handler(message: Message) -> None:
    """Выдать роль в указанной беседе"""
    text = message.text.replace("/sysrole", "").strip()
    
    if not text:
        await message.answer("📋 Использование: /sysrole [peer_id] [@user] [приоритет] [причина]\nПример: /sysrole 2000000001 @user 50 Тестовая роль")
        return

    import re
    
    # Парсим peer_id (первый аргумент - число)
    parts = text.split()
    if len(parts) < 3:
        await message.answer("📋 Использование: /sysrole [peer_id] [@user] [приоритет] [причина]\nПример: /sysrole 2000000001 @user 50 Тестовая роль")
        return
    
    try:
        peer_id = int(parts[0])
    except ValueError:
        await message.answer("❌ Первый аргумент должен быть peer_id (число)")
        return
    
    # Парсим user_id из @user или [id123|]
    mention_match = re.search(r'\[id(\d+)\|', text)
    if mention_match:
        user_id = int(mention_match.group(1))
    else:
        # Ищем @username
        user_match = re.search(r'@(\w+)', text)
        if user_match:
            username = user_match.group(1)
            try:
                users = await bp.api.users.get(user_ids=[username])
                if users:
                    user_id = users[0].id
                else:
                    await message.answer("❌ Пользователь не найден")
                    return
            except Exception:
                await message.answer("❌ Не удалось найти пользователя")
                return
        else:
            await message.answer("❌ Укажите пользователя в формате @user или [id123|]")
            return
    
    # Убираем peer_id и mention, получаем приоритет и причину
    remaining = re.sub(r'\[id\d+\|[^\]]+\]', '', text)
    remaining = re.sub(r'@\w+', '', remaining)
    remaining = remaining.replace(str(peer_id), '', 1).strip()
    
    parts2 = remaining.split(maxsplit=1)
    if not parts2:
        await message.answer("📋 Использование: /sysrole [peer_id] [@user] [приоритет] [причина]")
        return
    
    try:
        priority = int(parts2[0])
    except ValueError:
        await message.answer("❌ Приоритет должен быть числом")
        return
    
    reason = parts2[1] if len(parts2) > 1 else ""
    
    # Проверяем, что чат существует в БД
    all_chats = await db.get_all_chats()
    if peer_id not in all_chats:
        await message.answer(f"⚠️ Бот не состоит в беседе {peer_id}")
        return
    
    # Выдаём роль
    success = await db.set_user_role(peer_id, user_id, priority)
    
    if success:
        user_mention = f"[id{user_id}|Пользователь]"
        admin_mention = f"[id{BOT_OWNER_ID}|Владелец бота]"
        
        if reason:
            await message.answer(f"✅ {admin_mention} выдал пользователю {user_mention} роль с приоритетом {priority} в беседе {peer_id}\n📝 Причина: {reason}")
        else:
            await message.answer(f"✅ {admin_mention} выдал пользователю {user_mention} роль с приоритетом {priority} в беседе {peer_id}")
    else:
        await message.answer(f"❌ Роль с приоритетом {priority} не найдена в беседе {peer_id}")


# ==================== Команда /addownerbot ====================
@bp.on.message(MessageRule("/addownerbot"), IsOwnerRule())
async def addownerbot_handler(message: Message) -> None:
    """Добавить дополнительного владельца бота (только для владельца в коде)"""
    # Проверяем что это именно основной владелец (тот, кто в коде)
    if message.from_id not in BOT_OWNER_IDS:
        await message.answer("❌ Эта команда доступна только основному владельцу бота")
        return
    
    user_id = None
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            user_match = re.search(r'@(\w+)', text.replace("/addownerbot", "").strip())
            if user_match:
                username = user_match.group(1)
                try:
                    users = await bp.api.users.get(user_ids=[username])
                    if users:
                        user_id = users[0].id
                except Exception:
                    pass
    
    if not user_id:
        await message.answer("📋 Использование: /addownerbot [reply/@user]")
        return
    
    if user_id in BOT_OWNER_IDS:
        await message.answer("❌ Это основной владелец бота")
        return

    if await db.is_bot_owner(user_id):
        await message.answer("ℹ️ Этот пользователь уже является владельцем бота")
        return

    success = await db.add_bot_owner(user_id)
    
    if success:
        user_mention = f"[id{user_id}|Пользователь]"
        await message.answer(f"✅ {user_mention} добавлен как владелец бота")
    else:
        await message.answer("❌ Не удалось добавить владельца")


# ==================== Команда /delownerbot ====================
@bp.on.message(MessageRule("/delownerbot"), IsOwnerRule())
async def delownerbot_handler(message: Message) -> None:
    """Удалить дополнительного владельца бота (только для владельца в коде)"""
    # Проверяем что это именно основной владелец (тот, кто в коде)
    if message.from_id not in BOT_OWNER_IDS:
        await message.answer("❌ Эта команда доступна только основному владельцу бота")
        return
    
    user_id = None
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            user_match = re.search(r'@(\w+)', text.replace("/delownerbot", "").strip())
            if user_match:
                username = user_match.group(1)
                try:
                    users = await bp.api.users.get(user_ids=[username])
                    if users:
                        user_id = users[0].id
                except Exception:
                    pass
    
    if not user_id:
        await message.answer("📋 Использование: /delownerbot [reply/@user]")
        return
    
    if user_id in BOT_OWNER_IDS:
        await message.answer("❌ Нельзя удалить основного владельца бота")
        return

    success = await db.remove_bot_owner(user_id)
    
    if success:
        user_mention = f"[id{user_id}|Пользователь]"
        await message.answer(f"✅ {user_mention} удалён из владельцев бота")
    else:
        await message.answer("ℹ️ Этот пользователь не является владельцем бота")


# ==================== Команды для руководства бота ====================

# ==================== Команда /givevip ====================
@bp.on.message(CommandNameRule("givevip"), IsOwnerRule())
async def givevip_handler(message: Message) -> None:
    """Выдать ВИП-статус пользователю (только для владельцев бота)"""
    payload = extract_command_payload(message.text, "givevip") or ""
    target_user_id = message.reply_message.from_id if message.reply_message else parse_target_user_id(message, payload)
    
    if not target_user_id:
        await message.answer("📋 Использование: /givevip [reply/@user] [дней]\nПример: /givevip 123456789 30")
        return

    # Парсим количество дней
    days = 30  # значение по умолчанию
    counts = re.findall(r"\b(\d+)\b", payload)
    if counts:
        days = int(counts[-1])
    
    if days <= 0:
        await message.answer("❌ Количество дней должно быть больше 0")
        return
    
    success = await db.add_vip(target_user_id, days)
    
    if success:
        user_mention = f"[id{target_user_id}|Пользователь]"
        await message.answer(f"✅ {user_mention} получил ВИП-статус на {days} дней!\n💡 Теперь на работе (/job) будете получать от 2500$ до 5000$")
    else:
        await message.answer("❌ Не удалось выдать ВИП-статус")


# ==================== Команда /addrukbot ====================
@bp.on.message(MessageRule("/addrukbot"), IsOwnerRule())
async def addrukbot_handler(message: Message) -> None:
    """Добавить руководство бота (только для владельца в коде)"""   
    # Проверяем что это именно основной владелец
    if message.from_id not in BOT_OWNER_IDS:
        await message.answer("❌ Эта команда доступна только основному владельцу бота")
        return
    
    user_id = None
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            user_match = re.search(r'@(\w+)', text.replace("/addrukbot", "").strip())
            if user_match:
                username = user_match.group(1)
                try:
                    users = await bp.api.users.get(user_ids=[username])
                    if users:
                        user_id = users[0].id
                except Exception:
                    pass
    
    if not user_id:
        await message.answer("📋 Использование: /addrukbot [reply/@user]")
        return
    
    if user_id in BOT_OWNER_IDS:
        await message.answer("❌ Это основной владелец бота")
        return

    if await db.is_bot_owner(user_id):
        await message.answer("ℹ️ Этот пользователь уже является владельцем бота")
        return
    
    if await db.is_bot_leader(user_id):
        await message.answer("ℹ️ Этот пользователь уже является руководством бота")
        return
    
    success = await db.add_bot_leader(user_id)
    
    if success:
        user_mention = f"[id{user_id}|Пользователь]"
        await message.answer(f"✅ {user_mention} добавлен как руководство бота")
    else:
        await message.answer("❌ Не удалось добавить")


# ==================== Команда /delrukbot ====================
@bp.on.message(MessageRule("/delrukbot"), IsOwnerRule())
async def delrukbot_handler(message: Message) -> None:
    """Удалить руководство бота (для всех владельцев бота)"""
    user_id = None
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            user_match = re.search(r'@(\w+)', text.replace("/delrukbot", "").strip())
            if user_match:
                username = user_match.group(1)
                try:
                    users = await bp.api.users.get(user_ids=[username])
                    if users:
                        user_id = users[0].id
                except Exception:
                    pass
    
    if not user_id:
        await message.answer("📋 Использование: /delrukbot [reply/@user]")
        return
    
    success = await db.remove_bot_leader(user_id)
    
    if success:
        user_mention = f"[id{user_id}|Пользователь]"
        await message.answer(f"✅ {user_mention} удалён из руководства бота")
    else:
        await message.answer("ℹ️ Этот пользователь не является руководством бота")


# ==================== Команда /addadminbot ====================
@bp.on.message(MessageRule("/addadminbot"), IsLeaderRule())
async def addadminbot_handler(message: Message) -> None:
    """Добавить админа бота"""
    user_id = None
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            user_match = re.search(r'@(\w+)', text.replace("/addadminbot", "").strip())
            if user_match:
                username = user_match.group(1)
                try:
                    users = await bp.api.users.get(user_ids=[username])
                    if users:
                        user_id = users[0].id
                except Exception:
                    pass
    
    if not user_id:
        await message.answer("📋 Использование: /addadminbot [reply/@user]")
        return
    
    if user_id in BOT_OWNER_IDS:
        await message.answer("❌ Это основной владелец бота")
        return
    
    if await db.is_bot_owner(user_id) or await db.is_bot_leader(user_id):
        await message.answer("ℹ️ Этот пользователь уже имеет высший статус")
        return
    
    if await db.is_bot_admin(user_id):
        await message.answer("ℹ️ Этот пользователь уже является админом бота")
        return
    
    success = await db.add_bot_admin(user_id)
    
    if success:
        user_mention = f"[id{user_id}|Пользователь]"
        await message.answer(f"✅ {user_mention} добавлен как админ бота")
    else:
        await message.answer("❌ Не удалось добавить")


# ==================== Команда /deladminbot ====================
@bp.on.message(MessageRule("/deladminbot"), IsLeaderRule())
async def deladminbot_handler(message: Message) -> None:
    """Удалить админа бота"""
    user_id = None
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            user_match = re.search(r'@(\w+)', text.replace("/deladminbot", "").strip())
            if user_match:
                username = user_match.group(1)
                try:
                    users = await bp.api.users.get(user_ids=[username])
                    if users:
                        user_id = users[0].id
                except Exception:
                    pass
    
    if not user_id:
        await message.answer("📋 Использование: /deladminbot [reply/@user]")
        return
    
    success = await db.remove_bot_admin(user_id)
    
    if success:
        user_mention = f"[id{user_id}|Пользователь]"
        await message.answer(f"✅ {user_mention} удалён из админов бота")
    else:
        await message.answer("ℹ️ Этот пользователь не является админом бота")


# ==================== Команда /addmoderbot ====================
@bp.on.message(MessageRule("/addmoderbot"), IsBotAdminRule())
async def addmoderbot_handler(message: Message) -> None:
    """Добавить модератора бота"""
    user_id = None
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            user_match = re.search(r'@(\w+)', text.replace("/addmoderbot", "").strip())
            if user_match:
                username = user_match.group(1)
                try:
                    users = await bp.api.users.get(user_ids=[username])
                    if users:
                        user_id = users[0].id
                except Exception:
                    pass
    
    if not user_id:
        await message.answer("📋 Использование: /addmoderbot [reply/@user]")
        return
    
    if user_id in BOT_OWNER_IDS:
        await message.answer("❌ Это основной владелец бота")
        return

    if await db.is_bot_owner(user_id) or await db.is_bot_leader(user_id) or await db.is_bot_admin(user_id):
        await message.answer("ℹ️ Этот пользователь уже имеет высший статус")
        return
    
    if await db.is_bot_moderator(user_id):
        await message.answer("ℹ️ Этот пользователь уже является модератором бота")
        return
    
    success = await db.add_bot_moderator(user_id)
    
    if success:
        user_mention = f"[id{user_id}|Пользователь]"
        await message.answer(f"✅ {user_mention} добавлен как модератор бота")
    else:
        await message.answer("❌ Не удалось добавить")


# ==================== Команда /delmoderbot ====================
@bp.on.message(MessageRule("/delmoderbot"), IsBotAdminRule())
async def delmoderbot_handler(message: Message) -> None:
    """Удалить модератора бота"""
    user_id = None
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            user_match = re.search(r'@(\w+)', text.replace("/delmoderbot", "").strip())
            if user_match:
                username = user_match.group(1)
                try:
                    users = await bp.api.users.get(user_ids=[username])
                    if users:
                        user_id = users[0].id
                except Exception:
                    pass
    
    if not user_id:
        await message.answer("📋 Использование: /delmoderbot [reply/@user]")
        return
    
    success = await db.remove_bot_moderator(user_id)
    
    if success:
        user_mention = f"[id{user_id}|Пользователь]"
        await message.answer(f"✅ {user_mention} удалён из модераторов бота")
    else:
        await message.answer("ℹ️ Этот пользователь не является модератором бота")


# ==================== Команда /addhelper ====================
@bp.on.message(MessageRule("/addhelper"), IsBotAdminRule())
async def addhelper_handler(message: Message) -> None:
    """Добавить хелпера бота"""
    user_id = None
    level = 1
    
    text = message.text.replace("/addhelper", "").strip()
    
    # Парсим уровень
    parts = text.split()
    if parts:
        try:
            level = int(parts[0])
            if level < 1 or level > 3:
                await message.answer("❌ Уровень должен быть от 1 до 3")
                return
            # Убираем уровень из текста для поиска user_id
            text = " ".join(parts[1:])
        except ValueError:
            pass
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            user_match = re.search(r'@(\w+)', text)
            if user_match:
                username = user_match.group(1)
                try:
                    users = await bp.api.users.get(user_ids=[username])
                    if users:
                        user_id = users[0].id
                except Exception:
                    pass
    
    if not user_id:
        await message.answer("📋 Использование: /addhelper [уровень 1-3] [reply/@user]\n1 - Стажер\n2 - Советник\n3 - Старший советник")
        return
    
    if user_id in BOT_OWNER_IDS:
        await message.answer("❌ Это основной владелец бота")
        return
    
    if await db.is_bot_owner(user_id) or await db.is_bot_leader(user_id) or await db.is_bot_admin(user_id) or await db.is_bot_moderator(user_id):
        await message.answer("ℹ️ Этот пользователь уже имеет высший статус")
        return
    
    success = await db.add_bot_helper(user_id, level)
    
    level_names = {1: "Стажер", 2: "Советник", 3: "Старший советник"}
    user_mention = f"[id{user_id}|Пользователь]"
    await message.answer(f"✅ {user_mention} добавлен как хелпер бота (уровень {level} - {level_names.get(level, 'Стажер')})")


# ==================== Команда /delhelper ====================
@bp.on.message(MessageRule("/delhelper"), IsBotAdminRule())
async def delhelper_handler(message: Message) -> None:
    """Удалить хелпера бота"""
    user_id = None
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            user_match = re.search(r'@(\w+)', text.replace("/delhelper", "").strip())
            if user_match:
                username = user_match.group(1)
                try:
                    users = await bp.api.users.get(user_ids=[username])
                    if users:
                        user_id = users[0].id
                except Exception:
                    pass
    
    if not user_id:
        await message.answer("📋 Использование: /delhelper [reply/@user]")
        return
    
    success = await db.remove_bot_helper(user_id)
    
    if success:
        user_mention = f"[id{user_id}|Пользователь]"
        await message.answer(f"✅ {user_mention} удалён из хелперов бота")
    else:
        await message.answer(f"ℹ️ Этот пользователь не является хелпером бота")


# ==================== Команда /news (для админов бота) ====================
@bp.on.message(MessageRule("/news"), IsBotAdminRule())
async def news_handler(message: Message) -> None:
    """Отправить сообщение во все чаты"""
    news_text = message.text.replace("/news", "").strip()
    
    if not news_text:
        await message.answer("📋 Использование: /news [текст]\nПример: /news Важное сообщение!")
        return

    # Создаём клавиатуру с кнопкой отключения уведомлений
    keyboard = (
        Keyboard(inline=True)
        .row()
        .add(
            Callback("🔕 Отключить уведомления", payload={"action": "disable_notify"}),
            color=KeyboardButtonColor.SECONDARY
        )
    )
    
    # Получаем все чаты с уведомлениями
    chats = await db.get_all_chats_with_notify()
    
    sent_count = 0
    for chat_id in chats:
        try:
            await bp.api.messages.send(
                peer_id=chat_id,
                message=f"📢 Объявление от команды бота:\n\n{news_text}",
                keyboard=keyboard.get_json(),
                random_id=0
            )
            sent_count += 1
        except Exception as e:
            logger.error(f"Ошибка при отправке в чат {chat_id}: {e}")
    
    await message.answer(f"✅ Сообщение отправлено в {sent_count} чатов")


# ==================== Команда /banlist ====================
@bp.on.message(MessageRule("/banlist"), PriorityRule(30))
async def banlist_handler(message: Message) -> None:
    """Показать список забаненных пользователей"""
    peer_id = message.peer_id
    
    banned_users = await db.get_banned_users(peer_id)
    
    if not banned_users:
        await message.answer("📋 В этом чате нет забаненных пользователей")
        return
    
    result_text = "🚫 Список забаненных пользователей:\n\n"
    
    for user_id, reason, duration, end_time in banned_users:
        user_mention = await get_user_mention(peer_id, user_id)
        
        if duration == -1:
            result_text += f"▪️ {user_mention}\n   🔒 Навсегда\n"
        else:
            result_text += f"▪️ {user_mention}\n   ⏱️ {duration} дней\n"
        
        if reason:
            result_text += f"   📝 Причина: {reason}\n"
        result_text += "\n"
    
    await message.answer(result_text)


# ==================== Команда /nlist ====================
@bp.on.message(MessageRule("/nlist"), PriorityRule(30))
async def nlist_handler(message: Message) -> None:
    """Показать ники пользователей"""
    peer_id = message.peer_id
    
    nicknames = await db.get_all_nicknames(peer_id)
    
    if not nicknames:
        await message.answer("📋 В этом чате нет ников")
        return
    
    result_text = "📋 Ники пользователей:\n\n"
    
    for user_id, nickname in nicknames:
        user_mention = f"[id{user_id}|Пользователь]"
        result_text += f"▪️ {user_mention} → {nickname}\n"
    
    await message.answer(result_text)


# ==================== Команда /warnhistory ====================
@bp.on.message(MessageRule("/warnhistory"), PriorityRule(10))
async def warnhistory_handler(message: Message) -> None:
    """Показать историю предупреждений пользователя"""
    peer_id = message.peer_id
    user_id = None
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            await message.answer("📋 Использование: /warnhistory [reply/@user]")
            return
    
    if not user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    warnings = await db.get_warnings(peer_id, user_id)
    warnings_count = await db.get_warnings_count(peer_id, user_id)
    
    user_mention = await get_user_mention(peer_id, user_id)
    
    if not warnings:
        await message.answer(f"📋 История предупреждений {user_mention}: нет предупреждений")
        return
    
    result_text = f"📋 История предупреждений {user_mention} (всего: {warnings_count}):\n\n"
    
    for i, (warn_id, reason, created_at) in enumerate(warnings, 1):
        result_text += f"{i}. 📅 {created_at}\n"
        if reason:
            result_text += f"   📝 Причина: {reason}\n"
        result_text += "\n"
    
    await message.answer(result_text)


# ==================== Команда /silent ====================
@bp.on.message(MessageRule("/silent"), PriorityRule(30))
async def silent_handler(message: Message) -> None:
    """Включить/выключить режим тишины"""
    peer_id = message.peer_id
    from_id = message.from_id
    
    args = message.text.replace("/silent", "").strip().lower()
    
    current_silent = await db.get_silent_mode(peer_id)
    
    if args in ["on", "вкл", "1", "true"]:
        await db.set_silent_mode(peer_id, True)
        admin_mention = f"[id{from_id}|Администратор]"
        await message.answer(f"🔇 {admin_mention} включил режим тишины\nОбычные пользователи не могут писать в чат")
    elif args in ["off", "выкл", "0", "false"]:
        await db.set_silent_mode(peer_id, False)
        admin_mention = f"[id{from_id}|Администратор]"
        await message.answer(f"🔊 {admin_mention} выключил режим тишины")
    else:
        status = "включён" if current_silent else "выключен"
        await message.answer(f"📋 Режим тишины: {status}\nИспользуйте /silent [on/off]")


# ==================== Команда /welcome ====================
@bp.on.message(CommandNameRule("welcome"))
async def welcome_handler(message: Message) -> None:
    """Настроить приветствие для новых пользователей в беседе."""
    peer_id = message.peer_id
    from_id = message.from_id

    if peer_id < 2000000000:
        await message.answer("❌ /welcome работает только в беседах")
        return

    role = await db.get_user_role(peer_id, from_id)
    if not role or role[1] != 100:
        await message.answer("❌ Только владелец беседы (приоритет 100) может настроить /welcome")
        return

    payload = extract_command_payload(message.text, "welcome") or ""
    payload = payload.strip()
    if not payload:
        await message.answer("📋 Использование: /welcome [текст]\nПример: /welcome Добро пожаловать!")
        return

    await db.set_welcome(peer_id, payload)
    await message.answer("✅ Приветствие сохранено. При заходе новых пользователей буду отправлять в чат.")


# ==================== Команда /sysaddcmd ====================
@bp.on.message(CommandNameRule("sysaddcmd"), IsOwnerRule())
async def sysaddcmd_handler(message: Message) -> None:
    """Выдать пользователю доступ к системной команде бота (не беседной)."""
    payload = extract_command_payload(message.text, "sysaddcmd") or ""
    target_user_id = message.reply_message.from_id if message.reply_message else parse_target_user_id(message, payload)
    if not target_user_id:
        await message.answer("📋 Использование: /sysaddcmd [reply/@user/id] [команда]\nПример: /sysaddcmd 123456789 tickets")
        return

    parts = payload.split()
    cmd = parts[-1].lower().lstrip("/").lstrip("!") if parts else ""
    if not cmd:
        await message.answer("❌ Укажите команду")
        return

    if cmd not in SYSTEM_COMMANDS:
        await message.answer("❌ Можно выдавать доступ только к системным командам: " + ", ".join(sorted(SYSTEM_COMMANDS)))
        return

    await db.grant_system_cmd_access(target_user_id, cmd)
    await message.answer(f"✅ Доступ выдан: [id{target_user_id}|пользователь] -> /{cmd}")


@bp.on.message(CommandNameRule("sysuncmd"), IsOwnerRule())
async def sysuncmd_handler(message: Message) -> None:
    """Забрать у пользователя доступ к системной команде бота."""
    payload = extract_command_payload(message.text, "sysuncmd") or ""
    target_user_id = message.reply_message.from_id if message.reply_message else parse_target_user_id(message, payload)
    if not target_user_id:
        await message.answer("📋 Использование: /sysuncmd [reply/@user/id] [команда]\nПример: /sysuncmd 123456789 tickets")
        return

    parts = payload.split()
    cmd = parts[-1].lower().lstrip("/").lstrip("!") if parts else ""
    if not cmd:
        await message.answer("❌ Укажите команду")
        return

    if cmd not in SYSTEM_COMMANDS:
        await message.answer("❌ Это не системная команда. Доступ можно забирать только у: " + ", ".join(sorted(SYSTEM_COMMANDS)))
        return

    removed = await db.revoke_system_cmd_access(target_user_id, cmd)
    if removed:
        await message.answer(f"✅ Доступ забран: [id{target_user_id}|пользователь] -> /{cmd}")
    else:
        await message.answer(f"ℹ️ У пользователя [id{target_user_id}|пользователь] не было доступа к /{cmd}")


# ==================== Команда /zov ====================
@bp.on.message(MessageRule("/zov"), PriorityRule(100))
async def zov_handler(message: Message) -> None:
    """Пингует всех участников чата"""
    peer_id = message.peer_id
    
    # Проверяем что это беседа
    if peer_id < 2000000000:
        await message.answer("❌ Эта команда работает только в беседах")
        return
    
    zov_text = message.text.replace("/zov", "").strip()
    caller_id = message.from_id
    
    if not zov_text:
        await message.answer("📋 Использование: /zov [текст]\nПример: /zov Срочное собрание!")
        return

    try:
        # Защита от двойной обработки одного и того же сообщения
        cmid = getattr(message, "conversation_message_id", None)
        if isinstance(cmid, int):
            key = (peer_id, cmid)
            now_ts = time.time()
            last_ts = zov_dedupe.get(key)
            if last_ts and now_ts - last_ts < 5:
                return
            zov_dedupe[key] = now_ts

        members = await bp.api.messages.get_conversation_members(peer_id=peer_id)
        if not members.items:
            await message.answer("❌ Не удалось получить участников")
            return

        # Формируем упоминания для всех участников
        mentions = []
        for member in members.items:
            if member.member_id > 0:  # Исключаем бота
                mentions.append(f"[id{member.member_id}|👤]")
        
        # Отправляем сообщение с упоминаниями
        result_text = f"📢 Вызов от [id{caller_id}|пользователя]\n📝 {zov_text}\n\n" + " ".join(mentions)
        
        await message.answer(result_text)
    except Exception as e:
        logger.error(f"Ошибка при /zov: {e}")
        await message.answer("❌ Ошибка при выполнении команды")


# ==================== Команда /addowner ====================
@bp.on.message(MessageRule("/addowner"), PriorityRule(100))
async def addowner_handler(message: Message) -> None:
    """Добавить владельца беседы"""
    peer_id = message.peer_id
    from_id = message.from_id
    
    # Проверяем, является ли отправитель владельцем беседы (приоритет 100)
    sender_role = await db.get_user_role(peer_id, from_id)
    if not sender_role or sender_role[1] != 100:
        await message.answer("❌ Эта команда доступна только владельцу беседы")
        return
    
    user_id = None
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            await message.answer("📋 Использование: /addowner [reply/@user]")
            return
    
    if not user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    # Проверяем, не является ли уже владельцем
    user_role = await db.get_user_role(peer_id, user_id)
    if user_role and user_role[1] == 100:
        await message.answer("ℹ️ Этот пользователь уже является владельцем беседы")
        return
    
    # Выдаём роль 100
    await db.set_user_role(peer_id, user_id, 100)
    
    user_mention = await get_user_mention(peer_id, user_id)
    admin_mention = f"[id{from_id}|Владелец беседы]"
    
    await message.answer(f"✅ {admin_mention} назначил {user_mention} владельцем беседы")


# ==================== Команда /delowner ====================
@bp.on.message(MessageRule("/delowner"), PriorityRule(100))
async def delowner_handler(message: Message) -> None:
    """Убрать владельца беседы"""
    peer_id = message.peer_id
    from_id = message.from_id
    
    # Проверяем, является ли отправитель владельцем беседы (приоритет 100)
    sender_role = await db.get_user_role(peer_id, from_id)
    if not sender_role or sender_role[1] != 100:
        await message.answer("❌ Эта команда доступна только владельцу беседы")
        return
    
    user_id = None
    
    if message.reply_message:
        user_id = message.reply_message.from_id
    else:
        text = message.text
        import re
        mention_match = re.search(r'\[id(\d+)\|', text)
        if mention_match:
            user_id = int(mention_match.group(1))
        else:
            await message.answer("📋 Использование: /delowner [reply/@user]")
            return
    
    if not user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    # Нельзя удалить самого себя
    if user_id == from_id:
        await message.answer("❌ Нельзя удалить самого себя")
        return
    
    # Удаляем роль (выдаём роль 0 - пользователь)
    await db.set_user_role(peer_id, user_id, 0)
    
    user_mention = await get_user_mention(peer_id, user_id)
    admin_mention = f"[id{from_id}|Владелец беседы]"
    
    await message.answer(f"✅ {admin_mention} снял статус владельца беседы с {user_mention}")


# ==================== Команда /setcmd ====================
@bp.on.message(MessageRule("/setcmd"), PriorityRule(100))
async def setcmd_handler(message: Message) -> None:
    """Установить приоритет для команды"""
    peer_id = message.peer_id
    from_id = message.from_id
    
    # Проверяем права (только владелец беседы может менять приоритеты)
    sender_role = await db.get_user_role(peer_id, from_id)
    if not sender_role or sender_role[1] != 100:
        await message.answer("❌ Эта команда доступна только владельцу беседы (приоритет 100)")
        return
    
    text = message.text.replace("/setcmd", "").strip()
    if not text:
        await message.answer("📋 Использование: /setcmd [приоритет] [команда]\nПример: /setcmd 30 ban")
        return
    
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("📋 Использование: /setcmd [приоритет] [команда]\nПример: /setcmd 30 ban")
        return

    try:
        priority = int(parts[0])
    except ValueError:
        await message.answer("❌ Приоритет должен быть числом!")
        return

    if priority < 0 or priority > 100:
        await message.answer("❌ Приоритет должен быть от 0 до 100")
        return
    
    cmd = parts[1].strip().lower().lstrip("/").lstrip("!")
    
    await db.set_cmd_priority(cmd, priority)
    await message.answer(f"✅ Команда /{cmd} теперь требует приоритет: {priority}")


# ==================== Команда /delcmd ====================
@bp.on.message(MessageRule("/delcmd"), PriorityRule(100))
async def delcmd_handler(message: Message) -> None:
    """Сбросить приоритет команды на дефолтный"""
    peer_id = message.peer_id
    from_id = message.from_id
    
    sender_role = await db.get_user_role(peer_id, from_id)
    if not sender_role or sender_role[1] != 100:
        await message.answer("❌ Эта команда доступна только владельцу беседы (приоритет 100)")
        return
    
    text = message.text.replace("/delcmd", "").strip()
    if not text:
        await message.answer("📋 Использование: /delcmd [команда]\nПример: /delcmd ban")
        return

    cmd = text.strip().lower().lstrip("/").lstrip("!")
    
    success = await db.delete_cmd_priority(cmd)
    if success:
        await message.answer(f"✅ Приоритет команды /{cmd} сброшен на дефолтный")
    else:
        await message.answer(f"ℹ️ У команды /{cmd} не было установлено кастомного приоритета")


# ==================== Команда /cmdlist ====================
@bp.on.message(MessageRule("/cmdlist"), PriorityRule(90))
async def cmdlist_handler(message: Message) -> None:
    """Показать список кастомных приоритетов команд"""
    peer_id = message.peer_id
    
    priorities = await db.get_all_cmd_priorities()
    
    if not priorities:
        await message.answer("📋 Список кастомных приоритетов команд пуст.\nИспользуйте /setcmd [приоритет] [команда] для установки.")
        return
    
    text = "📋 Кастомные приоритеты команд:\n\n"
    for cmd, priority in priorities:
        text += f"🔹 /{cmd} — приоритет {priority}\n"
    
    await message.answer(text)


# ==================== Команда /pin ====================
@bp.on.message(MessageRule("/pin"), PriorityRule(75))
async def pin_handler(message: Message) -> None:
    """Закрепить сообщение"""
    peer_id = message.peer_id
    
    if peer_id < 2000000000:
        await message.answer("❌ Команда /pin работает только в беседах")
        return
    
    if not message.reply_message:
        await message.answer("📋 Использование: /pin (reply на сообщение)")
        return
    
    # Для беседы VK ожидает conversation_message_id (а не глобальный message_id)
    cmid = getattr(message.reply_message, "conversation_message_id", None)
    if not isinstance(cmid, int):
        await message.answer("❌ Не удалось определить ID сообщения в беседе. Ответьте на обычное сообщение и повторите /pin")
        return

    try:
        await bp.api.messages.pin(
            peer_id=peer_id,
            conversation_message_id=cmid
        )
        await message.answer("✅ Сообщение закреплено")
    except Exception as e:
        logger.error(f"Ошибка при закреплении: {e}")
        await message.answer("❌ Не удалось закрепить сообщение")


# ==================== Команда /unpin ====================
@bp.on.message(MessageRule("/unpin"), PriorityRule(75))
async def unpin_handler(message: Message) -> None:
    """Открепить сообщение"""
    peer_id = message.peer_id
    
    if not message.reply_message:
        await message.answer("📋 Использование: /unpin (reply на сообщение)")
        return
    
    # Используем conversation_message_id для бесед
    message_id = getattr(message.reply_message, 'conversation_message_id', None)
    if not message_id:
        message_id = message.reply_message.id
    
    # Проверяем права админа
    chat_status = await db.get_chat_status(peer_id)
    if not chat_status:
        await message.answer("❌ У меня нет прав администратора для открепления сообщений")
        return

    try:
        await bp.api.messages.unpin(
            peer_id=peer_id,
            message_id=message_id
        )
        await message.answer("✅ Сообщение откреплено")
    except Exception as e:
        error_str = str(e).lower()
        if "internal server error" in error_str:
            await message.answer("❌ Не удалось открепить сообщение. Возможные причины:\n• Сообщение не было закреплено\n• У бота нет прав на открепление")
        elif "permission" in error_str or "not enough rights" in error_str:
            await message.answer("❌ У меня нет прав на открепление сообщений. Выдайте право \"Закрепление сообщений\" в настройках беседы.")
        else:
            logger.error(f"Ошибка при откреплении: {e}")
            await message.answer("❌ Не удалось открепить сообщение")


# ==================== Хэндлер для команд с ! ====================
class ExclamationRule(rules.ABCRule[Message]):
    """Правило для обработки команд с !"""
    
    def __init__(self, command: str):
        self.command = command.lower()
    
    async def check(self, message: Message) -> bool:
        text = message.text.lower().strip()
        return text.startswith("!" + self.command) or text.startswith(self.command + " ")


@bp.on.message(ExclamationRule("ban"), PriorityRule(40))
async def exclaim_ban_handler(message: Message) -> None:
    """Обработка !ban"""
    await ban_handler(message)


@bp.on.message(ExclamationRule("warn"), PriorityRule(10))
async def exclaim_warn_handler(message: Message) -> None:
    """Обработка !warn"""
    await warn_handler(message)


@bp.on.message(ExclamationRule("unban"), PriorityRule(40))
async def exclaim_unban_handler(message: Message) -> None:
    """Обработка !unban"""
    await unban_handler(message)


@bp.on.message(ExclamationRule("unwarn"), PriorityRule(30))
async def exclaim_unwarn_handler(message: Message) -> None:
    """Обработка !unwarn"""
    await unwarn_handler(message)


# ==================== Команда /sysnewrole ====================
@bp.on.message(MessageRule("/sysnewrole"), IsOwnerRule())
async def sysnewrole_handler(message: Message) -> None:
    """Создать роль в указанной беседе"""
    text = message.text.replace("/sysnewrole", "").strip()
    
    if not text:
        await message.answer("📋 Использование: /sysnewrole [peer_id] [приоритет] [название] [причина]\nПример: /sysnewrole 2000000001 55 НоваяРоль")
        return
    
    import re
    
    # Парсим peer_id (первый аргумент - число)
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("📋 Использование: /sysnewrole [peer_id] [приоритет] [название] [причина]\nПример: /sysnewrole 2000000001 55 НоваяРоль")
        return
        

@bp.on.message(CommandNameRule("giveruletka"))
async def giveruletka_handler(message: Message) -> None:
    """Выдать рулетку (добавить прокрутки пользователю)."""
    if not await has_system_command_access(message.from_id, "giveruletka"):
        await message.answer("❌ Нет доступа")
        return
    payload = extract_command_payload(message.text, "giveruletka") or ""
    if not payload and not message.reply_message:
        await message.answer("📋 Использование: /giveruletka [reply/@user/id] [кол-во]\nПример: /giveruletka 123456789 3")
        return

    target_user_id = message.reply_message.from_id if message.reply_message else parse_target_user_id(message, payload)
    if not target_user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    counts = re.findall(r"\b(\d+)\b", payload)
    amount = int(counts[-1]) if counts else 1
    amount = max(1, min(amount, 100))

    spins_left = await db.add_roulette_spins(int(target_user_id), amount)
    await message.answer(f"✅ Вы выдали рулетку [id{target_user_id}|пользователю]: +{amount}\n🎰 Теперь рулеток: {spins_left}")


@bp.on.message(CommandNameRule("mybusiness"))
async def mybusiness_handler(message: Message) -> None:
    if not await ensure_games_enabled_for_message(message):
        return
    user_id = message.from_id
    payload = (extract_command_payload(message.text, "mybusiness") or "").strip().lower()
    
    earned, hours = await db.process_my_business(user_id)
    notice = ""
    if earned > 0:
        notice = f"✅ Начислено прибыли за {hours}ч: +{earned}$\n\n"

    if payload.startswith("buyraw"):
        parts = payload.split()
        packs = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        ok, msg = await db.my_business_buy_raw(user_id, packs)
        await message.answer(("✅ " if ok else "❌ ") + msg)
        return
    if payload.startswith("hire"):
        parts = payload.split()
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        ok, msg = await db.my_business_hire(user_id, count)
        await message.answer(("✅ " if ok else "❌ ") + msg)
        return
    if payload.startswith("ads"):
        parts = payload.split()
        levels = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        ok, msg = await db.my_business_advertise(user_id, levels)
        await message.answer(("✅ " if ok else "❌ ") + msg)
        return
    if payload.startswith("paytax"):
        ok, msg = await db.my_business_pay_tax(user_id)
        await message.answer(("✅ " if ok else "❌ ") + msg)
        return
    if payload.startswith("withdraw"):
        parts = payload.split()
        amount = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        ok, msg = await db.my_business_withdraw(user_id, amount)
        await message.answer(("✅ " if ok else "❌ ") + msg)
        return

    text, keyboard_json = await render_mybusiness_text(user_id, notice=notice.strip())
    await message.answer(text, keyboard=keyboard_json)


# ==================== Хэндлер добавления бота в беседу ====================
@bp.on.raw_event(GroupEventType.MESSAGE_NEW)
async def chat_invite_handler(event: dict) -> None:
    """
    Обработчик новых сообщений.
    Проверяем событие добавления бота в чат.
    """
    # Получаем объект message из события
    obj = event.get("object", {})
    message_data = obj.get("message", {})
    
    if not message_data:
        return

    # Проверяем, что это событие добавления в беседу
    action = message_data.get("action")
    if not action:
        return

    action_type = action.get("type")
    peer_id = message_data.get("peer_id")
    
    # Получаем ID группы
    group_id = CtxStorage().get("group_id")
    
    if action_type == "chat_invite_user":
        member_id = action.get("member_id")
        inviter_id = message_data.get("from_id")
        
        if member_id == -group_id:
            await db.add_chat(peer_id)
            await db.set_admin_status(peer_id, False)
            
            keyboard = (
                Keyboard(inline=True)
                .row()
                .add(
                    Callback("Я выдал!", payload={"action": "grant_admin"}),
                    color=KeyboardButtonColor.POSITIVE
                )
            )
                
            # Отправляем сообщение
            await bp.api.messages.send(
                peer_id=peer_id,
                message="👋 Привет! Чтобы начать работу, выдайте мне права администратора",
                keyboard=keyboard.get_json(),
                random_id=0
            )
            return

        # Ограничение на добавление ботов/сообществ
        if member_id and member_id < 0:
            allow_community_add = await db.get_allow_community_add(peer_id)
            if not allow_community_add:
                chat_id = peer_id - 2000000000
                try:
                    await bp.api.messages.remove_chat_user(chat_id=chat_id, member_id=member_id)
                except Exception as e:
                    logger.error(f"Ошибка при кике добавленного сообщества {member_id}: {e}")

                if inviter_id and inviter_id > 0:
                    try:
                        await bp.api.messages.remove_chat_user(chat_id=chat_id, member_id=inviter_id)
                    except Exception as e:
                        logger.error(f"Ошибка при кике пользователя {inviter_id}, добавившего сообщество: {e}")

                await bp.api.messages.send(
                    peer_id=peer_id,
                    message=(
                        "⛔ Добавление ботов в этой беседе запрещено владельцем.\n"
                        f"Удалён бот [club{abs(int(member_id))}|]"
                        + (f" и пользователь [id{inviter_id}|добавивший]." if inviter_id and inviter_id > 0 else ".")
                    ),
                    random_id=0
                )
                return
            
        # Если добавили обычного пользователя - проверяем, не забанен ли он
        if member_id and member_id > 0:
            # Проверяем, забанен ли пользователь в этом чате
            if await db.is_banned(peer_id, member_id):
                try:
                    await bp.api.messages.remove_chat_user(
                        chat_id=peer_id - 2000000000,
                        member_id=member_id
                    )
                    await bp.api.messages.send(
                        peer_id=peer_id,
                        message=f"🚫 Пользователь [id{member_id}|] был кикнут — он в бане",
                        random_id=0
                    )
                except Exception as e:
                    logger.error(f"Ошибка при кике: {e}")
            else:
                # Отправляем welcome, если настроено владельцем беседы
                try:
                    welcome_text = await db.get_welcome(peer_id)
                    if welcome_text:
                        await bp.api.messages.send(
                            peer_id=peer_id,
                            message=f"👋 [id{member_id}|пользователь] {welcome_text}",
                            random_id=0
                        )
                except Exception as e:
                    logger.error(f"Ошибка при отправке welcome: {e}")

    elif action_type == "chat_kick_user":
        member_id = action.get("member_id")
        actor_id = message_data.get("from_id")

        # Пользователь вышел сам (actor == member) и включён авто-кик при выходе
        if member_id and member_id > 0 and actor_id == member_id:
            auto_kick_on_leave = await db.get_auto_kick_on_leave(peer_id)
            if auto_kick_on_leave:
                await db.add_ban(peer_id, member_id)
                await bp.api.messages.send(
                    peer_id=peer_id,
                    message=f"🚪 [id{member_id}|Пользователь] вышел из беседы и автоматически добавлен в бан-лист чата.",
                    random_id=0
                )
    

# ==================== Хэндлер callback-кнопок ====================
@bp.on.raw_event(GroupEventType.MESSAGE_EVENT)
async def message_event_handler(event: dict) -> None:
    """
    Обработчик callback-событий (нажатие inline-кнопок).
    """
    # Извлекаем данные из события
    obj = event.get("object", {})
    peer_id = obj.get("peer_id")
    user_id = obj.get("user_id")
    event_id = obj.get("event_id")
    payload = obj.get("payload", {})
    event_cmid = obj.get("conversation_message_id")
    
    if not peer_id or not user_id:
        return
    
    action = payload.get("action")

    # Настройки беседы (доступ с приоритета 90)
    if action in {"settings_toggle_games", "settings_toggle_community_add", "settings_toggle_auto_kick_leave"}:
        if peer_id < 2000000000:
            return
    
        role = await db.get_user_role(peer_id, user_id)
        if not role or role[1] < 90:
            try:
                await bp.api.messages.send_message_event_answer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data={"type": "show_snackbar", "text": "❌ Требуется приоритет 90"}
                )
            except Exception:
                pass
            return
    
        allow_games, allow_community_add, auto_kick_on_leave = await db.get_chat_settings(peer_id)

        if action == "settings_toggle_games":
            allow_games = not allow_games
            await db.set_allow_games(peer_id, allow_games)
            snackbar = "✅ Игровые команды включены" if allow_games else "⛔ Игровые команды отключены"
        elif action == "settings_toggle_community_add":
            allow_community_add = not allow_community_add
            await db.set_allow_community_add(peer_id, allow_community_add)
            snackbar = "✅ Добавление ботов разрешено" if allow_community_add else "⛔ Добавление ботов запрещено"
        else:
            auto_kick_on_leave = not auto_kick_on_leave
            await db.set_auto_kick_on_leave(peer_id, auto_kick_on_leave)
            snackbar = "✅ Авто-кик при выходе включён" if auto_kick_on_leave else "⛔ Авто-кик при выходе выключен"

        # Повторно читаем фактические настройки и обновляем клавиатуру
        allow_games, allow_community_add, auto_kick_on_leave = await db.get_chat_settings(peer_id)
        keyboard_json = build_settings_keyboard(allow_games, allow_community_add, auto_kick_on_leave)
        settings_text = "⚙️ Настройки беседы"

        if event_cmid:
            try:
                await bp.api.messages.edit(
                    peer_id=peer_id,
                    conversation_message_id=event_cmid,
                    message=settings_text,
                    keyboard=keyboard_json
                )
            except Exception:
                await bp.api.messages.send(
                    peer_id=peer_id,
                    message=settings_text,
                    keyboard=keyboard_json,
                    random_id=0
                )
        else:
            await bp.api.messages.send(
                peer_id=peer_id,
                message=settings_text,
                keyboard=keyboard_json,
                random_id=0
            )

        try:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": snackbar}
            )
        except Exception:
            pass
        return

    async def edit_or_send(text: str, keyboard: Optional[str] = None) -> None:
        if event_cmid:
            try:
                await bp.api.messages.edit(
                    peer_id=peer_id,
                    conversation_message_id=event_cmid,
                    message=text,
                    keyboard=keyboard
                )
                return
            except Exception:
                pass
        await bp.api.messages.send(
            peer_id=peer_id,
            message=text,
            keyboard=keyboard,
            random_id=0
        )

    # Блокировка игровых callback-действий, если игры выключены в беседе
    game_actions = {
        "job_refresh", "business_buy", "business_view",
        "mybusiness_view", "mybusiness_buyraw", "mybusiness_hire", "mybusiness_ads", "mybusiness_paytax", "mybusiness_withdraw_1000", "mybusiness_withdraw_all",
        "marriage_accept", "marriage_reject", "open_profile"
    }
    if action in game_actions and peer_id >= 2000000000:
        if not await db.get_allow_games(peer_id):
            try:
                await bp.api.messages.send_message_event_answer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data={"type": "show_snackbar", "text": "⛔ Игровые команды отключены владельцем"}
                )
            except Exception:
                pass
            return
    
    # Обработка подтверждения админских прав
    if action == "grant_admin":
        try:
            # Сначала добавляем чат в БД
            await db.add_chat(peer_id)
            
            members = await bp.api.messages.get_conversation_members(peer_id=peer_id)
            
            has_admin_rights = False
            owner_id = None
            for member in members.items:
                if member.member_id == user_id:
                    has_admin_rights = member.is_admin
                # Ищем создателя беседы
                if hasattr(member, 'is_owner') and member.is_owner:
                    owner_id = member.member_id
            
            # Если не нашли owner через members, пробуем получить через conversations
            if owner_id is None:
                try:
                    conv = await bp.api.messages.get_conversations_by_id(peer_ids=[peer_id])
                    if conv.items and hasattr(conv.items[0], 'chat_settings'):
                        owner_id = conv.items[0].chat_settings.get('owner_id')
                except Exception:
                    pass
                
            if has_admin_rights:
                await db.set_admin_status(peer_id, True)
                
                # Выдаём роль 100 создателю беседы если она ещё не выдана
                if owner_id:
                    user_role = await db.get_user_role(peer_id, owner_id)
                    if not user_role:
                        await db.set_user_role(peer_id, owner_id, 100)
                
                await bp.api.messages.send_message_event_answer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data={"type": "show_snackbar", "text": "✅ Я запустился! Команды доступны."}
                )
            else:
                await bp.api.messages.send_message_event_answer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data={"type": "show_snackbar", "text": "❌ Ты не выдал админку :("}
                )
        
        except Exception as e:
            error_msg = str(e)
            if "You don't have access to this chat" in error_msg:
                await bp.api.messages.send_message_event_answer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data={"type": "show_snackbar", "text": "Я не могу проверить — дайте права админа!"}
                )
            else:
                logger.error(f"Ошибка при проверке прав: {e}")
                try:
                    await bp.api.messages.send_message_event_answer(
                        event_id=event_id,
                        user_id=user_id,
                        peer_id=peer_id,
                        event_data={"type": "show_snackbar", "text": "Ошибка :("}
                    )
                except Exception:
                    pass
        return
    
    # Быстрые действия экономики/бизнеса
    if action == "job_refresh":
        ok, earned, cooldown_left = await db.do_job(user_id)
        if not ok:
            minutes = cooldown_left // 60
            seconds = cooldown_left % 60
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": f"Откат: {minutes}м {seconds}с"}
            )
            return
    
        balance = await db.get_balance(user_id)
        await edit_or_send(f"💼 Вы заработали: +{earned}$\n💰 Текущий баланс: {balance}$")
        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": f"+{earned}$, баланс {balance}$"}
        )
        return
    
    if action == "open_profile":
        text = await render_profile_text(user_id)
        await edit_or_send(text)
        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": "Профиль открыт"}
        )
        return
    
    if action == "business_buy":
        # Получаем уровень из payload кнопки
        target_level = payload.get("level")
        level, _, _ = await db.get_business(user_id)
        
        # Если уровень не указан в payload - используем логику покупки следующего
        if target_level is None:
            if level == 0:
                target_level = 1
            else:
                target_level = level + 1
        
        target_level = int(target_level)
        
        # Проверяем валидность
        if target_level < 1 or target_level >= len(BUSINESS_TYPES):
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Неверный номер бизнеса"}
            )
            return

        # Проверяем, не куплен ли уже этот бизнес
        if level >= target_level:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Уже куплен"}
            )
            return

        # Проверяем последовательность покупки
        if target_level > level + 1:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Сначала купите предыдущий"}
            )
            return

        cost = BUSINESS_TYPES[target_level][1]
        business_name = BUSINESS_TYPES[target_level][0]
        ok, reason, _ = await db.upgrade_business(user_id, cost)
        
        if ok:
            text, keyboard_json = await render_business_text(user_id)
            await edit_or_send(text, keyboard_json)
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": f"Куплено: {business_name}!"}
            )
        else:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": reason}
            )
        return
    
    if action == "business_view":
        text, keyboard_json = await render_business_text(user_id)
        await edit_or_send(text, keyboard_json)
        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": "Бизнес открыт"}
        )
        return
    
    if action in {"mybusiness_view", "mybusiness_buyraw", "mybusiness_hire", "mybusiness_ads", "mybusiness_paytax", "mybusiness_withdraw_1000", "mybusiness_withdraw_all"}:
        owner_id = payload.get("owner_id")
        if owner_id is None or int(owner_id) != int(user_id):
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "❌ Это не ваш бизнес"}
            )
            return
    
        earned, hours = await db.process_my_business(user_id)
        if action == "mybusiness_buyraw":
            ok, msg = await db.my_business_buy_raw(user_id, 1)
        elif action == "mybusiness_hire":
            ok, msg = await db.my_business_hire(user_id, 1)
        elif action == "mybusiness_ads":
            ok, msg = await db.my_business_advertise(user_id, 1)
        elif action == "mybusiness_paytax":
            ok, msg = await db.my_business_pay_tax(user_id)
        elif action == "mybusiness_withdraw_1000":
            ok, msg = await db.my_business_withdraw(user_id, 1000)
        elif action == "mybusiness_withdraw_all":
            # Выводим всю кассу
            raw_material, workers, ad_level, cashbox, tax_debt = await db.get_my_business(user_id)
            if tax_debt > 0:
                ok, msg = False, "Сначала оплатите налоги"
            elif cashbox <= 0:
                ok, msg = False, "Касса пустая"
            else:
                ok, msg = await db.my_business_withdraw(user_id, int(cashbox))
        else:
            ok, msg = True, "Обновлено"

        notice = f"Начислено за {hours}ч: +{earned}$" if earned > 0 else ""
        text, keyboard_json = await render_mybusiness_text(user_id, notice=notice)
        await edit_or_send(text, keyboard_json)
        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": msg if ok else f"❌ {msg}"}
        )
        return

    if action in {"marriage_accept", "marriage_reject"}:
        proposal_id = payload.get("proposal_id")
        if not proposal_id:
            return
        proposal = await db.get_marriage_proposal(int(proposal_id))
        if not proposal:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Предложение не найдено"}
            )
            return

        p_id, from_user_id, to_user_id, proposal_peer_id, status = proposal
        if status != "pending":
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Предложение уже обработано"}
            )
            return

        # Только получатель может принять/отклонить
        if user_id != to_user_id:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Только получатель может принять/отклонить"}
            )
            return

        if action == "marriage_reject":
            await db.close_marriage_proposal(p_id, "rejected")
            await bp.api.messages.send(
                peer_id=proposal_peer_id,
                message=f"💔 Предложение брака от [id{from_user_id}|пользователя] для [id{to_user_id}|пользователя] отклонено.",
                random_id=0
            )
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Отклонено"}
            )
            return

        ok, reason = await db.create_marriage(from_user_id, to_user_id)
        if not ok:
            await db.close_marriage_proposal(p_id, "cancelled")
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": reason}
            )
            return

        await db.close_marriage_proposal(p_id, "accepted")
        await bp.api.messages.send(
            peer_id=proposal_peer_id,
            message=f"💍 Брак зарегистрирован: [id{from_user_id}|пользователь] + [id{to_user_id}|пользователь]",
            random_id=0
        )
        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": "Брак подтвержден"}
        )
        return

    if action in {"report_take", "report_close", "report_refresh"}:
        report_id = str(payload.get("report_id", "")).lstrip("#").upper()
        if not report_id:
            return

        if not await is_staff_user(user_id):
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Нет доступа"}
            )
            return

        report_data = await db.get_report_by_id(report_id)
        if not report_data:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Репорт не найден"}
            )
            return

        _, rep_id, report_user_id, report_chat_id, report_text, status, answered_by, answer_text, created_at, answered_at = report_data

        if action == "report_take":
            success = await db.take_report(rep_id, user_id)
            text = "Репорт взят" if success else "Не удалось взять (возможно уже взят)"
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": text}
            )
            return

        if action == "report_close":
            success, close_msg = await db.close_report(rep_id, user_id)
            if success:
                try:
                    await bp.api.messages.send(
                        peer_id=report_chat_id,
                        message=f"Ваш тикет {rep_id} был закрыт агентом поддержки.",
                        random_id=0
                    )
                except Exception:
                    pass
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Репорт закрыт" if success else close_msg}
            )
            return

        status_emoji = {"pending": "⏳", "in_progress": "🔄", "answered": "✅", "closed": "🔒"}
        report_chat_title = await get_chat_title(report_chat_id)
        await bp.api.messages.send(
            peer_id=peer_id,
            message=(
                f"{status_emoji.get(status, '❓')} Тикет {rep_id}\n"
                f"👤 [id{report_user_id}|Пользователь]\n"
                f"📍 Чат: {report_chat_title} ({report_chat_id})\n"
                f"📄 {(report_text or '')[:250]}"
            ),
            keyboard=build_report_keyboard(rep_id),
            random_id=0
        )
        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": f"Статус: {status}"}
        )
        return

    # Обработка снятия варна
    if action == "unwarn":
        target_user_id = payload.get("user_id")
        count = payload.get("count", 1)

        if target_user_id:
            target_user_id = int(target_user_id)
            
            # Проверяем, есть ли варны
            warnings_count = await db.get_warnings_count(peer_id, target_user_id)
            if warnings_count == 0:
                await bp.api.messages.send_message_event_answer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data={"type": "show_snackbar", "text": "У пользователя нет предупреждений"}
                )
                return
            
            # Снимаем указанное количество варнов
            await db.remove_warnings_count(peer_id, target_user_id, count)

            user_mention = f"[id{target_user_id}|Пользователь]"
            admin_mention = f"[id{user_id}|Администратор]"
            
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": f"Снято {count} варн(а)!"}
            )
            
            # Удаляем сообщение с клавиатурой
            try:
                obj_msg = event.get("object", {})
                msg_id = obj_msg.get("message_id")
                if msg_id:
                    await bp.api.messages.delete(
                        message_ids=[msg_id],
                        peer_id=peer_id,
                        delete_for_all=True
                    )
            except Exception:
                pass
            
            await bp.api.messages.send(
                peer_id=peer_id,
                message=f"✅ {admin_mention} снял {count} предупреждение(й) с {user_mention}",
                random_id=0
            )
        return

    # Обработка разбана
    if action == "unban":
        target_user_id = payload.get("user_id")
        if target_user_id:
            target_user_id = int(target_user_id)

            if not await db.is_banned(peer_id, target_user_id):
                user_mention = f"[id{target_user_id}|Пользователь]"
                await bp.api.messages.send_message_event_answer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data={"type": "show_snackbar", "text": f"Пользователь {user_mention} не забанен"}
                )
                return
            
            await db.remove_ban(peer_id, target_user_id)
            
            user_mention = f"[id{target_user_id}|Пользователь]"
            admin_mention = f"[id{user_id}|Администратор]"
            
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Пользователь разбанен!"}
            )
            
            await bp.api.messages.send(
                peer_id=peer_id,
                message=f"✅ {admin_mention} разбанил пользователя {user_mention}",
                random_id=0
            )
        return
    
    # Обработка отключения уведомлений
    if action == "disable_notify":
        await db.set_notify(peer_id, False)

        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": "🔕 Уведомления отключены!"}
        )
        
        await bp.api.messages.send(
            peer_id=peer_id,
            message="🔕 Вы отключили уведомления от бота. Используйте /notify для включения.",
            random_id=0
        )
        return
    
    # Обработка пагинации /groupall
    if action == "groupall_page":
        page = payload.get("page", 1)
        import math

        all_chats = await db.get_all_chats()
        if not all_chats:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Нет бесед"}
            )
            return
    
        per_page = 5
        total_pages = math.ceil(len(all_chats) / per_page)

        if page < 1:
            page = 1
        if page > total_pages:
            page = total_pages
        
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        chats_page = all_chats[start_idx:end_idx]
        
        result_text = f"📋 Список бесед с ботом ({len(all_chats)} всего, стр. {page}/{total_pages}):\n\n"
        
        for chat_id in chats_page:
            try:
                conv = await bp.api.messages.get_conversations_by_id(peer_ids=[chat_id])
                if conv.items:
                    chat_title = conv.items[0].chat_settings.title if hasattr(conv.items[0], 'chat_settings') else "Беседа"
                else:
                    chat_title = "Беседа"
                
                members = await bp.api.messages.get_conversation_members(peer_id=chat_id)
                member_count = len(members.items) if members.items else 0
                
                invite_link = "нет"
                try:
                    link_info = await bp.api.messages.get_invite_link(peer_id=chat_id, reset=0)
                    if link_info:
                        invite_link = link_info.link
                except Exception:
                    pass
                
                result_text += f"📌 {chat_title}\n"
                result_text += f"   🔗 Ссылка: {invite_link}\n"
                result_text += f"   🆔 Peer ID: {chat_id}\n"
                result_text += f"   👥 Участников: {member_count}\n\n"
            except Exception as e:
                logger.error(f"Ошибка при получении инфы о чате {chat_id}: {e}")
                result_text += f"📌 Беседа {chat_id}\n   🆔 Peer ID: {chat_id}\n   ⚠️ Ошибка получения данных\n\n"
        
        keyboard = Keyboard(inline=True)
        if total_pages > 1:
            if page > 1:
                keyboard.add(Callback("◀ Назад", payload={"action": "groupall_page", "page": page - 1}))
            if page < total_pages:
                keyboard.add(Callback("Вперёд ▶", payload={"action": "groupall_page", "page": page + 1}))
        
        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": f"Страница {page}"}
        )
        
        keyboard_json = keyboard.get_json()
        await bp.api.messages.send(
            peer_id=peer_id,
            message=result_text,
            keyboard=keyboard_json if total_pages > 1 else None,
            random_id=0
        )
        return
    
    # Обработка кнопок /admins (Имена/Ники)
    if action in {"admins_view", "admins_refresh"}:
        view_type = payload.get("view", "nicknames")
        if action == "admins_refresh":
            view_type = "nicknames"  # сброс на ники при обновлении

        peer_id = obj.get("peer_id")
        if not peer_id:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Ошибка: не найден чат"}
            )
            return
    
        # Проверяем доступ (приоритет 75)
        user_role = await db.get_user_role(peer_id, user_id)
        user_priority = user_role[1] if user_role else 0
        if user_priority < 0:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Нет доступа "}
            )
            return
    
        admins = await db.get_admins(peer_id)
        if not admins:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Нет админов"}
            )
            return

        # Получаем данные о пользователях
        user_ids = [user_id for user_id, _, _ in admins]
        try:
            users_info = await bp.api.users.get(user_ids=user_ids)
            users_map = {u.id: f"{u.first_name} {u.last_name}" for u in users_info}
        except Exception:
            users_map = {uid: "Пользователь" for uid in user_ids}

        # Формируем текст
        current_priority = None
        admins_text = f"👥 Администраторы беседы ({'Ники' if view_type == 'nicknames' else 'Имена'}):\n\n"

        for admin_user_id, role_name, priority in admins:
            if priority != current_priority:
                admins_text += f"\n🏆 {role_name} (приоритет: {priority}):\n"
                current_priority = priority

            if view_type == "names":
                vk_name = users_map.get(admin_user_id, "Пользователь")
                display_name = sanitize_plain_name(vk_name)
                admins_text += f"▪️ [vk.com/id{admin_user_id}|{display_name}]\n"
            else:
                custom_nick = await db.get_nickname(peer_id, admin_user_id)
                display_name = sanitize_plain_name(custom_nick) if custom_nick else sanitize_plain_name(users_map.get(admin_user_id, "Пользователь"))
                admins_text += f"▪️ [vk.com/id{admin_user_id}|{display_name}]\n"

        # Клавиатура
        keyboard = Keyboard(inline=True).row()
        keyboard.add(
            Callback("👤 Имена", payload={"action": "admins_view", "view": "names"}),
            color=KeyboardButtonColor.SECONDARY if view_type == "nicknames" else KeyboardButtonColor.PRIMARY
        )
        keyboard.add(
            Callback("🔖 Ники", payload={"action": "admins_view", "view": "nicknames"}),
            color=KeyboardButtonColor.PRIMARY if view_type == "nicknames" else KeyboardButtonColor.SECONDARY
        )
        keyboard.row()
        keyboard.add(Callback("🔄 Обновить", payload={"action": "admins_refresh"}))

        keyboard_json = keyboard.get_json()

        # Редактируем сообщение
        try:
            await bp.api.messages.edit(
                peer_id=peer_id,
                conversation_message_id=event_cmid,
                message=admins_text,
                keyboard=keyboard_json
            )
        except Exception:
            await bp.api.messages.send(
                peer_id=peer_id,
                message=admins_text,
                keyboard=keyboard_json,
                random_id=0
            )

        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": f"Показываем: {'Ники' if view_type == 'nicknames' else 'Имена'}"}
        )
        return
    

# ==================== Обработчик команд без доступа ====================
class CommandCheckRule(rules.ABCRule[Message]):
    """Проверяет есть ли доступ к команде и отвечает если нет"""
    
    async def check(self, message: Message) -> bool:
        text = normalize_command_text(message.text).lower()
        if not text:
            return True
        
        # Проверяем только команды
        if not text.startswith('/') and not text.startswith('!'):
            return True  # Не команда - пропускаем
        
        # Получаем имя команды
        if text.startswith('/'):
            cmd = text.split()[0][1:]  # убираем /
        elif text.startswith('!'):
            cmd = text.split()[0][1:]  # убираем !
        else:
            return True

        # Системные команды бота (работают без чат-ролей) - пропускаем сразу
        support_cmds = {
            "report", "tickets", "ans", "answer", "vreport", "creport", "reports",
            "banreport", "unbanreport",
            "stats", "q"
        }
        if cmd in support_cmds:
            return True
        
        # Проверяем заблокированность в боте
        peer_id = message.peer_id
        user_id = message.from_id
        if await db.is_sysbanned(user_id):
            await message.answer("❌ Вы заблокированы в боте")
            return False
        
        # Для бесед: сначала проверяем мут и режим тишины
        if peer_id >= 2000000000:
            if await db.is_muted(peer_id, user_id):
                cmid = getattr(message, "conversation_message_id", None)
                if isinstance(cmid, int):
                    await delete_by_cmid(peer_id, cmid)
                return False

            if await is_silent_blocked_message(message):
                return False

        # Команда /settings доступна с приоритета 90
        if cmd == "settings":
            if peer_id < 2000000000:
                return True
            role = await db.get_user_role(peer_id, user_id)
            if not role or role[1] < 90:
                await message.answer("❌ Эта команда доступна с приоритета 90")
                return False
            return True
        
        # Личные сообщения - пропускаем
        if peer_id < 2000000000:
            return True

        # Проверяем владельца бота
        if user_id in BOT_OWNER_IDS:
            return True

        # Проверяем статус админа
        status = await db.get_chat_status(peer_id)
        has_admin = bool(status) if status is not None else False
        if not has_admin:
            return True  # Проверит другой обработчик
        
        # Получаем роль пользователя
        user_role = await db.get_user_role(peer_id, user_id)
        if not user_role:
            await message.answer("❌ У вас нет роли в этом чате")
            return False

        user_priority = user_role[1]
        
        # Проверяем приоритет из БД
        cmd_priority = await db.get_cmd_priority(cmd)
        
        # Определяем минимальный приоритет для команды
        min_priority = cmd_priority if cmd_priority is not None else 0
        
        # Проверяем команды с дефолтными приоритетами
        default_cmds = {
            'stats': 10, 'warn': 10, 'warnhistory': 10,
            'ban': 30, 'unban': 30, 'unwarn': 30, 'snick': 30, 'rnick': 30, 'nlist': 30, 'banlist': 30,
            'roles': 40, 'role': 40,
            'newrole': 75, 'delrole': 75, 'admins': 75,
            'addowner': 100, 'delowner': 100, 'zov': 100,
            'silent': 30, 'ban': 40, 'unban': 40, 'pin': 75, 'unpin': 75,
            'setcmd': 100, 'settings': 90, 'cmdlist': 90
        }
        
        if cmd in default_cmds:
            min_priority = default_cmds[cmd]
        
        if user_priority < min_priority:
            await message.answer(f"❌ Требуется приоритет {min_priority}")
            return False
        
        return True

        # Проверяем только команды
        if not text.startswith('/') and not text.startswith('!'):
            return True  # Не команда - пропускаем
        
        # Получаем имя команды
        if text.startswith('/'):
            cmd = text.split()[0][1:]  # убираем /
        elif text.startswith('!'):
            cmd = text.split()[0][1:]  # убираем !
        else:
            return True

        # Системные команды бота (работают без чат-ролей) - проверяем СРАЗУ
        support_cmds = {
            "report", "tickets", "ans", "answer", "vreport", "creport", "reports",
            "banreport", "unbanreport", "stats", "q"
        }
        if cmd in support_cmds:
            return True
        
        # Для остальных команд проверяем sysban
        peer_id = message.peer_id
        user_id = message.from_id
        if await db.is_sysbanned(user_id):
            await message.answer("❌ Вы заблокированы в боте")
            return False
        
        # Для бесед: проверяем мут и режим тишины
        if peer_id >= 2000000000:
            if await db.is_muted(peer_id, user_id):
                cmid = getattr(message, "conversation_message_id", None)
                if isinstance(cmid, int):
                    await delete_by_cmid(peer_id, cmid)
                return False

            if await is_silent_blocked_message(message):
                return False
        
        # Команда /settings доступна с приоритета 90
        if cmd == "settings":
            if peer_id < 2000000000:
                return True
            role = await db.get_user_role(peer_id, user_id)
            if not role or role[1] < 90:
                await message.answer("❌ Эта команда доступна с приоритета 90")
                return False
            return True
        
        # Личные сообщения - пропускаем
        if peer_id < 2000000000:
            return True

        # Проверяем владельца бота
        if user_id in BOT_OWNER_IDS:
            return True

        # Проверяем статус админа
        status = await db.get_chat_status(peer_id)
        has_admin = bool(status) if status is not None else False
        if not has_admin:
            return True  # Проверит другой обработчик
        
        # Получаем роль пользователя
        user_role = await db.get_user_role(peer_id, user_id)
        if not user_role:
            await message.answer("❌ У вас нет роли в этом чате")
            return False

        user_priority = user_role[1]
        
        # Проверяем приоритет из БД
        cmd_priority = await db.get_cmd_priority(cmd)
        
        # Определяем минимальный приоритет для команды
        min_priority = cmd_priority if cmd_priority is not None else 0
        
        # Проверяем команды с дефолтными приоритетами
        default_cmds = {
            'stats': 10, 'warn': 10, 'warnhistory': 10,
            'ban': 30, 'unban': 30, 'unwarn': 30, 'snick': 30, 'rnick': 30, 'nlist': 30, 'banlist': 30,
            'roles': 40, 'role': 40,
            'newrole': 75, 'delrole': 75, 'admins': 75,
            'addowner': 100, 'delowner': 100, 'zov': 100,
            'silent': 30, 'ban': 40, 'unban': 40, 'pin': 75, 'unpin': 75,
            'setcmd': 100, 'settings': 90, 'cmdlist': 90
        }
        
        if cmd in default_cmds:
            min_priority = default_cmds[cmd]
        
        if user_priority < min_priority:
            await message.answer(f"❌ Нет доступа. Требуется приоритет: {min_priority}")
            return False
        
        return True


# Удалён пустой message-handler c CommandCheckRule: он перехватывал команды
# (включая /report) и мешал исполнению профильных хендлеров.


class NonCommandRule(rules.ABCRule[Message]):
    """Пропускает только некомандные сообщения."""

    async def check(self, message: Message) -> bool:
        text = normalize_command_text(message.text).lower()
        if not text:
            return True
        return not (text.startswith("/") or text.startswith("!"))


# ==================== Обработчик сообщений без прав ====================
@bp.on.message(NonCommandRule())
async def no_access_handler(message: Message) -> None:
    """Обработчик для чатов без прав админа"""
    peer_id = message.peer_id
    
    # Если это личные сообщения - пропускаем
    if peer_id < 2000000000:
        return
    
    # Проверяем статус админа
    status = await db.get_chat_status(peer_id)
    has_admin = bool(status) if status is not None else False
    
    if not has_admin:
        await message.answer("⚠️ Выдайте права администратора для использования бота.")
        return

    user_id = message.from_id
    
    # Проверяем, не замучен ли пользователь
    if await db.is_muted(peer_id, user_id):
        cmid = getattr(message, 'conversation_message_id', None)
        if isinstance(cmid, int):
            await delete_by_cmid(peer_id, cmid)
        return
    
    # Режим тишины: пользователи с приоритетом 0..10 писать не могут
    if await is_silent_blocked_message(message):
        return


# ==================== Таблица для банов репортов ====================
# Добавим методы для работы с банами репортов в класс Database
async def add_report_ban(self, user_id: int, duration_minutes: int = -1, reason: str = "") -> None:
    """Забанить пользователя в репортах (duration_minutes: -1 = навсегда)."""
    duration = int(duration_minutes)
    banned_until = None
    if duration >= 0:
        banned_until = (datetime.now() + timedelta(minutes=duration)).isoformat()

    async with aiosqlite.connect(self.db_name) as db:
        await db.execute(
            """INSERT INTO report_bans (user_id, reason, duration_minutes, banned_until)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   reason = excluded.reason,
                   duration_minutes = excluded.duration_minutes,
                   banned_until = excluded.banned_until,
                   created_at = CURRENT_TIMESTAMP""",
            (int(user_id), str(reason or ""), duration, banned_until)
        )
        await db.commit()

async def remove_report_ban(self, user_id: int) -> None:
    """Разбанить пользователя в репортах"""
    async with aiosqlite.connect(self.db_name) as db:
        await db.execute("DELETE FROM report_bans WHERE user_id = ?", (int(user_id),))
        await db.commit()

async def is_report_banned(self, user_id: int) -> bool:
    """Проверить, забанен ли пользователь в репортах с учётом срока."""
    async with aiosqlite.connect(self.db_name) as db:
        cursor = await db.execute(
            "SELECT banned_until FROM report_bans WHERE user_id = ? LIMIT 1",
            (int(user_id),)
        )
        row = await cursor.fetchone()
        if not row:
            return False

        banned_until = row[0]
        if not banned_until:
            return True

        try:
            end_dt = datetime.fromisoformat(str(banned_until))
        except Exception:
            return True

        if end_dt <= datetime.now():
            await db.execute("DELETE FROM report_bans WHERE user_id = ?", (int(user_id),))
            await db.commit()
            return False

        return True

# Добавляем методы в класс Database
Database.add_report_ban = add_report_ban
Database.remove_report_ban = remove_report_ban
Database.is_report_banned = is_report_banned


# ==================== Команда /report (для пользователей) ====================
class ReportRule(rules.ABCRule[Message]):
    """Правило для команды /report - работает в беседах и ЛС"""
    
    async def check(self, message: Message) -> bool:
        # Репорт доступен везде: беседы и личные сообщения боту
        if extract_command_payload(message.text, "report") is not None:
            return True
        # Доп. алиасы
        if extract_command_payload(message.text, "репорт") is not None:
            return True
        if extract_command_payload(message.text, "жалоба") is not None:
            return True
        if extract_command_payload(message.text, "creport") is not None:
            return True
        return False


async def is_staff_user(user_id: int) -> bool:
    if user_id in BOT_OWNER_IDS:
        return True
    if await db.is_bot_owner(user_id):
        return True
    if await db.is_bot_leader(user_id):
        return True
    if await db.is_bot_admin(user_id):
        return True
    if await db.is_bot_moderator(user_id):
        return True
    if await db.is_bot_helper(user_id):
        return True
    return False


SYSTEM_COMMANDS = {
    # репорты/тикеты
    "tickets", "vreport", "ans", "answer", "creport", "reports",
    # экономика (системные)
    "givemoney", "setmoney", "giveruletka",
    # системные команды владельца/админов бота
    "sysban", "unsysban", "sysrole", "sysnewrole",
    "addownerbot", "delownerbot",
    "addrukbot", "delrukbot",
    "addadminbot", "deladminbot",
    "addmoderbot", "delmoderbot",
    "addhelper", "delhelper",
    "groupall", "news",
}


async def has_system_command_access(user_id: int, command: str) -> bool:
    cmd = (command or "").strip().lower().lstrip("/").lstrip("!")
    if user_id in BOT_OWNER_IDS:
        return True
    if await is_staff_user(user_id):
        return True
    return await db.has_system_cmd_access(user_id, cmd)


async def get_chat_title(peer_id: int) -> str:
    if peer_id < 2000000000:
        return "ЛС"

    try:
        conv = await bp.api.messages.get_conversations_by_id(peer_ids=[peer_id])
        if conv.items and hasattr(conv.items[0], "chat_settings") and conv.items[0].chat_settings:
            title = conv.items[0].chat_settings.title
            if title:
                return str(title)
    except Exception:
        pass

    return f"Беседа {peer_id}"


def format_ticket_source(peer_id: int) -> str:
    return f"Беседа {peer_id}" if peer_id >= 2000000000 else "ЛС"


def build_ticket_line(ticket_id: int, user_id: int, peer_id: int, text: str, status: str, created_at: str) -> str:
    snippet = (text or "").replace("\n", " ").strip()
    short = snippet[:90] + ("..." if len(snippet) > 90 else "")
    return (
        f"🆔 #{ticket_id} [{status}]\n"
        f"👤 [id{user_id}|Пользователь] ({format_ticket_source(peer_id)})\n"
        f"📄 {short}\n"
        f"📅 {created_at}\n\n"
    )


async def get_report_recipients() -> set[int]:
    recipients: set[int] = set(BOT_OWNER_IDS)
    try:
        owners = await db.get_bot_owners()
        for owner_id, _ in owners:
            recipients.add(int(owner_id))
    except Exception:
        pass
    try:
        async with aiosqlite.connect(db.db_name) as db_conn:
            cursor = await db_conn.execute("SELECT user_id FROM bot_leaders")
            for row in await cursor.fetchall():
                recipients.add(int(row[0]))
    except Exception:
        pass
    try:
        async with aiosqlite.connect(db.db_name) as db_conn:
            cursor = await db_conn.execute("SELECT user_id FROM bot_admins")
            for row in await cursor.fetchall():
                recipients.add(int(row[0]))
    except Exception:
        pass
    try:
        async with aiosqlite.connect(db.db_name) as db_conn:
            cursor = await db_conn.execute("SELECT user_id FROM bot_moderators")
            for row in await cursor.fetchall():
                recipients.add(int(row[0]))
    except Exception:
        pass
    try:
        helpers = await db.get_all_helpers()
        for helper_id, _ in helpers:
            recipients.add(int(helper_id))
    except Exception:
        pass
    return recipients


def build_report_keyboard(report_id: str) -> str:
    keyboard = (
        Keyboard(inline=True)
        .row()
        .add(Callback("🔄 Взять", payload={"action": "report_take", "report_id": report_id}), color=KeyboardButtonColor.POSITIVE)
        .add(Callback("🔒 Закрыть", payload={"action": "report_close", "report_id": report_id}), color=KeyboardButtonColor.NEGATIVE)
        .add(Callback("♻️ Обновить", payload={"action": "report_refresh", "report_id": report_id}), color=KeyboardButtonColor.SECONDARY)
    )
    return keyboard.get_json()


async def send_report_to_support(message_text: str, report_id: str) -> None:
    """Отправляет репорт в поддержку (HELP_GROUP_ID)."""
    keyboard_json = build_report_keyboard(report_id)
    support_peer_id = HELP_GROUP_ID if int(HELP_GROUP_ID) >= 2000000000 else 2000000000 + int(HELP_GROUP_ID)

    try:
        await bp.api.messages.send(
            peer_id=support_peer_id,
            message=message_text,
            keyboard=keyboard_json,
            random_id=0
        )
    except Exception as e:
        logger.error(f"Не удалось отправить репорт в support group (peer_id={support_peer_id}): {e}")

@bp.on.message(ReportRule())
async def report_handler(message: Message) -> None:
    """Создать репорт или дополнить активный."""
    user_id = message.from_id
    chat_id = message.peer_id
    
    # Проверяем, не забанен ли пользователь в репортах
    if await db.is_report_banned(user_id):
        await message.answer("❌ Вы заблокированы от отправки репортов")
        return
    
    report_text = (
        extract_command_payload(message.text, "report")
        or extract_command_payload(message.text, "репорт")
        or extract_command_payload(message.text, "жалоба")
        or extract_command_payload(message.text, "creport")
        or ""
    )
    
    if not report_text:
        await message.answer(
            "📋 Использование: /report [текст]\n"
            "Работает и в беседах, и в ЛС боту.\n"
            "Пример: /report Проблема с ботом"
        )
        return

    active_report_id = await db.get_active_report(user_id, chat_id)
    if active_report_id:
        await db.append_to_report(active_report_id, report_text)
        await message.answer(f"✅ Тикет {active_report_id} успешно дополнен.")
        chat_title = await get_chat_title(chat_id)
        notify = (
            f"📢 ТИКЕТ ДОПОЛНИЛИ {active_report_id}\n\n"
            f"👤 От: [id{user_id}|Пользователь]\n"
            f"📍 Чат: {chat_title} ({chat_id})\n"
            f"💬 Дополнение: {report_text}"
        )
    else:
        new_report_id = await db.create_report(user_id, chat_id, report_text)
        await message.answer(f"✅ Тикет {new_report_id} успешно создан.")
        chat_title = await get_chat_title(chat_id)
        notify = (
            f"📢 НОВЫЙ ТИКЕТ {new_report_id}\n\n"
            f"👤 От: [id{user_id}|Пользователь]\n"
            f"📍 Чат: {chat_title} ({chat_id})\n"
            f"💬 Текст: {report_text}\n\n"
            f"🔹 Взять: /vreport {new_report_id}"
        )

    target_report_id = active_report_id if active_report_id else new_report_id
    await send_report_to_support(notify, target_report_id)


# ==================== Проверка подписки при входе в беседу ====================
@bp.on.raw_event(GroupEventType.MESSAGE_NEW)
async def check_subscription_on_join(event: dict) -> None:
    """Проверяет подписку на сообщество при входе пользователя в беседу"""
    try:
        obj = event.get("object", {}) or {}
        message_data = obj.get("message", {})
        
        # Проверяем, есть ли action (событие в чате)
        action = message_data.get("action")
        if not action:
            return
    
        action_type = action.get("type")
        if action_type != "chat_invite_user":
            return
    
        # Получаем ID пользователя, который присоединился
        member_id = action.get("member_id")
        if not member_id or member_id <= 0:
            return
        
        # Получаем peer_id чата
        peer_id = message_data.get("peer_id")
        if not peer_id or peer_id < 2000000000:
            return
        
        # Проверяем, настроена ли проверка подписки для этого чата
        community_id = await db.get_sub_community(peer_id)
        if not community_id:
            return  # Проверка не настроена
        
        # Проверяем подписку пользователя на сообщество
        try:
            is_subscribed = await bp.api.groups.is_member(group_id=community_id, user_id=member_id)
            if not is_subscribed:
                # Пользователь не подписан - отправляем сообщение и кикаем
                user_mention = f"[id{member_id}|Пользователь]"
                
                # Получаем информацию о сообществе
                try:
                    community_info = await bp.api.groups.get_by_id(group_ids=[community_id])
                    community_name = community_info.groups[0].name if community_info and community_info.groups else "сообщество"
                    community_link = f"https://vk.com/club{community_id}"
                except Exception:
                    community_name = "сообщество"
                    community_link = f"https://vk.com/club{community_id}"
                
                await bp.api.messages.send(
                    peer_id=peer_id,
                    message=f"⚠️ {user_mention}, вы не подписаны на {community_name}!\n"
                            f"Подпишитесь, чтобы остаться в чате: {community_link}",
                    random_id=0
                )
                
                # Кикаем пользователя из чата
                try:
                    await bp.api.messages.remove_chat_user(
                        chat_id=peer_id - 2000000000,
                        member_id=member_id
                    )
                except Exception as e:
                    logger.error(f"Не удалось кикнуть пользователя {member_id}: {e}")
                    
        except Exception as e:
            logger.error(f"Ошибка при проверке подписки: {e}")
            
    except Exception as e:
        logger.error(f"Ошибка в check_subscription_on_join: {e}")


# ==================== Команда /vreport и /tickets ====================
@bp.on.message(CommandNameRule("vreport"))
async def vreport_handler(message: Message) -> None:
    """Список тикетов или просмотр конкретного тикета"""
    user_id = message.from_id

    # Проверка доступа (staff)
    if not await is_staff_user(user_id):
        await message.answer("❌ Нет доступа. Команда доступна staff.")
        return

    vreport_text = extract_command_payload(message.text, "vreport") or ""

    # Если указан ID тикета - показать его
    if vreport_text:
        ticket_id = vreport_text.strip()
        report_data = await db.get_report_by_report_id(ticket_id)
        if not report_data:
            await message.answer(f"❌ Тикет #{ticket_id} не найден")
            return

        _, report_id, user_id_rep, chat_id, report_text, status, answered_by, answer_text, created_at, answered_at = report_data
        chat_title = await get_chat_title(chat_id)

        ticket_info = (
            f"🎫 Тикет #{report_id}\n"
            f"📊 Статус: {status}\n"
            f"👤 Создал: [id{user_id_rep}|Пользователь]\n"
            f"📍 Источник: {chat_title}\n"
            f"📅 Создан: {created_at}\n"
            f"💬 Текст: {report_text}"
        )
        await message.answer(ticket_info)
        return
    
    # Иначе - показываем список активных тикетов
    reports = await db.get_active_reports()

    if not reports:
        await message.answer("📭 Нет активных тикетов")
        return
    
    tickets_list = "📋 Активные тикеты:\n\n"
    for report in reports[:20]:  # максимум 20
        _, report_id, user_id_rep, chat_id, report_text, status, answered_by, answer_text, created_at, answered_at = report
        tickets_list += build_ticket_line(report_id, user_id_rep, chat_id, report_text, status, created_at)

    await message.answer(tickets_list)


@bp.on.message(CommandNameRule("tickets"))
async def tickets_handler(message: Message) -> None:
    """Список тикетов (алиас vreport)"""
    await vreport_handler(message)


# ==================== Команда /ans / answer ====================
@bp.on.message(CommandNameRule("ans"))
@bp.on.message(CommandNameRule("answer"))
async def ans_handler(message: Message) -> None:
    """Ответить на тикет"""
    user_id = message.from_id
    
    # Проверка доступа (staff)
    if not await is_staff_user(user_id):
        await message.answer("❌ Нет доступа. Команда доступна staff.")
        return
    
    ans_text = extract_command_payload(message.text, "ans") or ""
    if not ans_text:
        ans_text = extract_command_payload(message.text, "answer") or ""

    if not ans_text:
        await message.answer(
            "📋 Использование: /ans [ID тикета] [ответ]\n"
            "Пример: /ans 5 Проблема решена"
        )
        return

    # Парсим ID тикета и ответ
    parts = ans_text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("❌ Укажите ID тикета и ответ\nПример: /ans 85539 Ваша проблема решена")
        return

    ticket_id = parts[0]
    staff_answer = parts[1]

    # Получаем тикет по report_id (строковый идентификатор)
    report_data = await db.get_report_by_report_id(ticket_id)
    if not report_data:
        await message.answer(f"❌ Тикет #{ticket_id} не найден")
        return

    _, report_id, target_user_id, chat_id, report_text, status, answered_by, db_answer_text, created_at, answered_at = report_data

    # Закрываем тикет
    await db.close_report(report_id, user_id)

    # Отправляем ответ в чат, откуда был отправлен тикет
    try:
        await bp.api.messages.send(
            peer_id=chat_id,
            message=f"📬 Ответ на тикет #{report_id} от [id{user_id}|сотрудника]:\n\n{staff_answer}",
            random_id=0
        )
    except Exception as e:
        logger.error(f"Не удалось отправить ответ в чат: {e}")
        await message.answer(f"⚠️ Тикет #{report_id} закрыт, но не удалось отправить ответ в чат.")
        return

    await message.answer(f"✅ Ответ на тикет #{report_id} отправлен в чат")


async def setup() -> Bot:
    """Функция инициализации бота"""
    # Читаем токен из config.json
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r", encoding="utf-8-sig") as f:
        config = json.load(f)
    
    token = config.get("vk_token")
    if not token or token == "YOUR_VK_TOKEN_HERE":
        raise ValueError("Укажите токен в config.json (поле vk_token)")
    
    # Инициализируем базу данных
    await db.init_db()
    logger.info("База данных инициализирована")
    
    # Создаём API и Bot с отключенной проверкой SSL
    ssl_context = aiohttp.TCPConnector(ssl=False)
    http_client = AiohttpClient(connector=ssl_context)
    api = API(token=token, http_client=http_client)
    bot = Bot(api=api)
    
    # Получаем ID группы автоматически через API
    try:
        groups_info = await api.groups.get_by_id()
        if groups_info.groups:
            group_id = groups_info.groups[0].id
            CtxStorage().set("group_id", group_id)
            logger.info(f"ID группы автоматически определён: {group_id}")
    except Exception as e:
        logger.error(f"Ошибка при получении ID группы: {e}")
        raise
    
    # Подключаем blueprint
    bp.load(bot)
    
    logger.info("Бот настроен")
    return bot
    

async def main() -> None:
    """Главная функция запуска бота"""
    bot = await setup()
    logger.info("Бот настроен, запускаем polling...")
    
    # Запускаем polling
    await bot.run_polling()


async def render_mybusiness_text(user_id: int, notice: str = "") -> tuple[str, str]:
    raw_material, workers, ad_level, cashbox, tax_debt = await db.get_my_business(user_id)
    balance = await db.get_balance(user_id)
    text = "🏭 Мой бизнес\n\n"
    if notice:
        text += f"{notice}\n\n"
    text += f"💰 Баланс: {balance}$\n"
    text += f"🧱 Сырьё: {raw_material}\n"
    text += f"👷 Работники: {workers}\n"
    text += f"📣 Реклама: {ad_level}\n"
    text += f"🏦 Касса бизнеса: {cashbox}$\n"
    text += f"🧾 Налоговый долг: {tax_debt}$\n\n"
    text += "Быстрые команды: /mybusiness buyraw 1 | hire 1 | ads 1 | paytax | withdraw 1000"

    # owner_id добавляем в payload, чтобы кнопки работали только у владельца карточки
    keyboard = (
        Keyboard(inline=True)
        .row()
        .add(Callback("🧱 +Сырьё", payload={"action": "mybusiness_buyraw", "owner_id": user_id}), color=KeyboardButtonColor.PRIMARY)
        .add(Callback("👷 +Работник", payload={"action": "mybusiness_hire", "owner_id": user_id}), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Callback("📣 +Реклама", payload={"action": "mybusiness_ads", "owner_id": user_id}), color=KeyboardButtonColor.POSITIVE)
        .add(Callback("🧾 Налоги", payload={"action": "mybusiness_paytax", "owner_id": user_id}), color=KeyboardButtonColor.NEGATIVE)
        .row()
        .add(Callback("💸 Вывести 1000$", payload={"action": "mybusiness_withdraw_1000", "owner_id": user_id}), color=KeyboardButtonColor.SECONDARY)
        .add(Callback("🏦 Вывести всё", payload={"action": "mybusiness_withdraw_all", "owner_id": user_id}), color=KeyboardButtonColor.SECONDARY)
        .add(Callback("🔄 Обновить", payload={"action": "mybusiness_view", "owner_id": user_id}), color=KeyboardButtonColor.SECONDARY)
    )
    return text, keyboard.get_json()


@bp.on.message(CommandNameRule("balance"))
async def balance_handler(message: Message) -> None:
    if not await ensure_games_enabled_for_message(message):
        return
    balance = await db.get_balance(message.from_id)
    await message.answer(f"💰 Ваш баланс: {balance}$")
    

@bp.on.message(CommandNameRule("job"))
async def job_handler(message: Message) -> None:
    if not await ensure_games_enabled_for_message(message):
        return
    ok, earned, cooldown_left = await db.do_job(message.from_id)
    if not ok:
        minutes = cooldown_left // 60
        seconds = cooldown_left % 60
        keyboard = (
            Keyboard(inline=True)
            .add(Callback("🔄 Обновить", payload={"action": "job_refresh"}), color=KeyboardButtonColor.SECONDARY)
        )
        await message.answer(
            f"⏳ Работать можно раз в 30 минут.\nПопробуйте через {minutes}м {seconds}с",
            keyboard=keyboard.get_json()
        )
        return

    balance = await db.get_balance(message.from_id)
    keyboard = (
        Keyboard(inline=True)
        .row()
        .add(Callback("💼 Работать снова", payload={"action": "job_refresh"}), color=KeyboardButtonColor.POSITIVE)
        .add(Callback("👤 Профиль", payload={"action": "open_profile"}), color=KeyboardButtonColor.PRIMARY)
    )
    await message.answer(
        f"💼 Вы заработали: +{earned}$\n💰 Текущий баланс: {balance}$",
        keyboard=keyboard.get_json()
    )


@bp.on.message(CommandNameRule("pay"))
async def pay_handler(message: Message) -> None:
    if not await ensure_games_enabled_for_message(message):
        return
    payload = extract_command_payload(message.text, "pay") or ""
    if not payload:
        await message.answer("📋 Использование: /pay [reply/@user/id] [сумма]\nПример: /pay 123456789 500")
        return

    target_user_id = parse_target_user_id(message, payload)
    amounts = re.findall(r"\b(\d+)\b", payload)
    if not target_user_id or not amounts:
        await message.answer("❌ Не удалось определить пользователя или сумму")
        return

    amount = int(amounts[-1])
    ok, reason = await db.transfer_balance(message.from_id, target_user_id, amount)
    if not ok:
        await message.answer(f"❌ {reason}")
        return

    sender_balance = await db.get_balance(message.from_id)
    await message.answer(
        f"✅ Перевод выполнен: {amount}$ -> [id{target_user_id}|пользователю]\n"
        f"💰 Ваш баланс: {sender_balance}$"
    )


@bp.on.message(CommandNameRule("business"))
async def business_handler(message: Message) -> None:
    payload = (extract_command_payload(message.text, "business") or "").strip().lower()
    user_id = message.from_id
    level, _, _ = await db.get_business(user_id)

    if payload.startswith("buy"):
        # Покупка бизнеса
        target_level = None
        
        # Парсим номер бизнеса из команды /business buy [номер]
        parts = payload.replace("buy", "").strip().split()
        if parts:
            try:
                target_level = int(parts[0])
            except ValueError:
                pass
        
        if target_level is None:
            # Если не указан номер - покупаем следующий уровень
            if level == 0:
                target_level = 1  # Покупаем первый бизнес (Киоск)
            else:
                target_level = level + 1
        
        # Проверяем валидность уровня
        if target_level < 1 or target_level >= len(BUSINESS_TYPES):
            await message.answer("❌ Неверный номер бизнеса")
            return
    
        # Проверяем, не куплен ли уже этот бизнес
        if level >= target_level:
            await message.answer(f"❌ У вас уже есть бизнес уровня {level} ({BUSINESS_TYPES[level][0]})")
            return
    
        # Проверяем, что есть промежуточные бизнесы
        if target_level > level + 1:
            await message.answer(f"❌ Сначала купите предыдущие бизнесы. У вас сейчас: {BUSINESS_TYPES[level][0] if level > 0 else 'нет'}")
            return
        
        cost = BUSINESS_TYPES[target_level][1]
        business_name = BUSINESS_TYPES[target_level][0]
        
        ok, reason, new_balance = await db.upgrade_business(user_id, cost)
        if not ok:
            await message.answer(f"❌ {reason}\n💰 Ваш баланс: {new_balance}$")
            return
        
        keyboard = (
            Keyboard(inline=True)
            .add(Callback("🏢 Открыть бизнес", payload={"action": "business_view"}), color=KeyboardButtonColor.PRIMARY)
        )
        await message.answer(
            f"✅ Вы приобрели: {business_name}!\n"
            f"💸 Потрачено: {cost}$\n"
            f"💰 Баланс: {new_balance}$",
            keyboard=keyboard.get_json()
        )
        return

    text, keyboard_json = await render_business_text(user_id)
    await message.answer(text, keyboard=keyboard_json)


@bp.on.message(CommandNameRule("brak"))
async def brak_handler(message: Message) -> None:
    if not await ensure_games_enabled_for_message(message):
        return
    payload = extract_command_payload(message.text, "brak") or ""
    target_user_id = parse_target_user_id(message, payload)
    if not target_user_id:
        await message.answer("📋 Использование: /brak [reply/@user/id]")
        return
    
    ok, reason, proposal_id = await db.create_marriage_proposal(message.from_id, target_user_id, message.peer_id)
    if not ok:
        await message.answer(f"❌ {reason}")
        return

    keyboard = (
        Keyboard(inline=True)
        .row()
        .add(
            Callback("✅ Принять", payload={"action": "marriage_accept", "proposal_id": proposal_id}),
            color=KeyboardButtonColor.POSITIVE
        )
        .add(
            Callback("❌ Отклонить", payload={"action": "marriage_reject", "proposal_id": proposal_id}),
            color=KeyboardButtonColor.NEGATIVE
        )
    )
    await message.answer(
        f"💍 [id{target_user_id}|Пользователь], вам сделали предложение брака от [id{message.from_id}|пользователя].\n"
        f"Нажмите кнопку ниже. Владелец(ы) бота тоже могут подтвердить/отклонить.",
        keyboard=keyboard.get_json()
    )


@bp.on.message(CommandNameRule("unbrak"))
async def unbrak_handler(message: Message) -> None:
    if not await ensure_games_enabled_for_message(message):
        return
    spouse_id = await db.get_spouse(message.from_id)
    if spouse_id is None:
        await message.answer("❌ Вы не состоите в браке")
        return

    removed = await db.remove_marriage(message.from_id)
    if not removed:
        await message.answer("❌ Не удалось расторгнуть брак")
        return

    await message.answer(f"💔 Брак расторгнут с [id{spouse_id}|пользователем]")


# ==================== Лимиты выдачи денег ====================
# Хранилище для отслеживания дневных лимитов выдачи денег
# {user_id: {date: total_amount}}
money_daily_limits: dict[int, dict[str, int]] = {}
MONEY_DAILY_LIMIT = 5_000_000  # 5 миллионов в день


def check_money_limit(user_id: int, amount: int) -> tuple[bool, str, int]:
    """
    Проверяет дневной лимит на выдачу денег.
    Возвращает (разрешено, причина, остаток лимита).
    """
    # Основные владельцы бота не имеют лимитов
    if user_id in BOT_OWNER_IDS:
        return True, "", MONEY_DAILY_LIMIT
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    # Инициализируем запись пользователя, если нет
    if user_id not in money_daily_limits:
        money_daily_limits[user_id] = {}
    
    user_limits = money_daily_limits[user_id]
    
    # Очищаем старые записи (не сегодняшние)
    for date in list(user_limits.keys()):
        if date != today:
            del user_limits[date]
    
    current_used = user_limits.get(today, 0)
    remaining = MONEY_DAILY_LIMIT - current_used
    
    if amount > remaining:
        return False, f"❌ Превышен дневной лимит! Осталось: {remaining}$", remaining
    
    return True, "", remaining


def add_money_to_limit(user_id: int, amount: int) -> None:
    """Добавляет сумму к дневному лимиту пользователя."""
    if user_id in BOT_OWNER_IDS:
        return
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    if user_id not in money_daily_limits:
        money_daily_limits[user_id] = {}
    
    user_limits = money_daily_limits[user_id]
    user_limits[today] = user_limits.get(today, 0) + amount


@bp.on.message(CommandNameRule("profile"))
async def profile_handler(message: Message) -> None:
    await message.answer("Команда /profile удалена. Используйте /stats для просмотра своей статистики.")


@bp.on.message(CommandNameRule("givemoney"))
async def givemoney_handler(message: Message) -> None:
    # Проверка доступа (руководство бота)
    if not await check_is_leader_or_owner(message.from_id):
        await message.answer("❌ Нет доступа. Команда доступна только руководству бота.")
        return
    payload = extract_command_payload(message.text, "givemoney") or ""
    if not payload:
        await message.answer("�‹ Использование: /givemoney [user/reply] [сумма]")
        return

    target_user_id = parse_target_user_id(message, payload)
    amounts = re.findall(r"\b(\d+)\b", payload)
    if not target_user_id or not amounts:
        await message.answer("❌ Не удалось определить пользователя или сумму")
        return
    
    amount = int(amounts[-1])
    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0")
        return

    # Проверка дневного лимита
    allowed, reason, remaining = check_money_limit(message.from_id, amount)
    if not allowed:
        await message.answer(reason)
        return

    new_balance = await db.add_balance(target_user_id, amount)
    add_money_to_limit(message.from_id, amount)
    await message.answer(
        f"✅ Вы выдали [id{target_user_id}|пользователю] {amount}$\n"
        f"💰 Новый баланс пользователя: {new_balance}$"
    )


@bp.on.message(CommandNameRule("setmoney"))
async def setmoney_handler(message: Message) -> None:
    """Установить баланс пользователю (не прибавить)."""
    # Проверка доступа (руководство бота)
    if not await check_is_leader_or_owner(message.from_id):
        await message.answer("❌ Нет доступа. Команда доступна только руководству бота.")
        return
    payload = extract_command_payload(message.text, "setmoney") or ""
    if not payload:
        await message.answer("📋 Использование: /setmoney [user/reply] [сумма]\nПример: /setmoney 123456789 10000")
        return

    target_user_id = parse_target_user_id(message, payload)
    amounts = re.findall(r"\b(\d+)\b", payload)
    if not target_user_id or not amounts:
        await message.answer("❌ Не удалось определить пользователя или сумму")
        return

    amount = int(amounts[-1])
    
    # Проверка лимита для обычных пользователей
    allowed, reason, remaining = check_money_limit(message.from_id, amount)
    if not allowed:
        await message.answer(f"{reason}\n💡 Лимит: {MONEY_DAILY_LIMIT}$ в день")
        return

    new_balance = await db.set_balance(target_user_id, amount)
    add_money_to_limit(message.from_id, amount)
    
    # Показываем остаток лимита, если не владелец
    limit_info = ""
    if message.from_id not in BOT_OWNER_IDS:
        limit_info = f"\n💰 Лимит сегодня: {remaining - amount}$ / {MONEY_DAILY_LIMIT}$"
    
    await message.answer(f"✅ Баланс [id{target_user_id}|пользователя] установлен: {new_balance}${limit_info}")


@bp.on.message(CommandNameRule("giveruletka"))
async def giveruletka_handler(message: Message) -> None:
    """Выдать рулетку (добавить прокрутки пользователю)."""
    if not await has_system_command_access(message.from_id, "giveruletka"):
        await message.answer("❌ Нет доступа")
        return
    payload = extract_command_payload(message.text, "giveruletka") or ""
    if not payload and not message.reply_message:
        await message.answer("📋 Использование: /giveruletka [reply/@user/id] [кол-во]\nПример: /giveruletka 123456789 3")
        return

    target_user_id = message.reply_message.from_id if message.reply_message else parse_target_user_id(message, payload)
    if not target_user_id:
        await message.answer("❌ Не удалось определить пользователя")
        return
    
    counts = re.findall(r"\b(\d+)\b", payload)
    amount = int(counts[-1]) if counts else 1
    amount = max(1, min(amount, 100))

    spins_left = await db.add_roulette_spins(int(target_user_id), amount)
    await message.answer(f"✅ Вы выдали рулетку [id{target_user_id}|пользователю]: +{amount}\n🎰 Теперь рулеток: {spins_left}")


@bp.on.message(CommandNameRule("mybusiness"))
async def mybusiness_handler(message: Message) -> None:
    if not await ensure_games_enabled_for_message(message):
        return
    user_id = message.from_id
    payload = (extract_command_payload(message.text, "mybusiness") or "").strip().lower()
    
    earned, hours = await db.process_my_business(user_id)
    notice = ""
    if earned > 0:
        notice = f"✅ Начислено прибыли за {hours}ч: +{earned}$\n\n"

    if payload.startswith("buyraw"):
        parts = payload.split()
        packs = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        ok, msg = await db.my_business_buy_raw(user_id, packs)
        await message.answer(("✅ " if ok else "❌ ") + msg)
        return
    if payload.startswith("hire"):
        parts = payload.split()
        count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        ok, msg = await db.my_business_hire(user_id, count)
        await message.answer(("✅ " if ok else "❌ ") + msg)
        return
    if payload.startswith("ads"):
        parts = payload.split()
        levels = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        ok, msg = await db.my_business_advertise(user_id, levels)
        await message.answer(("✅ " if ok else "❌ ") + msg)
        return
    if payload.startswith("paytax"):
        ok, msg = await db.my_business_pay_tax(user_id)
        await message.answer(("✅ " if ok else "❌ ") + msg)
        return
    if payload.startswith("withdraw"):
        parts = payload.split()
        amount = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        ok, msg = await db.my_business_withdraw(user_id, amount)
        await message.answer(("✅ " if ok else "❌ ") + msg)
        return

    text, keyboard_json = await render_mybusiness_text(user_id, notice=notice.strip())
    await message.answer(text, keyboard=keyboard_json)


# ==================== Хэндлер добавления бота в беседу ====================
@bp.on.raw_event(GroupEventType.MESSAGE_NEW)
async def chat_invite_handler(event: dict) -> None:
    """
    Обработчик новых сообщений.
    Проверяем событие добавления бота в чат.
    """
    # Получаем объект message из события
    obj = event.get("object", {})
    message_data = obj.get("message", {})
    
    if not message_data:
        return

    # Проверяем, что это событие добавления в беседу
    action = message_data.get("action")
    if not action:
        return

    action_type = action.get("type")
    peer_id = message_data.get("peer_id")
    
    # Получаем ID группы
    group_id = CtxStorage().get("group_id")
    
    if action_type == "chat_invite_user":
        member_id = action.get("member_id")
        inviter_id = message_data.get("from_id")
        
        if member_id == -group_id:
            await db.add_chat(peer_id)
            await db.set_admin_status(peer_id, False)
            
            keyboard = (
                Keyboard(inline=True)
                .row()
                .add(
                    Callback("Я выдал!", payload={"action": "grant_admin"}),
                    color=KeyboardButtonColor.POSITIVE
                )
            )
                
            # Отправляем сообщение
            await bp.api.messages.send(
                peer_id=peer_id,
                message="👋 Привет! Чтобы начать работу, выдайте мне права администратора",
                keyboard=keyboard.get_json(),
                random_id=0
            )
            return

        # Ограничение на добавление ботов/сообществ
        if member_id and member_id < 0:
            allow_community_add = await db.get_allow_community_add(peer_id)
            if not allow_community_add:
                chat_id = peer_id - 2000000000
                try:
                    await bp.api.messages.remove_chat_user(chat_id=chat_id, member_id=member_id)
                except Exception as e:
                    logger.error(f"Ошибка при кике добавленного сообщества {member_id}: {e}")

                if inviter_id and inviter_id > 0:
                    try:
                        await bp.api.messages.remove_chat_user(chat_id=chat_id, member_id=inviter_id)
                    except Exception as e:
                        logger.error(f"Ошибка при кике пользователя {inviter_id}, добавившего сообщество: {e}")

                await bp.api.messages.send(
                    peer_id=peer_id,
                    message=(
                        "⛔ Добавление ботов в этой беседе запрещено владельцем.\n"
                        f"Удалён бот [club{abs(int(member_id))}|]"
                        + (f" и пользователь [id{inviter_id}|добавивший]." if inviter_id and inviter_id > 0 else ".")
                    ),
                    random_id=0
                )
                return
            
        # Если добавили обычного пользователя - проверяем, не забанен ли он
        if member_id and member_id > 0:
            # Проверяем, забанен ли пользователь в этом чате
            if await db.is_banned(peer_id, member_id):
                try:
                    await bp.api.messages.remove_chat_user(
                        chat_id=peer_id - 2000000000,
                        member_id=member_id
                    )
                    await bp.api.messages.send(
                        peer_id=peer_id,
                        message=f"🚫 Пользователь [id{member_id}|] был кикнут — он в бане",
                        random_id=0
                    )
                except Exception as e:
                    logger.error(f"Ошибка при кике: {e}")
            else:
                # Отправляем welcome, если настроено владельцем беседы
                try:
                    welcome_text = await db.get_welcome(peer_id)
                    if welcome_text:
                        await bp.api.messages.send(
                            peer_id=peer_id,
                            message=f"👋 [id{member_id}|пользователь] {welcome_text}",
                            random_id=0
                        )
                except Exception as e:
                    logger.error(f"Ошибка при отправке welcome: {e}")

    elif action_type == "chat_kick_user":
        member_id = action.get("member_id")
        actor_id = message_data.get("from_id")

        # Пользователь вышел сам (actor == member) и включён авто-кик при выходе
        if member_id and member_id > 0 and actor_id == member_id:
            auto_kick_on_leave = await db.get_auto_kick_on_leave(peer_id)
            if auto_kick_on_leave:
                await db.add_ban(peer_id, member_id)
                await bp.api.messages.send(
                    peer_id=peer_id,
                    message=f"🚪 [id{member_id}|Пользователь] вышел из беседы и автоматически добавлен в бан-лист чата.",
                    random_id=0
                )
    

# ==================== Хэндлер callback-кнопок ====================
@bp.on.raw_event(GroupEventType.MESSAGE_EVENT)
async def message_event_handler(event: dict) -> None:
    """
    Обработчик callback-событий (нажатие inline-кнопок).
    """
    # Извлекаем данные из события
    obj = event.get("object", {})
    peer_id = obj.get("peer_id")
    user_id = obj.get("user_id")
    event_id = obj.get("event_id")
    payload = obj.get("payload", {})
    event_cmid = obj.get("conversation_message_id")
    
    if not peer_id or not user_id:
        return
    
    action = payload.get("action")

    # Настройки беседы (доступ с приоритета 90)
    if action in {"settings_toggle_games", "settings_toggle_community_add", "settings_toggle_auto_kick_leave"}:
        if peer_id < 2000000000:
            return
    
        role = await db.get_user_role(peer_id, user_id)
        if not role or role[1] < 90:
            try:
                await bp.api.messages.send_message_event_answer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data={"type": "show_snackbar", "text": "❌ Требуется приоритет 90"}
                )
            except Exception:
                pass
            return
    
        allow_games, allow_community_add, auto_kick_on_leave = await db.get_chat_settings(peer_id)

        if action == "settings_toggle_games":
            allow_games = not allow_games
            await db.set_allow_games(peer_id, allow_games)
            snackbar = "✅ Игровые команды включены" if allow_games else "⛔ Игровые команды отключены"
        elif action == "settings_toggle_community_add":
            allow_community_add = not allow_community_add
            await db.set_allow_community_add(peer_id, allow_community_add)
            snackbar = "✅ Добавление ботов разрешено" if allow_community_add else "⛔ Добавление ботов запрещено"
        else:
            auto_kick_on_leave = not auto_kick_on_leave
            await db.set_auto_kick_on_leave(peer_id, auto_kick_on_leave)
            snackbar = "✅ Авто-кик при выходе включён" if auto_kick_on_leave else "⛔ Авто-кик при выходе выключен"

        # Повторно читаем фактические настройки и обновляем клавиатуру
        allow_games, allow_community_add, auto_kick_on_leave = await db.get_chat_settings(peer_id)
        keyboard_json = build_settings_keyboard(allow_games, allow_community_add, auto_kick_on_leave)
        settings_text = "⚙️ Настройки беседы"

        if event_cmid:
            try:
                await bp.api.messages.edit(
                    peer_id=peer_id,
                    conversation_message_id=event_cmid,
                    message=settings_text,
                    keyboard=keyboard_json
                )
            except Exception:
                await bp.api.messages.send(
                    peer_id=peer_id,
                    message=settings_text,
                    keyboard=keyboard_json,
                    random_id=0
                )
        else:
            await bp.api.messages.send(
                peer_id=peer_id,
                message=settings_text,
                keyboard=keyboard_json,
                random_id=0
            )

        try:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": snackbar}
            )
        except Exception:
            pass
        return

    async def edit_or_send(text: str, keyboard: Optional[str] = None) -> None:
        if event_cmid:
            try:
                await bp.api.messages.edit(
                    peer_id=peer_id,
                    conversation_message_id=event_cmid,
                    message=text,
                    keyboard=keyboard
                )
                return
            except Exception:
                pass
        await bp.api.messages.send(
            peer_id=peer_id,
            message=text,
            keyboard=keyboard,
            random_id=0
        )

    # Блокировка игровых callback-действий, если игры выключены в беседе
    game_actions = {
        "job_refresh", "business_buy", "business_view",
        "mybusiness_view", "mybusiness_buyraw", "mybusiness_hire", "mybusiness_ads", "mybusiness_paytax", "mybusiness_withdraw_1000", "mybusiness_withdraw_all",
        "marriage_accept", "marriage_reject", "open_profile"
    }
    if action in game_actions and peer_id >= 2000000000:
        if not await db.get_allow_games(peer_id):
            try:
                await bp.api.messages.send_message_event_answer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data={"type": "show_snackbar", "text": "⛔ Игровые команды отключены владельцем"}
                )
            except Exception:
                pass
            return
    
    # Обработка подтверждения админских прав
    if action == "grant_admin":
        try:
            # Сначала добавляем чат в БД
            await db.add_chat(peer_id)
            
            members = await bp.api.messages.get_conversation_members(peer_id=peer_id)
            
            has_admin_rights = False
            owner_id = None
            for member in members.items:
                if member.member_id == user_id:
                    has_admin_rights = member.is_admin
                # Ищем создателя беседы
                if hasattr(member, 'is_owner') and member.is_owner:
                    owner_id = member.member_id
            
            # Если не нашли owner через members, пробуем получить через conversations
            if owner_id is None:
                try:
                    conv = await bp.api.messages.get_conversations_by_id(peer_ids=[peer_id])
                    if conv.items and hasattr(conv.items[0], 'chat_settings'):
                        owner_id = conv.items[0].chat_settings.get('owner_id')
                except Exception:
                    pass
                
            if has_admin_rights:
                await db.set_admin_status(peer_id, True)
                
                # Выдаём роль 100 создателю беседы если она ещё не выдана
                if owner_id:
                    user_role = await db.get_user_role(peer_id, owner_id)
                    if not user_role:
                        await db.set_user_role(peer_id, owner_id, 100)
                
                await bp.api.messages.send_message_event_answer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data={"type": "show_snackbar", "text": "✅ Я запустился! Команды доступны."}
                )
            else:
                await bp.api.messages.send_message_event_answer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data={"type": "show_snackbar", "text": "❌ Ты не выдал админку :("}
                )
        
        except Exception as e:
            error_msg = str(e)
            if "You don't have access to this chat" in error_msg:
                await bp.api.messages.send_message_event_answer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data={"type": "show_snackbar", "text": "Я не могу проверить — дайте права админа!"}
                )
            else:
                logger.error(f"Ошибка при проверке прав: {e}")
                try:
                    await bp.api.messages.send_message_event_answer(
                        event_id=event_id,
                        user_id=user_id,
                        peer_id=peer_id,
                        event_data={"type": "show_snackbar", "text": "Ошибка :("}
                    )
                except Exception:
                    pass
        return
    
    # Быстрые действия экономики/бизнеса
    if action == "job_refresh":
        ok, earned, cooldown_left = await db.do_job(user_id)
        if not ok:
            minutes = cooldown_left // 60
            seconds = cooldown_left % 60
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": f"Откат: {minutes}м {seconds}с"}
            )
            return
    
        balance = await db.get_balance(user_id)
        await edit_or_send(f"💼 Вы заработали: +{earned}$\n💰 Текущий баланс: {balance}$")
        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": f"+{earned}$, баланс {balance}$"}
        )
        return
    
    if action == "open_profile":
        text = await render_profile_text(user_id)
        await edit_or_send(text)
        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": "Профиль открыт"}
        )
        return
    
    if action == "business_buy":
        # Получаем уровень из payload кнопки
        target_level = payload.get("level")
        level, _, _ = await db.get_business(user_id)
        
        # Если уровень не указан в payload - используем логику покупки следующего
        if target_level is None:
            if level == 0:
                target_level = 1
            else:
                target_level = level + 1
        
        target_level = int(target_level)
        
        # Проверяем валидность
        if target_level < 1 or target_level >= len(BUSINESS_TYPES):
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Неверный номер бизнеса"}
            )
            return

        # Проверяем, не куплен ли уже этот бизнес
        if level >= target_level:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Уже куплен"}
            )
            return

        # Проверяем последовательность покупки
        if target_level > level + 1:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Сначала купите предыдущий"}
            )
            return

        cost = BUSINESS_TYPES[target_level][1]
        business_name = BUSINESS_TYPES[target_level][0]
        ok, reason, _ = await db.upgrade_business(user_id, cost)
        
        if ok:
            text, keyboard_json = await render_business_text(user_id)
            await edit_or_send(text, keyboard_json)
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": f"Куплено: {business_name}!"}
            )
        else:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": reason}
            )
        return
    
    if action == "business_view":
        text, keyboard_json = await render_business_text(user_id)
        await edit_or_send(text, keyboard_json)
        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": "Бизнес открыт"}
        )
        return
    
    if action in {"mybusiness_view", "mybusiness_buyraw", "mybusiness_hire", "mybusiness_ads", "mybusiness_paytax", "mybusiness_withdraw_1000", "mybusiness_withdraw_all"}:
        owner_id = payload.get("owner_id")
        if owner_id is None or int(owner_id) != int(user_id):
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "❌ Это не ваш бизнес"}
            )
            return
    
        earned, hours = await db.process_my_business(user_id)
        if action == "mybusiness_buyraw":
            ok, msg = await db.my_business_buy_raw(user_id, 1)
        elif action == "mybusiness_hire":
            ok, msg = await db.my_business_hire(user_id, 1)
        elif action == "mybusiness_ads":
            ok, msg = await db.my_business_advertise(user_id, 1)
        elif action == "mybusiness_paytax":
            ok, msg = await db.my_business_pay_tax(user_id)
        elif action == "mybusiness_withdraw_1000":
            ok, msg = await db.my_business_withdraw(user_id, 1000)
        elif action == "mybusiness_withdraw_all":
            # Выводим всю кассу
            raw_material, workers, ad_level, cashbox, tax_debt = await db.get_my_business(user_id)
            if tax_debt > 0:
                ok, msg = False, "Сначала оплатите налоги"
            elif cashbox <= 0:
                ok, msg = False, "Касса пустая"
            else:
                ok, msg = await db.my_business_withdraw(user_id, int(cashbox))
        else:
            ok, msg = True, "Обновлено"

        notice = f"Начислено за {hours}ч: +{earned}$" if earned > 0 else ""
        text, keyboard_json = await render_mybusiness_text(user_id, notice=notice)
        await edit_or_send(text, keyboard_json)
        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": msg if ok else f"❌ {msg}"}
        )
        return

    if action in {"marriage_accept", "marriage_reject"}:
        proposal_id = payload.get("proposal_id")
        if not proposal_id:
            return
        proposal = await db.get_marriage_proposal(int(proposal_id))
        if not proposal:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Предложение не найдено"}
            )
            return

        p_id, from_user_id, to_user_id, proposal_peer_id, status = proposal
        if status != "pending":
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Предложение уже обработано"}
            )
            return

        # Только получатель может принять/отклонить
        if user_id != to_user_id:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Только получатель может принять/отклонить"}
            )
            return

        if action == "marriage_reject":
            await db.close_marriage_proposal(p_id, "rejected")
            await bp.api.messages.send(
                peer_id=proposal_peer_id,
                message=f"💔 Предложение брака от [id{from_user_id}|пользователя] для [id{to_user_id}|пользователя] отклонено.",
                random_id=0
            )
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Отклонено"}
            )
            return

        ok, reason = await db.create_marriage(from_user_id, to_user_id)
        if not ok:
            await db.close_marriage_proposal(p_id, "cancelled")
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": reason}
            )
            return

        await db.close_marriage_proposal(p_id, "accepted")
        await bp.api.messages.send(
            peer_id=proposal_peer_id,
            message=f"💍 Брак зарегистрирован: [id{from_user_id}|пользователь] + [id{to_user_id}|пользователь]",
            random_id=0
        )
        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": "Брак подтвержден"}
        )
        return

    if action in {"report_take", "report_close", "report_refresh"}:
        report_id = str(payload.get("report_id", "")).lstrip("#").upper()
        if not report_id:
            return

        if not await is_staff_user(user_id):
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Нет доступа"}
            )
            return

        report_data = await db.get_report_by_id(report_id)
        if not report_data:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Репорт не найден"}
            )
            return

        _, rep_id, report_user_id, report_chat_id, report_text, status, answered_by, answer_text, created_at, answered_at = report_data

        if action == "report_take":
            success = await db.take_report(rep_id, user_id)
            text = "Репорт взят" if success else "Не удалось взять (возможно уже взят)"
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": text}
            )
            return

        if action == "report_close":
            success, close_msg = await db.close_report(rep_id, user_id)
            if success:
                try:
                    await bp.api.messages.send(
                        peer_id=report_chat_id,
                        message=f"Ваш тикет {rep_id} был закрыт агентом поддержки.",
                        random_id=0
                    )
                except Exception:
                    pass
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Репорт закрыт" if success else close_msg}
            )
            return

        status_emoji = {"pending": "⏳", "in_progress": "🔄", "answered": "✅", "closed": "🔒"}
        report_chat_title = await get_chat_title(report_chat_id)
        await bp.api.messages.send(
            peer_id=peer_id,
            message=(
                f"{status_emoji.get(status, '❓')} Тикет {rep_id}\n"
                f"👤 [id{report_user_id}|Пользователь]\n"
                f"📍 Чат: {report_chat_title} ({report_chat_id})\n"
                f"📄 {(report_text or '')[:250]}"
            ),
            keyboard=build_report_keyboard(rep_id),
            random_id=0
        )
        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": f"Статус: {status}"}
        )
        return

    # Обработка снятия варна
    if action == "unwarn":
        target_user_id = payload.get("user_id")
        count = payload.get("count", 1)

        if target_user_id:
            target_user_id = int(target_user_id)
            
            # Проверяем, есть ли варны
            warnings_count = await db.get_warnings_count(peer_id, target_user_id)
            if warnings_count == 0:
                await bp.api.messages.send_message_event_answer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data={"type": "show_snackbar", "text": "У пользователя нет предупреждений"}
                )
                return
            
            # Снимаем указанное количество варнов
            await db.remove_warnings_count(peer_id, target_user_id, count)

            user_mention = f"[id{target_user_id}|Пользователь]"
            admin_mention = f"[id{user_id}|Администратор]"
            
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": f"Снято {count} варн(а)!"}
            )
            
            # Удаляем сообщение с клавиатурой
            try:
                obj_msg = event.get("object", {})
                msg_id = obj_msg.get("message_id")
                if msg_id:
                    await bp.api.messages.delete(
                        message_ids=[msg_id],
                        peer_id=peer_id,
                        delete_for_all=True
                    )
            except Exception:
                pass
            
            await bp.api.messages.send(
                peer_id=peer_id,
                message=f"✅ {admin_mention} снял {count} предупреждение(й) с {user_mention}",
                random_id=0
            )
        return

    # Обработка разбана
    if action == "unban":
        target_user_id = payload.get("user_id")
        if target_user_id:
            target_user_id = int(target_user_id)

            if not await db.is_banned(peer_id, target_user_id):
                user_mention = f"[id{target_user_id}|Пользователь]"
                await bp.api.messages.send_message_event_answer(
                    event_id=event_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    event_data={"type": "show_snackbar", "text": f"Пользователь {user_mention} не забанен"}
                )
                return
            
            await db.remove_ban(peer_id, target_user_id)
            
            user_mention = f"[id{target_user_id}|Пользователь]"
            admin_mention = f"[id{user_id}|Администратор]"
            
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Пользователь разбанен!"}
            )
            
            await bp.api.messages.send(
                peer_id=peer_id,
                message=f"✅ {admin_mention} разбанил пользователя {user_mention}",
                random_id=0
            )
        return
    
    # Обработка отключения уведомлений
    if action == "disable_notify":
        await db.set_notify(peer_id, False)

        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": "🔕 Уведомления отключены!"}
        )
        
        await bp.api.messages.send(
            peer_id=peer_id,
            message="🔕 Вы отключили уведомления от бота. Используйте /notify для включения.",
            random_id=0
        )
        return
    
    # Обработка пагинации /groupall
    if action == "groupall_page":
        page = payload.get("page", 1)
        import math

        all_chats = await db.get_all_chats()
        if not all_chats:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Нет бесед"}
            )
            return
    
        per_page = 5
        total_pages = math.ceil(len(all_chats) / per_page)

        if page < 1:
            page = 1
        if page > total_pages:
            page = total_pages
        
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        chats_page = all_chats[start_idx:end_idx]
        
        result_text = f"📋 Список бесед с ботом ({len(all_chats)} всего, стр. {page}/{total_pages}):\n\n"
        
        for chat_id in chats_page:
            try:
                conv = await bp.api.messages.get_conversations_by_id(peer_ids=[chat_id])
                if conv.items:
                    chat_title = conv.items[0].chat_settings.title if hasattr(conv.items[0], 'chat_settings') else "Беседа"
                else:
                    chat_title = "Беседа"
                
                members = await bp.api.messages.get_conversation_members(peer_id=chat_id)
                member_count = len(members.items) if members.items else 0
                
                invite_link = "нет"
                try:
                    link_info = await bp.api.messages.get_invite_link(peer_id=chat_id, reset=0)
                    if link_info:
                        invite_link = link_info.link
                except Exception:
                    pass
                
                result_text += f"📌 {chat_title}\n"
                result_text += f"   🔗 Ссылка: {invite_link}\n"
                result_text += f"   🆔 Peer ID: {chat_id}\n"
                result_text += f"   👥 Участников: {member_count}\n\n"
            except Exception as e:
                logger.error(f"Ошибка при получении инфы о чате {chat_id}: {e}")
                result_text += f"📌 Беседа {chat_id}\n   🆔 Peer ID: {chat_id}\n   ⚠️ Ошибка получения данных\n\n"
        
        keyboard = Keyboard(inline=True)
        if total_pages > 1:
            if page > 1:
                keyboard.add(Callback("◀ Назад", payload={"action": "groupall_page", "page": page - 1}))
            if page < total_pages:
                keyboard.add(Callback("Вперёд ▶", payload={"action": "groupall_page", "page": page + 1}))
        
        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": f"Страница {page}"}
        )
        
        keyboard_json = keyboard.get_json()
        await bp.api.messages.send(
            peer_id=peer_id,
            message=result_text,
            keyboard=keyboard_json if total_pages > 1 else None,
            random_id=0
        )
        return
    
    # Обработка кнопок /admins (Имена/Ники)
    if action in {"admins_view", "admins_refresh"}:
        view_type = payload.get("view", "nicknames")
        if action == "admins_refresh":
            view_type = "nicknames"  # сброс на ники при обновлении

        peer_id = obj.get("peer_id")
        if not peer_id:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Ошибка: не найден чат"}
            )
            return
    
        # Проверяем доступ (приоритет 75)
        user_role = await db.get_user_role(peer_id, user_id)
        user_priority = user_role[1] if user_role else 0
        if user_priority < 0:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Нет доступа "}
            )
            return
    
        admins = await db.get_admins(peer_id)
        if not admins:
            await bp.api.messages.send_message_event_answer(
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data={"type": "show_snackbar", "text": "Нет админов"}
            )
            return

        # Получаем данные о пользователях
        user_ids = [user_id for user_id, _, _ in admins]
        try:
            users_info = await bp.api.users.get(user_ids=user_ids)
            users_map = {u.id: f"{u.first_name} {u.last_name}" for u in users_info}
        except Exception:
            users_map = {uid: "Пользователь" for uid in user_ids}

        # Формируем текст
        current_priority = None
        admins_text = f"👥 Администраторы беседы ({'Ники' if view_type == 'nicknames' else 'Имена'}):\n\n"

        for admin_user_id, role_name, priority in admins:
            if priority != current_priority:
                admins_text += f"\n🏆 {role_name} (приоритет: {priority}):\n"
                current_priority = priority

            if view_type == "names":
                vk_name = users_map.get(admin_user_id, "Пользователь")
                display_name = sanitize_plain_name(vk_name)
                admins_text += f"▪️ [vk.com/id{admin_user_id}|{display_name}]\n"
            else:
                custom_nick = await db.get_nickname(peer_id, admin_user_id)
                display_name = sanitize_plain_name(custom_nick) if custom_nick else sanitize_plain_name(users_map.get(admin_user_id, "Пользователь"))
                admins_text += f"▪️ [vk.com/id{admin_user_id}|{display_name}]\n"

        # Клавиатура
        keyboard = Keyboard(inline=True).row()
        keyboard.add(
            Callback("👤 Имена", payload={"action": "admins_view", "view": "names"}),
            color=KeyboardButtonColor.SECONDARY if view_type == "nicknames" else KeyboardButtonColor.PRIMARY
        )
        keyboard.add(
            Callback("🔖 Ники", payload={"action": "admins_view", "view": "nicknames"}),
            color=KeyboardButtonColor.PRIMARY if view_type == "nicknames" else KeyboardButtonColor.SECONDARY
        )
        keyboard.row()
        keyboard.add(Callback("🔄 Обновить", payload={"action": "admins_refresh"}))

        keyboard_json = keyboard.get_json()

        # Редактируем сообщение
        try:
            await bp.api.messages.edit(
                peer_id=peer_id,
                conversation_message_id=event_cmid,
                message=admins_text,
                keyboard=keyboard_json
            )
        except Exception:
            await bp.api.messages.send(
                peer_id=peer_id,
                message=admins_text,
                keyboard=keyboard_json,
                random_id=0
            )

        await bp.api.messages.send_message_event_answer(
            event_id=event_id,
            user_id=user_id,
            peer_id=peer_id,
            event_data={"type": "show_snackbar", "text": f"Показываем: {'Ники' if view_type == 'nicknames' else 'Имена'}"}
        )
        return
    

# ==================== Обработчик команд без доступа ====================
class CommandCheckRule(rules.ABCRule[Message]):
    """Проверяет есть ли доступ к команде и отвечает если нет"""
    
    async def check(self, message: Message) -> bool:
        text = normalize_command_text(message.text).lower()
        if not text:
            return True
        
        # Проверяем только команды
        if not text.startswith('/') and not text.startswith('!'):
            return True  # Не команда - пропускаем
        
        # Получаем имя команды
        if text.startswith('/'):
            cmd = text.split()[0][1:]  # убираем /
        elif text.startswith('!'):
            cmd = text.split()[0][1:]  # убираем !
        else:
            return True

        # Системные команды бота (работают без чат-ролей) - пропускаем сразу
        support_cmds = {
            "report", "tickets", "ans", "answer", "vreport", "creport", "reports",
            "banreport", "unbanreport",
            "stats", "q"
        }
        if cmd in support_cmds:
            return True
        
        # Проверяем заблокированность в боте
        peer_id = message.peer_id
        user_id = message.from_id
        if await db.is_sysbanned(user_id):
            await message.answer("❌ Вы заблокированы в боте")
            return False
        
        # Для бесед: сначала проверяем мут и режим тишины
        if peer_id >= 2000000000:
            if await db.is_muted(peer_id, user_id):
                cmid = getattr(message, "conversation_message_id", None)
                if isinstance(cmid, int):
                    await delete_by_cmid(peer_id, cmid)
                return False

            if await is_silent_blocked_message(message):
                return False

        # Команда /settings доступна с приоритета 90
        if cmd == "settings":
            if peer_id < 2000000000:
                return True
            role = await db.get_user_role(peer_id, user_id)
            if not role or role[1] < 90:
                await message.answer("❌ Эта команда доступна с приоритета 90")
                return False
            return True
        
        # Личные сообщения - пропускаем
        if peer_id < 2000000000:
            return True

        # Проверяем владельца бота
        if user_id in BOT_OWNER_IDS:
            return True

        # Проверяем статус админа
        status = await db.get_chat_status(peer_id)
        has_admin = bool(status) if status is not None else False
        if not has_admin:
            return True  # Проверит другой обработчик
        
        # Получаем роль пользователя
        user_role = await db.get_user_role(peer_id, user_id)
        if not user_role:
            await message.answer("❌ У вас нет роли в этом чате")
            return False

        user_priority = user_role[1]
        
        # Проверяем приоритет из БД
        cmd_priority = await db.get_cmd_priority(cmd)
        
        # Определяем минимальный приоритет для команды
        min_priority = cmd_priority if cmd_priority is not None else 0
        
        # Проверяем команды с дефолтными приоритетами
        default_cmds = {
            'stats': 10, 'warn': 10, 'warnhistory': 10,
            'ban': 30, 'unban': 30, 'unwarn': 30, 'snick': 30, 'rnick': 30, 'nlist': 30, 'banlist': 30,
            'roles': 40, 'role': 40,
            'newrole': 75, 'delrole': 75, 'admins': 75,
            'addowner': 100, 'delowner': 100, 'zov': 100,
            'silent': 30, 'ban': 40, 'unban': 40, 'pin': 75, 'unpin': 75,
            'setcmd': 100, 'settings': 90, 'cmdlist': 90
        }
        
        if cmd in default_cmds:
            min_priority = default_cmds[cmd]
        
        if user_priority < min_priority:
            await message.answer(f"❌ Требуется приоритет {min_priority}")
            return False
        
        return True

        # Проверяем только команды
        if not text.startswith('/') and not text.startswith('!'):
            return True  # Не команда - пропускаем
        
        # Получаем имя команды
        if text.startswith('/'):
            cmd = text.split()[0][1:]  # убираем /
        elif text.startswith('!'):
            cmd = text.split()[0][1:]  # убираем !
        else:
            return True

        # Системные команды бота (работают без чат-ролей) - проверяем СРАЗУ
        support_cmds = {
            "report", "tickets", "ans", "answer", "vreport", "creport", "reports",
            "banreport", "unbanreport", "stats", "q"
        }
        if cmd in support_cmds:
            return True
        
        # Для остальных команд проверяем sysban
        peer_id = message.peer_id
        user_id = message.from_id
        if await db.is_sysbanned(user_id):
            await message.answer("❌ Вы заблокированы в боте")
            return False
        
        # Для бесед: проверяем мут и режим тишины
        if peer_id >= 2000000000:
            if await db.is_muted(peer_id, user_id):
                cmid = getattr(message, "conversation_message_id", None)
                if isinstance(cmid, int):
                    await delete_by_cmid(peer_id, cmid)
                return False

            if await is_silent_blocked_message(message):
                return False
        
        # Команда /settings доступна с приоритета 90
        if cmd == "settings":
            if peer_id < 2000000000:
                return True
            role = await db.get_user_role(peer_id, user_id)
            if not role or role[1] < 90:
                await message.answer("❌ Эта команда доступна с приоритета 90")
                return False
            return True
        
        # Личные сообщения - пропускаем
        if peer_id < 2000000000:
            return True

        # Проверяем владельца бота
        if user_id in BOT_OWNER_IDS:
            return True

        # Проверяем статус админа
        status = await db.get_chat_status(peer_id)
        has_admin = bool(status) if status is not None else False
        if not has_admin:
            return True  # Проверит другой обработчик
        
        # Получаем роль пользователя
        user_role = await db.get_user_role(peer_id, user_id)
        if not user_role:
            await message.answer("❌ У вас нет роли в этом чате")
            return False

        user_priority = user_role[1]
        
        # Проверяем приоритет из БД
        cmd_priority = await db.get_cmd_priority(cmd)
        
        # Определяем минимальный приоритет для команды
        min_priority = cmd_priority if cmd_priority is not None else 0
        
        # Проверяем команды с дефолтными приоритетами
        default_cmds = {
            'stats': 10, 'warn': 10, 'warnhistory': 10,
            'ban': 30, 'unban': 30, 'unwarn': 30, 'snick': 30, 'rnick': 30, 'nlist': 30, 'banlist': 30,
            'roles': 40, 'role': 40,
            'newrole': 75, 'delrole': 75, 'admins': 75,
            'addowner': 100, 'delowner': 100, 'zov': 100,
            'silent': 30, 'ban': 40, 'unban': 40, 'pin': 75, 'unpin': 75,
            'setcmd': 100, 'settings': 90, 'cmdlist': 90
        }
        
        if cmd in default_cmds:
            min_priority = default_cmds[cmd]
        
        if user_priority < min_priority:
            await message.answer(f"❌ Нет доступа. Требуется приоритет: {min_priority}")
            return False
        
        return True


# Удалён пустой message-handler c CommandCheckRule: он перехватывал команды
# (включая /report) и мешал исполнению профильных хендлеров.


class NonCommandRule(rules.ABCRule[Message]):
    """Пропускает только некомандные сообщения."""

    async def check(self, message: Message) -> bool:
        text = normalize_command_text(message.text).lower()
        if not text:
            return True
        return not (text.startswith("/") or text.startswith("!"))


# ==================== Обработчик сообщений без прав ====================
@bp.on.message(NonCommandRule())
async def no_access_handler(message: Message) -> None:
    """Обработчик для чатов без прав админа"""
    peer_id = message.peer_id
    
    # Если это личные сообщения - пропускаем
    if peer_id < 2000000000:
        return
    
    # Проверяем статус админа
    status = await db.get_chat_status(peer_id)
    has_admin = bool(status) if status is not None else False
    
    if not has_admin:
        await message.answer("⚠️ Выдайте права администратора для использования бота.")
        return

    user_id = message.from_id
    
    # Проверяем, не замучен ли пользователь
    if await db.is_muted(peer_id, user_id):
        cmid = getattr(message, 'conversation_message_id', None)
        if isinstance(cmid, int):
            await delete_by_cmid(peer_id, cmid)
        return
    
    # Режим тишины: пользователи с приоритетом 0..10 писать не могут
    if await is_silent_blocked_message(message):
        return


# ==================== Таблица для банов репортов ====================
# Добавим методы для работы с банами репортов в класс Database
async def add_report_ban(self, user_id: int, duration_minutes: int = -1, reason: str = "") -> None:
    """Забанить пользователя в репортах (duration_minutes: -1 = навсегда)."""
    duration = int(duration_minutes)
    banned_until = None
    if duration >= 0:
        banned_until = (datetime.now() + timedelta(minutes=duration)).isoformat()

    async with aiosqlite.connect(self.db_name) as db:
        await db.execute(
            """INSERT INTO report_bans (user_id, reason, duration_minutes, banned_until)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   reason = excluded.reason,
                   duration_minutes = excluded.duration_minutes,
                   banned_until = excluded.banned_until,
                   created_at = CURRENT_TIMESTAMP""",
            (int(user_id), str(reason or ""), duration, banned_until)
        )
        await db.commit()

async def remove_report_ban(self, user_id: int) -> None:
    """Разбанить пользователя в репортах"""
    async with aiosqlite.connect(self.db_name) as db:
        await db.execute("DELETE FROM report_bans WHERE user_id = ?", (int(user_id),))
        await db.commit()

async def is_report_banned(self, user_id: int) -> bool:
    """Проверить, забанен ли пользователь в репортах с учётом срока."""
    async with aiosqlite.connect(self.db_name) as db:
        cursor = await db.execute(
            "SELECT banned_until FROM report_bans WHERE user_id = ? LIMIT 1",
            (int(user_id),)
        )
        row = await cursor.fetchone()
        if not row:
            return False

        banned_until = row[0]
        if not banned_until:
            return True

        try:
            end_dt = datetime.fromisoformat(str(banned_until))
        except Exception:
            return True

        if end_dt <= datetime.now():
            await db.execute("DELETE FROM report_bans WHERE user_id = ?", (int(user_id),))
            await db.commit()
            return False

        return True

# Добавляем методы в класс Database
Database.add_report_ban = add_report_ban
Database.remove_report_ban = remove_report_ban
Database.is_report_banned = is_report_banned


# ==================== Команда /report (для пользователей) ====================
class ReportRule(rules.ABCRule[Message]):
    """Правило для команды /report - работает в беседах и ЛС"""

    async def check(self, message: Message) -> bool:
        # Репорт доступен везде: беседы и личные сообщения боту
        if extract_command_payload(message.text, "report") is not None:
            return True
        # Доп. алиасы
        if extract_command_payload(message.text, "репорт") is not None:
            return True
        if extract_command_payload(message.text, "жалоба") is not None:
            return True
        if extract_command_payload(message.text, "creport") is not None:
            return True
        return False


async def is_staff_user(user_id: int) -> bool:
    if user_id in BOT_OWNER_IDS:
        return True
    if await db.is_bot_owner(user_id):
        return True
    if await db.is_bot_leader(user_id):
        return True
    if await db.is_bot_admin(user_id):
        return True
    if await db.is_bot_moderator(user_id):
        return True
    if await db.is_bot_helper(user_id):
        return True
    return False


SYSTEM_COMMANDS = {
    # репорты/тикеты
    "tickets", "vreport", "ans", "answer", "creport", "reports",
    # экономика (системные)
    "givemoney", "setmoney", "giveruletka",
    # системные команды владельца/админов бота
    "sysban", "unsysban", "sysrole", "sysnewrole",
    "addownerbot", "delownerbot",
    "addrukbot", "delrukbot",
    "addadminbot", "deladminbot",
    "addmoderbot", "delmoderbot",
    "addhelper", "delhelper",
    "groupall", "news",
}


async def has_system_command_access(user_id: int, command: str) -> bool:
    cmd = (command or "").strip().lower().lstrip("/").lstrip("!")
    if user_id in BOT_OWNER_IDS:
        return True
    if await is_staff_user(user_id):
        return True
    return await db.has_system_cmd_access(user_id, cmd)


async def get_chat_title(peer_id: int) -> str:
    if peer_id < 2000000000:
        return "ЛС"

    try:
        conv = await bp.api.messages.get_conversations_by_id(peer_ids=[peer_id])
        if conv.items and hasattr(conv.items[0], "chat_settings") and conv.items[0].chat_settings:
            title = conv.items[0].chat_settings.title
            if title:
                return str(title)
    except Exception:
        pass

    return f"Беседа {peer_id}"


def format_ticket_source(peer_id: int) -> str:
    return f"Беседа {peer_id}" if peer_id >= 2000000000 else "ЛС"


def build_ticket_line(ticket_id: int, user_id: int, peer_id: int, text: str, status: str, created_at: str) -> str:
    snippet = (text or "").replace("\n", " ").strip()
    short = snippet[:90] + ("..." if len(snippet) > 90 else "")
    return (
        f"🆔 #{ticket_id} [{status}]\n"
        f"👤 [id{user_id}|Пользователь] ({format_ticket_source(peer_id)})\n"
        f"📄 {short}\n"
        f"📅 {created_at}\n\n"
    )


async def get_report_recipients() -> set[int]:
    recipients: set[int] = set(BOT_OWNER_IDS)
    try:
        owners = await db.get_bot_owners()
        for owner_id, _ in owners:
            recipients.add(int(owner_id))
    except Exception:
        pass
    try:
        async with aiosqlite.connect(db.db_name) as db_conn:
            cursor = await db_conn.execute("SELECT user_id FROM bot_leaders")
            for row in await cursor.fetchall():
                recipients.add(int(row[0]))
    except Exception:
        pass
    try:
        async with aiosqlite.connect(db.db_name) as db_conn:
            cursor = await db_conn.execute("SELECT user_id FROM bot_admins")
            for row in await cursor.fetchall():
                recipients.add(int(row[0]))
    except Exception:
        pass
    try:
        async with aiosqlite.connect(db.db_name) as db_conn:
            cursor = await db_conn.execute("SELECT user_id FROM bot_moderators")
            for row in await cursor.fetchall():
                recipients.add(int(row[0]))
    except Exception:
        pass
    try:
        helpers = await db.get_all_helpers()
        for helper_id, _ in helpers:
            recipients.add(int(helper_id))
    except Exception:
        pass
    return recipients


def build_report_keyboard(report_id: str) -> str:
    keyboard = (
        Keyboard(inline=True)
        .row()
        .add(Callback("🔄 Взять", payload={"action": "report_take", "report_id": report_id}), color=KeyboardButtonColor.POSITIVE)
        .add(Callback("🔒 Закрыть", payload={"action": "report_close", "report_id": report_id}), color=KeyboardButtonColor.NEGATIVE)
        .add(Callback("♻️ Обновить", payload={"action": "report_refresh", "report_id": report_id}), color=KeyboardButtonColor.SECONDARY)
    )
    return keyboard.get_json()


async def send_report_to_support(message_text: str, report_id: str) -> None:
    """Отправляет репорт в поддержку (HELP_GROUP_ID)."""
    keyboard_json = build_report_keyboard(report_id)
    support_peer_id = HELP_GROUP_ID if int(HELP_GROUP_ID) >= 2000000000 else 2000000000 + int(HELP_GROUP_ID)

    try:
        await bp.api.messages.send(
            peer_id=support_peer_id,
            message=message_text,
            keyboard=keyboard_json,
            random_id=0
        )
    except Exception as e:
        logger.error(f"Не удалось отправить репорт в support group (peer_id={support_peer_id}): {e}")

@bp.on.message(ReportRule())
async def report_handler(message: Message) -> None:
    """Создать репорт или дополнить активный."""
    user_id = message.from_id
    chat_id = message.peer_id
    
    # Проверяем, не забанен ли пользователь в репортах
    if await db.is_report_banned(user_id):
        await message.answer("❌ Вы заблокированы от отправки репортов")
        return
    
    report_text = (
        extract_command_payload(message.text, "report")
        or extract_command_payload(message.text, "репорт")
        or extract_command_payload(message.text, "жалоба")
        or extract_command_payload(message.text, "creport")
        or ""
    )
    
    if not report_text:
        await message.answer(
            "📋 Использование: /report [текст]\n"
            "Работает и в беседах, и в ЛС боту.\n"
            "Пример: /report Проблема с ботом"
        )
        return

    active_report_id = await db.get_active_report(user_id, chat_id)
    if active_report_id:
        await db.append_to_report(active_report_id, report_text)
        await message.answer(f"✅ Тикет {active_report_id} успешно дополнен.")
        chat_title = await get_chat_title(chat_id)
        notify = (
            f"📢 ТИКЕТ ДОПОЛНИЛИ {active_report_id}\n\n"
            f"👤 От: [id{user_id}|Пользователь]\n"
            f"📍 Чат: {chat_title} ({chat_id})\n"
            f"💬 Дополнение: {report_text}"
        )
    else:
        new_report_id = await db.create_report(user_id, chat_id, report_text)
        await message.answer(f"✅ Тикет {new_report_id} успешно создан.")
        chat_title = await get_chat_title(chat_id)
        notify = (
            f"📢 НОВЫЙ ТИКЕТ {new_report_id}\n\n"
            f"👤 От: [id{user_id}|Пользователь]\n"
            f"📍 Чат: {chat_title} ({chat_id})\n"
            f"💬 Текст: {report_text}\n\n"
            f"🔹 Взять: /vreport {new_report_id}"
        )

    target_report_id = active_report_id if active_report_id else new_report_id
    await send_report_to_support(notify, target_report_id)


# ==================== Проверка подписки при входе в беседу ====================
@bp.on.raw_event(GroupEventType.MESSAGE_NEW)
async def check_subscription_on_join(event: dict) -> None:
    """Проверяет подписку на сообщество при входе пользователя в беседу"""
    try:
        obj = event.get("object", {}) or {}
        message_data = obj.get("message", {})
        
        # Проверяем, есть ли action (событие в чате)
        action = message_data.get("action")
        if not action:
            return
    
        action_type = action.get("type")
        if action_type != "chat_invite_user":
            return
    
        # Получаем ID пользователя, который присоединился
        member_id = action.get("member_id")
        if not member_id or member_id <= 0:
            return
        
        # Получаем peer_id чата
        peer_id = message_data.get("peer_id")
        if not peer_id or peer_id < 2000000000:
            return
        
        # Проверяем, настроена ли проверка подписки для этого чата
        community_id = await db.get_sub_community(peer_id)
        if not community_id:
            return  # Проверка не настроена
        
        # Проверяем подписку пользователя на сообщество
        try:
            is_subscribed = await bp.api.groups.is_member(group_id=community_id, user_id=member_id)
            if not is_subscribed:
                # Пользователь не подписан - отправляем сообщение и кикаем
                user_mention = f"[id{member_id}|Пользователь]"
                
                # Получаем информацию о сообществе
                try:
                    community_info = await bp.api.groups.get_by_id(group_ids=[community_id])
                    community_name = community_info.groups[0].name if community_info and community_info.groups else "сообщество"
                    community_link = f"https://vk.com/club{community_id}"
                except Exception:
                    community_name = "сообщество"
                    community_link = f"https://vk.com/club{community_id}"
                
                await bp.api.messages.send(
                    peer_id=peer_id,
                    message=f"⚠️ {user_mention}, вы не подписаны на {community_name}!\n"
                            f"Подпишитесь, чтобы остаться в чате: {community_link}",
                    random_id=0
                )
                
                # Кикаем пользователя из чата
                try:
                    await bp.api.messages.remove_chat_user(
                        chat_id=peer_id - 2000000000,
                        member_id=member_id
                    )
                except Exception as e:
                    logger.error(f"Не удалось кикнуть пользователя {member_id}: {e}")
                    
        except Exception as e:
            logger.error(f"Ошибка при проверке подписки: {e}")
            
    except Exception as e:
        logger.error(f"Ошибка в check_subscription_on_join: {e}")


async def setup() -> None:
    """Функция инициализации бота"""
    # Читаем токен из config.json
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r", encoding="utf-8-sig") as f:
        config = json.load(f)
    
    token = config.get("vk_token")
    if not token or token == "YOUR_VK_TOKEN_HERE":
        raise ValueError("Укажите токен в config.json (поле vk_token)")
    
    # Инициализируем базу данных
    await db.init_db()
    logger.info("База данных инициализирована")
    
    # Создаём API и Bot с отключенной проверкой SSL
    ssl_context = aiohttp.TCPConnector(ssl=False)
    http_client = AiohttpClient(connector=ssl_context)
    api = API(token=token, http_client=http_client)
    bot = Bot(api=api)
    
    # Получаем ID группы автоматически через API
    try:
        groups_info = await api.groups.get_by_id()
        if groups_info.groups:
            group_id = groups_info.groups[0].id
            CtxStorage().set("group_id", group_id)
            logger.info(f"ID группы автоматически определён: {group_id}")
        else:
            raise ValueError("Не удалось получить информацию о группе")
    except Exception as e:
        raise ValueError(f"Ошибка при получении ID группы: {e}")
    
    # Подключаем blueprint
    bp.load(bot)
    
    logger.info("Бот настроен, запускаем polling...")
    return bot
    

async def main() -> None:
    """Главная функция запуска бота"""
    # Читаем токен из config.json
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r", encoding="utf-8-sig") as f:
        config = json.load(f)
    
    token = config.get("vk_token")
    
    if not token or token == "YOUR_VK_TOKEN_HERE":
        raise ValueError("Укажите токен в config.json (поле vk_token)")
    
    # Инициализируем базу данных
    await db.init_db()
    logger.info("База данных инициализирована")
    
    # Создаём API и Bot с отключенной проверкой SSL
    ssl_context = aiohttp.TCPConnector(ssl=False)
    http_client = AiohttpClient(connector=ssl_context)
    api = API(token=token, http_client=http_client)
    bot = Bot(api=api)
    
    # Получаем ID группы автоматически через API
    try:
        groups_info = await api.groups.get_by_id()
        if groups_info.groups:
            group_id = groups_info.groups[0].id
            CtxStorage().set("group_id", group_id)
            logger.info(f"ID группы автоматически определён: {group_id}")
        else:
            raise ValueError("Не удалось получить информацию о группе")
    except Exception as e:
        raise ValueError(f"Ошибка при получении ID группы: {e}")
    

    # Подключаем blueprint
    bp.load(bot)
    
    logger.info("Бот настроен, запускаем polling...")
    
    # Запускаем polling вручную через loop_wrapper
    from vkbottle.polling import BotPolling
    
    polling = BotPolling(api)
    
    async for event in polling.listen():
        for update in event.get("updates", []):
            asyncio.create_task(bot.router.route(update, api))


# ==================== Универсальный обработчик команд без доступа ====================
class AnyCommandRule(rules.ABCRule[Message]):
    """Правило для любой команды"""
    
    async def check(self, message: Message) -> bool:
        text = (message.text or "").strip()
        if not text:
            return False
        return text.startswith("/") or text.startswith("!")


@bp.on.message(AnyCommandRule(), IsAdminRule())
async def command_no_access_handler(message: Message) -> None:
    """Обработчик команд без доступа - отвечает когда команда отклонена"""
    text = message.text.strip().lower()
    
    # Извлекаем имя команды
    if text.startswith("/"):
        cmd = text.split()[0][1:]
    elif text.startswith("!"):
        cmd = text.split()[0][1:]
    else:
        return
    
    # Системные команды, которые не требуют проверки прав
    system_cmds = {
        "report", "tickets", "ans", "answer", "vreport", "creport", "reports",
        "banreport", "unbanreport", "stats", "q", "help"
    }
    if cmd in system_cmds:
        return  # Эти команды обрабатываются отдельно
    
    peer_id = message.peer_id
    user_id = message.from_id
    
    # Проверяем статус админа
    status = await db.get_chat_status(peer_id)
    has_admin = bool(status) if status is not None else False
    
    if not has_admin:
        await message.answer("⚠️ Бот не имеет прав администратора в этом чате.")
        return
    
    # Получаем роль пользователя
    user_role = await db.get_user_role(peer_id, user_id)
    if not user_role:
        return  # Молча игнорируем - нет роли
    
    user_priority = user_role[1]
    
    # Проверяем приоритет из БД
    cmd_priority = await db.get_cmd_priority(cmd)
    
    # Дефолтные приоритеты команд
    default_cmds = {
        'stats': 10, 'warn': 10, 'warnhistory': 10,
        'ban': 30, 'unban': 30, 'unwarn': 30, 'snick': 30, 'rnick': 30, 'nlist': 30, 'banlist': 30,
        'roles': 40, 'role': 40,
        'newrole': 75, 'delrole': 75, 'admins': 75,
        'addowner': 100, 'delowner': 100, 'zov': 100,
        'silent': 30, 'pin': 75, 'unpin': 75,
        'setcmd': 100, 'settings': 90, 'cmdlist': 90
    }
    
    min_priority = cmd_priority if cmd_priority is not None else default_cmds.get(cmd, 0)
    
    if user_priority < min_priority:
        await message.answer(f"❌ Нет доступа! Требуется приоритет: {min_priority}")


# ==================== Обработчик ввода страницы для /groupall ====================
import time as time_module

@bp.on.message(NonCommandRule())
async def groupall_page_input_handler(message: Message) -> None:
    """Обрабатывает ввод номера страницы для /groupall"""
    peer_id = message.peer_id
    user_id = message.from_id
    
    # Проверяем, есть ли ожидающий ввод для этого чата
    if peer_id not in groupall_pending_input:
        return
    
    pending = groupall_pending_input[peer_id]
    
    # Проверяем, что время не истекло
    if time_module.time() > pending["timeout_at"]:
        # Время вышло, удаляем ожидание
        del groupall_pending_input[peer_id]
        return
    
    # Проверяем, что это тот же пользователь
    if pending["user_id"] != user_id:
        return
    
    # Проверяем, что сообщение - это число
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("❌ Пожалуйста, введите число (номер страницы)")
        return
    
    page = int(text)
    
    # Удаляем ожидание ввода
    del groupall_pending_input[peer_id]
    
    # Получаем все чаты и показываем нужную страницу
    import math
    all_chats = await db.get_all_chats()
    
    if not all_chats:
        await message.answer("📋 Бот не состоит ни в одной беседе")
        return

    per_page = 5
    total_pages = math.ceil(len(all_chats) / per_page)

    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages
    
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    chats_page = all_chats[start_idx:end_idx]
    
    result_text = f"📋 Список бесед с ботом ({len(all_chats)} всего, стр. {page}/{total_pages}):\n\n"
    
    for chat_id in chats_page:
        try:
            conv = await bp.api.messages.get_conversations_by_id(peer_ids=[chat_id])
            if conv.items:
                chat_title = conv.items[0].chat_settings.title if hasattr(conv.items[0], 'chat_settings') else "Беседа"
            else:
                chat_title = "Беседа"
            
            members = await bp.api.messages.get_conversation_members(peer_id=chat_id)
            member_count = len(members.items) if members.items else 0
            
            invite_link = "нет"
            try:
                link_info = await bp.api.messages.get_invite_link(peer_id=chat_id, reset=0)
                if link_info:
                    invite_link = link_info.link
            except Exception:
                pass
            
            result_text += f"📌 {chat_title}\n"
            result_text += f"   🔗 Ссылка: {invite_link}\n"
            result_text += f"   🆔 Peer ID: {chat_id}\n"
            result_text += f"   👥 Участников: {member_count}\n\n"
        except Exception as e:
            logger.error(f"Ошибка при получении инфы о чате {chat_id}: {e}")
            result_text += f"📌 Беседа {chat_id}\n   🆔 Peer ID: {chat_id}\n   ⚠️ Ошибка получения данных\n\n"
    
    keyboard = Keyboard(inline=True)
    if total_pages > 1:
        if page > 1:
            keyboard.add(Callback("◀ Назад", payload={"action": "groupall_page", "page": page - 1}))
        if page < total_pages:
            keyboard.add(Callback("Вперёд ▶", payload={"action": "groupall_page", "page": page + 1}))
        keyboard.row()
        keyboard.add(Callback("🔢 Ввести страницу", payload={"action": "groupall_input_page"}))
    
    keyboard_json = keyboard.get_json()
    await message.answer(result_text, keyboard=keyboard_json if total_pages > 1 else None)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")   