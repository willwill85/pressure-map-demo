# Pressure Map Demo

A Python-based sleep and pressure monitoring demo. It reads pressure-mat and vital-sign sensor data, displays a live pressure map and waveforms in a browser, and provides basic on-bed detection and heart-rate / respiration analysis.

## Features

- Live 16 × 32 pressure-map visualization
- On-bed / off-bed detection based on pressure data
- Heart-rate and respiration-rate estimation from BCG data
- Browser interface with adjustable detection thresholds
- Serial-port discovery and self-test commands

## Requirements

- Python 3.10 or newer
- `pyserial`, `numpy`, and `aiohttp`
- A compatible pressure mat and serial sensor, when using live hardware

## Quick Start

### macOS / Linux

```bash
chmod +x run.sh
./run.sh
```

### Windows

Double-click `run.bat`, or run:

```bat
run.bat
```

The startup script creates a virtual environment, installs dependencies, starts the service, and opens the web interface.

## Command Line

```bash
# List available serial ports
./run.sh list-ports

# Run built-in checks
./run.sh selftest

# Start with explicit sensor ports
./run.sh run --pressure-port /dev/ttyUSB0 --vitals-port /dev/ttyUSB1
```

On Windows, use `run.bat` with the same subcommands and replace the port names with values such as `COM5`.

## Sensor Interfaces

### Vital-sign sensor

The vital-sign port uses ASCII `D` to start raw ADC streaming. Each frame is 6 bytes at 200 Hz:

```text
FA 04 ADC1_H ADC1_L ADC2_H ADC2_L
```

Both channels are 12-bit ADC values. The current BCG pipeline uses ADC2 for heart-rate and respiration analysis.

### Pressure mat

The pressure mat uses a 16 × 32 sensor grid. A complete frame contains 514 bytes (`16 × 32 + 2`). After receiving a complete frame, the reader returns the query response beginning with the byte `1`.

## Configuration

Runtime thresholds are stored in `settings.json`. The web interface can update the on-bed cell and count thresholds, and changes are saved for subsequent runs.

Command-line options take precedence over `settings.json`, which takes precedence over built-in defaults.

## Project Layout

- `sleepmonitor/` — Python service, serial readers, signal processing, and pressure handling
- `web/monitor.html` — browser-based monitoring interface
- `settings.json` — runtime threshold configuration
- `run.sh` / `run.bat` — startup scripts
- `HANDOFF.md` — development handoff notes
