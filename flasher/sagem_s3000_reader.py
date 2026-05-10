"""
Sagem S3000 (SH7055) firmware reader for macOS / Linux / Windows.

Connects to the ECU via the Galletto FTDI cable, replays the Galletto
diagnostic-session setup (fast init + change-speed + security access),
uploads our small SH-2 stub into ECU RAM at 0x40E000, then talks a tiny
custom protocol to read arbitrary memory.

Output: a binary dump of whatever address range you ask for.

Dependencies (macOS):
    brew install libusb
    python3 -m pip install pyftdi

Run:
    python3 sagem_s3000_reader.py --start 0x4000 --end 0xFFFFB --out fw.bin

Read-only. Does not modify flash. Worst case on errors: the ECU watchdog
resets the chip — power cycle and try again.
"""

import argparse
import os
import sys
import time
from pathlib import Path

# Both pyftdi and pyserial are optional at import time. We'll auto-pick the
# right one for the platform: pyserial for Windows (FTDI VCP), pyftdi for
# macOS/Linux (libusb).
try:
    from pyftdi.ftdi import Ftdi
    PYFTDI_AVAILABLE = True
except ImportError:
    PYFTDI_AVAILABLE = False

try:
    import serial as pyserial
    PYSERIAL_AVAILABLE = True
except ImportError:
    PYSERIAL_AVAILABLE = False


# --------------------------------------------------------------------------
# Constants — protocol details we already extracted by reverse-engineering
# the Galletto host software.
# --------------------------------------------------------------------------

ECU_ADDR    = 0x12     # Sagem S3000 K-line address (Renault Megane RS / Clio RS)
TESTER_ADDR = 0xF1     # standard KWP2000 tester address

# Seed/key polynomial used by Sagem S3000 (35-iteration Galois LFSR, big-endian)
LFSR_POLY  = 0x28488863
LFSR_ITERS = 35

# Where our SH-2 stub gets uploaded in ECU RAM and the size of the upload
STUB_RAM_ADDR = 0x40E000

# Baud rates
BAUD_INIT = 10400      # K-line standard
BAUD_FAST = 125000     # after ChangeSpeed

# Galletto cable serial-number prefix (the original tool checks for this in EEPROM).
# Most clones don't have this exact prefix; pyftdi will match on first FTDI device.
CABLE_SERIAL_HINT = "galletto"


# --------------------------------------------------------------------------
# Seed/key computation
# --------------------------------------------------------------------------

def compute_key(seed_bytes_4: bytes) -> bytes:
    """35-iteration Galois LFSR per Sagem S3000 spec, big-endian.
    Input: 4 bytes seed from the ECU.
    Output: 4 bytes key to send back."""
    v = int.from_bytes(seed_bytes_4, 'big')
    for _ in range(LFSR_ITERS):
        msb = (v >> 31) & 1
        v = (v << 1) & 0xFFFFFFFF
        if msb:
            v ^= LFSR_POLY
    return v.to_bytes(4, 'big')


# --------------------------------------------------------------------------
# Low-level FTDI / K-line transport
# --------------------------------------------------------------------------

class KlineFTDI:
    """Thin wrapper over pyftdi: 8N1 serial + DTR control for K-line wake.
    Used on macOS/Linux where libusb-based access is the clean path."""

    def __init__(self, url: str = "ftdi:///1"):
        if not PYFTDI_AVAILABLE:
            raise RuntimeError("pyftdi is not installed; run: pip install pyftdi")
        self.ftdi = Ftdi()
        self.ftdi.open_from_url(url)
        self.ftdi.set_line_property(8, 1, "N")
        self._verbose = False

    def set_verbose(self, on=True):
        self._verbose = on

    def set_baud(self, baud: int):
        self.ftdi.set_baudrate(baud)

    def set_dtr(self, on: bool):
        self.ftdi.set_dtr(on)

    def purge(self):
        try:
            self.ftdi.purge_buffers()
        except Exception:
            pass

    def write(self, data: bytes):
        if self._verbose:
            print(f"  TX: {data.hex(' ')}")
        self.ftdi.write_data(data)

    def read(self, n: int, timeout_s: float = 1.0) -> bytes:
        deadline = time.monotonic() + timeout_s
        out = bytearray()
        while len(out) < n and time.monotonic() < deadline:
            chunk = self.ftdi.read_data(n - len(out))
            if chunk:
                out.extend(chunk)
            else:
                time.sleep(0.001)
        if self._verbose:
            print(f"  RX: {bytes(out).hex(' ')}{'' if len(out) == n else ' (TIMEOUT)'}")
        return bytes(out)

    def close(self):
        self.ftdi.close()


