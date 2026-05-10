"""
Minimal SH-2 emulator + test harness for stub.bin.

We implement only the ~22 instructions our stub actually uses, model SCI1
peripheral registers as a simple RX/TX queue, plant some test data in
"flash" memory, drive the stub with fake commands, and verify it streams
back the correct bytes.

Run with Python 3:
    python test_stub.py
"""
import struct, os, sys

STUB_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub.bin")
STUB_BASE = 0x40E000

# ---- minimal SH-2 BE emulator --------------------------------------------

class SH2:
    """16 GPRs (r0..r15), PC, PR, SR (with T bit at SR[0])."""

    def __init__(self):
        self.r = [0] * 16
        self.pc = 0
        self.pr = 0
        self.sr_t = 0          # T bit
        self.sr_imask = 0      # IMASK 4 bits
        self.mem = {}          # sparse address -> byte
        self.read_hooks = {}   # addr -> callable() -> u8
        self.write_hooks = {}  # addr -> callable(u8)
        self.delay_slot = None  # if set, after current insn execute, jump here
        self.steps = 0

    # memory --------------------------------------------------------------

    def map_bytes(self, addr, data):
        for i, b in enumerate(data):
            self.mem[addr + i] = b

    def read_u8(self, addr):
        if addr in self.read_hooks:
            return self.read_hooks[addr]() & 0xFF
        return self.mem.get(addr, 0xFF)

    def write_u8(self, addr, value):
        if addr in self.write_hooks:
            self.write_hooks[addr](value & 0xFF)
            return
        self.mem[addr] = value & 0xFF

    def fetch_word(self, addr):
        return (self.read_u8(addr) << 8) | self.read_u8(addr + 1)

    # helpers -------------------------------------------------------------

    @staticmethod
    def s8(x):
        return x - 0x100 if x & 0x80 else x

    @staticmethod
    def s12(x):
        return x - 0x1000 if x & 0x800 else x

    def write_u16(self, addr, value):
        # Word write: 2 bytes, BE.
        self.write_u8(addr, (value >> 8) & 0xFF)
        self.write_u8(addr + 1, value & 0xFF)

    @staticmethod
    def to_u32(x):
        return x & 0xFFFFFFFF

    @staticmethod
    def to_s32(x):
        x &= 0xFFFFFFFF
        return x - 0x100000000 if x & 0x80000000 else x

    # main step -----------------------------------------------------------

    def step(self):
        if self.delay_slot is not None:
            target = self.delay_slot
            self.delay_slot = None
            self._execute_one()  # delay-slot instruction
            self.pc = target & 0xFFFFFFFF
            return
        self._execute_one()

    def _execute_one(self):
        word = self.fetch_word(self.pc)
        old_pc = self.pc
        self.pc = (self.pc + 2) & 0xFFFFFFFF
        self.steps += 1

        n = (word >> 8) & 0xF
        m = (word >> 4) & 0xF
        d = word & 0xF
        d8 = word & 0xFF
        d12 = word & 0xFFF
        op = (word >> 12) & 0xF

        # Match patterns most-specific-first
        if word == 0x0009:                         # nop
            return
        if word == 0x000B:                         # rts
            self.delay_slot = self.pr
            return
        if (word & 0xF0FF) == 0x0002:              # stc sr, Rn
            sr_value = self.sr_t | (self.sr_imask << 4)
            self.r[n] = sr_value & 0xFFFFFFFF
            return
        if (word & 0xF0FF) == 0x400E:              # ldc Rm, sr
            v = self.r[n]
            self.sr_t = v & 1
            self.sr_imask = (v >> 4) & 0xF
            return

        if (word & 0xFF00) == 0xCB00:              # or #imm, R0
            self.r[0] = (self.r[0] | d8) & 0xFFFFFFFF
            return
        if (word & 0xFF00) == 0xC900:              # and #imm, R0
            self.r[0] = self.r[0] & d8
            return
        if (word & 0xFF00) == 0xC800:              # tst #imm, R0
            self.sr_t = 1 if (self.r[0] & d8) == 0 else 0
            return

        if (word & 0xF000) == 0xE000:              # mov #imm, Rn
            self.r[n] = SH2.to_u32(SH2.s8(d8))
            return
        if (word & 0xF000) == 0x7000:              # add #imm, Rn
            self.r[n] = SH2.to_u32(self.r[n] + SH2.s8(d8))
            return

        if (word & 0xF00F) == 0x6003:              # mov Rm, Rn
            self.r[n] = self.r[m]
            return
        if (word & 0xF00F) == 0x600C:              # extu.b Rm, Rn
            self.r[n] = self.r[m] & 0xFF
            return
        if (word & 0xF00F) == 0x200B:              # or Rm, Rn
            self.r[n] = (self.r[n] | self.r[m]) & 0xFFFFFFFF
            return
        if (word & 0xF00F) == 0x6004:              # mov.b @Rm+, Rn (sign-extends)
            v = self.read_u8(self.r[m])
            self.r[m] = (self.r[m] + 1) & 0xFFFFFFFF
            self.r[n] = SH2.to_u32(v - 0x100 if v & 0x80 else v)
            return

        if (word & 0xF0FF) == 0x4015:              # cmp/pl Rn  (T = Rn > 0 signed)
            self.sr_t = 1 if SH2.to_s32(self.r[n]) > 0 else 0
            return
        if (word & 0xF0FF) == 0x4018:              # shll8 Rn
            self.r[n] = (self.r[n] << 8) & 0xFFFFFFFF
            return

        if (word & 0xFF00) == 0x8400:              # mov.b @(disp, Rm), R0  (sign-extends)
            disp = d
            mreg = (word >> 4) & 0xF
            v = self.read_u8(self.r[mreg] + disp)
            self.r[0] = SH2.to_u32(v - 0x100 if v & 0x80 else v)
            return
        if (word & 0xFF00) == 0x8000:              # mov.b R0, @(disp, Rn)
            disp = d
            nreg = (word >> 4) & 0xF
            self.write_u8(self.r[nreg] + disp, self.r[0] & 0xFF)
            return

        if (word & 0xF00F) == 0x2001:              # mov.w Rm, @Rn  (word store)
            self.write_u16(self.r[n], self.r[m] & 0xFFFF)
            return

        if (word & 0xF000) == 0x9000:              # mov.w @(disp, PC), Rn
            disp = d8
            target = ((old_pc + 4) + disp * 2) & 0xFFFFFFFF
            v = (self.read_u8(target) << 8) | self.read_u8(target + 1)
            # mov.w sign-extends
            self.r[n] = SH2.to_u32(v - 0x10000 if v & 0x8000 else v)
            return

        if (word & 0xFF00) == 0x8900:              # bt   label  (no delay slot)
            disp_units = SH2.s8(d8)
            if self.sr_t == 1:
                self.pc = (old_pc + 4 + disp_units * 2) & 0xFFFFFFFF
            return
        if (word & 0xFF00) == 0x8B00:              # bf   label  (no delay slot)
            disp_units = SH2.s8(d8)
            if self.sr_t == 0:
                self.pc = (old_pc + 4 + disp_units * 2) & 0xFFFFFFFF
            return

        if (word & 0xF000) == 0xA000:              # bra label (12-bit signed, delay slot)
            disp_units = SH2.s12(d12)
            target = (old_pc + 4 + disp_units * 2) & 0xFFFFFFFF
            self.delay_slot = target
            return
        if (word & 0xF000) == 0xB000:              # bsr label (saves PR, delay slot)
            disp_units = SH2.s12(d12)
            target = (old_pc + 4 + disp_units * 2) & 0xFFFFFFFF
            self.pr = (old_pc + 4) & 0xFFFFFFFF
            self.delay_slot = target
            return

        if (word & 0xF000) == 0xD000:              # mov.l @(disp, PC), Rn
            disp = d8
            pc_aligned = (old_pc + 4) & ~3
            target = (pc_aligned + disp * 4) & 0xFFFFFFFF
            v = (self.read_u8(target) << 24) | (self.read_u8(target+1) << 16) | \
                (self.read_u8(target+2) << 8) | self.read_u8(target+3)
            self.r[n] = v
            return

        raise NotImplementedError(
            f"Unknown SH-2 word 0x{word:04X} at PC=0x{old_pc:08X}"
        )

    def run(self, max_steps=200000):
        try:
            for _ in range(max_steps):
                self.step()
        except StopIteration:
            return


