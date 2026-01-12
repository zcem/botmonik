from __future__ import annotations  # ← Добавить эту строку!

import asyncio
import socket
import platform
from datetime import datetime
from dataclasses import dataclass
from typing import Optional


@dataclass
class CheckResult:
    """Результат проверки"""
    is_available: bool
    method: str
    response_time: Optional[float]
    error: Optional[str]


async def check_ping(host: str, timeout: int = 5) -> CheckResult:
    """Проверка через ICMP ping"""
    start_time = datetime.now()
    
    param = "-n" if platform.system().lower() == "windows" else "-c"
    timeout_param = "-w" if platform.system().lower() == "windows" else "-W"
    
    command = ["ping", param, "1", timeout_param, str(timeout), host]
    
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(process.wait(), timeout=timeout + 2)
        
        response_time = (datetime.now() - start_time).total_seconds() * 1000
        
        if process.returncode == 0:
            return CheckResult(True, "ping", response_time, None)
        else:
            return CheckResult(False, "ping", None, "Ping failed")
            
    except asyncio.TimeoutError:
        return CheckResult(False, "ping", None, "Timeout")
    except Exception as e:
        return CheckResult(False, "ping", None, str(e))


async def check_tcp_port(host: str, port: int, timeout: int = 5) -> CheckResult:
    """Проверка TCP порта"""
    start_time = datetime.now()
    
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        
        response_time = (datetime.now() - start_time).total_seconds() * 1000
        return CheckResult(True, "tcp", response_time, None)
        
    except asyncio.TimeoutError:
        return CheckResult(False, "tcp", None, "Connection timeout")
    except ConnectionRefusedError:
        return CheckResult(False, "tcp", None, "Connection refused")
    except OSError as e:
        return CheckResult(False, "tcp", None, f"OS Error: {e}")
    except Exception as e:
        return CheckResult(False, "tcp", None, str(e))


async def check_udp_port(host: str, port: int, timeout: int = 5) -> CheckResult:
    """Проверка UDP порта"""
    start_time = datetime.now()
    
    try:
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        
        await loop.run_in_executor(None, lambda: sock.sendto(b'\x00', (host, port)))
        
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: sock.recvfrom(1024)),
                timeout=timeout
            )
            response_time = (datetime.now() - start_time).total_seconds() * 1000
            return CheckResult(True, "udp", response_time, None)
        except asyncio.TimeoutError:
            response_time = (datetime.now() - start_time).total_seconds() * 1000
            return CheckResult(True, "udp", response_time, None)
        finally:
            sock.close()
            
    except Exception as e:
        return CheckResult(False, "udp", None, str(e))


async def check_server(host: str, port: int, protocol: str = "tcp") -> CheckResult:
    """Проверка сервера"""
    # Сначала ping
    ping_result = await check_ping(host)
    
    if not ping_result.is_available:
        return ping_result
    
    # Затем порт
    if protocol.lower() == "tcp":
        return await check_tcp_port(host, port)
    else:
        return await check_udp_port(host, port)