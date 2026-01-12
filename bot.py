from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional, List, Callable, Dict, Any, Awaitable

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, 
    CallbackQuery, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    TelegramObject,
    BufferedInputFile,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import config
from database import db, Server
from monitor import check_server, check_ping, check_tcp_port, CheckResult
from charts import (
    generate_uptime_chart,
    generate_all_servers_chart,
    generate_weekly_chart,
    generate_realtime_status_image
)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Инициализация
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()
router = Router()

# Состояние мониторинга
monitoring_active = False


# ============= MIDDLEWARE ДЛЯ ПРОВЕРКИ ДОСТУПА =============

class AccessMiddleware(BaseMiddleware):
    """Middleware для проверки доступа к боту"""
    
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user_id = None
        
        if isinstance(event, Message):
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id
        
        if user_id and user_id not in config.ADMIN_IDS:
            logger.warning(f"⛔ Unauthorized access from user_id={user_id}")
            
            if isinstance(event, Message):
                await event.answer(
                    "⛔ <b>Доступ запрещён</b>\n\n"
                    "Этот бот работает только для авторизованных пользователей.",
                    parse_mode=ParseMode.HTML
                )
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Доступ запрещён", show_alert=True)
            
            return
        
        return await handler(event, data)


# Регистрируем middleware
router.message.middleware(AccessMiddleware())
router.callback_query.middleware(AccessMiddleware())


# ============= FSM States =============

class AddServerState(StatesGroup):
    waiting_for_name = State()
    waiting_for_host = State()
    waiting_for_port = State()
    waiting_for_protocol = State()


# ============= Вспомогательные функции =============

def is_admin(user_id: int) -> bool:
    """Проверка является ли пользователь админом"""
    return user_id in config.ADMIN_IDS


def get_status_emoji(is_available: bool) -> str:
    return "🟢" if is_available else "🔴"


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Основная Reply-клавиатура с динамической кнопкой мониторинга"""
    
    # Выбираем кнопку в зависимости от состояния мониторинга
    if monitoring_active:
        monitoring_btn = KeyboardButton(text="⏹ Стоп мониторинга")
    else:
        monitoring_btn = KeyboardButton(text="▶️ Старт мониторинга")
    
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📋 Серверы"),
                KeyboardButton(text="📊 Статус")
            ],
            [
                KeyboardButton(text="🔍 Проверить всё"),
                KeyboardButton(text="📈 Дашборд")
            ],
            [
                monitoring_btn
            ],
            [
                KeyboardButton(text="🏠 Главное меню")
            ]
        ],
        resize_keyboard=True,
        is_persistent=True
    )
    return keyboard

def get_server_keyboard(server: Server) -> InlineKeyboardMarkup:
    """Клавиатура для управления сервером"""
    status_text = "⏸ Отключить" if server.is_active else "▶️ Включить"
    
    buttons = [
        [
            InlineKeyboardButton(text="🔍 Проверить", callback_data=f"check_{server.id}"),
            InlineKeyboardButton(text="📊 Статистика", callback_data=f"stats_{server.id}")
        ],
        [
            InlineKeyboardButton(text="📈 График", callback_data=f"chart_24h_{server.id}"),
            InlineKeyboardButton(text=status_text, callback_data=f"toggle_{server.id}")
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_{server.id}"),
            InlineKeyboardButton(text="◀️ Назад", callback_data="list_servers")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_servers_list_keyboard(servers: List[Server]) -> InlineKeyboardMarkup:
    """Клавиатура со списком серверов"""
    buttons = []
    
    for server in servers:
        status = get_status_emoji(server.last_status) if server.is_active else "⏸"
        buttons.append([
            InlineKeyboardButton(
                text=f"{status} {server.name} ({server.host}:{server.port})",
                callback_data=f"server_{server.id}"
            )
        ])
    
    buttons.append([
        InlineKeyboardButton(text="➕ Добавить сервер", callback_data="add_server")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def safe_edit_or_send(
    callback: CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = ParseMode.HTML
):
    """
    Безопасное редактирование сообщения.
    Если сообщение - фото или редактирование не удалось, 
    удаляет и отправляет новое.
    """
    if callback.message.photo or callback.message.document:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            text, 
            parse_mode=parse_mode, 
            reply_markup=reply_markup
        )
        return
    
    try:
        await callback.message.edit_text(
            text, 
            parse_mode=parse_mode, 
            reply_markup=reply_markup
        )
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            text, 
            parse_mode=parse_mode, 
            reply_markup=reply_markup
        )


# ============= Команды =============

@router.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик /start"""
    await db.add_subscriber(message.chat.id)
    
    text = """
🔐 <b>VPN Monitor Bot</b>

Я слежу за доступностью ваших VPN-серверов и уведомляю о проблемах.

<b>📋 Основные команды:</b>
/servers - список серверов
/add - добавить сервер
/status - общий статус
/check - проверить все серверы

<b>📊 Графики:</b>
/chart - графики по серверам
/dashboard - дашборд статуса

<b>🔧 Управление:</b>
/startmon - запустить мониторинг
/stopmon - остановить мониторинг

<b>📈 Информация:</b>
/stats - общая статистика
"""
    
    await message.answer(
        text, 
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )


# ============= Обработчики Reply-кнопок =============


@router.message(F.text == "▶️ Старт мониторинга")
async def reply_start_monitoring(message: Message):
    """Запуск мониторинга"""
    await cmd_start_monitoring(message)


@router.message(F.text == "⏹ Стоп мониторинга")
async def reply_stop_monitoring(message: Message):
    """Остановка мониторинга"""
    await cmd_stop_monitoring(message)

@router.message(F.text == "🏠 Главное меню")
async def reply_main_menu(message: Message):
    """Главное меню"""
    await cmd_start(message)


@router.message(F.text == "📋 Серверы")
async def reply_servers(message: Message):
    """Список серверов"""
    await cmd_servers(message)


@router.message(F.text == "📊 Статус")
async def reply_status(message: Message):
    """Статус"""
    await cmd_status(message)


@router.message(F.text == "🔍 Проверить всё")
async def reply_check_all(message: Message):
    """Проверить все серверы"""
    await cmd_check_all(message)


@router.message(F.text == "📈 Дашборд")
async def reply_dashboard(message: Message):
    """Дашборд"""
    await cmd_dashboard(message)


# ============= Остальные команды =============

@router.message(Command("servers", "list"))
async def cmd_servers(message: Message):
    """Список серверов"""
    servers = await db.get_all_servers()
    
    if not servers:
        await message.answer(
            "📭 Список серверов пуст.\n\nИспользуйте /add для добавления сервера.",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )
        return
    
    text = "📋 <b>Список серверов:</b>\n\n"
    text += "<i>Нажмите на сервер для управления</i>"
    
    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_servers_list_keyboard(servers)
    )


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    """Добавить сервер"""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет прав для добавления серверов.")
        return
    
    await state.set_state(AddServerState.waiting_for_name)
    await message.answer(
        "➕ <b>Добавление нового сервера</b>\n\n"
        "Шаг 1/4: Введите название сервера:\n"
        "<i>Например: Main VPN, Office Server</i>\n\n"
        "/cancel - отмена",
        parse_mode=ParseMode.HTML
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Отмена операции"""
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нечего отменять.", reply_markup=get_main_keyboard())
        return
    
    await state.clear()
    await message.answer("❌ Операция отменена.", reply_markup=get_main_keyboard())


@router.message(AddServerState.waiting_for_name)
async def process_server_name(message: Message, state: FSMContext):
    """Обработка имени сервера"""
    await state.update_data(name=message.text.strip())
    await state.set_state(AddServerState.waiting_for_host)
    
    await message.answer(
        "Шаг 2/4: Введите IP-адрес или домен сервера:\n"
        "<i>Например: 123.45.67.89 или vpn.example.com</i>",
        parse_mode=ParseMode.HTML
    )


@router.message(AddServerState.waiting_for_host)
async def process_server_host(message: Message, state: FSMContext):
    """Обработка хоста"""
    host = message.text.strip()
    await state.update_data(host=host)
    await state.set_state(AddServerState.waiting_for_port)
    
    await message.answer(
        "Шаг 3/4: Введите порт сервера:\n"
        "<i>Например: 1194, 443, 51820</i>",
        parse_mode=ParseMode.HTML
    )


@router.message(AddServerState.waiting_for_port)
async def process_server_port(message: Message, state: FSMContext):
    """Обработка порта"""
    try:
        port = int(message.text.strip())
        if port < 1 or port > 65535:
            raise ValueError("Invalid port range")
    except ValueError:
        await message.answer("❌ Неверный порт. Введите число от 1 до 65535:")
        return
    
    await state.update_data(port=port)
    await state.set_state(AddServerState.waiting_for_protocol)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="TCP", callback_data="protocol_tcp"),
            InlineKeyboardButton(text="UDP", callback_data="protocol_udp")
        ]
    ])
    
    await message.answer(
        "Шаг 4/4: Выберите протокол:",
        reply_markup=keyboard
    )


@router.callback_query(F.data.startswith("protocol_"))
async def process_protocol(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора протокола"""
    protocol = callback.data.split("_")[1]
    data = await state.get_data()
    
    server_id = await db.add_server(
        name=data["name"],
        host=data["host"],
        port=data["port"],
        protocol=protocol
    )
    
    await state.clear()
    
    if server_id:
        text = (
            f"✅ <b>Сервер успешно добавлен!</b>\n\n"
            f"📛 Название: {data['name']}\n"
            f"🖥 Адрес: {data['host']}:{data['port']}\n"
            f"📡 Протокол: {protocol.upper()}\n\n"
            f"Используйте /servers для управления."
        )
    else:
        text = "❌ Ошибка: сервер с таким адресом уже существует."
    
    await safe_edit_or_send(callback, text)
    await callback.answer()


@router.message(Command("status"))
async def cmd_status(message: Message):
    """Общий статус"""
    servers = await db.get_all_servers()
    active_servers = [s for s in servers if s.is_active]
    
    online = sum(1 for s in active_servers if s.last_status)
    offline = len(active_servers) - online
    
    mon_status = "🟢 Работает" if monitoring_active else "🔴 Остановлен"
    
    text = f"""
📊 <b>Общий статус</b>

🔄 Мониторинг: {mon_status}

📋 Серверов всего: {len(servers)}
✅ Активных: {len(active_servers)}
🟢 Онлайн: {online}
🔴 Оффлайн: {offline}

⏱ Интервал проверки: {config.CHECK_INTERVAL} сек
⚠️ Порог уведомления: {config.FAIL_THRESHOLD} попыток
"""
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())