# ---- SCI1 peripheral mock --------------------------------------------------

class SCI1Mock:
    """Provides RX bytes from a queue, captures TX bytes into a list.
    K-line is half-duplex, so each TX appears as RX too (echo).

    Realistic timing model: bytes don't arrive all at once. Each SSR poll
    counts as a 'tick'; after BYTE_INTERVAL_TICKS ticks since the last
    byte was consumed, the next pending byte 'arrives' (RDRF=1).
    """

    SSR = 0xFFFFF00C
    RDR = 0xFFFFF00D
    TDR = 0xFFFFF00B

    BYTE_INTERVAL_TICKS = 30      # SSR polls between consecutive byte arrivals

    def __init__(self, rx_bytes):
        # 'pending' = bytes the host wants to send but haven't 'arrived' yet
        self.pending_rx = list(rx_bytes)
        self.tx = []
        self.echo_queue = []      # half-duplex echoes (arrive immediately)
        self.ssr_tdre = 1
        self.ssr_rdrf = 0         # nothing in RDR yet
        self.current_byte = None  # the byte currently 'in' RDR
        self.poll_ticks = 0       # SSR polls since last byte was read

    def _try_deliver(self):
        """Move a byte from pending_rx into RDR if enough time has passed
        and RDR is currently empty. Echoes have priority and arrive instantly."""
        if self.current_byte is not None:
            return
        if self.echo_queue:
            self.current_byte = self.echo_queue.pop(0)
            self.ssr_rdrf = 1
            return
        if self.pending_rx and self.poll_ticks >= self.BYTE_INTERVAL_TICKS:
            self.current_byte = self.pending_rx.pop(0)
            self.ssr_rdrf = 1
            self.poll_ticks = 0

    def read_ssr(self):
        # Each SSR poll counts as a tick (advances simulated time)
        if self.current_byte is None:
            self.poll_ticks += 1
        self._try_deliver()
        v = 0
        if self.ssr_tdre:
            v |= 0x80
        if self.ssr_rdrf:
            v |= 0x40
        return v

    def write_ssr(self, v):
        if (v & 0x40) == 0:
            self.ssr_rdrf = 0
        # TDRE is auto-managed (self-clearing & restoring on the SH7055
        # in this simplified model): always 1 outside a TX.
        self.ssr_tdre = 1

    def read_rdr(self):
        if self.current_byte is None:
            return 0xFF
        b = self.current_byte
        self.current_byte = None
        self.ssr_rdrf = 0
        self.poll_ticks = 0  # reset interval timer
        return b

    def write_tdr(self, v):
        self.tx.append(v)
        # Half-duplex: byte appears on RX too as an echo (with delay 0)
        self.echo_queue.append(v)

    @property
    def all_consumed(self):
        return (not self.pending_rx) and (self.current_byte is None) \
               and (not self.echo_queue)