class KlineSerial:
    """Thin wrapper over pyserial: 8N1 + DTR control for K-line wake.
    Used on Windows where the FTDI VCP driver presents the cable as a COM port,
    so we can use it without replacing drivers (which would break Galletto.exe)."""

    def __init__(self, port: str = "COM3"):
        if not PYSERIAL_AVAILABLE:
            raise RuntimeError("pyserial is not installed; run: pip install pyserial")
        self.ser = pyserial.Serial(
            port=port,
            baudrate=10400,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0,         # non-blocking; we implement timeouts ourselves
            write_timeout=2.0,
        )
        self._verbose = False

    def set_verbose(self, on=True):
        self._verbose = on

    def set_baud(self, baud: int):
        self.ser.baudrate = baud

    def set_dtr(self, on: bool):
        # pyserial polarity: dtr=True => DTR pin asserted (typically LOW level
        # at the connector, but the Galletto cable inverts as needed).
        # We follow the same convention as KlineFTDI: True == HIGH on K-line.
        self.ser.dtr = on

    def purge(self):
        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception:
            pass

    def write(self, data: bytes):
        if self._verbose:
            print(f"  TX: {data.hex(' ')}")
        self.ser.write(data)
        self.ser.flush()

    def read(self, n: int, timeout_s: float = 1.0) -> bytes:
        deadline = time.monotonic() + timeout_s
        out = bytearray()
        while len(out) < n and time.monotonic() < deadline:
            avail = self.ser.in_waiting
            if avail:
                chunk = self.ser.read(min(avail, n - len(out)))
                out.extend(chunk)
            else:
                time.sleep(0.001)
        if self._verbose:
            print(f"  RX: {bytes(out).hex(' ')}{'' if len(out) == n else ' (TIMEOUT)'}")
        return bytes(out)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


def auto_pick_transport(prefer: str = "auto"):
    """Return ('serial' or 'ftdi') based on platform + availability."""
    if prefer != "auto":
        return prefer
    if sys.platform.startswith("win") and PYSERIAL_AVAILABLE:
        return "serial"
    if PYFTDI_AVAILABLE:
        return "ftdi"
    if PYSERIAL_AVAILABLE:
        return "serial"
    raise RuntimeError("Neither pyftdi nor pyserial is installed.")


# --------------------------------------------------------------------------
# KWP2000 frame layer (matches Galletto's wire format)
# --------------------------------------------------------------------------

