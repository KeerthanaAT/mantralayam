"""Starts the guest (8001) and staff (8002) servers together; Ctrl+C stops both.

Usage: python tools/run_all.py

Also runs the periodic job (staff alerts, waitlist messages) every 5 minutes, as the systemd timer does in production.
"""
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
procs = [subprocess.Popen([sys.executable, str(here / f"mantralayam_{n}.py"), "serve"]) for n in ("rooms", "staff")]
try:
    while all(p.poll() is None for p in procs):
        try:
            procs[0].wait(timeout=300)
        except subprocess.TimeoutExpired:
            subprocess.run([sys.executable, str(here / "mantralayam_rooms.py"), "tick"], check=False)
except KeyboardInterrupt:
    pass
finally:
    for p in procs:
        p.terminate()