class WDTMock:
    """Captures watchdog pets. Each correct pet (high byte=0x5A) increments
    `pets`; anything else (including writes with bad password) goes to
    `bad_writes` so we can flag it."""
    BASE_HI = 0xFFFFEC10
    BASE_LO = 0xFFFFEC11

    def __init__(self):
        self.pets = 0
        self.bad_writes = []

    def write_high(self, v):
        # captures top byte of word write to 0xFFFFEC10
        if v == 0x5A:
            self._pending_pet = True
        else:
            self._pending_pet = False
            self.bad_writes.append(("hi", v))

    def write_low(self, v):
        # captures low byte; if previous high was 0x5A, this completes a pet
        if getattr(self, "_pending_pet", False):
            self.pets += 1
            self._pending_pet = False
        else:
            self.bad_writes.append(("lo", v))


# ---- test cases ------------------------------------------------------------

def make_cmd(addr, length):
    return bytes([
        (addr >> 24) & 0xFF,
        (addr >> 16) & 0xFF,
        (addr >>  8) & 0xFF,
        (addr >>  0) & 0xFF,
        (length >> 8) & 0xFF,
        (length >> 0) & 0xFF,
    ])


def run_test(name, test_data, commands, expected_tx):
    print(f"=== {name} ===")
    cpu = SH2()
    stub = open(STUB_BIN, "rb").read()
    cpu.map_bytes(STUB_BASE, stub)
    cpu.map_bytes(0x4000_0000, b"")  # nop, just for clarity

    # Plant test data
    for addr, data in test_data:
        cpu.map_bytes(addr, data)

    # Wire SCI1 peripheral
    rx_stream = b"".join(commands)
    sci = SCI1Mock(rx_stream)
    cpu.read_hooks[SCI1Mock.SSR] = sci.read_ssr
    cpu.read_hooks[SCI1Mock.RDR] = sci.read_rdr
    cpu.write_hooks[SCI1Mock.SSR] = sci.write_ssr
    cpu.write_hooks[SCI1Mock.TDR] = sci.write_tdr

    # Wire WDT
    wdt = WDTMock()
    cpu.write_hooks[WDTMock.BASE_HI] = wdt.write_high
    cpu.write_hooks[WDTMock.BASE_LO] = wdt.write_low

    # Execute from stub entry
    cpu.pc = STUB_BASE
    cpu.r[15] = 0xFFFFB000  # arbitrary stack top in RAM (unused for our stub)

    # Run until all RX consumed and stub is back at the read poll
    max_steps = 200000
    step = 0
    while step < max_steps:
        cpu.step()
        step += 1
        # Stop when expected output is fully produced
        if len(sci.tx) >= len(expected_tx) and sci.all_consumed:
            # Run a few more steps to make sure stub doesn't err
            for _ in range(20):
                cpu.step()
            break

    consumed = len(rx_stream) - len(sci.pending_rx)
    print(f"  steps: {step}")
    print(f"  rx consumed: {consumed}/{len(rx_stream)}")
    print(f"  tx bytes ({len(sci.tx)}): {bytes(sci.tx).hex(' ')}")
    print(f"  expected   ({len(expected_tx)}): {expected_tx.hex(' ')}")
    print(f"  WDT pets: {wdt.pets}, bad writes: {len(wdt.bad_writes)}")
    ok = bytes(sci.tx[:len(expected_tx)]) == expected_tx and wdt.pets > 0 and not wdt.bad_writes
    print("  " + ("PASS" if ok else "FAIL"))
    return ok


