from __future__ import annotations

import io
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor

import matplotlib
matplotlib.use('Agg')  # Без GUI
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Patch

from database import db, Server


# Настройка стиля графиков
plt.style.use('seaborn-v0_8-darkgrid')
plt.rcParams['figure.facecolor'] = '#1a1a2e'
plt.rcParams['axes.facecolor'] = '#16213e'
plt.rcParams['axes.edgecolor'] = '#e94560'
plt.rcParams['axes.labelcolor'] = '#ffffff'
plt.rcParams['text.color'] = '#ffffff'
plt.rcParams['xtick.color'] = '#ffffff'
plt.rcParams['ytick.color'] = '#ffffff'
plt.rcParams['grid.color'] = '#0f3460'
plt.rcParams['font.size'] = 10


# Executor для запуска matplotlib в отдельном потоке
executor = ThreadPoolExecutor(max_workers=2)


async def generate_uptime_chart(server_id: int, hours: int = 24) -> Optional[bytes]:
    """Генерация графика доступности сервера"""
    
    server = await db.get_server(server_id)
    if not server:
        return None
    
    history = await db.get_server_history(server_id, limit=1000)
    
    if not history:
        return None
    
    # Фильтруем по времени
    cutoff_time = datetime.now() - timedelta(hours=hours)
    
    # Парсим данные
    times = []
    statuses = []
    response_times = []
    
    for record in reversed(history):
        try:
            if isinstance(record['checked_at'], str):
                check_time = datetime.fromisoformat(record['checked_at'].replace('Z', '+00:00'))
            else:
                check_time = record['checked_at']
            
            if check_time.tzinfo:
                check_time = check_time.replace(tzinfo=None)
            
            if check_time >= cutoff_time:
                times.append(check_time)
                statuses.append(1 if record['is_available'] else 0)
                response_times.append(record['response_time'] if record['response_time'] else 0)
        except Exception:
            continue
    
    if not times:
        return None
    
    # Генерируем график в отдельном потоке
    loop = asyncio.get_event_loop()
    image_bytes = await loop.run_in_executor(
        executor,
        _create_uptime_chart,
        server.name,
        times,
        statuses,
        response_times,
        hours
    )
    
    return image_bytes


def _create_uptime_chart(
    server_name: str,
    times: List[datetime],
    statuses: List[int],
    response_times: List[float],
    hours: int
) -> bytes:
    """Создание графика (синхронная функция)"""
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), height_ratios=[1, 2])
    fig.suptitle(f'📊 Мониторинг: {server_name}\nПоследние {hours} часов', 
                 fontsize=14, fontweight='bold', color='#ffffff')
    
    # === График 1: Статус (доступен/недоступен) ===
    colors = ['#00ff88' if s == 1 else '#ff4757' for s in statuses]
    ax1.bar(times, [1] * len(times), color=colors, width=0.01, alpha=0.8)
    ax1.set_ylabel('Статус')
    ax1.set_ylim(0, 1.2)
    ax1.set_yticks([])
    ax1.set_title('🟢 Доступность', fontsize=11, loc='left', color='#00ff88')
    
    # Легенда
    legend_elements = [
        Patch(facecolor='#00ff88', label='Online'),
        Patch(facecolor='#ff4757', label='Offline')
    ]
    ax1.legend(handles=legend_elements, loc='upper right', 
               facecolor='#1a1a2e', edgecolor='#e94560')
    
    # === График 2: Время отклика ===
    valid_times = []
    valid_responses = []
    for t, r in zip(times, response_times):
        if r > 0:
            valid_times.append(t)
            valid_responses.append(r)
    
    if valid_responses:
        ax2.fill_between(valid_times, valid_responses, alpha=0.3, color='#00d9ff')
        ax2.plot(valid_times, valid_responses, color='#00d9ff', linewidth=1.5, marker='o', markersize=2)
        
        avg_response = sum(valid_responses) / len(valid_responses)
        ax2.axhline(y=avg_response, color='#ffa502', linestyle='--', 
                    label=f'Среднее: {avg_response:.1f}ms', alpha=0.8)
        ax2.legend(loc='upper right', facecolor='#1a1a2e', edgecolor='#e94560')
    
    ax2.set_ylabel('Время отклика (ms)')
    ax2.set_xlabel('Время')
    ax2.set_title('⏱ Время отклика', fontsize=11, loc='left', color='#00d9ff')
    
    # Форматирование оси X
    for ax in [ax1, ax2]:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=max(1, hours // 12)))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right')
    
    # Статистика
    uptime_percent = (sum(statuses) / len(statuses)) * 100 if statuses else 0
    total_checks = len(statuses)
    failures = statuses.count(0)
    
    stats_text = (
        f"📈 Uptime: {uptime_percent:.1f}%  |  "
        f"✅ Проверок: {total_checks}  |  "
        f"❌ Сбоев: {failures}"
    )
    fig.text(0.5, 0.02, stats_text, ha='center', fontsize=10, 
             color='#ffffff', style='italic')
    
    plt.tight_layout(rect=[0, 0.05, 1, 0.95])
    
    # Сохраняем в bytes
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight',
                facecolor='#1a1a2e', edgecolor='none')
    buf.seek(0)
    plt.close(fig)
    
    return buf.getvalue()