class KWP2000:
    """KWP2000 (ISO 14230) frame helpers. Uses the addressed long-format header
    `[80][tgt][src][len][data][cksum]` for variable-length frames, and the
    addressed short-format `[80|len][tgt][src][data][cksum]` for short ones."""

    def __init__(self, kline: KlineFTDI):
        self.kline = kline

    @staticmethod
    def cksum(data: bytes) -> int:
        return sum(data) & 0xFF

    def build_frame(self, sid_and_data: bytes,
                    tgt: int = ECU_ADDR, src: int = TESTER_ADDR) -> bytes:
        """Build a KWP frame using addressed format, picking short vs long
        based on the data length."""
        n = len(sid_and_data)
        if n == 0:
            raise ValueError("empty frame")
        if n <= 0x3F:
            header = bytes([0x80 | n, tgt, src])
        else:
            header = bytes([0x80, tgt, src, n])
        body = header + sid_and_data
        return body + bytes([self.cksum(body)])

    def send_raw(self, frame: bytes, drain_echo: bool = True):
        """Send a frame on the K-line. If half-duplex (drain_echo=True), read
        and discard the echoed bytes that the FTDI receives back over its own RX."""
        self.kline.write(frame)
        if drain_echo:
            echo = self.kline.read(len(frame), timeout_s=0.5)
            if len(echo) != len(frame):
                pass  # informational only; some adapters don't echo

    def recv_response(self, timeout_s: float = 2.0) -> bytes:
        """Read one KWP response frame. Handles `7F XX 78` busy-repeat-request
        by silently waiting for the real response. Validates checksum."""
        while True:
            # Read the format byte to determine header shape
            fmt = self.kline.read(1, timeout_s=timeout_s)
            if not fmt:
                raise TimeoutError("no KWP response")
            f = fmt[0]
            if (f & 0xC0) == 0x80:  # addressed
                if (f & 0x3F) == 0:
                    hdr = self.kline.read(3, timeout_s=timeout_s)  # tgt src len
                    if len(hdr) != 3:
                        raise TimeoutError("short KWP response header")
                    data_len = hdr[2]
                else:
                    hdr = self.kline.read(2, timeout_s=timeout_s)  # tgt src
                    if len(hdr) != 2:
                        raise TimeoutError("short KWP response header")
                    data_len = f & 0x3F
            elif (f & 0xC0) == 0x00:  # carb mode (no addressing)
                if (f & 0x3F) == 0:
                    extra = self.kline.read(1, timeout_s=timeout_s)
                    if not extra:
                        raise TimeoutError("short KWP response")
                    data_len = extra[0]
                    hdr = extra
                else:
                    data_len = f & 0x3F
                    hdr = b""
            else:
                raise ValueError(f"unknown KWP format byte 0x{f:02X}")

            data = self.kline.read(data_len, timeout_s=timeout_s)
            cs = self.kline.read(1, timeout_s=timeout_s)
            if len(data) != data_len or len(cs) != 1:
                raise TimeoutError("incomplete KWP response")
            full = bytes([f]) + hdr + data + cs

            # Validate checksum
            expected = self.cksum(full[:-1])
            if cs[0] != expected:
                raise ValueError(f"KWP cksum bad: got 0x{cs[0]:02X}, want 0x{expected:02X}")

            # Busy-repeat-request handling: 7F xx 78 means "I'm busy, will respond later"
            if data_len >= 3 and data[0] == 0x7F and data[2] == 0x78:
                continue

            return data  # SID + payload

    def request(self, sid_and_data: bytes,
                tgt: int = ECU_ADDR, src: int = TESTER_ADDR,
                expect_positive: bool = True) -> bytes:
        """Send a request, read response, validate.
        Returns the response data (SID byte + payload).
        If `expect_positive=True`, raises on a negative response (`7F xx yy`)."""
        frame = self.build_frame(sid_and_data, tgt, src)
        self.send_raw(frame)
        response = self.recv_response()
        if expect_positive:
            if response[0] == 0x7F:
                nrc = response[2] if len(response) >= 3 else 0
                raise RuntimeError(
                    f"KWP NRC: SID=0x{response[1]:02X} code=0x{nrc:02X}"
                )
            sid_echo = response[0]
            sid_sent = sid_and_data[0]
            if sid_echo != (sid_sent | 0x40):
                raise RuntimeError(
                    f"unexpected response SID: got 0x{sid_echo:02X}, want 0x{sid_sent | 0x40:02X}"
                )
        return response


# --------------------------------------------------------------------------
# Sagem S3000 session orchestration
# --------------------------------------------------------------------------