@router.message(Command("check"))
async def cmd_check_all(message: Message):
    """Проверить все серверы"""
    servers = await db.get_active_servers()
    
    if not servers:
        await message.answer(
            "📭 Нет активных серверов для проверки.",
            reply_markup=get_main_keyboard()
        )
        return
    
    msg = await message.answer(f"🔍 Проверяю {len(servers)} серверов...")
    
    results = []
    for server in servers:
        result = await check_server(server.host, server.port, server.protocol)
        status = get_status_emoji(result.is_available)
        response = f"{result.response_time:.0f}ms" if result.response_time else "N/A"
        results.append(f"{status} {server.name} - {response}")
        
        await db.update_server_status(
            server.id,
            result.is_available,
            result.response_time,
            result.error
        )
    
    text = "🔍 <b>Результаты проверки:</b>\n\n"
    text += "\n".join(results)
    
    await msg.edit_text(text, parse_mode=ParseMode.HTML)


@router.message(Command("startmon"))
async def cmd_start_monitoring(message: Message):
    """Запуск мониторинга"""
    global monitoring_active
    
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет прав для управления мониторингом.")
        return
    
    if monitoring_active:
        await message.answer("⚠️ Мониторинг уже запущен!", reply_markup=get_main_keyboard())
        return
    
    servers = await db.get_active_servers()
    if not servers:
        await message.answer(
            "📭 Нет активных серверов. Добавьте серверы с помощью /add",
            reply_markup=get_main_keyboard()
        )
        return
    
    monitoring_active = True
    asyncio.create_task(monitoring_loop())
    
    await message.answer(
        f"✅ <b>Мониторинг запущен!</b>\n\n"
        f"📋 Активных серверов: {len(servers)}\n"
        f"⏱ Интервал: {config.CHECK_INTERVAL} сек\n"
        f"⚠️ Порог уведомления: {config.FAIL_THRESHOLD} попыток",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )


