# stub

The ~180-byte SH-2 read-only stub that gets uploaded to ECU RAM and takes over the K-line.

| File | Purpose |
|---|---|
| `build_stub.py` | Pure-Python SH-2 BE encoder + the stub source. Run it to produce `stub.bin`. No external SH-2 toolchain required. |
| `stub.bin` | The assembled stub bytes. Already built and ready to upload. |
| `test_stub.py` | A small SH-2 emulator + 5 functional tests. Validates the stub end-to-end without needing hardware. |

## Build / test

```bash
python build_stub.py    # produces stub.bin (180 bytes)
python test_stub.py     # functional tests, expect "OVERALL: PASS"
```

## Stub design

```
0x40E000  init             disable IRQ, load SCI1+WDT bases
0x40E00E  flush_rx         drain any leftover RX from before stub took over
0x40E020  main_loop        pet WDT, read 6-byte command (4 addr + 2 len)
0x40E04E  send_loop        pet WDT, send N bytes from addr, decrement, loop
0x40E060  read_byte        poll RDRF on SSR, read RDR, clear RDRF flag
0x40E078  write_byte       poll TDRE, write TDR, clear TDRE, drain echo
0x40E0AC  literals         sci1_base = 0xFFFFF008, wdt_base = 0xFFFFEC10
```

### Protocol

```
host -> stub:  [a3] [a2] [a1] [a0] [l1] [l0]    (4-byte BE address + 2-byte BE length)
stub -> host:  raw stream of `len` bytes from `addr`
```

That's it. No framing, no checksum, no retry.

## Why the encoder is in Python

Cross-platform: works on macOS, Linux, Windows with just Python 3, no SH-2 cross-toolchain to install. SH-2A is a fixed 16-bit instruction set with regular encoding; covering the ~22 instructions we use is < 100 lines of Python.

## Modification ideas

The stub is small enough that you can hand-modify and re-test in seconds. Some natural extensions:

- **Add a write command** — implement the SH7055 flash controller routines (FCU). Adds maybe 200 bytes. Be careful with this one.
- **Add a "memory probe" command** — read-attempt with bus-error catching. Lets you discover what address ranges actually exist on the chip without crashing.
- **Add cyclic measurement** — modeled after CCP DAQ, periodically push N bytes from a list of addresses (live data streaming).
- **Add a "find pattern" command** — string-search through flash on the ECU side, return offsets only. Saves bandwidth when looking for specific signatures.

Whatever you add, run `test_stub.py` after every change. The emulator catches most issues before you risk hardware.