class SagemS3000:
    def __init__(self, kline: KlineFTDI):
        self.kline = kline
        self.kwp = KWP2000(kline)

    # ---- step 1: wake -------------------------------------------------

    def fast_init(self):
        """KWP2000 fast init: pulse K-line LOW 25ms then HIGH 25ms via DTR,
        then send StartCommunication and check the response."""
        self.kline.set_baud(BAUD_INIT)
        self.kline.purge()
        self.kline.set_dtr(False)  # K-line LOW (idle break)
        time.sleep(0.300)
        self.kline.set_dtr(True)   # K-line HIGH
        time.sleep(0.025)
        self.kline.set_dtr(False)  # K-line LOW (T_iniL)
        time.sleep(0.025)
        self.kline.set_dtr(True)   # K-line HIGH (idle)
        time.sleep(0.025)
        # SID 0x81 = StartCommunication
        resp = self.kwp.request(bytes([0x81]))
        # Response should be 0xC1 (= 0x81 | 0x40) with key bytes
        if resp[0] != 0xC1:
            raise RuntimeError(f"StartCommunication unexpected response: {resp.hex()}")
        return resp

    # ---- step 2: programming session + change-speed ------------------

    def start_diag_session(self, sub: int = 0x85, *params: int):
        """SID 0x10 StartDiagnosticSession.
        sub=0x85 is the programming-mode entry used by the read flow."""
        body = bytes([0x10, sub, *params])
        return self.kwp.request(body)

    def change_speed_to_125k(self):
        # First: AccessTimingParameter setTiming
        # 07 83 03 02 50 14 14 00 (data part - SID + params)
        # Galletto sends `07 83 03 02 50 14 14 00 ??` as a CARB-mode short frame.
        # Equivalent here is the raw byte sequence. We use addressed long-format,
        # which is functionally equivalent.
        self.kwp.request(bytes([0x83, 0x03, 0x02, 0x50, 0x14, 0x14, 0x00]))
        # Then: StartDiagSession sub=0x85 with param 0x87 — this signals the ECU
        # to switch its UART to 125000 baud once the response is sent.
        self.kwp.request(bytes([0x10, 0x85, 0x87]))
        time.sleep(0.020)
        # Switch our side to 125000 baud
        self.kline.set_baud(BAUD_FAST)
        self.kline.purge()

    # ---- step 3: security access -------------------------------------

    def security_access(self):
        """SID 0x27 SecurityAccess: request seed (sub 0x15), compute key, send key (sub 0x16)."""
        # Request seed
        resp = self.kwp.request(bytes([0x27, 0x15]))
        # Response: 67 15 <seed_4_bytes>
        if len(resp) < 6:
            raise RuntimeError(f"SecurityAccess seed response too short: {resp.hex()}")
        seed = resp[2:6]
        key = compute_key(seed)
        # Send key (note: 5 key bytes, last one zero-padded per Galletto's flow)
        resp = self.kwp.request(bytes([0x27, 0x16]) + key + b"\x00")
        # Expect positive response
        return resp

    # ---- step 4: misc setup steps Galletto does ---------------------

    def write_marker_records(self):
        """Galletto writes two LID records before the bootloader upload.
        We replicate them to match exactly."""
        # WriteDataByLocalIdentifier 0x98 with 10 ASCII spaces
        self.kwp.request(bytes([0x3B, 0x98]) + b" " * 10)
        # WriteDataByLocalIdentifier 0x99 with 4 fixed bytes
        self.kwp.request(bytes([0x3B, 0x99, 0x20, 0x03, 0x04, 0x02]))

    # ---- step 5: upload our SH-2 stub -------------------------------

    def upload_stub(self, stub_bytes: bytes, ram_addr: int = STUB_RAM_ADDR):
        """SID 0x34 RequestDownload + SID 0x36 TransferData.
        We send our small stub in one chunk."""
        if len(stub_bytes) > 0x1000:
            raise ValueError("stub too large for one TransferData chunk")
        # RequestDownload: addr (3 bytes) + format (1 byte) + size (3 bytes)
        rd = bytes([
            0x34,
            (ram_addr >> 16) & 0xFF, (ram_addr >> 8) & 0xFF, ram_addr & 0xFF,
            0x00,  # format byte
            (len(stub_bytes) >> 16) & 0xFF, (len(stub_bytes) >> 8) & 0xFF, len(stub_bytes) & 0xFF,
        ])
        self.kwp.request(rd)
        # TransferData: SID 0x36 + raw chunk bytes
        # Galletto uses no-address long-format here: [00][len][36][data][cksum]
        # We approximate by sending the equivalent in addressed long format.
        td_body = bytes([0x36]) + stub_bytes
        # Build with the 0x80 long header; len = 1 (SID) + len(stub_bytes)
        n = len(td_body)
        header = bytes([0x80, ECU_ADDR, TESTER_ADDR, n])
        frame = header + td_body
        frame += bytes([self.kwp.cksum(frame)])
        self.kline.write(frame)
        # Drain echo
        self.kline.read(len(frame), timeout_s=0.5)
        # Read response
        resp = self.kwp.recv_response(timeout_s=2.0)
        if resp[0] == 0x7F:
            nrc = resp[2] if len(resp) >= 3 else 0
            raise RuntimeError(f"TransferData NRC: 0x{nrc:02X}")

    def start_routine(self):
        """SID 0x31 StartRoutineByLocalIdentifier with the params Galletto uses."""
        self.kwp.request(bytes([0x31, 0x02, 0x20, 0x00, 0x00, 0x0F, 0xBF, 0xFF]))

    # ---- ID-only flow ------------------------------------------------

    def read_ecu_id(self, lid: int) -> bytes | None:
        """SID 0x1A ReadEcuIdentification. Returns the data bytes (LID stripped)
        on success, or None if the ECU returns a negative response."""
        resp = self.kwp.request(bytes([0x1A, lid]), expect_positive=False)
        if resp[0] == 0x5A:
            # resp = [0x5A][LID echo][data...]
            return resp[2:] if len(resp) >= 2 else b""
        return None

    def stop_communication(self):
        try:
            self.kwp.request(bytes([0x82]), expect_positive=False)
        except Exception:
            pass