@router.message(Command("stopmon"))
async def cmd_stop_monitoring(message: Message):
    """Остановка мониторинга"""
    global monitoring_active
    
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет прав для управления мониторингом.")
        return
    
    if not monitoring_active:
        await message.answer("⚠️ Мониторинг не запущен!", reply_markup=get_main_keyboard())
        return
    
    monitoring_active = False
    await message.answer("🛑 Мониторинг остановлен!", reply_markup=get_main_keyboard())


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Общая статистика"""
    servers = await db.get_all_servers()
    
    if not servers:
        await message.answer("📭 Нет серверов для статистики.", reply_markup=get_main_keyboard())
        return
    
    total_checks = sum(s.total_checks for s in servers)
    total_failures = sum(s.total_failures for s in servers)
    
    uptime = "N/A"
    if total_checks > 0:
        uptime = f"{((total_checks - total_failures) / total_checks) * 100:.1f}%"
    
    text = f"""
📊 <b>Общая статистика</b>

📋 Всего серверов: {len(servers)}
📈 Всего проверок: {total_checks}
✅ Успешных: {total_checks - total_failures}
❌ Неудачных: {total_failures}
📊 Средняя доступность: {uptime}

<b>По серверам:</b>
"""
    
    for server in servers:
        srv_uptime = "N/A"
        if server.total_checks > 0:
            srv_uptime = f"{((server.total_checks - server.total_failures) / server.total_checks) * 100:.0f}%"
        status = get_status_emoji(server.last_status) if server.is_active else "⏸"
        text += f"\n{status} {server.name}: {srv_uptime}"
    
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    """Подписаться на уведомления"""
    await db.add_subscriber(message.chat.id)
    await message.answer(
        "✅ Вы подписались на уведомления о статусе серверов.",
        reply_markup=get_main_keyboard()
    )


@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    """Отписаться от уведомлений"""
    await db.remove_subscriber(message.chat.id)
    await message.answer(
        "❌ Вы отписались от уведомлений.",
        reply_markup=get_main_keyboard()
    )


# ============= Callback-хендлеры =============

@router.callback_query(F.data == "list_servers")
async def callback_list_servers(callback: CallbackQuery):
    """Вернуться к списку серверов"""
    servers = await db.get_all_servers()
    
    if not servers:
        text = "📭 Список серверов пуст.\n\nИспользуйте /add для добавления."
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить сервер", callback_data="add_server")]
        ])
    else:
        text = "📋 <b>Список серверов:</b>\n\n<i>Нажмите на сервер для управления</i>"
        keyboard = get_servers_list_keyboard(servers)
    
    await safe_edit_or_send(callback, text, keyboard)
    await callback.answer()


@router.callback_query(F.data == "add_server")
async def callback_add_server(callback: CallbackQuery, state: FSMContext):
    """Добавление сервера через callback"""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ У вас нет прав", show_alert=True)
        return
    
    await state.set_state(AddServerState.waiting_for_name)
    
    text = (
        "➕ <b>Добавление нового сервера</b>\n\n"
        "Шаг 1/4: Введите название сервера:\n"
        "<i>Например: Main VPN, Office Server</i>\n\n"
        "/cancel - отмена"
    )
    
    await safe_edit_or_send(callback, text)
    await callback.answer()


@router.callback_query(F.data.startswith("server_"))
async def callback_server_info(callback: CallbackQuery):
    """Информация о сервере"""
    server_id = int(callback.data.split("_")[1])
    server = await db.get_server(server_id)
    
    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return
    
    status = get_status_emoji(server.last_status) if server.is_active else "⏸ Отключен"
    active_status = "✅ Активен" if server.is_active else "⏸ Отключен"
    last_check = server.last_check if server.last_check else "Не проверялся"
    
    uptime = "N/A"
    if server.total_checks > 0:
        uptime = f"{((server.total_checks - server.total_failures) / server.total_checks) * 100:.1f}%"
    
    text = f"""
🖥 <b>{server.name}</b>

📍 Адрес: <code>{server.host}:{server.port}</code>
📡 Протокол: {server.protocol.upper()}
📊 Статус: {status}
🔄 Мониторинг: {active_status}

📈 <b>Статистика:</b>
• Всего проверок: {server.total_checks}
• Неудачных: {server.total_failures}
• Доступность: {uptime}
• Ошибок подряд: {server.consecutive_failures}

