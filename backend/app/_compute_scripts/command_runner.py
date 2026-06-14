"""Command runner invoked by the GCP compute-job VM startup script.

argv[1]: command string to run via bash -lc
argv[2]: path to write the bounded command log JSON
argv[3]: max log bytes (integer; defaults to 32768)
"""

import json
import pathlib
import subprocess
import sys

command = sys.argv[1]
log_path = pathlib.Path(sys.argv[2])
max_log_bytes = int(sys.argv[3]) if len(sys.argv) > 3 else 32768
log_path.parent.mkdir(parents=True, exist_ok=True)
proc = subprocess.Popen(
    ["bash", "-lc", command],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
)
captured = bytearray()
truncated = False
assert proc.stdout is not None
while True:
    chunk = proc.stdout.read(8192)
    if not chunk:
        break
    if len(captured) < max_log_bytes:
        remaining = max_log_bytes - len(captured)
        if len(chunk) <= remaining:
            captured.extend(chunk)
        else:
            captured.extend(chunk[:remaining])
            truncated = True
    else:
        truncated = True
exit_code = proc.wait()
if proc.stdout is not None:
    proc.stdout.close()
if captured or truncated:
    payload = {
        "text": captured.decode("utf-8", "replace"),
        "truncated": truncated,
        "byte_count": len(captured),
    }
    log_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
sys.exit(exit_code)