async def generate_all_servers_chart(hours: int = 24) -> Optional[bytes]:
    """График всех серверов"""
    
    servers = await db.get_all_servers()
    
    if not servers:
        return None
    
    server_data = []
    
    for server in servers:
        history = await db.get_server_history(server.id, limit=500)
        
        if history:
            cutoff = datetime.now() - timedelta(hours=hours)
            recent = []
            
            for h in history:
                try:
                    if isinstance(h['checked_at'], str):
                        check_time = datetime.fromisoformat(h['checked_at'].replace('Z', '+00:00'))
                    else:
                        check_time = h['checked_at']
                    
                    if check_time.tzinfo:
                        check_time = check_time.replace(tzinfo=None)
                    
                    if check_time >= cutoff:
                        recent.append(h)
                except Exception:
                    continue
            
            if recent:
                uptime = sum(1 for h in recent if h['is_available']) / len(recent) * 100
                avg_response = sum(h['response_time'] or 0 for h in recent if h['is_available']) / max(1, sum(1 for h in recent if h['is_available']))
            else:
                uptime = 100 if server.last_status else 0
                avg_response = 0
        else:
            uptime = 100 if server.last_status else 0
            avg_response = 0
        
        server_data.append({
            'name': server.name,
            'uptime': uptime,
            'avg_response': avg_response,
            'is_active': server.is_active,
            'last_status': server.last_status
        })
    
    loop = asyncio.get_event_loop()
    image_bytes = await loop.run_in_executor(
        executor,
        _create_all_servers_chart,
        server_data,
        hours
    )
    
    return image_bytes


def _create_all_servers_chart(server_data: List[Dict], hours: int) -> bytes:
    """Создание сводного графика"""
    
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
    fig.suptitle(f'📊 Сводка по всем серверам\nПоследние {hours} часов',
                 fontsize=14, fontweight='bold', color='#ffffff')
    
    names = [d['name'][:15] for d in server_data]
    uptimes = [d['uptime'] for d in server_data]
    responses = [d['avg_response'] for d in server_data]
    
    # === График 1: Uptime ===
    colors = ['#00ff88' if u >= 99 else '#ffa502' if u >= 95 else '#ff4757' for u in uptimes]
    bars1 = ax1.barh(names, uptimes, color=colors, alpha=0.8, edgecolor='white', linewidth=0.5)
    ax1.set_xlabel('Uptime %')
    ax1.set_title('🎯 Доступность', fontsize=11, loc='left', color='#00ff88')
    ax1.set_xlim(0, 105)
    ax1.axvline(x=99, color='#00ff88', linestyle='--', alpha=0.5, label='99%')
    ax1.axvline(x=95, color='#ffa502', linestyle='--', alpha=0.5, label='95%')
    
    # Значения на барах
    for bar, uptime in zip(bars1, uptimes):
        ax1.text(bar.get_width() + 1, bar.get_y() + bar.get_height()/2,
                f'{uptime:.1f}%', va='center', fontsize=9, color='#ffffff')
    
    # === График 2: Время отклика ===
    colors2 = ['#00d9ff' if r < 100 else '#ffa502' if r < 300 else '#ff4757' for r in responses]
    bars2 = ax2.barh(names, responses, color=colors2, alpha=0.8, edgecolor='white', linewidth=0.5)
    ax2.set_xlabel('Время отклика (ms)')
    ax2.set_title('⏱ Среднее время отклика', fontsize=11, loc='left', color='#00d9ff')
    
    for bar, resp in zip(bars2, responses):
        if resp > 0:
            ax2.text(bar.get_width() + 5, bar.get_y() + bar.get_height()/2,
                    f'{resp:.0f}ms', va='center', fontsize=9, color='#ffffff')
    
    # Легенда
    legend_elements = [
        Patch(facecolor='#00ff88', label='Отлично (≥99%)'),
        Patch(facecolor='#ffa502', label='Хорошо (95-99%)'),
        Patch(facecolor='#ff4757', label='Проблемы (<95%)')
    ]
    fig.legend(handles=legend_elements, loc='lower center', ncol=3,
               facecolor='#1a1a2e', edgecolor='#e94560', fontsize=9)
    
    plt.tight_layout(rect=[0, 0.08, 1, 0.92])
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight',
                facecolor='#1a1a2e', edgecolor='none')
    buf.seek(0)
    plt.close(fig)
    
    return buf.getvalue()


