# SH7055 / Sagem S3000 memory map

The Sagem S3000 ECU uses a **Renesas SH7055** microcontroller (SH-2A core, big-endian, 1 MB on-chip flash, 24 KB on-chip RAM).

## CPU

- **Core**: SH-2A (32-bit RISC, 16-bit fixed-length instructions, big-endian)
- **Manufactured by**: Renesas (formerly Hitachi)
- **Used in**: Sagem ECUs by Valeo Pontoise (France)

## Memory map

| Range | Size | Purpose |
|---|---|---|
| `0x00000000` – `0x00003FFF` | 16 KB | **Boot ROM** (factory-locked, not readable from app code) |
| `0x00004000` – `0x00007FFF` | 16 KB | `CODE1` — application boot/init (often unused, all FF on N/A) |
| `0x00020084` – `0x0003FFFF` | ~131 KB | `DATA1` — calibration |
| `0x00040000` – `0x000FFFFB` | ~768 KB | `CODE2` — main firmware code |
| `0x40E000` | (RAM) | Where Galletto's bootloader (and ours) is uploaded |
| `0xFFFF6000` – `0xFFFFBFFF` | 24 KB | On-chip RAM |
| `0xFFFFE400` – `0xFFFFFFFF` | — | Peripheral registers |

The lower 16 KB (boot ROM) is not in any flash dump — only the boot ROM itself can read those addresses, and it doesn't expose them.

## Application header at `0x40000`

The first 16 bytes of `CODE2` are an application descriptor checked by the boot ROM:

```
0x40000:  89 5B FF FF        magic = 0x895BFFFF
0x40004:  00 02 00 00        cal-area pointer = 0x00020000
0x40008:  FF FF FF FF        reserved
0x4000C:  FF FF FF FF        reserved
0x40010:  ... vector table starts here (32-bit BE function pointers)
```

Vector table layout follows SH-2 convention:
- `vec[0]` = power-on reset PC = first user instruction
- `vec[1]` = power-on reset SP = top of on-chip RAM
- `vec[2]` = manual reset PC
- `vec[3]` = manual reset SP
- `vec[4+]` = exception/interrupt handlers (most point to a default no-op handler)

## Peripheral registers we touch

### SCI1 (K-line UART)

| Register | Address | Purpose |
|---|---|---|
| `SMR1`  | `0xFFFFF008` | mode |
| `BRR1`  | `0xFFFFF009` | baud rate |
| `SCR1`  | `0xFFFFF00A` | TX/RX enable + interrupts |
| `TDR1`  | `0xFFFFF00B` | byte to transmit |
| `SSR1`  | `0xFFFFF00C` | status (TDRE bit 7, RDRF bit 6, ORER bit 5, FER bit 4) |
| `RDR1`  | `0xFFFFF00D` | received byte |
| `SDCR1` | `0xFFFFF00E` | smart-card mode (unused for K-line) |

K-line is on **SCI1**, confirmed by reverse-engineering the stock firmware's RXI handler at `sub_9628E` — it touches `0xFFFFF00C` (SCR1+2 = SSR1) and the variable `rt_Communication_k_active`.

### Watchdog (WDT)

| Register | Address | Notes |
|---|---|---|
| `WDT_TCSR` | `0xFFFFEC10` | control/status, password byte `0xA5` |
| `WDT_TCNT` | `0xFFFFEC10` (word write with high byte `0x5A`) | counter, password byte `0x5A` |
| `WDT_RSTCSR` | `0xFFFFEC12` | reset control |

Pet sequence: `mov.w #0x5A00, @0xFFFFEC10` — password `0x5A` + counter reload to `0x00` (max time before next overflow).

The WDT is enabled at boot by the stock firmware (`sub_9F63A`). Our stub pets it inside the main read loop (and in the helper functions for safety).

## Calibration A2L

The Renault A2L file (`26430000.A2L` for Megane RS, similar for non-RS) is the authoritative description of:
- 6,319 `CHARACTERISTIC` entries (~50 KB densely packed in `DATA1`)
- 8,601 `MEASUREMENT` entries (RAM real-time variables)
- 511 `FUNCTION` entries (logical groupings)

A2L is ASAM-MCD-2 standard, parseable with any A2L tool (e.g., python `pya2l`).
