"""命令行入口。

    python -m sleepmonitor run          # 启动监测程序 (默认)
    python -m sleepmonitor list-ports   # 列出串口并显示自动识别结果
    python -m sleepmonitor selftest     # 自检: 依赖 / 页面资源 / 串口 / 算法
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .monitor import MonitorService, Options


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        default="INFO", help="日志级别 (默认 INFO)")

    parser = argparse.ArgumentParser(
        prog="sleepmonitor",
        description="睡眠监测 — SleepMonitor (C#/.NET) 的 Python 实现",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="启动监测程序 (默认命令)", parents=[common])
    run.add_argument("--pressure-port", metavar="PORT", help="压力垫串口, 不填则自动识别")
    run.add_argument("--vitals-port", metavar="PORT", help="心率呼吸串口, 不填则自动识别")
    run.add_argument("--baudrate", type=int, default=115200, help="波特率 (默认 115200)")
    run.add_argument("--query-parameter", type=lambda v: int(v, 0), default=None,
                     metavar="N", help="压力查询参数, 支持 0x05 写法 (默认读 settings.json, 再默认 0x05)")
    run.add_argument("--host", default="localhost", help="监听地址 (默认 localhost)")
    run.add_argument("--port", type=int, default=None,
                     help="监听端口 (默认读 settings.json, 再默认 5000)")
    run.add_argument("--web-root", metavar="DIR", help="页面目录 (默认仓库的 SleepMonitor/web)")
    run.add_argument("--on-bed-cell", type=int, default=None,
                     help="在离床: 单个测点算有压力的阈值 (默认 30)")
    run.add_argument("--on-bed-count", type=int, default=None,
                     help="在离床: 超阈值测点数达到多少算有人 (默认 40)")
    run.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    run.add_argument("--keep-alive", action="store_true",
                     help="没有浏览器连接也不退出 (默认 15 秒无连接自动退出)")

    sub.add_parser("list-ports", help="列出串口并显示自动识别结果", parents=[common])
    sub.add_parser("selftest", help="自检: 依赖 / 页面资源 / 串口 / 算法", parents=[common])
    return parser


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def _cmd_run(args: argparse.Namespace) -> int:
    options = Options(
        host=args.host,
        port=args.port,
        web_root=args.web_root,
        pressure_port=args.pressure_port,
        vitals_port=args.vitals_port,
        query_parameter=args.query_parameter,
        baudrate=args.baudrate,
        on_bed_cell=args.on_bed_cell,
        on_bed_count=args.on_bed_count,
        open_browser=not args.no_browser,
        idle_exit=not args.keep_alive,
    )
    service = MonitorService(options)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, service.request_stop)
            except NotImplementedError:   # Windows 不支持
                pass
        await service.run()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:          # 端口占用等启动期的可预期失败
        print(f"\n[错误] {exc}\n", file=sys.stderr)
        return 1
    return 0


def _cmd_list_ports() -> int:
    from .serial_ports import format_port_table

    print(format_port_table())
    return 0


def _cmd_selftest() -> int:
    failures = 0

    print("== 依赖 ==")
    for module in ("serial", "numpy", "aiohttp"):
        try:
            __import__(module)
            print(f"  [ok]   {module}")
        except ImportError as exc:
            failures += 1
            print(f"  [FAIL] {module}: {exc}")

    print("\n== 页面资源 ==")
    root = Options().resolved_web_root()
    for name in ("monitor.html",):
        path = root / name
        ok = path.is_file()
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'MISSING':<7}] {path}")

    print("\n== 串口 ==")
    from .serial_ports import discover_ports

    pair = discover_ports()
    print(f"  压力垫  : {pair.pressure or '未找到'}")
    print(f"  心率呼吸: {pair.vitals or '未找到'}")
    print(f"  判定依据: {pair.source}")
    if not pair.is_complete():
        print("  提示: 未插设备或驱动未装时属正常, 不计为失败。")

    print("\n== 算法自检 ==")
    try:
        _vitals_smoke_test()
        print("  [ok]   心率/呼吸算法可运行")
    except Exception as exc:
        failures += 1
        print(f"  [FAIL] 算法异常: {exc}")

    try:
        _pressure_smoke_test()
        print("  [ok]   压力图处理可运行")
    except Exception as exc:
        failures += 1
        print(f"  [FAIL] 压力图处理异常: {exc}")

    print(f"\n结论: {'全部通过' if failures == 0 else f'{failures} 项失败'}")
    return 0 if failures == 0 else 1


def _vitals_smoke_test() -> None:
    """用合成信号验证 BCG: 72bpm 心跳 + 15 次/分呼吸, 看能不能还原出来。"""
    import math

    from .bcg import BCG_FS, VitalsEngine

    engine = VitalsEngine()
    hr_hz, br_hz = 72 / 60.0, 15 / 60.0
    for i in range(BCG_FS * 60):
        t = i / BCG_FS
        sample = (120.0 * math.sin(2 * math.pi * br_hz * t)      # 呼吸: 大幅低频
                  + 25.0 * math.sin(2 * math.pi * hr_hz * t))    # 心跳: 小幅高频
        engine.push(sample)
    heart, resp = engine.compute()
    print(f"         合成 72bpm/15次分 → 检出 心率={heart.hr_bpm:.1f} 呼吸={resp.rr_bpm:.1f} "
          f"(SQI {heart.sqi:.0f}/{resp.sqi:.0f})")
    assert heart.ready and resp.ready, "BCG 未能在合成信号上给出结果"


def _pressure_smoke_test() -> None:
    import numpy as np

    from .pressure import GRID_H, GRID_W, PressureMap

    mapper = PressureMap()
    rng = np.random.default_rng(42)
    for _ in range(3):
        frame = bytes([0xFF, 0x31]) + bytes(rng.integers(0, 255, 512, dtype=np.uint8).tolist())
        press_map = mapper.process_frame(frame)
    assert press_map.shape == (GRID_W * GRID_H,), press_map.shape
    print(f"         压力图 {press_map.shape[0]} 点, 峰值={press_map.max():.2f}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = _build_parser()
    args = parser.parse_args(argv)

    command = args.command or "run"
    if command == "run" and args.command is None:
        # 不带子命令时等价于 run, 重新解析一次好拿到 run 的默认值
        args = parser.parse_args([*argv, "run"])
    _setup_logging(args.log_level)

    if command == "run":
        return _cmd_run(args)
    if command == "list-ports":
        return _cmd_list_ports()
    if command == "selftest":
        return _cmd_selftest()

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
