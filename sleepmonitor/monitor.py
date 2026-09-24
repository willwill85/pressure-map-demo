"""采集与广播服务, 对应 SleepMonitor/MonitorService.cs + Program.cs 的编排。

    压力串口 → 卡尔曼 + 高斯 → 48×96 热图 ─┐
                                            ├→ WebSocket → 浏览器
    心率呼吸串口 → ADC 滤波 → 降采样 → 检测 ┘
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .bcg import BCG_MIN_SEC, VitalsEngine
from .pressure import DEFAULT_CELL_THRESHOLD, DEFAULT_COUNT_THRESHOLD, PressureMap
from .readers import PressureReader, VitalsReader
from .serial_ports import available_ports, discover_ports
from .server import MonitorServer

log = logging.getLogger(__name__)

# 广播周期。波形按 20Hz 推, 每次只前进 1-2 个点, 页面上才不会一卡一卡的
BROADCAST_INTERVAL = 0.05
PRESSURE_EVERY_TICKS = 4          # 200ms 一次热图
# BCG 一次全窗计算约 5ms, 没必要每帧都跑; 每 2 秒一次足够
VITAL_CALC_EVERY_TICKS = int(2.0 / BROADCAST_INTERVAL)

# 无浏览器连接超过这么久就退出, 对应 C# 的看门狗
IDLE_EXIT_SECONDS = 15


# 页面上"保存"按钮写入的设置文件, 放在 python/ 目录下
SETTINGS_FILE = "settings.json"


@dataclass
class Options:
    host: str = "localhost"
    port: int | None = None
    web_root: Path | None = None
    pressure_port: str | None = None
    vitals_port: str | None = None
    # 下面三个为 None 时表示命令行没指定, 由 settings.json 或内置默认值填充
    query_parameter: int | None = None
    baudrate: int = 115200
    on_bed_cell: int | None = None
    on_bed_count: int | None = None
    open_browser: bool = True
    idle_exit: bool = True

    def resolved_web_root(self) -> Path:
        """页面目录。Python 版有自己的一份 UI, 不动 C# 版的 SleepMonitor/web。"""
        if self.web_root:
            return Path(self.web_root).expanduser().resolve()
        # python/sleepmonitor/monitor.py → python/ → python/web
        return (Path(__file__).resolve().parents[1] / "web").resolve()

    def settings_path(self) -> Path:
        return (Path(__file__).resolve().parents[1] / SETTINGS_FILE).resolve()