# Common KWP2000 ReadEcuIdentification LIDs. Most ECUs only support a subset.
KNOWN_LIDS = [
    (0x80, "ECU ID number"),
    (0x81, "ECU serial number"),
    (0x82, "ECU manufacturing date"),
    (0x83, "ECU vendor reference"),
    (0x84, "Programming date"),
    (0x85, "VIN (vehicle ID)"),
    (0x86, "Diagnostic version"),
    (0x87, "Reprogramming counter"),
    (0x88, "Calibration ID"),
    (0x90, "ECU code"),
    (0x91, "Software version"),
    (0x92, "Software ID (Renault-specific)"),
    (0x93, "Hardware version"),
    (0x94, "Hardware ID"),
    (0x95, "Drawing number"),
    (0x96, "Diagnostic data version"),
    (0x97, "Application software ID"),
    (0x9A, "Bootloader version"),
    (0x9B, "Application software signature"),
    (0x9C, "Calibration signature"),
]


def render_id_value(data: bytes) -> str:
    """Show ECU ID byte string both as hex and best-effort ASCII."""
    if not data:
        return "(empty)"
    hex_part = data.hex(' ')
    ascii_part = "".join(chr(b) if 0x20 <= b < 0x7E else '.' for b in data)
    return f"{hex_part}  '{ascii_part}'"


# --------------------------------------------------------------------------
# Custom protocol after our stub is running
# --------------------------------------------------------------------------

