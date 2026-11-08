from pathlib import Path
import subprocess
import sys

data = Path(sys.argv[1]).read_bytes()
offset = 0
while True:
    offset = data.find(b'hsqs', offset + 1)
    if offset < 0:
        raise SystemExit('No SquashFS v4 payload found')
    if data[offset + 28:offset + 32] == b'\x04\0\0\0':
        break
subprocess.run(['unsquashfs','-o',str(offset),'-d',sys.argv[2],sys.argv[1]],check=True)
