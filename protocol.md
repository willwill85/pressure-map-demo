# MIAN Pressure Mat Serial Protocol

This document summarizes the dual serial interface protocol for the MIAN pressure mat. The mat provides:

- Pressure-array data from the Standard serial port
- Piezoelectric raw data for heart-rate and respiration analysis from the Enhanced serial port

The protocol uses raw serial bytes. Do not append carriage returns, line feeds, or string terminators.

## V3.0 Serial Configuration

| Interface | Windows device | Baud rate | Data bits | Parity | Stop bits | Flow control |
|---|---|---:|---:|---|---:|---|
| Pressure array | Silicon Labs Dual CP2105 USB to UART Bridge: Standard COM Port | 115200 | 8 | None | 1 | None |
| Piezo heart/respiration | Silicon Labs Dual CP2105 USB to UART Bridge: Enhanced COM Port | 115200 | 8 | None | 1 | None |

The COM port number is assigned by Windows and must be detected at runtime.

## Enhanced Port: Heart Rate and Respiration

### B command

Send ASCII `B`, or the raw byte `0x42`, to start the legacy two-byte stream.

At 200 Hz, the device continuously returns two bytes per sample:

| Byte | Meaning |
|---:|---|
| 0 | Raw respiration-rate signal |
| 1 | Raw heart-rate signal |

Example: `79 92 ...`

### D command

Send ASCII `D`, or the raw byte `0x44`, to start the framed six-byte stream.

At 200 Hz, each frame is:

```text
FA 04 n1 n2 n3 n4
```

| Position | Meaning |
|---:|---|
| 1 | Fixed frame header `0xFA` |
| 2 | Fixed length value `0x04` |
| 3 | Respiration sample, high 8 bits |
| 4 | Respiration sample, low 8 bits |
| 5 | Heart-rate sample, high 8 bits |
| 6 | Heart-rate sample, low 8 bits |

The 16-bit sample values are reconstructed in big-endian order:

```python
respiration = (n1 << 8) | n2
heart_rate = (n3 << 8) | n4
```

The `D` stream is preferred because the fixed header and length byte allow frame synchronization.

## Standard Port: Pressure Array

### Request

Send the two raw bytes below to request one pressure-map frame:

```text
31 A8
```

### Response

Read exactly 514 bytes:

```text
FF 31 [512 pressure values]
```

- Byte offset `0`: fixed prefix `0xFF`
- Byte offset `1`: fixed prefix `0x31`
- Byte offsets `2..513`: 512 unsigned pressure values, range `0..255`
- Layout: 16 columns × 32 rows
- Byte offset `2`: row `0`, column `0`
- Byte offset `17`: row `0`, column `15`
- Byte offset `18`: row `1`, column `0`
- Byte offset `513`: row `31`, column `15`

For a zero-based row and column, the payload offset is:

```python
payload_offset = 2 + row * 16 + column
value = frame[payload_offset]
```

A complete pressure frame is therefore:

```python
assert len(frame) == 514
assert frame[0:2] == bytes([0xFF, 0x31])
pressure = frame[2:]  # 512 values
```

## V1.0 Compatibility Notes

Older V1.0 units may expose two CH342 USB serial ports:

| Interface | Baud rate | Data bits | Parity | Stop bits |
|---|---:|---:|---|---:|
| Piezo data (`USB-Enhanced-SERIAL-A CH342`) | 115200 | 8 | None | 1 |
| Pressure data (`USB-Enhanced-SERIAL-B CH342`) | 57600 | 8 | None | 1 |

The V1.0 piezo interface uses the `B` command and returns two bytes at 200 Hz. The V1.0 pressure interface requests the four pressure membranes in sequence with the ASCII strings `11`, `22`, `33`, and `44`.

A V1.0 membrane response is 642 bytes:

- Byte `0`: fixed prefix `0xFF`
- Byte `1`: membrane ID (`0x31`, `0x32`, `0x33`, or `0x34`)
- Bytes `2..641`: 640 pressure values for a 16 × 40 membrane

Confirm the hardware revision before selecting the V1.0 or V3.0 command set.

## Related Code

The Python serial readers are in `sleepmonitor/readers.py` and `sleepmonitor/pressure.py`.