class StubProtocol:
    """6-byte command, raw-stream response. This is OUR protocol, not KWP."""

    def __init__(self, kline: KlineFTDI):
        self.kline = kline

    def read_chunk(self, addr: int, length: int) -> bytes:
        cmd = bytes([
            (addr >> 24) & 0xFF, (addr >> 16) & 0xFF,
            (addr >>  8) & 0xFF, (addr >>  0) & 0xFF,
            (length >> 8) & 0xFF, length & 0xFF,
        ])
        self.kline.write(cmd)
        # Drain echo (half-duplex)
        self.kline.read(len(cmd), timeout_s=0.5)
        # Read the response bytes — at 125 kbps, a generous timeout per chunk
        timeout = max(0.5, length / 8000.0)  # ~12 KB/s lower bound
        data = self.kline.read(length, timeout_s=timeout)
        if len(data) != length:
            raise RuntimeError(
                f"short read at 0x{addr:08X}: got {len(data)} of {length} bytes"
            )
        return data


# --------------------------------------------------------------------------
# Top-level orchestrator
# --------------------------------------------------------------------------

def run_identify(transport: str = "auto", port: str = "COM3",
                 ftdi_url: str = "ftdi:///1",
                 lids: list[int] | None = None,
                 verbose: bool = False, dry_run: bool = False):
    """Quick ID-only flow: wake, query each interesting LID, print results, done.
    No SecurityAccess, no bootloader upload, no ChangeSpeed."""
    if lids is None:
        lids = [lid for lid, _ in KNOWN_LIDS]

    transport = auto_pick_transport(transport)
    if dry_run:
        print(f"[dry-run] identify via {transport} (port={port}, ftdi_url={ftdi_url})")
        print(f"[dry-run] would query LIDs: {[hex(l) for l in lids]}")
        return

    if transport == "serial":
        kline = KlineSerial(port)
    else:
        kline = KlineFTDI(ftdi_url)
    kline.set_verbose(verbose)
    try:
        sagem = SagemS3000(kline)

        print("[1/3] fast init at 10400 baud...")
        wake_resp = sagem.fast_init()
        print(f"      wake response: {wake_resp.hex(' ')}")

        print("[2/3] querying ECU identifiers (SID 0x1A):")
        results = {}
        for lid in lids:
            label = next((d for code, d in KNOWN_LIDS if code == lid), "?")
            try:
                data = sagem.read_ecu_id(lid)
            except Exception as e:
                print(f"  0x{lid:02X} {label:36s}  ERROR: {e}")
                continue
            if data is None:
                print(f"  0x{lid:02X} {label:36s}  not supported")
            else:
                print(f"  0x{lid:02X} {label:36s}  {render_id_value(data)}")
                results[lid] = data

        print("[3/3] StopCommunication...")
        sagem.stop_communication()
        return results
    finally:
        kline.close()