async def generate_weekly_chart(server_id: int) -> Optional[bytes]:
    """График за неделю с разбивкой по дням"""
    
    server = await db.get_server(server_id)
    if not server:
        return None
    
    history = await db.get_server_history(server_id, limit=5000)
    
    if not history:
        return None
    
    # Группируем по дням
    cutoff = datetime.now() - timedelta(days=7)
    daily_stats: Dict[str, Dict] = {}
    
    for record in history:
        try:
            if isinstance(record['checked_at'], str):
                check_time = datetime.fromisoformat(record['checked_at'].replace('Z', '+00:00'))
            else:
                check_time = record['checked_at']
            
            if check_time.tzinfo:
                check_time = check_time.replace(tzinfo=None)
            
            if check_time >= cutoff:
                day_key = check_time.strftime('%Y-%m-%d')
                
                if day_key not in daily_stats:
                    daily_stats[day_key] = {'checks': 0, 'successes': 0, 'responses': []}
                
                daily_stats[day_key]['checks'] += 1
                if record['is_available']:
                    daily_stats[day_key]['successes'] += 1
                    if record['response_time']:
                        daily_stats[day_key]['responses'].append(record['response_time'])
        except Exception:
            continue
    
    if not daily_stats:
        return None
    
    loop = asyncio.get_event_loop()
    image_bytes = await loop.run_in_executor(
        executor,
        _create_weekly_chart,
        server.name,
        daily_stats
    )
    
    return image_bytes


def _create_weekly_chart(server_name: str, daily_stats: Dict[str, Dict]) -> bytes:
    """Создание недельного графика"""
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8))
    fig.suptitle(f'📅 Недельная статистика: {server_name}',
                 fontsize=14, fontweight='bold', color='#ffffff')
    
    days = sorted(daily_stats.keys())
    day_labels = [datetime.strptime(d, '%Y-%m-%d').strftime('%a\n%d.%m') for d in days]
    
    uptimes = []
    avg_responses = []
    
    for day in days:
        stats = daily_stats[day]
        uptime = (stats['successes'] / stats['checks'] * 100) if stats['checks'] > 0 else 0
        avg_resp = sum(stats['responses']) / len(stats['responses']) if stats['responses'] else 0
        uptimes.append(uptime)
        avg_responses.append(avg_resp)
    
    # === Uptime по дням ===
    colors = ['#00ff88' if u >= 99 else '#ffa502' if u >= 95 else '#ff4757' for u in uptimes]
    bars = ax1.bar(day_labels, uptimes, color=colors, alpha=0.8, edgecolor='white')
    ax1.set_ylabel('Uptime %')
    ax1.set_ylim(0, 105)
    ax1.axhline(y=99, color='#00ff88', linestyle='--', alpha=0.5)
    ax1.set_title('📈 Ежедневная доступность', fontsize=11, loc='left', color='#00ff88')
    
    for bar, uptime in zip(bars, uptimes):
        ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                f'{uptime:.1f}%', ha='center', fontsize=9, color='#ffffff')
    
    # === Время отклика по дням ===
    ax2.bar(day_labels, avg_responses, color='#00d9ff', alpha=0.8, edgecolor='white')
    ax2.set_ylabel('Среднее время отклика (ms)')
    ax2.set_title('⏱ Среднее время отклика', fontsize=11, loc='left', color='#00d9ff')
    
    overall_uptime = sum(uptimes) / len(uptimes) if uptimes else 0
    overall_response = sum(avg_responses) / len(avg_responses) if avg_responses else 0
    
    stats_text = f"📊 Средний Uptime: {overall_uptime:.1f}%  |  ⏱ Среднее время: {overall_response:.0f}ms"
    fig.text(0.5, 0.02, stats_text, ha='center', fontsize=10,
             color='#ffffff', style='italic')
    
    plt.tight_layout(rect=[0, 0.05, 1, 0.95])
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight',
                facecolor='#1a1a2e', edgecolor='none')
    buf.seek(0)
    plt.close(fig)
    
    return buf.getvalue()