⏰ Последняя проверка: {last_check}
"""
    
    await safe_edit_or_send(callback, text, get_server_keyboard(server))
    await callback.answer()


@router.callback_query(F.data.startswith("check_"))
async def callback_check_server(callback: CallbackQuery):
    """Проверить сервер"""
    server_id = int(callback.data.split("_")[1])
    server = await db.get_server(server_id)
    
    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return
    
    await callback.answer("🔍 Проверяю...")
    
    result = await check_server(server.host, server.port, server.protocol)
    
    status = get_status_emoji(result.is_available)
    response_time = f"{result.response_time:.1f}ms" if result.response_time else "N/A"
    
    text = f"""
🔍 <b>Результат проверки</b>

🖥 Сервер: {server.name}
📍 Адрес: <code>{server.host}:{server.port}</code>

{status} Статус: {"Доступен" if result.is_available else "Недоступен"}
⏱ Время отклика: {response_time}
{"❌ Ошибка: " + result.error if result.error and not result.is_available else ""}
"""
    
    await db.update_server_status(
        server_id,
        result.is_available,
        result.response_time,
        result.error
    )
    
    updated_server = await db.get_server(server_id)
    await safe_edit_or_send(callback, text, get_server_keyboard(updated_server))


@router.callback_query(F.data.startswith("toggle_"))
async def callback_toggle_server(callback: CallbackQuery):
    """Включить/выключить мониторинг сервера"""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ У вас нет прав", show_alert=True)
        return
    
    server_id = int(callback.data.split("_")[1])
    new_status = await db.toggle_server(server_id)
    
    if new_status is None:
        await callback.answer("Сервер не найден", show_alert=True)
        return
    
    status_text = "включен" if new_status else "отключен"
    await callback.answer(f"Мониторинг {status_text}")
    
    callback.data = f"server_{server_id}"
    await callback_server_info(callback)


@router.callback_query(F.data.startswith("delete_"))
async def callback_delete_server(callback: CallbackQuery):
    """Удалить сервер"""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ У вас нет прав", show_alert=True)
        return
    
    server_id = int(callback.data.split("_")[1])
    server = await db.get_server(server_id)
    
    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_delete_{server_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"server_{server_id}")
        ]
    ])
    
    text = (
        f"🗑 <b>Удаление сервера</b>\n\n"
        f"Вы уверены, что хотите удалить сервер <b>{server.name}</b>?\n"
        f"Это действие нельзя отменить."
    )
    
    await safe_edit_or_send(callback, text, keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_delete_"))
async def callback_confirm_delete(callback: CallbackQuery):
    """Подтверждение удаления"""
    server_id = int(callback.data.split("_")[2])
    
    if await db.remove_server(server_id):
        await callback.answer("✅ Сервер удалён")
        
        servers = await db.get_all_servers()
        if servers:
            text = "📋 <b>Список серверов:</b>"
            keyboard = get_servers_list_keyboard(servers)
        else:
            text = "📭 Список серверов пуст."
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить сервер", callback_data="add_server")]
            ])
        
        await safe_edit_or_send(callback, text, keyboard)
    else:
        await callback.answer("❌ Ошибка при удалении", show_alert=True)


@router.callback_query(F.data.startswith("stats_"))
async def callback_server_stats(callback: CallbackQuery):
    """Статистика сервера"""
    server_id = int(callback.data.split("_")[1])
    server = await db.get_server(server_id)
    
    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return
    
    history = await db.get_server_history(server_id, limit=10)
    
    uptime = "N/A"
    if server.total_checks > 0:
        uptime = f"{((server.total_checks - server.total_failures) / server.total_checks) * 100:.1f}%"
    
    text = f"""
📊 <b>Статистика: {server.name}</b>

📈 Общая статистика:
• Всего проверок: {server.total_checks}
• Успешных: {server.total_checks - server.total_failures}
• Неудачных: {server.total_failures}
• Доступность: {uptime}