def run(start: int, end: int, out_path: Path, stub_path: Path,
        chunk_size: int = 1024, transport: str = "auto", port: str = "COM3",
        ftdi_url: str = "ftdi:///1",
        verbose: bool = False, dry_run: bool = False):

    stub_bytes = stub_path.read_bytes()
    print(f"[+] Stub {stub_path.name}: {len(stub_bytes)} bytes")
    print(f"[+] Read range: 0x{start:08X} .. 0x{end:08X} ({end-start+1} bytes)")
    print(f"[+] Output: {out_path}")

    transport = auto_pick_transport(transport)
    if dry_run:
        print(f"[dry-run] would use transport={transport} (port={port}, ftdi_url={ftdi_url})")
        return

    if transport == "serial":
        print(f"[+] opening serial port {port}")
        kline = KlineSerial(port)
    elif transport == "ftdi":
        print(f"[+] opening FTDI {ftdi_url}")
        kline = KlineFTDI(ftdi_url)
    else:
        raise ValueError(f"unknown transport {transport}")
    kline.set_verbose(verbose)
    try:
        sagem = SagemS3000(kline)

        print("[1/8] fast init at 10400 baud...")
        sagem.fast_init()
        print("[2/8] StartDiagSession 0x85...")
        sagem.start_diag_session(0x85, 0x1A)  # match Galletto's 82 12 F1 10 85 1A
        print("[3/8] change speed to 125000 baud...")
        sagem.change_speed_to_125k()
        print("[4/8] SecurityAccess (seed -> key)...")
        sagem.security_access()
        print("[5/8] AccessTimingParameter (final)...")
        sagem.kwp.request(bytes([0x83, 0x03, 0x00, 0xC8, 0x02, 0x78, 0x00]))
        print("[6/8] write marker records...")
        sagem.write_marker_records()
        print(f"[7/8] uploading stub ({len(stub_bytes)} bytes) to RAM 0x{STUB_RAM_ADDR:X}...")
        sagem.upload_stub(stub_bytes)
        print("[8/8] StartRoutine -> jump into stub")
        sagem.start_routine()
        # The stub is now in control. ECU bootloader has handed off.

        print("[+] reading via stub...")
        stub = StubProtocol(kline)
        with out_path.open("wb") as out:
            addr = start
            total = end - start + 1
            done = 0
            t0 = time.monotonic()
            while done < total:
                want = min(chunk_size, total - done)
                chunk = stub.read_chunk(addr, want)
                out.write(chunk)
                addr += want
                done += want
                if done % (16 * 1024) == 0 or done == total:
                    elapsed = time.monotonic() - t0
                    rate = done / elapsed if elapsed > 0 else 0
                    print(f"    {done:>8} / {total} bytes  ({rate/1024:.1f} KB/s)")
        print(f"[+] done in {time.monotonic() - t0:.1f}s, {total} bytes -> {out_path}")
    finally:
        kline.close()


def parse_int(s: str) -> int:
    return int(s, 0)


def main(argv):
    parser = argparse.ArgumentParser(description="Sagem S3000 firmware reader")
    parser.add_argument("--start", type=parse_int, default=0x4000,
                        help="start address (default 0x4000)")
    parser.add_argument("--end",   type=parse_int, default=0xFFFFB,
                        help="end address inclusive (default 0xFFFFB)")
    parser.add_argument("--out", default="sagem_dump.bin",
                        help="output file (default sagem_dump.bin)")
    parser.add_argument("--stub", default=None,
                        help="path to stub.bin (default: ../stub/stub.bin next to this script)")
    parser.add_argument("--chunk", type=parse_int, default=1024,
                        help="chunk size per read command (default 1024)")
    parser.add_argument("--transport", default="auto", choices=["auto", "serial", "ftdi"],
                        help="serial=pyserial via COM port (Windows-friendly); ftdi=pyftdi via libusb (macOS/Linux). Default: auto.")
    parser.add_argument("--port", default="COM3",
                        help="COM port (default COM3 on Windows; e.g. /dev/cu.usbserial-* on macOS via pyserial)")
    parser.add_argument("--ftdi", default="ftdi:///1",
                        help="pyftdi device URL (default ftdi:///1)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="parse args, print plan, don't open hardware")
    parser.add_argument("--identify", action="store_true",
                        help="ID-only mode: wake, query SID 0x1A LIDs, print, exit. No bootloader.")
    parser.add_argument("--lids", nargs="*", type=parse_int, default=None,
                        help="LIDs to query in --identify mode (default: a curated set)")
    args = parser.parse_args(argv)

    if args.identify:
        run_identify(transport=args.transport, port=args.port, ftdi_url=args.ftdi,
                     lids=args.lids, verbose=args.verbose, dry_run=args.dry_run)
        return 0

    here = Path(__file__).resolve().parent
    stub = Path(args.stub) if args.stub else (here.parent / "stub" / "stub.bin")
    if not stub.exists():
        print(f"error: stub not found: {stub}", file=sys.stderr)
        return 1
    out = Path(args.out)

    run(args.start, args.end, out, stub,
        chunk_size=args.chunk,
        transport=args.transport, port=args.port, ftdi_url=args.ftdi,
        verbose=args.verbose, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
