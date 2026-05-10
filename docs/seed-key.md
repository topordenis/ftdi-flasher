# Sagem S3000 SecurityAccess — seed/key algorithm

The ECU's `SID 0x27` SecurityAccess uses a **35-iteration Galois LFSR** with a fixed polynomial. This is well-documented in tuner forums and is published openly across many Renault / Valeo Sagem reverse-engineering writeups going back ~20 years.

## Parameters

- **Iterations**: 35
- **Polynomial**: `0x28488863`
- **Endianness**: big-endian (4 input bytes treated as a 32-bit BE integer)

## Algorithm (Python)

```python
LFSR_POLY  = 0x28488863
LFSR_ITERS = 35

def compute_key(seed_4_bytes: bytes) -> bytes:
    v = int.from_bytes(seed_4_bytes, 'big')
    for _ in range(LFSR_ITERS):
        msb = (v >> 31) & 1
        v = (v << 1) & 0xFFFFFFFF
        if msb:
            v ^= LFSR_POLY
    return v.to_bytes(4, 'big')
```

## Wire format

Request seed:
```
host -> ECU:  82 12 F1 27 15 ??
ECU  -> host: 8X F1 12 67 15 S0 S1 S2 S3 ??     (4-byte seed)
```

Compute key, then:
```
host -> ECU:  86 12 F1 27 16 K0 K1 K2 K3 00 ??   (4-byte key + 1 zero pad)
ECU  -> host: 82 F1 12 67 16 ??                  (positive ack)
```

Note the trailing `0x00` byte after the 4 key bytes — Galletto's flow includes it; some other tools omit it.

## Self-check

A round-trip seed-derive-key for any non-zero seed should produce a value that the ECU accepts. There's no public reference vector since it's a deterministic 35-iter computation — anyone can reproduce by running the algorithm.
