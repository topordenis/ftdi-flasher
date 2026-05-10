# Protocol notes

This document captures the K-line / KWP2000 / custom-stub protocol details specific to the Sagem S3000.

## Physical layer

- **K-line, half-duplex single-wire serial** at 12 V idle level
- ECU drives K-line via OBD-II pin 7
- Initial baud: **10 400 bps**, 8N1
- After change-speed: **125 000 bps**, 8N1
- The diagnostic cable's UART receives its own TX as RX (echo); software on both sides drains the echo

## Wake — KWP2000 fast init

```
host: K-line LOW for ~25 ms (via DTR-driven pull-down on the cable)
host: K-line HIGH for ~25 ms
host: send StartCommunication frame at 10400 baud
ECU:  respond with positive response (SID 0xC1) + key bytes (W1, W2, ...)
```

Frame format (KWP2000 short-with-length, addressed):

```
[0x80 | data_len] [tgt=0x12] [src=0xF1] [data...] [cksum]
```

`tgt = 0x12` is the **Sagem S3000 K-line address** (Renault). `src = 0xF1` is the standard tester address. `cksum = sum_mod_256` of all preceding bytes.

For data lengths > 0x3F, switch to long-form:
```
[0x80] [tgt] [src] [data_len] [data...] [cksum]
```

## KWP2000 services we use

| SID  | Name | Purpose |
|------|------|---------|
| 0x10 | StartDiagnosticSession | enter programming mode (sub `0x85`) |
| 0x1A | ReadEcuIdentification | query ECU info by Local Identifier |
| 0x27 | SecurityAccess | seed/key (sub `0x15` request seed, sub `0x16` send key) |
| 0x31 | StartRoutineByLocalIdentifier | invoke uploaded routine |
| 0x34 | RequestDownload | request to push N bytes to a memory address |
| 0x36 | TransferData | actually push the bytes |
| 0x82 | StopCommunication | end the session |
| 0x83 | AccessTimingParameter | configure P2/P3/P4 timeouts |
| 0x3B | WriteDataByLocalIdentifier | write a byte-string by LID (used for marker records) |

Negative response: `[0x7F][SID][NRC]`. NRC `0x78` means "busy, response pending" — the receiver loops.

## Sagem S3000 read-flow handshake (matches Galletto exactly)

1. Wake (fast init)
2. `82 12 F1 10 85 1A` — StartDiagSession sub `0x85`, extra param `0x1A`
3. **ChangeSpeed**:
   - `02 83 03 02 50 14 14 00 ??` — AccessTimingParameter setTimingParameters
   - `82 12 F1 10 85 87 ??` — StartDiagSession sub `0x85`, param `0x87` (signals re-baud)
   - Both sides switch to **125 000 bps**
4. **SecurityAccess**:
   - `82 12 F1 27 15 ??` — request seed
   - ECU returns 4 seed bytes
   - Compute key (see [seed-key.md](seed-key.md))
   - `86 12 F1 27 16 K0 K1 K2 K3 00 ??` — send 4-byte key + 1 zero pad
5. `02 83 03 00 C8 02 78 00 ??` — final AccessTimingParameter
6. `0C 3B 98 [10 spaces] ??` — WriteDataByLID `0x98` (marker)
7. `06 3B 99 20 03 04 02 ??` — WriteDataByLID `0x99` (marker)
8. `34` RequestDownload — `08 34 40 E0 00 00 00 04 20 ??` — addr `0x40E000`, length `0x420`
9. `36` TransferData — one or more chunks of stub bytes
10. `08 31 02 20 00 00 0F BF FF ??` — StartRoutine 0x02, mode `0x20` (read)

After step 10, the stock firmware has done `JSR @0x40E000` and we're talking to our stub.

## Custom stub protocol (replaces KWP2000 from step 10 onward)

```
host -> stub:  6 bytes  [addr_31:24] [addr_23:16] [addr_15:8] [addr_7:0] [len_15:8] [len_7:0]
stub -> host:  raw stream of `len` bytes
```

No framing, no checksum, no ack. The stub reads `addr` then `len`, then memcpy's `len` bytes from `addr` into the SCI1 TX buffer.

For full firmware reads we issue many small commands (e.g. 1024-byte chunks) so that any single-chunk failure can be retried without restarting the whole flow. There's no protocol-level retry — the host can simply re-send the command if a previous chunk timed out.

## Identification (no SecurityAccess needed)

The `--identify` flow stops after step 1 (wake) and queries `SID 0x1A` ReadEcuIdentification with various LIDs. Common LIDs on Sagem S3000:

| LID | Description |
|-----|-------------|
| 0x80 | ECU ID number (often the Renault part number) |
| 0x81 | ECU serial number |
| 0x86 | Diagnostic protocol version |
| 0x90 | ECU code |
| 0x91 | Software version |
| 0x92 | Software ID (Renault-specific) |
| 0x93 | Hardware version |
| 0x94 | Hardware ID |

Each query is `82 12 F1 1A LID ??`. Positive response is `5A LID <data>`. Negative response is `7F 1A 12` (subFunctionNotSupported) when the ECU doesn't implement that LID.

## Sources

- ISO 14230-2 / -3 (KWP2000) — public standard
- ASAM-MCD-2 / A2L (the Renault `26430000.A2L` we have)
- Renesas SH7055 hardware manual — public datasheet
- Sagem ECU tuner forums (algorithm + LIDs documented since the early 2000s)
