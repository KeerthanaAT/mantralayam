"""Starts the guest (8001) and staff (8002) servers together; Ctrl+C stops both.

Usage: python tools/run_all.py
"""
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
procs = [subprocess.Popen([sys.executable, str(here / f"mantralayam_{n}.py"), "serve"]) for n in ("rooms", "staff")]
try:
    for p in procs:
        p.wait()
except KeyboardInterrupt:
    pass
finally:
    for p in procs:
        p.terminate()
