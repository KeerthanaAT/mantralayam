"""Runs the whole live audit and writes TEST_REPORT_LIVE.md. Usage: python tools/audit_run.py [phase ...]   (phases: p1 p2 p3 p4; default all)"""
import atexit
import shutil
import sys
import traceback

import audit_live as A

PHASES = sys.argv[1:] or ["p1", "p2", "p3", "p4"]


def main():
    before, after, ids = A.seed()
    A.start("guest", A.GP, script="mantralayam_rooms.py")
    A.start("staff", A.SP, script="mantralayam_staff.py")
    X = type("X", (), {})()
    X.before, X.after, X.ids = before, after, ids
    try:
        adm = A.staff("admin")
        X.sev, X.sev2, X.pkg = A.seed_via_api(adm)
        for ph in PHASES:
            mod = __import__("audit_" + ph)
            for fn in ("run_http", "run_browser_all"):
                if hasattr(mod, fn):
                    try:
                        getattr(mod, fn)(X)
                    except Exception:
                        traceback.print_exc()
                        A.rec(ph, f"phase {ph} {fn}", "ran to completion", False, "crashed: see the console")
            if hasattr(mod, "run_browser") and A.HAVE_PW:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    try:
                        errs = mod.run_browser(X, p)
                        A.check(ph, "Browser console and network clean on every page of this phase", "pageerror / console.error / same-origin HTTP>=400 collected", lambda: (not errs, str(errs[:5])))
                    except Exception:
                        traceback.print_exc()
                        A.rec(ph, f"browser part of {ph}", "ran to completion", False, "crashed: see the console")
            elif hasattr(mod, "run_browser"):
                A.rec(ph, "Browser checks", "Playwright", "blocked", "Playwright is not installed")
    finally:
        counts = A.write_report()
        for n in list(A.PROCS):
            A.stop(n)
        print(counts)
        print("logs and screenshots:", A.TMP)


main()
