"""两路串口的后台读取线程。

* :class:`PressureReader` —— 对应 SleepMonitor/SerialReader.cs:
  每帧固定 514 字节, 收满立刻回一个 ``'1' + 查询参数`` 的应答, 下位机据此吐下一帧。
* :class:`VitalsReader` —— 打开后发 ``D`` 启动原始 ADC 上报, 按 6 字节定长帧解析,
  取 ADC2 送进 BCG 引擎; 心跳和呼吸都由带通从这一路信号分出来。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import serial

from .bcg import VitalsEngine
from .pressure import FRAME_SIZE

log = logging.getLogger(__name__)

BAUDRATE = 115200


def _open(port: str, baudrate: int) -> serial.Serial:
    handle = serial.Serial(
        port, baudrate, bytesize=8, parity=serial.PARITY_NONE, stopbits=1,
        timeout=1.0, write_timeout=1.0,
    )
    handle.reset_input_buffer()
    return handle


class PressureReader(threading.Thread):
    """压力垫读取线程, 收满一帧就通过 ``on_frame`` 回调抛出去。"""

    def __init__(self, port: str, on_frame: Callable[[bytes], None],
                 query_parameter: int = 0x05, baudrate: int = BAUDRATE) -> None:
        super().__init__(name="PressureReader", daemon=True)
        self._port_name = port
        self._on_frame = on_frame
        self._query_parameter = query_parameter & 0xFF
        self._baudrate = baudrate
        self._stop = threading.Event()
        self._serial: serial.Serial | None = None
        self.frames_received = 0
        self.last_error: str | None = None

    @property
    def query_parameter(self) -> int:
        return self._query_parameter

    @query_parameter.setter
    def query_parameter(self, value: int) -> None:
        self._query_parameter = max(0, min(255, int(value)))

    def open(self) -> None:
        self._serial = _open(self._port_name, self._baudrate)
        self._serial.write(b"11")      # 启动命令, 与 OpenSerials 一致

    def run(self) -> None:
        assert self._serial is not None, "先调用 open()"
        buffer = bytearray()
        while not self._stop.is_set():
            try:
                chunk = self._serial.read(FRAME_SIZE - len(buffer))
                if not chunk:
                    # 读超时: 下位机可能在等应答, 补发一次
                    self._serial.write(bytes([ord("1"), self._query_parameter]))
                    continue
                buffer.extend(chunk)
                if len(buffer) < FRAME_SIZE:
                    continue

                frame = bytes(buffer[:FRAME_SIZE])
                del buffer[:FRAME_SIZE]
                self._serial.write(bytes([ord("1"), self._query_parameter]))
                self.frames_received += 1
                self._on_frame(frame)
            except Exception as exc:
                self.last_error = str(exc)
                if not self._stop.is_set():
                    log.warning("压力串口读取异常: %s", exc)
                if self._stop.wait(0.2):
                    break

    def stop(self) -> None:
        self._stop.set()
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception:
                pass


class VitalsReader(threading.Thread):
    """心率 / 呼吸传感器读取线程。

    ``D`` 协议的帧格式 (6 字节定长, 200Hz)::

        FA 04 ADC1_H ADC1_L ADC2_H ADC2_L

    两路都是 12 位 ADC(围绕中点 2048)。**只取 ADC2** —— 那是原始 BCG 信号,
    心跳和呼吸都从这一路用不同带通分出来; ADC1 实测几乎不动(std ≈ 2), 不用。

    采样值按固件约定减去 2048 后送进 :class:`~sleepmonitor.bcg.VitalsEngine`。
    """

    FRAME_LEN = 6
    SYNC = b"\xfa\x04"
    ADC_MIDPOINT = 2048

    def __init__(self, port: str, engine: VitalsEngine, lock: threading.Lock,
                 baudrate: int = BAUDRATE) -> None:
        super().__init__(name="VitalsReader", daemon=True)
        self._port_name = port
        self._engine = engine
        self._lock = lock
        self._baudrate = baudrate
        self._stop = threading.Event()
        self._serial: serial.Serial | None = None
        self._buffer = bytearray()

        self.frames_received = 0
        self.resyncs = 0
        self.bytes_received = 0
        self.last_error: str | None = None

    def open(self) -> None:
        self._serial = _open(self._port_name, self._baudrate)
        # D 启动下位机原始 ADC 上报, 所有滤波都在上位机做,
        # 保证检测器和页面波形用的是同一路信号
        self._serial.write(b"D")

    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    @property
    def seconds_buffered(self) -> float:
        return self._engine.seconds

    def run(self) -> None:
        assert self._serial is not None, "先调用 open()"
        while not self._stop.is_set():
            try:
                # 阻塞等第 1 个字节, 然后把缓冲里已有的全部带走。
                # 不能用 read(4096): 数据流速 ~1.2KB/s 永远攒不满,
                # 会等满 1 秒超时才返回, 页面波形就一秒跳一格。
                chunk = self._serial.read(1)
                if not chunk:
                    continue
                waiting = self._serial.in_waiting
                if waiting:
                    chunk += self._serial.read(waiting)
                self.bytes_received += len(chunk)
                self._buffer.extend(chunk)
                self._drain()
            except Exception as exc:
                self.last_error = str(exc)
                if not self._stop.is_set():
                    log.warning("心率串口读取异常: %s", exc)
                if self._stop.wait(0.2):
                    break

    def _drain(self) -> None:
        """从缓冲里尽可能多地取出完整帧, 丢帧时重新同步。"""
        buffer = self._buffer
        samples: list[float] = []
        i = 0
        n = len(buffer)

        while n - i >= self.FRAME_LEN:
            if buffer[i] != 0xFA or buffer[i + 1] != 0x04:
                # 丢同步: 找下一个帧头
                nxt = buffer.find(self.SYNC, i + 1)
                if nxt < 0:
                    i = n - 1          # 留一个字节, 防止帧头被切开
                    break
                self.resyncs += 1
                i = nxt
                continue

            adc2 = (buffer[i + 4] << 8) | buffer[i + 5]
            samples.append(float(adc2 - self.ADC_MIDPOINT))
            self.frames_received += 1
            i += self.FRAME_LEN

        del buffer[:i]
        if samples:
            with self._lock:
                for value in samples:
                    self._engine.push(value)

    def stop(self) -> None:
        self._stop.set()
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception:
                pass