class MonitorService:
    def __init__(self, options: Options) -> None:
        self.options = options
        self._settings = self._load_settings()
        # 服务端口: 命令行 > settings.json > 默认 5000。改了要重启才生效。
        self._desired_server_port = self._setting("serverPort", options.port, 5000)
        self.server = MonitorServer(options.resolved_web_root(), options.host,
                                    self._desired_server_port)
        self.server.on_message(self._on_message)
        self.server.on_connect(self._broadcast_serial_state)

        self.pressure = PressureMap(
            self._setting("onBedCell", options.on_bed_cell, DEFAULT_CELL_THRESHOLD),
            self._setting("onBedCount", options.on_bed_count, DEFAULT_COUNT_THRESHOLD),
        )
        self.engine = VitalsEngine()

        self._pressure_lock = threading.Lock()
        self._vital_lock = threading.Lock()

        self._pressure_reader: PressureReader | None = None
        self._vitals_reader: VitalsReader | None = None
        self._serial_open = False
        self._pressure_port_name = ""
        self._vitals_port_name = ""
        self._forced_pressure_port: str | None = None   # 页面上手动选的串口
        self._forced_vitals_port: str | None = None
        self._query_parameter = self._setting("qp", options.query_parameter, 0x05) & 0xFF

        self._heart_rate = 0
        self._breath_rate = 0
        self._vital_ticks = VITAL_CALC_EVERY_TICKS   # 首次立刻算一次
        self._hr_sqi = 0.0
        self._br_sqi = 0.0
        self._apnea_events = 0

        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ================= 设置持久化 =================
    def _load_settings(self) -> dict[str, Any]:
        """读 python/settings.json。文件不存在或坏了都不影响启动。"""
        path = self.options.settings_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                log.info("已载入设置: %s", path)
                return data
        except Exception as exc:
            log.warning("读取设置失败, 用默认值: %s", exc)
        return {}

    def _setting(self, key: str, cli_value: int | None, fallback: int) -> int:
        """优先级: 命令行 > settings.json > 内置默认值。"""
        if cli_value is not None:
            return cli_value
        value = self._settings.get(key)
        return int(value) if isinstance(value, (int, float)) else fallback

    def _setting_str(self, key: str, cli_value: str | None) -> str | None:
        if cli_value:
            return cli_value
        value = self._settings.get(key)
        return value if isinstance(value, str) and value else None

    def _write_settings(self) -> Path:
        with self._pressure_lock:
            cell, count = self.pressure.cell_threshold, self.pressure.count_threshold
        self._settings = {
            "qp": self._query_parameter,
            "onBedCell": cell,
            "onBedCount": count,
            "pressurePort": self._pressure_port_name,
            "vitalsPort": self._vitals_port_name,
            "serverPort": self._desired_server_port,
        }
        path = self.options.settings_path()
        path.write_text(json.dumps(self._settings, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        log.info("设置已保存: %s %s", path, self._settings)
        return path

    # ================= 生命周期 =================
    async def run(self) -> None:
        await self.server.start()
        self._open_serials()

        self._tasks.append(asyncio.create_task(self._broadcast_loop(), name="broadcast"))
        if self.options.idle_exit:
            self._tasks.append(asyncio.create_task(self._watchdog(), name="watchdog"))

        if self.options.open_browser:
            url = f"http://{self.options.host}:{self._desired_server_port}/monitor.html"
            with contextlib.suppress(Exception):
                webbrowser.open(url)

        log.info("界面地址: http://%s:%d/monitor.html", self.options.host,
                 self._desired_server_port)
        try:
            await self._stop_event.wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        self._close_serials()
        await self.server.stop()
        log.info("已退出。")

    def request_stop(self) -> None:
        self._stop_event.set()

    # ================= 串口 =================
    def _open_serials(self) -> None:
        # 优先级: 页面选的 > 命令行 > settings.json > 自动识别
        pressure_port = self._forced_pressure_port or self._setting_str(
            "pressurePort", self.options.pressure_port)
        vitals_port = self._forced_vitals_port or self._setting_str(
            "vitalsPort", self.options.vitals_port)

        # 记住的串口可能因为换设备/换 USB 口已经不存在了, 这时退回自动识别,
        # 否则会一直打不开而看不出原因
        existing = {p["device"] for p in available_ports()}
        for name, value in (("压力垫", pressure_port), ("心率呼吸", vitals_port)):
            if value and value not in existing:
                log.warning("配置的%s串口 %s 不存在, 改用自动识别", name, value)
        if pressure_port not in existing:
            pressure_port = None
        if vitals_port not in existing:
            vitals_port = None

        if not (pressure_port and vitals_port):
            pair = discover_ports()
            pressure_port = pressure_port or pair.pressure
            vitals_port = vitals_port or pair.vitals
            log.info("串口自动识别 (%s): 压力=%s 心率呼吸=%s", pair.source, pressure_port, vitals_port)

        if not (pressure_port and vitals_port):
            self._serial_open = False
            log.warning("传感器连接失败: 未找到成对的串口设备")
            return

        try:
            # 重开时清空算法状态和热图, 避免残留旧数据
            with self._vital_lock:
                self.engine.reset()
                self._vital_ticks = VITAL_CALC_EVERY_TICKS
                self._heart_rate = 0
                self._breath_rate = 0
            with self._pressure_lock:
                self.pressure.reset()

            reader = PressureReader(pressure_port, self._on_pressure_frame,
                                    self._query_parameter, self.options.baudrate)
            reader.open()
            reader.start()
            self._pressure_reader = reader

            vitals = VitalsReader(vitals_port, self.engine, self._vital_lock,
                                  self.options.baudrate)
            vitals.open()
            vitals.start()
            self._vitals_reader = vitals

            self._pressure_port_name = pressure_port
            self._vitals_port_name = vitals_port
            self._serial_open = True
            log.info("传感器已连接（压力 %s / 心率呼吸 %s）", pressure_port, vitals_port)
        except Exception as exc:
            self._serial_open = False
            log.warning("传感器连接失败: %s", exc)
            self._close_serials()

    def _close_serials(self) -> None:
        self._serial_open = False
        for reader in (self._pressure_reader, self._vitals_reader):
            if reader is not None:
                with contextlib.suppress(Exception):
                    reader.stop()
        self._pressure_reader = None
        self._vitals_reader = None
        with self._vital_lock:
            self._heart_rate = 0
            self._breath_rate = 0

    def _reopen_serials(self) -> None:
        """关掉再按新配置打开。等驱动释放句柄, 否则立刻重开会失败。"""
        self._close_serials()
        time.sleep(0.8)
        self._open_serials()

    def _toggle_serial(self) -> None:
        if self._serial_open:
            self._close_serials()
            log.info("串口已手动断开")
        else:
            time.sleep(0.8)   # 等驱动释放上次的句柄, 否则立即重开会失败
            self._open_serials()

    def _on_pressure_frame(self, frame: bytes) -> None:
        """压力串口线程回调。卡尔曼依赖逐帧状态, 所以每帧都要处理。"""
        try:
            with self._pressure_lock:
                self.pressure.process_frame(frame)
        except Exception:
            log.exception("处理压力帧出错")

    # ================= 指标 =================
    def _update_vital_signs(self) -> None:
        reader = self._vitals_reader
        if reader is None or not reader.is_open():
            with self._vital_lock:
                self._heart_rate = 0
                self._breath_rate = 0
            return

        # 在离床由压力垫判定: 没人时 BPM 直接归零, 波形照常显示
        with self._pressure_lock:
            occupied = self.pressure.occupied

        with self._vital_lock:
            self._vital_ticks += 1
            if self._vital_ticks < VITAL_CALC_EVERY_TICKS:
                if not occupied:
                    self._heart_rate = self._breath_rate = 0
                return
            self._vital_ticks = 0

            if not occupied:
                self._heart_rate = self._breath_rate = 0
                self._hr_sqi = self._br_sqi = 0.0
                return

            if self.engine.seconds < BCG_MIN_SEC:
                return      # 攒够 30 秒才出第一个读数

            heart, resp = self.engine.compute()
            self._heart_rate = int(round(heart.hr_bpm)) if heart.ready else 0
            self._breath_rate = int(round(resp.rr_bpm)) if resp.ready else 0
            self._hr_sqi = heart.sqi
            self._br_sqi = resp.sqi
            self._apnea_events = resp.apnea_events

    # ================= 广播 =================
    async def _broadcast_loop(self) -> None:
        tick = 0
        while not self._stop_event.is_set():
            try:
                tick += 1
                if tick % PRESSURE_EVERY_TICKS == 0:
                    with self._pressure_lock:
                        data = self.pressure.press_map.tobytes()
                    await self.server.broadcast({"type": "pressure", "data": _b64(data)})

                self._update_vital_signs()
                with self._pressure_lock:
                    occupied = self.pressure.occupied
                    active_cells = self.pressure.active_cells
                with self._vital_lock:
                    heart_rate = self._heart_rate
                    breath_rate = self._breath_rate
                    hr_sqi, br_sqi = self._hr_sqi, self._br_sqi
                    hr_wave = np.fromiter(self.engine.heart_wave, dtype=np.float32)
                    br_wave = np.fromiter(self.engine.resp_wave, dtype=np.float32)

                await self.server.broadcast({
                    "type": "sensors",
                    "hr": heart_rate,
                    "br": breath_rate,
                    "hrWave": _b64(hr_wave.tobytes()),
                    "brWave": _b64(br_wave.tobytes()),
                    "occupied": occupied,
                    "cells": active_cells,
                    "hrSqi": round(hr_sqi, 1),
                    "brSqi": round(br_sqi, 1),
                    "apnea": self._apnea_events,
                })
            except Exception:
                log.exception("广播循环出错")
            await asyncio.sleep(BROADCAST_INTERVAL)

    async def _broadcast_serial_state(self) -> None:
        with self._pressure_lock:
            cell = self.pressure.cell_threshold
            count = self.pressure.count_threshold
        await self.server.broadcast({
            "type": "serial",
            "connected": self._serial_open,
            "pressurePort": self._pressure_port_name,
            "vitalsPort": self._vitals_port_name,
            "qp": self._query_parameter,
            "onBedCell": cell,
            "onBedCount": count,
            "serverPort": self._desired_server_port,
            "runningPort": self.server.port,
            "ports": available_ports(),
        })

    # ================= 页面控制消息 =================
    async def _on_message(self, message: dict[str, Any]) -> None:
        action = message.get("action")
        if not action:
            return
        log.info("收到控制消息: %s", action)

        if action == "toggleSerial":
            await asyncio.to_thread(self._toggle_serial)
            await self._broadcast_serial_state()

        elif action == "setPressureQueryParameter":
            try:
                value = int(message.get("value"))
            except (TypeError, ValueError):
                return
            self._query_parameter = max(0, min(255, value))
            if self._pressure_reader is not None:
                self._pressure_reader.query_parameter = self._query_parameter

        elif action == "setOnBedThresholds":
            try:
                cell = int(message.get("cell"))
                count = int(message.get("count"))
            except (TypeError, ValueError):
                return
            with self._pressure_lock:
                self.pressure.cell_threshold = max(0, min(255, cell))
                self.pressure.count_threshold = max(1, min(512, count))
            log.info("在离床阈值更新: 单点 %d / 测点数 %d",
                     self.pressure.cell_threshold, self.pressure.count_threshold)

        elif action == "listPorts":
            await self._broadcast_serial_state()

        elif action == "setPorts":
            pressure = (message.get("pressure") or "").strip() or None
            vitals = (message.get("vitals") or "").strip() or None
            log.info("切换串口: 压力=%s 心率呼吸=%s", pressure, vitals)
            self._forced_pressure_port = pressure
            self._forced_vitals_port = vitals
            await asyncio.to_thread(self._reopen_serials)
            await self._broadcast_serial_state()

        elif action == "setServerPort":
            try:
                port = int(message.get("value"))
            except (TypeError, ValueError):
                return
            if not 1 <= port <= 65535:
                return
            self._desired_server_port = port
            log.info("服务端口将改为 %d (重启后生效)", port)
            await self._broadcast_serial_state()

        elif action == "shutdown":
            log.info("收到页面的退出指令")
            await self.server.broadcast({"type": "bye"})
            self.request_stop()

        elif action == "saveSettings":
            try:
                path = await asyncio.to_thread(self._write_settings)
                await self.server.broadcast({"type": "saved", "ok": True, "path": str(path)})
            except Exception as exc:
                log.error("保存设置失败: %s", exc)
                await self.server.broadcast({"type": "saved", "ok": False, "error": str(exc)})

    # ================= 看门狗 =================
    async def _watchdog(self) -> None:
        """浏览器全部断开超过 15 秒才退出 —— 页面刷新 2 秒内会自动重连, 不误判。"""
        idle_seconds = 0
        while not self._stop_event.is_set():
            await asyncio.sleep(1.0)
            if self.server.client_count <= 0:
                idle_seconds += 1
                if idle_seconds >= IDLE_EXIT_SECONDS:
                    log.info("超过 %d 秒无浏览器连接，程序退出", IDLE_EXIT_SECONDS)
                    self.request_stop()
                    return
            else:
                idle_seconds = 0


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
