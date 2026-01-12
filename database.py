from __future__ import annotations  # ← Добавить эту строку в самое начало!

import aiosqlite
import os
from datetime import datetime
from typing import Optional, List
from dataclasses import dataclass

import config


@dataclass
class Server:
    """Модель сервера"""
    id: int
    name: str
    host: str
    port: int
    protocol: str  # tcp/udp
    is_active: bool
    created_at: datetime
    last_check: Optional[datetime] = None
    last_status: bool = True
    consecutive_failures: int = 0
    notification_sent: bool = False
    total_checks: int = 0
    total_failures: int = 0


class Database:
    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = db_path
        
    async def init(self):
        """Инициализация базы данных"""
        # Создаём директорию если не существует
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS servers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    protocol TEXT DEFAULT 'tcp',
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_check TIMESTAMP,
                    last_status BOOLEAN DEFAULT 1,
                    consecutive_failures INTEGER DEFAULT 0,
                    notification_sent BOOLEAN DEFAULT 0,
                    total_checks INTEGER DEFAULT 0,
                    total_failures INTEGER DEFAULT 0,
                    UNIQUE(host, port)
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS check_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id INTEGER,
                    is_available BOOLEAN,
                    response_time REAL,
                    error TEXT,
                    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER UNIQUE,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await db.commit()
    
    async def add_server(self, name: str, host: str, port: int, protocol: str = "tcp") -> Optional[int]:
        """Добавить сервер"""
        async with aiosqlite.connect(self.db_path) as db:
            try:
                cursor = await db.execute(
                    "INSERT INTO servers (name, host, port, protocol) VALUES (?, ?, ?, ?)",
                    (name, host, port, protocol.lower())
                )
                await db.commit()
                return cursor.lastrowid
            except aiosqlite.IntegrityError:
                return None
    
    async def remove_server(self, server_id: int) -> bool:
        """Удалить сервер"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM servers WHERE id = ?", (server_id,))
            await db.commit()
            return cursor.rowcount > 0
    
    async def get_server(self, server_id: int) -> Optional[Server]:
        """Получить сервер по ID"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM servers WHERE id = ?", (server_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return self._row_to_server(row)
        return None
    
    async def get_all_servers(self) -> List[Server]:
        """Получить все серверы"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM servers ORDER BY id") as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_server(row) for row in rows]
    
    async def get_active_servers(self) -> List[Server]:
        """Получить активные серверы"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM servers WHERE is_active = 1 ORDER BY id") as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_server(row) for row in rows]
    
    async def update_server_status(
        self,
        server_id: int,
        is_available: bool,
        response_time: Optional[float] = None,
        error: Optional[str] = None
    ):
        """Обновить статус сервера после проверки"""
        async with aiosqlite.connect(self.db_path) as db:
            # Получаем текущие данные
            async with db.execute(
                "SELECT consecutive_failures, notification_sent, total_checks, total_failures FROM servers WHERE id = ?",
                (server_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return
                
                consecutive_failures, notification_sent, total_checks, total_failures = row
            
            # Обновляем счётчики
            if is_available:
                new_consecutive_failures = 0
            else:
                new_consecutive_failures = consecutive_failures + 1
                total_failures += 1
            
            total_checks += 1
            
            await db.execute("""
                UPDATE servers SET
                    last_check = CURRENT_TIMESTAMP,
                    last_status = ?,
                    consecutive_failures = ?,
                    total_checks = ?,
                    total_failures = ?
                WHERE id = ?
            """, (is_available, new_consecutive_failures, total_checks, total_failures, server_id))
            
            # Сохраняем в историю
            await db.execute("""
                INSERT INTO check_history (server_id, is_available, response_time, error)
                VALUES (?, ?, ?, ?)
            """, (server_id, is_available, response_time, error))
            
            await db.commit()
    
    async def set_notification_sent(self, server_id: int, sent: bool):
        """Установить флаг отправки уведомления"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE servers SET notification_sent = ? WHERE id = ?",
                (sent, server_id)
            )
            await db.commit()
    
    async def toggle_server(self, server_id: int) -> Optional[bool]:
        """Переключить активность сервера"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT is_active FROM servers WHERE id = ?", (server_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                
                new_status = not row[0]
                await db.execute(
                    "UPDATE servers SET is_active = ? WHERE id = ?",
                    (new_status, server_id)
                )
                await db.commit()
                return new_status
    
    async def add_subscriber(self, chat_id: int) -> bool:
        """Добавить подписчика на уведомления"""
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    "INSERT INTO subscribers (chat_id) VALUES (?)",
                    (chat_id,)
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                # Уже подписан, активируем
                await db.execute(
                    "UPDATE subscribers SET is_active = 1 WHERE chat_id = ?",
                    (chat_id,)
                )
                await db.commit()
                return True
    
    async def remove_subscriber(self, chat_id: int) -> bool:
        """Удалить подписчика"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE subscribers SET is_active = 0 WHERE chat_id = ?",
                (chat_id,)
            )
            await db.commit()
            return True
    
    async def get_subscribers(self) -> List[int]:
        """Получить список активных подписчиков"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT chat_id FROM subscribers WHERE is_active = 1"
            ) as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows]
    
    async def get_server_history(self, server_id: int, limit: int = 100) -> List[dict]:
        """Получить историю проверок сервера"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT * FROM check_history 
                WHERE server_id = ? 
                ORDER BY checked_at DESC 
                LIMIT ?
            """, (server_id, limit)) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
    
    async def reset_server_stats(self, server_id: int):
        """Сбросить статистику сервера"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE servers SET
                    total_checks = 0,
                    total_failures = 0,
                    consecutive_failures = 0,
                    notification_sent = 0
                WHERE id = ?
            """, (server_id,))
            await db.execute("DELETE FROM check_history WHERE server_id = ?", (server_id,))
            await db.commit()
    
    def _row_to_server(self, row) -> Server:
        """Преобразовать строку БД в объект Server"""
        return Server(
            id=row["id"],
            name=row["name"],
            host=row["host"],
            port=row["port"],
            protocol=row["protocol"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            last_check=row["last_check"],
            last_status=bool(row["last_status"]),
            consecutive_failures=row["consecutive_failures"],
            notification_sent=bool(row["notification_sent"]),
            total_checks=row["total_checks"],
            total_failures=row["total_failures"]
        )


# Глобальный экземпляр
db = Database()