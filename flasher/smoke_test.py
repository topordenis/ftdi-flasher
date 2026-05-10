"""Cable smoke test — exercises the transport without needing an ECU.

Just opens COM3, toggles baud rates, pulses DTR, attempts to write/read.
Useful to confirm pyserial + the cable + our wrapper work together before
actually plugging into the car.
"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sagem_s3000_reader import KlineSerial, PYSERIAL_AVAILABLE


def main(port: str = "COM3"):
    if not PYSERIAL_AVAILABLE:
        sys.exit("pyserial not installed; run: pip install pyserial")
    print(f"[*] opening {port}...")
    k = KlineSerial(port)
    k.set_verbose(True)
    print("[+] opened OK")

    print("[*] setting 10400 baud...")
    k.set_baud(10400)
    print("[+] OK")

    print("[*] testing DTR toggle (expect no error)...")
    k.set_dtr(False); time.sleep(0.05)
    k.set_dtr(True);  time.sleep(0.05)
    k.set_dtr(False)
    print("[+] DTR toggled OK")

    print("[*] purging buffers...")
    k.purge()
    print("[+] OK")

    print("[*] sending a harmless probe byte (0x00) at 10400 baud...")
    k.write(b"\x00")

    print("[*] reading any response with 0.5s timeout (expect 0 bytes — no ECU)...")
    data = k.read(8, timeout_s=0.5)
    print(f"[+] got {len(data)} byte(s)")

    print("[*] switching to 125000 baud...")
    k.set_baud(125000)
    print("[+] OK")

    print("[*] closing...")
    k.close()
    print("[+] all checks passed")
    print()
    print("Cable + transport are healthy. When you're at the car with ignition on,")
    print("run: python sagem_s3000_reader.py --out my_dump.bin")


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    main(port)