📜 Последние проверки:
"""
    
    for h in history[:5]:
        status = "✅" if h["is_available"] else "❌"
        time_str = h["checked_at"][:19] if h["checked_at"] else "N/A"
        response = f"{h['response_time']:.0f}ms" if h["response_time"] else ""
        text += f"{status} {time_str} {response}\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📈 График", callback_data=f"chart_24h_{server_id}"),
            InlineKeyboardButton(text="🔄 Сбросить", callback_data=f"reset_stats_{server_id}")
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"server_{server_id}")]
    ])
    
    await safe_edit_or_send(callback, text, keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("reset_stats_"))
async def callback_reset_stats(callback: CallbackQuery):
    """Сброс статистики"""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ У вас нет прав", show_alert=True)
        return
    
    server_id = int(callback.data.split("_")[2])
    await db.reset_server_stats(server_id)
    await callback.answer("✅ Статистика сброшена")
    
    callback.data = f"stats_{server_id}"
    await callback_server_stats(callback)


# ============= Команды для графиков =============

@router.message(Command("chart", "graph"))
async def cmd_chart(message: Message):
    """Показать график выбранного сервера"""
    servers = await db.get_all_servers()
    
    if not servers:
        await message.answer("📭 Нет серверов для отображения.", reply_markup=get_main_keyboard())
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{get_status_emoji(s.last_status)} {s.name}",
            callback_data=f"chart_24h_{s.id}"
        )] for s in servers
    ] + [[InlineKeyboardButton(text="📊 Все серверы", callback_data="chart_all")]])
    
    await message.answer(
        "📈 <b>Выберите сервер для графика:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


@router.message(Command("dashboard"))
async def cmd_dashboard(message: Message):
    """Дашборд со статусом всех серверов"""
    servers = await db.get_all_servers()
    
    if not servers:
        await message.answer("📭 Нет серверов.", reply_markup=get_main_keyboard())
        return
    
    msg = await message.answer("⏳ Генерирую дашборд...")
    
    image_bytes = await generate_realtime_status_image(servers)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_dashboard"),
            InlineKeyboardButton(text="📊 Графики", callback_data="chart_all")
        ]
    ])
    
    photo = BufferedInputFile(image_bytes, filename="dashboard.png")
    
    try:
        await msg.delete()
    except Exception:
        pass
    
    await message.answer_photo(
        photo=photo,
        caption="🖥 <b>Текущий статус серверов</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


@router.callback_query(F.data.startswith("chart_24h_"))
async def callback_chart_24h(callback: CallbackQuery):
    """График за 24 часа"""
    server_id = int(callback.data.split("_")[2])
    await _send_chart(callback, server_id, 24)


@router.callback_query(F.data.startswith("chart_6h_"))
async def callback_chart_6h(callback: CallbackQuery):
    """График за 6 часов"""
    server_id = int(callback.data.split("_")[2])
    await _send_chart(callback, server_id, 6)


@router.callback_query(F.data.startswith("chart_12h_"))
async def callback_chart_12h(callback: CallbackQuery):
    """График за 12 часов"""
    server_id = int(callback.data.split("_")[2])
    await _send_chart(callback, server_id, 12)


async def _send_chart(callback: CallbackQuery, server_id: int, hours: int):
    """Вспомогательная функция для отправки графика"""
    await callback.answer("📊 Генерирую график...")
    
    server = await db.get_server(server_id)
    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return
    
    try:
        await callback.message.delete()
    except Exception:
        pass
    
    loading_msg = await callback.message.answer("⏳ Генерирую график...")
    
    image_bytes = await generate_uptime_chart(server_id, hours=hours)
    
    if image_bytes:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="6ч", callback_data=f"chart_6h_{server_id}"),
                InlineKeyboardButton(text="12ч", callback_data=f"chart_12h_{server_id}"),
                InlineKeyboardButton(text="24ч", callback_data=f"chart_24h_{server_id}"),
            ],
            [
                InlineKeyboardButton(text="📅 Неделя", callback_data=f"chart_week_{server_id}"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data=f"chart_{hours}h_{server_id}"),
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="list_servers")]
        ])
        
        photo = BufferedInputFile(image_bytes, filename="chart.png")
        
        try:
            await loading_msg.delete()
        except Exception:
            pass
        
        await callback.message.answer_photo(
            photo=photo,
            caption=f"📈 <b>{server.name}</b>\nГрафик за последние {hours} часов",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
    else:
        await loading_msg.edit_text(
            "❌ Недостаточно данных для построения графика.\n"
            "Подождите, пока накопится история проверок.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="list_servers")]
            ])
        )


@router.callback_query(F.data.startswith("chart_week_"))
async def callback_chart_week(callback: CallbackQuery):
    """Недельный график"""
    server_id = int(callback.data.split("_")[2])
    
    await callback.answer("📊 Генерирую недельный график...")
    
    server = await db.get_server(server_id)
    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return
    
    try:
        await callback.message.delete()
    except Exception:
        pass
    
    loading_msg = await callback.message.answer("⏳ Генерирую недельный график...")
    
    image_bytes = await generate_weekly_chart(server_id)
    
    if image_bytes:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 24 часа", callback_data=f"chart_24h_{server_id}"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data=f"chart_week_{server_id}"),
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="list_servers")]
        ])
        
        photo = BufferedInputFile(image_bytes, filename="weekly_chart.png")
        
        try:
            await loading_msg.delete()
        except Exception:
            pass
        
        await callback.message.answer_photo(
            photo=photo,
            caption=f"📅 <b>{server.name}</b>\nСтатистика за неделю",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
    else:
        await loading_msg.edit_text(
            "❌ Недостаточно данных для недельного графика.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="list_servers")]
            ])
        )


@router.callback_query(F.data == "chart_all")
async def callback_chart_all_servers(callback: CallbackQuery):
    """Сводный график по всем серверам"""
    await callback.answer("📊 Генерирую сводный график...")
    
    try:
        await callback.message.delete()
    except Exception:
        pass
    
    loading_msg = await callback.message.answer("⏳ Генерирую сводный график...")
    
    image_bytes = await generate_all_servers_chart(hours=24)
    
    if image_bytes:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="chart_all"),
                InlineKeyboardButton(text="◀️ Назад", callback_data="list_servers")
            ]
        ])
        
        photo = BufferedInputFile(image_bytes, filename="all_servers.png")
        
        try:
            await loading_msg.delete()
        except Exception:
            pass
        
        await callback.message.answer_photo(
            photo=photo,
            caption="📊 <b>Сводка по всем серверам</b>\nЗа последние 24 часа",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
    else:
        await loading_msg.edit_text(
            "❌ Нет данных для графика.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="list_servers")]
            ])
        )


@router.callback_query(F.data == "refresh_dashboard")
async def callback_refresh_dashboard(callback: CallbackQuery):
    """Обновить дашборд"""
    servers = await db.get_all_servers()
    
    if not servers:
        await callback.answer("Нет серверов", show_alert=True)
        return
    
    await callback.answer("🔄 Обновляю...")
    
    try:
        await callback.message.delete()
    except Exception:
        pass
    
    loading_msg = await callback.message.answer("⏳ Обновляю дашборд...")
    
    image_bytes = await generate_realtime_status_image(servers)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_dashboard"),
            InlineKeyboardButton(text="📊 Графики", callback_data="chart_all")
        ]
    ])
    
    photo = BufferedInputFile(image_bytes, filename="dashboard.png")
    
    try:
        await loading_msg.delete()
    except Exception:
        pass
    
    await callback.message.answer_photo(
        photo=photo,
        caption=f"🖥 <b>Текущий статус серверов</b>\n<i>Обновлено: {datetime.now().strftime('%H:%M:%S')}</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


# ============= Мониторинг =============

async def monitoring_loop():
    """Основной цикл мониторинга с адаптивным интервалом"""
    global monitoring_active
    
    logger.info("Monitoring loop started")
    
    NORMAL_INTERVAL = config.CHECK_INTERVAL
    FAST_INTERVAL = 15
    CONFIRM_CHECKS = 2
    
    while monitoring_active:
        try:
            servers = await db.get_active_servers()
            
            has_down_servers = any(
                s.consecutive_failures >= config.FAIL_THRESHOLD 
                for s in servers
            )
            
            for server in servers:
                if not monitoring_active:
                    break
                
                result = await check_server(server.host, server.port, server.protocol)
                
                await db.update_server_status(
                    server.id,
                    result.is_available,
                    result.response_time,
                    result.error
                )
                
                updated_server = await db.get_server(server.id)
                
                if result.is_available:
                    logger.info(f"✅ {server.name} ({server.host}:{server.port}) - OK")
                    
                    if updated_server.notification_sent:
                        confirmed = await confirm_server_recovery(server, CONFIRM_CHECKS)
                        
                        if confirmed:
                            await send_recovery_notification(updated_server, result)
                            await db.set_notification_sent(server.id, False)
                            logger.info(f"✅ {server.name} - RECOVERY CONFIRMED!")
                else:
                    logger.warning(
                        f"❌ {server.name} ({server.host}:{server.port}) - FAIL "
                        f"({updated_server.consecutive_failures}/{config.FAIL_THRESHOLD})"
                    )
                    
                    if (updated_server.consecutive_failures >= config.FAIL_THRESHOLD 
                        and not updated_server.notification_sent):
                        
                        confirmed_down = await confirm_server_down(server, 2)
                        
                        if confirmed_down:
                            await send_down_notification(updated_server, result)
                            await db.set_notification_sent(server.id, True)
                
                await asyncio.sleep(0.5)
            
            if has_down_servers:
                await asyncio.sleep(FAST_INTERVAL)
            else:
                await asyncio.sleep(NORMAL_INTERVAL)
                
        except Exception as e:
            logger.error(f"Error in monitoring loop: {e}")
            await asyncio.sleep(5)
    
    logger.info("Monitoring loop stopped")


async def confirm_server_recovery(server: Server, checks: int = 2) -> bool:
    """Подтверждение восстановления сервера"""
    logger.info(f"🔄 Confirming recovery for {server.name}...")
    
    for i in range(checks):
        await asyncio.sleep(3)
        result = await check_server(server.host, server.port, server.protocol)
        
        if not result.is_available:
            logger.warning(f"❌ {server.name} - confirmation check {i+1} failed")
            return False
        
        logger.info(f"✅ {server.name} - confirmation check {i+1}/{checks} OK")
    
    return True


async def confirm_server_down(server: Server, checks: int = 2) -> bool:
    """Подтверждение падения сервера"""
    logger.info(f"🔄 Confirming down status for {server.name}...")
    
    for i in range(checks):
        await asyncio.sleep(2)
        result = await check_server(server.host, server.port, server.protocol)
        
        if result.is_available:
            logger.info(f"✅ {server.name} - came back during confirmation")
            return False
        
        logger.warning(f"❌ {server.name} - down confirmation {i+1}/{checks}")
    
    return True


async def send_down_notification(server: Server, result: CheckResult):
    """Уведомление о падении"""
    text = f"""
