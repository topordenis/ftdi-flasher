# Troubleshooting

Things that can go wrong on first run, ordered by likelihood.

## "Could not open COM3" / port busy

- **Galletto.exe (or another tool) is using the port** — close it.
- **Wrong COM number** — check Device Manager, pass `--port COMx`.
- **Multiple FTDI cables** — disconnect the others.

## Cable connects but no response from ECU

Symptoms: timeout at fast init.

- **Ignition not ON** — the ECU has no power. Key must be in the ON position (engine doesn't have to be running).
- **K-line not connected** — cable wiring problem. Multimeter from cable's K-line pin to OBD-II pin 7 should show continuity.
- **DTR polarity inverted on the cable** — try editing `KlineSerial.set_dtr` to invert (`self.ser.dtr = not on`).
- **Wake timing** — try increasing the LOW/HIGH pulses in `SagemS3000.fast_init` from 25 ms to 50 ms.

## SecurityAccess fails

Symptoms: ECU rejects the key with `7F 27 35` (invalid key) or similar NRC.

- **Wrong polynomial** — for Sagem S3000 it's `0x28488863`, 35 iters, big-endian. If your ECU is a different Sagem variant (S2000, S3000+, etc.) the polynomial may differ.
- **Endianness mistake** — verify the 4 seed bytes are read as big-endian when computing the key.
- **Wrong sub-function** — for Sagem S3000 the request is `0x27 0x15` for seed and `0x27 0x16` for key. Some other ECUs use `0x27 0x01` / `0x27 0x02`.

## TransferData fails

Symptoms: ECU returns `7F 36 ??` after we send the stub bytes.

- **Wrong RequestDownload header** — verify address (`0x40E000`) and size match what we're actually sending.
- **Frame too large** — we send the stub in one TransferData chunk. If the ECU rejects, split into ≤256-byte pieces and send multiple TransferDatas.
- **Address restriction** — the ECU's bootloader only accepts uploads to specific RAM ranges. `0x40E000` is the documented range.

## Stub uploads, but no response after StartRoutine

Symptoms: `StartRoutine` returns OK, but our 6-byte command times out.

- **SCI1 not configured for TX/RX after ChangeSpeed** — likely the SCR has TE=1 but RE=0. Add this at stub init (in `build_stub.py`):
  ```python
  a.mov_imm(0, 0x30)                  # TE=1 RE=1
  a.mov_b_r0_disp_rn(1, 2)            # SCR1 = 0x30
  ```
  Re-build, re-test in emulator, re-upload.
- **Stub crashed on entry** — something we're doing in init violates SH-2A semantics. Check by uploading a stub that's literally `bra 0` (infinite loop) and seeing if the ECU stays alive.

## Read returns garbage / wrong bytes

Symptoms: dump file is non-zero but doesn't match expected ECU contents.

- **Echo not drained** — host is reading our own TX as response. Check `KlineSerial.read` is consuming `len(frame)` bytes after each write.
- **Endianness in addr/len header** — we send 4-byte addr + 2-byte len both big-endian. If you swapped, you read at the wrong place.
- **Stub address-arithmetic bug** — our emulator covered this case but real hardware behavior may differ. Test with `--start 0x4000 --end 0x4040 --chunk 64` (small range) and compare bytes by hand.

## ECU resets mid-read

Symptoms: read works for a few seconds, then K-line goes silent and a new read attempt starts failing at wake.

- **Watchdog timeout** — our stub pets the WDT inside the main loop, but if SCI is stuck the pet never happens. Fix: add an unconditional pet at every poll iteration (already done in current `build_stub.py`).
- **Other ECU integrity check** — stock firmware has additional checks (e.g., CCP keep-alive, OBD logger) that can reset us. Less likely on key-on-engine-off.

## "Not yet validated on hardware"

This README admits the tool hasn't been run on a real car yet. The static analysis + emulator give us high confidence, but hardware always surfaces something. When you run the first real test:

1. Start with `--identify` (no SecurityAccess, no upload — just validates wake + KWP2000 round-trip).
2. If that prints sensible LID values, escalate to a tiny real read: `--start 0x4000 --end 0x4040 --chunk 64`.
3. If THAT works, do the full 1 MB.

Each step that succeeds rules out a layer of bugs.
