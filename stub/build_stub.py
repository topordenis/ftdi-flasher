"""
Sagem S3000 / SH7055 read-only stub builder.

Output:
  stub.bin    — raw SH-2 binary, ~150–200 bytes, loaded at 0x40E000

Protocol (talks over SCI1 K-line at 125 kbps):
  HOST -> ECU:  4 bytes addr (big-endian)  +  2 bytes len (big-endian)
  ECU  -> HOST: <len> bytes (raw stream from addr, addr+1, ..., addr+len-1)
  Then loops back to read another command.

The stub never returns — power cycle to reset the ECU.
Run this script with Python 3 to produce stub.bin.

No external SH-2 toolchain needed. The script is a hand-rolled SH-2 encoder
covering only the instructions used.
"""
import os
import struct


# ---- SH-2 minimal encoder -------------------------------------------------

class Asm:
    def __init__(self):
        self.code = bytearray()
        self.labels = {}
        self.fixups = []         # list of (offset, kind, arg)
        self.literals = []       # list of (label, u32_value)

    def emit(self, w16):
        self.code.append((w16 >> 8) & 0xFF)
        self.code.append(w16 & 0xFF)

    def label(self, name):
        if name in self.labels:
            raise ValueError(f"duplicate label {name}")
        self.labels[name] = len(self.code)

    def literal(self, name, value_u32):
        self.literals.append((name, value_u32 & 0xFFFFFFFF, "l"))

    # -- instructions we use --

    def stc_sr(self, n):                 # stc sr, Rn        (0n02)
        self.emit(0x0002 | (n << 8))

    def ldc_sr(self, m):                 # ldc Rm, sr        (4m0E)
        self.emit(0x400E | (m << 8))

    def or_imm_r0(self, imm8):           # or  #imm, R0      (CBii)
        self.emit(0xCB00 | (imm8 & 0xFF))

    def and_imm_r0(self, imm8):          # and #imm, R0      (C9ii)
        self.emit(0xC900 | (imm8 & 0xFF))

    def tst_imm_r0(self, imm8):          # tst #imm, R0      (C8ii)
        self.emit(0xC800 | (imm8 & 0xFF))

    def mov_imm(self, n, imm8):          # mov #imm, Rn      (Enii)  imm signed
        self.emit(0xE000 | (n << 8) | (imm8 & 0xFF))

    def mov_rm_rn(self, n, m):           # mov Rm, Rn        (6nm3)
        self.emit(0x6003 | (n << 8) | (m << 4))

    def add_imm(self, n, imm8):          # add #imm, Rn      (7nii)  imm signed
        self.emit(0x7000 | (n << 8) | (imm8 & 0xFF))

    def cmp_pl(self, n):                 # cmp/pl Rn         (4n15)  T = (Rn > 0)
        self.emit(0x4015 | (n << 8))

    def shll8(self, n):                  # shll8 Rn          (4n18)
        self.emit(0x4018 | (n << 8))

    def extu_b(self, n, m):              # extu.b Rm, Rn     (6nmC)
        self.emit(0x600C | (n << 8) | (m << 4))

    def or_rm_rn(self, n, m):            # or  Rm, Rn        (2nmB)
        self.emit(0x200B | (n << 8) | (m << 4))

    def mov_b_disp_rm_r0(self, m, disp): # mov.b @(disp,Rm), R0    (84md)
        if not (0 <= disp <= 0xF):
            raise ValueError("disp out of range for mov.b @disp,Rm")
        self.emit(0x8400 | (m << 4) | (disp & 0xF))

    def mov_b_r0_disp_rn(self, n, disp): # mov.b R0, @(disp,Rn)    (80nd)
        if not (0 <= disp <= 0xF):
            raise ValueError("disp out of range for mov.b R0,@disp,Rn")
        self.emit(0x8000 | (n << 4) | (disp & 0xF))

    def mov_b_at_rm_post_inc(self, n, m):# mov.b @Rm+, Rn    (6nm4)
        self.emit(0x6004 | (n << 8) | (m << 4))

    def mov_w_rm_at_rn(self, n, m):      # mov.w Rm, @Rn      (2nm1)
        self.emit(0x2001 | (n << 8) | (m << 4))

    def mov_w_pc(self, n, label):        # mov.w @(disp,PC), Rn  (9ndd) — 16-bit literal
        self.fixups.append((len(self.code), "mov_w_pc", (n, label)))
        self.emit(0x9000 | (n << 8))

    def short_literal(self, name, value_u16):
        self.literals.append((name, value_u16 & 0xFFFF, "w"))

    def mov_w_rm_at_rn(self, n, m):      # mov.w Rm, @Rn     (2nm1)
        self.emit(0x2001 | (n << 8) | (m << 4))

    def nop(self):                       # nop               (0009)
        self.emit(0x0009)

    def rts(self):                       # rts               (000B)
        self.emit(0x000B)

    def bsr(self, label):                # bsr label         (Bddd, 12-bit signed)
        self.fixups.append((len(self.code), "bsr", label))
        self.emit(0xB000)

    def bra(self, label):                # bra label         (Addd, 12-bit signed)
        self.fixups.append((len(self.code), "bra", label))
        self.emit(0xA000)

    def bt(self, label):                 # bt label          (89dd, 8-bit signed, NO delay slot)
        self.fixups.append((len(self.code), "bt", label))
        self.emit(0x8900)

    def bf(self, label):                 # bf label          (8Bdd, 8-bit signed, NO delay slot)
        self.fixups.append((len(self.code), "bf", label))
        self.emit(0x8B00)

    def mov_l_pc(self, n, label):        # mov.l @(disp,PC), Rn  (Dndd)
        self.fixups.append((len(self.code), "mov_l_pc", (n, label)))
        self.emit(0xD000 | (n << 8))

    # -- finalize: emit literal pool, resolve fixups --

    def finalize(self):
        # Emit 16-bit literals first (only need 2-byte alignment)
        for name, value, kind in self.literals:
            if kind != "w":
                continue
            if name in self.labels:
                raise ValueError(f"label {name} already defined")
            self.labels[name] = len(self.code)
            self.code.append((value >> 8) & 0xFF)
            self.code.append((value >> 0) & 0xFF)

        # 4-byte alignment for 32-bit literal pool
        while len(self.code) % 4 != 0:
            self.emit(0x0009)  # NOP padding

        for name, value, kind in self.literals:
            if kind != "l":
                continue
            if name in self.labels:
                raise ValueError(f"label {name} already defined")
            self.labels[name] = len(self.code)
            self.code.append((value >> 24) & 0xFF)
            self.code.append((value >> 16) & 0xFF)
            self.code.append((value >>  8) & 0xFF)
            self.code.append((value >>  0) & 0xFF)

        for offset, kind, arg in self.fixups:
            insn = (self.code[offset] << 8) | self.code[offset + 1]

            if kind in ("bsr", "bra"):
                target = self.labels[arg]
                disp_units = (target - (offset + 4)) // 2
                if not (-2048 <= disp_units <= 2047):
                    raise ValueError(f"{kind.upper()} {arg} out of range")
                insn |= disp_units & 0x0FFF

            elif kind in ("bt", "bf"):
                target = self.labels[arg]
                disp_units = (target - (offset + 4)) // 2
                if not (-128 <= disp_units <= 127):
                    raise ValueError(f"{kind.upper()} {arg} out of range")
                insn |= disp_units & 0xFF

            elif kind == "mov_l_pc":
                n, label = arg
                target = self.labels[label]
                pc_aligned = (offset + 4) & ~3
                disp_units = (target - pc_aligned) // 4
                if not (0 <= disp_units <= 255):
                    raise ValueError(f"MOV.L @(disp,PC) {label} out of range disp={disp_units}")
                insn |= disp_units & 0xFF

            elif kind == "mov_w_pc":
                n, label = arg
                target = self.labels[label]
                # mov.w aligns target to 2 bytes (no special alignment)
                disp_units = (target - (offset + 4)) // 2
                if not (0 <= disp_units <= 255):
                    raise ValueError(f"MOV.W @(disp,PC) {label} out of range disp={disp_units}")
                insn |= disp_units & 0xFF

            else:
                raise ValueError(f"unknown fixup kind {kind}")

            self.code[offset]     = (insn >> 8) & 0xFF
            self.code[offset + 1] = insn & 0xFF

        return bytes(self.code)