🚨🚨🚨 <b>СЕРВЕР НЕДОСТУПЕН!</b> 🚨🚨🚨

📛 Сервер: <b>{server.name}</b>
📍 Адрес: <code>{server.host}:{server.port}</code>
📡 Протокол: {server.protocol.upper()}

❌ Ошибка: {result.error}
⚠️ Неудачных попыток: {server.consecutive_failures + 1}
⏰ Время: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

<b>Требуется проверка!</b>
"""
    await send_notification_to_all(text)


async def send_recovery_notification(server: Server, result: CheckResult):
    """Уведомление о восстановлении"""
    response_time = f"{result.response_time:.1f}ms" if result.response_time else "N/A"
    
    text = f"""
✅✅✅ <b>СЕРВЕР ВОССТАНОВЛЕН!</b> ✅✅✅

📛 Сервер: <b>{server.name}</b>
📍 Адрес: <code>{server.host}:{server.port}</code>
📡 Протокол: {server.protocol.upper()}

⏱ Время отклика: {response_time}
⏰ Время: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

<b>Сервер работает нормально.</b>
"""
    await send_notification_to_all(text)


async def send_notification_to_all(text: str):
    """Отправить уведомление всем подписчикам"""
    subscribers = await db.get_subscribers()
    
    for chat_id in subscribers:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Failed to send notification to {chat_id}: {e}")


# ============= Запуск =============

async def main():
    """Главная функция"""
    logger.info("Starting VPN Monitor Bot...")
    
    # Инициализация БД
    await db.init()
    
    # Регистрируем роутер
    dp.include_router(router)
    
    # Уведомление о запуске админам
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text="🤖 <b>VPN Monitor Bot запущен!</b>\n\n"
                     "Используйте /startmon для запуска мониторинга.",
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard()
            )
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")
    
    # Запуск
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())