def main():
    if not os.path.exists(STUB_BIN):
        sys.exit(f"missing {STUB_BIN} — run build_stub.py first")

    # Plant some test data
    test_data = [
        (0x00040000, bytes(range(256))),         # 0..255
        (0x00080000, b"HELLO_FROM_FLASH_AAAA"),  # ASCII tag
        (0x000FFF00, bytes([0xDE, 0xAD, 0xBE, 0xEF])),
    ]

    ok = True

    # Test 1: read 4 bytes from 0x40000
    cmd = make_cmd(0x00040000, 4)
    expect = bytes([0x00, 0x01, 0x02, 0x03])
    ok &= run_test("read 4 bytes from 0x40000", test_data, [cmd], expect)

    # Test 2: read 16 bytes from 0x80000 (ASCII)
    cmd = make_cmd(0x00080000, 16)
    expect = b"HELLO_FROM_FLASH"
    ok &= run_test("read 16 bytes from 0x80000", test_data, [cmd], expect)

    # Test 3: read 4 bytes from 0xFFF00
    cmd = make_cmd(0x000FFF00, 4)
    expect = bytes([0xDE, 0xAD, 0xBE, 0xEF])
    ok &= run_test("read 4 bytes from 0xFFF00", test_data, [cmd], expect)

    # Test 4: read 32 bytes from 0x40000 (boundary)
    cmd = make_cmd(0x00040000, 32)
    expect = bytes(range(32))
    ok &= run_test("read 32 bytes from 0x40000", test_data, [cmd], expect)

    # Test 5: TWO commands back-to-back
    cmd1 = make_cmd(0x00040000, 4)
    cmd2 = make_cmd(0x000FFF00, 4)
    expect = bytes([0,1,2,3]) + bytes([0xDE, 0xAD, 0xBE, 0xEF])
    ok &= run_test("two back-to-back reads", test_data, [cmd1, cmd2], expect)

    print()
    print("OVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