# ---- the stub --------------------------------------------------------------

def build():
    a = Asm()

    # === init ==============================================================
    # Disable interrupts (SR.IMASK = 0xF, bits 4..7 of SR)
    a.stc_sr(0)                    # r0 = SR
    a.or_imm_r0(0xF0)              # r0 |= 0xF0
    a.ldc_sr(0)                    # SR = r0

    # r1 = SCI1 register base (0xFFFFF008)
    a.mov_l_pc(1, "sci1_base")

    # WDT pet setup (kept loaded for the duration of the stub):
    # r5 = WDT_TCSR address (0xFFFFEC10) — write 0x5A__ here to set TCNT
    # r6 = 0x5A00 (key 0x5A + count 0x00) — pet word: resets counter to 0
    a.mov_l_pc(5, "wdt_base")
    a.mov_imm(0, 0x5A)             # r0 = 0x5A
    a.shll8(0)                     # r0 = 0x5A00
    a.mov_rm_rn(6, 0)              # r6 = 0x5A00

    # Drain any leftover RX bytes (e.g. tail of the StartRoutine command)
    a.label("flush_rx")
    a.mov_b_disp_rm_r0(1, 4)       # r0 = SSR
    a.tst_imm_r0(0x40)             # T = (R0 & RDRF == 0)
    a.bt("main_loop")              # no leftover -> start
    a.mov_b_disp_rm_r0(1, 5)       # r0 = RDR (consume)
    a.mov_b_disp_rm_r0(1, 4)       # r0 = SSR
    a.and_imm_r0(0xBF)             # clear RDRF
    a.mov_b_r0_disp_rn(1, 4)       # write back SSR
    a.bra("flush_rx")
    a.nop()

    # === main loop =========================================================
    a.label("main_loop")
    a.mov_w_rm_at_rn(5, 6)         # pet WDT every command (*r5 = r6 = 0x5A00)

    # Read 4-byte address (big-endian) into R2
    a.bsr("read_byte"); a.nop()
    a.extu_b(2, 0)                 # r2 = uint8(r0)

    a.bsr("read_byte"); a.nop()
    a.shll8(2)
    a.extu_b(0, 0)
    a.or_rm_rn(2, 0)

    a.bsr("read_byte"); a.nop()
    a.shll8(2)
    a.extu_b(0, 0)
    a.or_rm_rn(2, 0)

    a.bsr("read_byte"); a.nop()
    a.shll8(2)
    a.extu_b(0, 0)
    a.or_rm_rn(2, 0)

    # Read 2-byte length (big-endian) into R3
    a.bsr("read_byte"); a.nop()
    a.extu_b(3, 0)

    a.bsr("read_byte"); a.nop()
    a.shll8(3)
    a.extu_b(0, 0)
    a.or_rm_rn(3, 0)

    # Stream R3 bytes from R2
    a.label("send_loop")
    a.mov_w_rm_at_rn(5, 6)         # pet WDT every byte (cheap insurance)
    a.cmp_pl(3)                    # T = (R3 > 0)
    a.bf("main_loop")              # done -> next command
    a.mov_b_at_rm_post_inc(0, 2)   # r0 = (uint8) *r2++
    a.bsr("write_byte"); a.nop()
    a.add_imm(3, -1)               # r3 -= 1
    a.bra("send_loop")
    a.nop()

    # === read_byte ========================================================
    # Returns received byte in R0 (zero-extended).
    # Uses R4 as save slot. R1 (SCI1 base), R5 (WDT base), R6 (pet word) preserved.
    a.label("read_byte")
    a.label("rb_poll")
    a.mov_w_rm_at_rn(5, 6)         # *r5 = r6  (WDT pet: TCNT = 0)
    a.mov_b_disp_rm_r0(1, 4)       # r0 = SSR
    a.tst_imm_r0(0x40)             # T = (R0 & RDRF == 0)
    a.bt("rb_poll")
    a.mov_b_disp_rm_r0(1, 5)       # r0 = RDR
    a.mov_rm_rn(4, 0)              # r4 = r0 (save)
    a.mov_b_disp_rm_r0(1, 4)       # r0 = SSR
    a.and_imm_r0(0xBF)             # clear RDRF
    a.mov_b_r0_disp_rn(1, 4)
    a.mov_rm_rn(0, 4)              # r0 = saved byte
    a.rts()
    a.nop()

    # === write_byte =======================================================
    # R0 = byte to send. Drains the K-line echo before returning.
    a.label("write_byte")
    a.mov_rm_rn(4, 0)              # r4 = byte (save)

    a.label("wb_poll_tx")
    a.mov_w_rm_at_rn(5, 6)         # WDT pet
    a.mov_b_disp_rm_r0(1, 4)       # r0 = SSR
    a.tst_imm_r0(0x80)             # T = (R0 & TDRE == 0)
    a.bt("wb_poll_tx")

    a.mov_rm_rn(0, 4)              # r0 = byte
    a.mov_b_r0_disp_rn(1, 3)       # TDR = r0

    a.mov_b_disp_rm_r0(1, 4)       # r0 = SSR
    a.and_imm_r0(0x7F)             # clear TDRE
    a.mov_b_r0_disp_rn(1, 4)

    # Drain echo (K-line is half-duplex: TX appears on RX too)
    a.label("wb_drain")
    a.mov_w_rm_at_rn(5, 6)         # WDT pet
    a.mov_b_disp_rm_r0(1, 4)       # r0 = SSR
    a.tst_imm_r0(0x40)
    a.bt("wb_drain")
    a.mov_b_disp_rm_r0(1, 5)       # r0 = RDR (discard)
    a.mov_b_disp_rm_r0(1, 4)
    a.and_imm_r0(0xBF)
    a.mov_b_r0_disp_rn(1, 4)

    a.rts()
    a.nop()

    # === literal pool =====================================================
    a.literal("sci1_base", 0xFFFFF008)
    a.literal("wdt_base",  0xFFFFEC10)

    return a.finalize(), a.labels


def disasm_summary(blob, labels, base=0x40E000):
    """Cheap print: label markers + raw 16-bit words."""
    inv_labels = {}
    for name, off in labels.items():
        inv_labels.setdefault(off, []).append(name)

    print(f"size: {len(blob)} bytes  (base 0x{base:08X})")
    print()
    i = 0
    while i < len(blob):
        if i in inv_labels:
            for nm in inv_labels[i]:
                print(f"{nm}:")
        if i + 1 < len(blob):
            w = (blob[i] << 8) | blob[i+1]
            print(f"  0x{base+i:08X}  {blob[i]:02X} {blob[i+1]:02X}    word 0x{w:04X}")
            i += 2
        else:
            print(f"  0x{base+i:08X}  {blob[i]:02X}        odd byte")
            i += 1


if __name__ == "__main__":
    blob, labels = build()
    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "stub.bin")
    with open(out_path, "wb") as f:
        f.write(blob)
    disasm_summary(blob, labels)
    print()
    print(f"wrote {out_path}: {len(blob)} bytes")
