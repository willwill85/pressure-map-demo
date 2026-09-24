"""串口发现, 对应 MonitorService.GetCommPorts。

原实现用 SetupDi 读设备友好名, 靠 "Enhanced"/"Standard" (CP2105 双口) 或
"A CH342"/"B CH342" 区分两个口。这里用 pyserial 做同样的事, 并补上
macOS / Linux 的判定方式方便联调。

角色约定与 C# 的返回顺序一致:

* ``vitals``   —— ports[0], 心率呼吸传感器 (Enhanced / A CH342)
* ``pressure`` —— ports[1], 压力垫 (Standard / B CH342)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from serial.tools import list_ports
from serial.tools.list_ports_common import ListPortInfo

CP2105_VID = 0x10C4
CP2105_PID = 0xEA70


@dataclass
class PortPair:
    vitals: str | None
    pressure: str | None
    source: str        # 判定依据, 便于排错

    def is_complete(self) -> bool:
        return bool(self.vitals) and bool(self.pressure)


def _describe(port: ListPortInfo) -> str:
    return " ".join(filter(None, [port.description, port.product, port.manufacturer, port.interface]))


def _by_friendly_name(ports: list[ListPortInfo]) -> PortPair | None:
    """Windows 路线: 按设备友好名匹配, 与 C# GetCommPorts 等价。"""
    vitals = pressure = None
    fallback: list[str] = []

    for port in ports:
        text = _describe(port)
        if "A CH342" in text:
            vitals = port.device
        elif "B CH342" in text:
            pressure = port.device
        elif "Enhanced" in text:
            vitals = port.device
        elif "Standard" in text:
            pressure = port.device
        elif "CP210" in text:
            fallback.append(port.device)

    if vitals and pressure:
        return PortPair(vitals, pressure, "friendly-name")

    # 只认出 CP210x 分不清主次时按枚举顺序兜底 (C# 里也是这么处理的)
    if len(fallback) >= 2:
        fallback.sort()
        return PortPair(fallback[0], fallback[1], "cp210x-order")
    return None


def _by_usb_interface(ports: list[ListPortInfo]) -> PortPair | None:
    """macOS / Linux 路线: CP2105 两个接口按接口号排序, 0=Enhanced, 1=Standard。"""
    candidates = [p for p in ports if p.vid == CP2105_VID and p.pid == CP2105_PID]
    if len(candidates) < 2:
        return None
    candidates.sort(key=lambda p: (p.location or "", p.device))
    return PortPair(candidates[0].device, candidates[1].device, "cp2105-interface")


def discover_ports() -> PortPair:
    """自动识别两个串口, 认不出来的字段为 None。"""
    ports = list(list_ports.comports())
    finders = (_by_friendly_name, _by_usb_interface) if sys.platform.startswith("win") \
        else (_by_usb_interface, _by_friendly_name)

    for finder in finders:
        result = finder(ports)
        if result and result.is_complete():
            return result
    return PortPair(None, None, "not-found")


def available_ports() -> list[dict]:
    """给页面下拉框用的串口清单。"""
    pair = discover_ports()
    result = []
    for port in sorted(list_ports.comports(), key=lambda p: p.device):
        if port.device == pair.vitals:
            role = "心率呼吸"
        elif port.device == pair.pressure:
            role = "压力垫"
        else:
            role = ""
        result.append({
            "device": port.device,
            "description": _describe(port) or "",
            "role": role,
        })
    return result


def format_port_table() -> str:
    """人可读的串口清单, 给 ``list-ports`` 子命令用。"""
    ports = list(list_ports.comports())
    if not ports:
        return "未发现任何串口设备。"

    pair = discover_ports()
    lines = [f"{'设备':<28} {'VID:PID':<12} {'角色':<10} 描述", "-" * 88]
    for port in sorted(ports, key=lambda p: p.device):
        if port.device == pair.vitals:
            role = "心率呼吸"
        elif port.device == pair.pressure:
            role = "压力垫"
        else:
            role = "-"
        vid_pid = f"{port.vid:04X}:{port.pid:04X}" if port.vid and port.pid else "-"
        lines.append(f"{port.device:<28} {vid_pid:<12} {role:<10} {_describe(port) or '-'}")
    lines += ["", f"判定依据: {pair.source}"]
    return "\n".join(lines)
