# flasher

Python tools that talk to the ECU.

| File | Purpose |
|---|---|
| `sagem_s3000_reader.py` | The main tool. Wakes the ECU, does the full session setup, uploads the SH-2 stub, reads memory. |
| `smoke_test.py` | Cable health check — opens the port, toggles DTR, exercises baud switching. No ECU required. |

## Usage

```bash
# cable check (no car needed)
python smoke_test.py [COM3]

# ECU identification (key on, engine off, OBD-II plugged)
python sagem_s3000_reader.py --identify

# specific LIDs
python sagem_s3000_reader.py --identify --lids 0x80 0x86 0x90 0x92

# full firmware read
python sagem_s3000_reader.py --out my_ecu.bin

# specific range
python sagem_s3000_reader.py --start 0x40000 --end 0x4FFFF --out cal.bin

# verbose (every byte on the wire)
python sagem_s3000_reader.py --identify --verbose

# pick non-default port
python sagem_s3000_reader.py --port COM5

# force pyftdi (libusb) instead of pyserial (COM port)
python sagem_s3000_reader.py --transport ftdi --ftdi 'ftdi:///1'

# dry run — parse args, print plan, don't talk to hardware
python sagem_s3000_reader.py --dry-run
```

## How transport selection works

| OS | Default | Why |
|---|---|---|
| Windows | `pyserial` over COM port | FTDI VCP driver presents the cable as a regular COM port; `pyftdi` would require swapping the driver via Zadig, which would also break Galletto.exe |
| macOS / Linux | `pyftdi` via libusb | `pyftdi` handles the kernel driver detach automatically; cleaner than dealing with `/dev/cu.usbserial-*` quirks |

Override with `--transport serial` or `--transport ftdi` if the auto-detection isn't right.