async def generate_realtime_status_image(servers: List[Server]) -> bytes:
    """Генерация изображения текущего статуса всех серверов"""
    
    loop = asyncio.get_event_loop()
    
    server_info = []
    for s in servers:
        server_info.append({
            'name': s.name,
            'host': f"{s.host}:{s.port}",
            'status': s.last_status,
            'active': s.is_active,
            'uptime': ((s.total_checks - s.total_failures) / s.total_checks * 100) if s.total_checks > 0 else 100
        })
    
    image_bytes = await loop.run_in_executor(
        executor,
        _create_status_image,
        server_info
    )
    
    return image_bytes


def _create_status_image(servers: List[Dict]) -> bytes:
    """Создание изображения статуса"""
    
    fig, ax = plt.subplots(figsize=(10, max(4, len(servers) * 0.8)))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, len(servers) + 1)
    ax.axis('off')
    
    fig.suptitle('🖥 Статус серверов', fontsize=16, fontweight='bold', 
                 color='#ffffff', y=0.98)
    
    for i, server in enumerate(reversed(servers)):
        y = i + 0.5
        
        # Статус
        if not server['active']:
            color = '#666666'
            status_icon = '⏸'
            status_text = 'PAUSED'
        elif server['status']:
            color = '#00ff88'
            status_icon = '🟢'
            status_text = 'ONLINE'
        else:
            color = '#ff4757'
            status_icon = '🔴'
            status_text = 'OFFLINE'
        
        # Фон строки
        rect = plt.Rectangle((0.1, y - 0.35), 9.8, 0.7, 
                             facecolor=color, alpha=0.2, edgecolor=color)
        ax.add_patch(rect)
        
        # Текст
        ax.text(0.3, y, status_icon, fontsize=14, va='center')
        ax.text(1.0, y, server['name'], fontsize=12, va='center', 
               color='#ffffff', fontweight='bold')
        ax.text(5.0, y, server['host'], fontsize=10, va='center', 
               color='#888888', family='monospace')
        ax.text(8.0, y, status_text, fontsize=10, va='center', 
               color=color, fontweight='bold')
        ax.text(9.5, y, f"{server['uptime']:.0f}%", fontsize=10, va='center',
               color='#ffffff')
    
    # Заголовки
    ax.text(1.0, len(servers) + 0.5, 'Сервер', fontsize=10, 
           color='#888888', fontweight='bold')
    ax.text(5.0, len(servers) + 0.5, 'Адрес', fontsize=10, 
           color='#888888', fontweight='bold')
    ax.text(8.0, len(servers) + 0.5, 'Статус', fontsize=10, 
           color='#888888', fontweight='bold')
    ax.text(9.5, len(servers) + 0.5, 'Uptime', fontsize=10, 
           color='#888888', fontweight='bold')
    
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    fig.text(0.5, 0.02, f'Обновлено: {timestamp}', ha='center', 
            fontsize=9, color='#666666')
    
    plt.tight_layout(rect=[0, 0.05, 1, 0.95])
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight',
                facecolor='#1a1a2e', edgecolor='none')
    buf.seek(0)
    plt.close(fig)
    
    return buf.getvalue()