"""Audit phase 4: production readiness (restarts, health, alerts, attacks, bots, HTTPS config, secrets), migrations, links, accessibility, mobile layout."""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import audit_live as A
from audit_live import D, check, db, guest, make_booking, phone, rec, staff, uniq_ip
from audit_p2 import log_text, tick

EXTRA = {}


def run_http(X):
    # ---------- migrations ----------
    def migration():
        before, after = X.before, X.after
        sums_b, sums_a = X.ids["sums"]
        counts_ok = all(before[t] == after[t] for t in before)
        data_ok = all(sums_b[t] == sums_a[t] for t in before)
        new = [t for (t,) in db().execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() if t not in before and not t.startswith("sqlite_")]
        diff = [t for t in before if sums_b[t] != sums_a[t]]
        return counts_ok and data_ok and len(new) >= 15, f"{len(before)} existing tables, row counts equal {counts_ok}, content hashes equal {data_ok} (differing: {diff}); {len(new)} new tables added"
    check("Production", "Migrations on a copy of the old database keep every existing row (counts and content hashes of all tables)", "hash every table of the pre-feature DB before and after migrate()", migration)

    # ---------- rate limits across a restart ----------
    def rate_restart():
        ip = "10.77.0.5"
        c = A.Client(A.GP, ip)
        body = lambda i: {"name": "RL", "phone": f"98765000{i:02d}", "call_time": "any", "lang": "en"}  # a different mobile number each time: the per-number limit is separate
        codes = [c.j("POST", "/api/public/callback", body(i))[0] for i in range(6)]
        A.stop("guest")
        A.start("guest", A.GP, script="mantralayam_rooms.py")
        after = A.Client(A.GP, ip).j("POST", "/api/public/callback", body(20))
        other = A.Client(A.GP, "10.77.0.6").j("POST", "/api/public/callback", body(21))[0]
        # the staff login lock-out as well
        s1 = A.Client(A.SP, "10.77.1.1")
        bad = [s1.j("POST", "/api/login", {"username": "viewer", "password": "wrong"})[0] for _ in range(5)]
        A.stop("staff")
        A.start("staff", A.SP, script="mantralayam_staff.py")
        locked = A.Client(A.SP, "10.77.1.1").j("POST", "/api/login", {"username": "viewer", "password": A.PW})
        return codes == [200] * 5 + [429] and after[0] == 429 and "retry_after" in after[1] and other == 200 and bad == [401] * 5 and locked[0] == 429, f"before restart {codes}, after {after[0]}, other address {other}, staff lock-out after restart {locked[0]}"
    check("Production", "Rate limits survive a server restart (guest call-back limit and the staff login lock-out)", "exhaust the limits, restart both servers mid-test, try again", rate_restart)

    # ---------- health ----------
    def health():
        out = {}
        g = A.Client(A.GP, uniq_ip())
        out["no backup yet"] = g.j("GET", "/health")
        r = subprocess.run([sys.executable, str(A.HERE / "mantralayam_rooms.py"), "backup"], env=A.ENV, capture_output=True, text=True, cwd=A.ROOT)
        out["after a backup"] = g.j("GET", "/health")
        out["head"] = g.req("HEAD", "/health")[0]
        logf = A.TMP / "backups" / "backup.log"
        old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 40 * 3600))
        good = logf.read_text()
        logf.write_text(f"{old}\tOK\tmantralayam-old.db\n")
        out["stale"] = g.j("GET", "/health")
        logf.write_text(good + f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\tFAILED\tdisk full\n")
        out["failed"] = g.j("GET", "/health")
        logf.write_text(good)
        # database down: the file is not there
        A.start("guest3", A.XP, {"MANTRALAYAM_DB": str(A.TMP / "missing.db")}, script="mantralayam_rooms.py")  # a server whose database file is not there
        A.start("staff3", A.XP + 1, {"MANTRALAYAM_DB": str(A.TMP / "missing.db")}, script="mantralayam_staff.py")
        try:
            out["db down"] = A.Client(A.XP, uniq_ip()).j("GET", "/health")
            out["db down (staff)"] = A.Client(A.XP + 1, uniq_ip()).j("GET", "/health")
        finally:
            A.stop("guest3")
            A.stop("staff3")
        out["recovered"] = g.j("GET", "/health")
        ok = (r.returncode == 0 and out["no backup yet"][0] == 503 and out["no backup yet"][1]["checks"]["backup"] == "none" and out["after a backup"][0] == 200 and out["head"] == 200 and out["stale"][0] == 503
              and out["stale"][1]["checks"]["backup"] == "stale" and out["failed"][0] == 503 and out["failed"][1]["checks"]["backup"] == "failed" and out["db down"][0] == 503 and out["db down"][1]["checks"]["database"] == "fail"
              and out["db down (staff)"][0] == 503 and out["recovered"][0] == 200 and str(A.DB) not in json.dumps(out) and "Traceback" not in json.dumps(out))
        return ok, "; ".join(f"{k}: {v[0] if isinstance(v, tuple) else v}" for k, v in out.items())
    check("Production", "/health: 200 when healthy; 503 for no backup, a stale backup, a failed backup and a missing database (guest and staff); no paths leaked; recovers", "real backup command, edited backup log, DB file moved away and back", health)

    # ---------- alerts ----------
    def alerts():
        mgr = staff("manager")
        env = A.ENV | {"STAFF_ALERT_EMAILS": "owner@example.org"}
        logf = A.TMP / "backups" / "backup.log"
        good = logf.read_text()
        logf.write_text(good + f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\tFAILED\tdisk full\n")
        run = lambda: subprocess.run([sys.executable, str(A.HERE / "mantralayam_rooms.py"), "tick"], env=env, capture_output=True, text=True, cwd=A.ROOT, timeout=120)
        t1 = run()
        dash = mgr.j("GET", "/api/dashboard")[1]
        backup_alert = [a for a in dash.get("alerts", []) if a["kind"] == "backup"]
        emailed = "owner@example.org" in t1.stdout
        logf.write_text(good)
        # many rooms on hold at once, then many holds lapsing unbooked
        c = db()
        c.execute("DELETE FROM holds")
        statuses = []
        pairs = [tuple(r) for r in c.execute("SELECT DISTINCT property_id, room_type_id FROM rooms WHERE active = 1 ORDER BY 1, 2").fetchall()]
        for i in range(18):  # one room each, from 18 different addresses, on different nights
            pid, tid = pairs[i % len(pairs)]
            statuses.append(A.Client(A.GP, f"10.88.{i}.9").j("POST", "/api/public/hold", A.base_req(property_id=pid, room_type_id=tid, check_in=D(30 + i), check_out=D(31 + i), guests=1))[0])
        n_hold = c.execute("SELECT COUNT(DISTINCT token) FROM holds").fetchone()[0]
        t2 = run()
        dash2 = mgr.j("GET", "/api/dashboard")[1]
        pressure = [a for a in dash2.get("alerts", []) if a["kind"] == "holds" and a["key"] == "pressure"]
        past = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 600))
        for i in range(22):
            c.execute("INSERT INTO holds (token, room_id, check_in, check_out, expires_at, ip) VALUES (?, 1, ?, ?, ?, 'x')", (f"lapsed{i}", D(5), D(6), past))
        run()
        lapsed = [a for a in mgr.j("GET", "/api/dashboard")[1].get("alerts", []) if a["kind"] == "holds" and a["key"] == "lapsed"]
        c.execute("DELETE FROM holds")
        run()
        cleared = [a for a in mgr.j("GET", "/api/dashboard")[1].get("alerts", []) if a["kind"] in ("holds", "backup")]
        return backup_alert and emailed and pressure and lapsed and not cleared, f"backup alert {bool(backup_alert)} emailed {emailed}; {n_hold} rooms on hold (statuses {set(statuses)}) pressure alert {bool(pressure)}; lapsed alert {bool(lapsed)}; cleared afterwards {not cleared}"
    check("Production", "Alerts fire and clear: failed backup (shown on the dashboard and emailed), many rooms on hold, many holds lapsing unbooked", "failed backup log line, 16 holds from different addresses, 22 lapsed holds; the real `tick`", alerts)

    # ---------- attacks ----------
    def attacks():
        out = []
        g, s = A.Client(A.GP, uniq_ip()), A.Client(A.SP, uniq_ip())
        hdr = g.req("GET", "/api/public/info")[1]
        shdr = s.req("GET", "/api/meta")[1]
        need = ("content-security-policy", "x-frame-options", "x-content-type-options", "referrer-policy", "permissions-policy", "cross-origin-opener-policy")
        out.append(("security headers (guest, staff)", all(k in hdr and k in shdr for k in need) and hdr["x-frame-options"] == "DENY" and "default-src 'self'" in hdr["content-security-policy"] and "frame-ancestors 'none'" in hdr["content-security-policy"]))
        out.append(("cross-site Origin refused", g.req("POST", "/api/public/callback", {"name": "x", "phone": "9876500002"}, headers={"Origin": "https://evil.example"})[0] == 403))
        out.append(("form post (not JSON) refused", g.req("POST", "/api/public/callback", raw=b"name=x", headers={"Content-Type": "application/x-www-form-urlencoded"})[0] == 415))
        out.append(("oversized body refused", g.req("POST", "/api/public/callback", raw=b'{"name": "' + b"A" * 30000 + b'"}', headers={"Content-Type": "application/json"})[0] == 413 and s.req("POST", "/api/login", raw=b'{"username": "' + b"A" * 30000 + b'"}', headers={"Content-Type": "application/json"})[0] == 413))
        out.append(("over-long field refused", g.j("POST", "/api/public/callback", {"name": "A" * 2500, "phone": "9876500003"})[0] == 400))
        deep = {"a": {"a": {"a": {"a": {"a": {"a": 1}}}}}}
        out.append(("deeply nested input refused", g.j("POST", "/api/public/callback", {"name": "x", "phone": "9876500004", "x": deep})[0] == 400))
        out.append(("too many keys refused", g.j("POST", "/api/public/callback", {f"k{i}": 1 for i in range(80)})[0] == 400))
        out.append(("JSON of the wrong type refused", g.req("POST", "/api/public/callback", raw=b"[1,2,3]", headers={"Content-Type": "application/json"})[0] == 400 and g.req("POST", "/api/public/callback", raw=b"{broken", headers={"Content-Type": "application/json"})[0] == 400))
        from urllib.parse import quote
        inj = ["' OR 1=1 --", "1; DROP TABLE bookings;--", "%00", "../../etc/passwd", "<script>alert(1)</script>"]
        codes = [g.j("GET", f"/api/public/search?check_in={quote(x)}&check_out={quote(x)}&guests={quote(x)}&property_id={quote(x)}")[0] for x in inj] + [staff("manager").j("GET", f"/api/bookings?q={quote(x)}")[0] for x in inj] + [g.j("POST", "/api/public/booking/lookup", {"booking_id": x, "phone": x})[0] for x in inj]
        out.append(("injection strings never cause a 500", all(c_ < 500 for c_ in codes) and db().execute("SELECT COUNT(*) FROM bookings").fetchone()[0] > 0))
        out.append(("unknown path and methods", g.req("GET", "/etc/passwd")[0] == 404 and g.req("GET", "/api/public/nothing")[0] == 404 and g.req("DELETE", "/api/public/info")[0] in (404, 501)))
        out.append(("no server error text in 4xx replies", "Traceback" not in json.dumps(g.j("POST", "/api/public/book", {"x": 1})[1])))
        bad = [n for n, ok in out if not ok]
        return not bad, f"failed: {bad}"
    check("Production", "Attacks: security headers, cross-site POST, non-JSON, oversized, over-long, deeply nested, wrong-type bodies, injection strings, bad paths", "raw HTTP against both servers", attacks)

    def xss_ui():
        # the payload is stored via a booking; the browser checks follow in the browser phase
        payload = "<img src=x onerror=window.__xss=1><script>window.__xss=2</script>"
        ph = phone()
        st, b, _ = make_booking(ph=ph, check_in=D(12), check_out=D(13), guest_name=payload, remarks=payload)
        EXTRA["xss"] = (b["booking_id"], ph)
        row = db().execute("SELECT guest_name, remarks FROM bookings WHERE group_id = ?", (b["booking_id"],)).fetchone()
        return st == 200 and row[0] == payload, f"stored as plain text: {row[0][:30]!r}"
    check("Production", "Script injection in booking forms is stored as plain text (rendering checked in the browser phase)", "book with an <img onerror>/<script> name and remarks", xss_ui)

    # ---------- honeypot and CAPTCHA ----------
    def bots():
        out = []
        g = A.Client(A.GP, uniq_ip())
        out.append(("honeypot on callback", g.j("POST", "/api/public/callback", {"name": "Bot", "phone": "9876500010", "website": "http://spam"})[0] == 400))
        cl, ph = A.Client(A.GP, uniq_ip()), phone()
        out.append(("honeypot on booking", make_booking(cl, ph=ph, website="x")[0] == 400))
        out.append(("honeypot on feedback/subscribe", g.j("POST", "/api/public/subscribe", {"phone": ph, "sms": True, "consent": True, "website": "x"})[0] == 400))
        A.start("guest3", A.XP, {"CAPTCHA_PROVIDER": "mock"}, script="mantralayam_rooms.py")
        try:
            c = A.Client(A.XP, uniq_ip())
            ph2 = phone()
            info = c.j("GET", "/api/public/info")[1]
            out.append(("captcha advertised", info["captcha"] == {"provider": "mock", "site_key": ""}))
            out.append(("no token blocked", c.j("POST", "/api/public/otp/send", {"phone": ph2, "purpose": "booking"})[0] == 400))
            out.append(("wrong token blocked", c.j("POST", "/api/public/otp/send", {"phone": ph2, "purpose": "booking", "captcha_token": "nope"})[0] == 400))
            out.append(("callback blocked without token", c.j("POST", "/api/public/callback", {"name": "x", "phone": "9876500011"})[0] == 400))
            real = c.j("POST", "/api/public/otp/send", {"phone": ph2, "purpose": "booking", "captcha_token": "mock-pass"})[0]
            real_cb = c.j("POST", "/api/public/callback", {"name": "Real", "phone": "9876500012", "captcha_token": "mock-pass"})[0]
            out.append(("a real user (valid token) gets through", real == 200 and real_cb == 200))
        finally:
            A.stop("guest3")
        bad = [n for n, ok in out if not ok]
        return not bad, f"failed: {bad}"
    check("Production", "Honeypot and CAPTCHA (mock widget): bots are refused, a real user with a valid token passes", "filled honeypot on 3 forms; second guest server with CAPTCHA_PROVIDER=mock", bots)

    # ---------- HTTPS mode ----------
    def https_mode():
        key = "audit-encryption-key-0123456789"
        env = {"MANTRALAYAM_HTTPS": "1", "ID_ENCRYPTION_KEY": key, "PAYMENT_PROVIDER": "", "WHATSAPP_PROVIDER": "", "SMS_PROVIDER": "mock"}
        refuse = subprocess.run([sys.executable, str(A.HERE / "mantralayam_rooms.py"), "serve", str(A.XP)], env=A.ENV | env, capture_output=True, text=True, cwd=A.ROOT, timeout=30)
        refuse_nokey = subprocess.run([sys.executable, str(A.HERE / "mantralayam_rooms.py"), "serve", str(A.XP)], env=A.ENV | env | {"SMS_PROVIDER": "http", "ID_ENCRYPTION_KEY": ""}, capture_output=True, text=True, cwd=A.ROOT, timeout=30)
        mock_pay = subprocess.run([sys.executable, str(A.HERE / "mantralayam_rooms.py"), "serve", str(A.XP)], env=A.ENV | env | {"SMS_PROVIDER": "http", "PAYMENT_PROVIDER": "mock"}, capture_output=True, text=True, cwd=A.ROOT, timeout=30)
        A.start("staff3", A.XP, env, script="mantralayam_staff.py")
        try:
            c = A.Client(A.XP, uniq_ip())
            st, h, out = c.req("POST", "/api/login", {"username": "viewer", "password": A.PW})
            ck = h.get("set-cookie", "")
            hsts = h.get("strict-transport-security", "")
            mock_checkout = "n/a"
        finally:
            A.stop("staff3")
        import mantralayam_rooms as m
        m.HTTPS_ONLY = True
        stub = type("S", (), {"headers": {}})()
        gc = m.Handler.guest_cookie(stub, "tok")
        gc_clear = m.Handler.guest_cookie(stub, "")
        m.HTTPS_ONLY = False
        ok = (refuse.returncode == 1 and "SMS_PROVIDER" in refuse.stderr and refuse_nokey.returncode == 1 and "ID_ENCRYPTION_KEY" in refuse_nokey.stderr and mock_pay.returncode == 1 and "PAYMENT_PROVIDER" in mock_pay.stderr
              and st == 200 and all(x in ck for x in ("Secure", "HttpOnly", "SameSite=Strict")) and "max-age=31536000" in hsts and all(x in gc for x in ("Secure", "HttpOnly", "SameSite=Strict")) and "Max-Age=0" in gc_clear)
        return ok, f"refuses mock SMS {refuse.returncode}, no key {refuse_nokey.returncode}, mock payments {mock_pay.returncode}; staff cookie '{ck[:90]}', HSTS '{hsts}', guest cookie '{gc[:80]}'"
    check("Production", "Production mode (MANTRALAYAM_HTTPS=1): refuses to start with mock SMS, mock payments or no ID key; staff and guest cookies are Secure+HttpOnly+SameSite=Strict; HSTS sent", "start the real servers in HTTPS mode and read the headers", https_mode)

    def config_files():
        out = []
        caddy, nginx = (A.ROOT / "deploy" / "Caddyfile").read_text(), (A.ROOT / "deploy" / "nginx.conf").read_text()
        for name, txt in (("Caddyfile", caddy), ("nginx.conf", nginx)):
            out.append((f"{name} braces balance", txt.count("{") == txt.count("}")))
        out.append(("nginx statements end with ;", all(l.strip().endswith((";", "{", "}")) or not l.strip() or l.strip().startswith("#") for l in nginx.splitlines())))
        import configparser
        for f in sorted((A.ROOT / "deploy").glob("*.service")) + sorted((A.ROOT / "deploy").glob("*.timer")):
            cp = configparser.ConfigParser(strict=False, interpolation=None)
            cp.optionxform = str
            try:
                cp.read_string(f.read_text())
                out.append((f"{f.name} parses", len(cp.sections()) >= 2))
            except configparser.Error as e:
                out.append((f"{f.name} parses: {e}", False))
        real = []
        for tool, args in (("caddy", ["validate", "--config", str(A.ROOT / "deploy" / "Caddyfile")]), ("nginx", ["-t", "-c", str(A.ROOT / "deploy" / "nginx.conf")]), ("systemd-analyze", ["verify", str(A.ROOT / "deploy" / "mantralayam.service")])):
            if shutil.which(tool):
                r = subprocess.run([tool] + args, capture_output=True, text=True)
                real.append((tool, r.returncode == 0))
        bad = [n for n, ok in out + real if not ok]
        return (not bad), f"structural checks only for tools that are not installed ({[t for t in ('caddy', 'nginx', 'systemd-analyze') if not shutil.which(t)]}); failed: {bad}"
    check("Production", "HTTPS/proxy config files: structure checks pass (balanced blocks, nginx statements, systemd units parse); real validators run where installed", "parse deploy/*; caddy/nginx/systemd-analyze only if present", config_files)

    # ---------- secrets ----------
    def secrets_():
        logs = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in A.TMP.glob("*.log") if p.name != "backup.log")
        hits = {n: len(re.findall(x, logs)) for n, x in {"ID number": r"1234-5678-9012|P7654321|ABCD1234", "card number": r"\b(?:\d[ -]?){15,16}\b", "razorpay secret": r"rzp_(live|test)_\w{6,}", "private key": r"BEGIN [A-Z ]*PRIVATE KEY", "encryption key": "audit-encryption-key"}.items()}
        otp_lines = len(re.findall(r"\[mock SMS to \d+\] \d{6} is your", logs))
        # the real SMS provider never writes a code to any log, even when it fails
        import mantralayam_rooms as m
        import secrets as _s
        env_before = {k: os.environ.get(k) for k in ("SMS_PROVIDER", "SMS_HTTP_URL")}
        os.environ["SMS_PROVIDER"], os.environ["SMS_HTTP_URL"] = "http", "https://127.0.0.1:9/send"
        real_rb = _s.randbelow
        _s.randbelow = lambda n: 424242
        buf_o, buf_e = io.StringIO(), io.StringIO()
        old_o, old_e = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = buf_o, buf_e
        try:
            m.dispatch("POST", "/api/public/otp/send", None, {"phone": "9876500099", "purpose": "booking"}, "10.55.5.5")
        finally:
            sys.stdout, sys.stderr = old_o, old_e
            _s.randbelow = real_rb
            for k, v in env_before.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        leaked = "424242" in buf_o.getvalue() + buf_e.getvalue()
        tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=A.ROOT).stdout.split()
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], capture_output=True, text=True, cwd=A.ROOT).stdout.split()
        files = [f for f in tracked + untracked if Path(A.ROOT, f).is_file() and not f.endswith((".png", ".jpg", ".pdf", ".db", ".woff2"))]
        repo = {}
        for f in files:
            if f.startswith("tools/audit_") or f in ("TEST_REPORT.md", "TEST_REPORT_LIVE.md"):
                continue
            t = Path(A.ROOT, f).read_text(encoding="utf-8", errors="ignore")
            for n, x in {"private key": r"BEGIN [A-Z ]*PRIVATE KEY(?!-----SECRETKEY)(?![^\n]{0,40}k\")", "live Razorpay key": r"rzp_live_\w+", "AWS key": r"AKIA[0-9A-Z]{16}", "real-looking secret assignment": r"(?m)^(RAZORPAY_KEY_SECRET|SMTP_PASSWORD|SMS_HTTP_TOKEN|ID_ENCRYPTION_KEY)=[^\s#<]+"}.items():
                if re.search(x, t):
                    repo.setdefault(n, []).append(f)
        secret_files = [f for f in tracked if f in (".env", "credentials.json", "token.json") or f.endswith((".db", ".pem"))]
        ok = not any(hits[k] for k in ("ID number", "card number", "razorpay secret", "private key", "encryption key")) and not leaked and not repo and not secret_files
        return ok, f"log hits {hits}; real SMS provider leaked its code on failure: {leaked}; repo findings {repo}; tracked secret files {secret_files}; mock-provider OTP lines (printed by design in development, never by the real provider): {otp_lines}"
    check("Production", "No secrets, card data, ID numbers or OTPs in server logs or the repo; the real SMS provider never logs a code, even when it fails", "regex scan of every server log of this audit and of all tracked/untracked files; failing real-provider send with a fixed code", secrets_)


# ======================================================================= browser phase
AXE = None


def axe_scan(pg, label, bad):
    global AXE
    if AXE is None:
        d = os.environ.get("AUDIT_AXE", "")
        f = Path(d) / "node_modules" / "axe-core" / "axe.min.js"
        AXE = f.read_text(encoding="utf-8") if f.exists() else ""
    if not AXE:
        return None
    pg.add_script_tag(content=AXE)
    res = pg.evaluate("axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] } }).then(r => r.violations.map(v => ({ id: v.id, impact: v.impact, n: v.nodes.length, help: v.help, sample: v.nodes[0].target.join(' ').slice(0, 90) })))")
    serious = [v for v in res if v["impact"] in ("serious", "critical")]
    bad.extend((label, v) for v in serious)
    return res


def run_browser(X, p):
    base, sbase = f"http://127.0.0.1:{A.GP}", f"http://127.0.0.1:{A.SP}"
    b = p.chromium.launch(channel="msedge", headless=True)
    errs, axe_bad, axe_all = [], [], {}

    def watch(pg, label):
        pg.on("pageerror", lambda e: errs.append(f"{label} pageerror {str(e)[:150]}"))
        pg.on("response", lambda r: errs.append(f"{label} HTTP {r.status} {r.url}") if r.status >= 400 and "127.0.0.1" in r.url and "/api/" not in r.url else None)

    # ---- stored XSS does not run ----
    def xss():
        gid, ph = EXTRA["xss"]
        pg = b.new_context(viewport={"width": 1200, "height": 900}).new_page()
        watch(pg, "xss")
        dialogs = []
        pg.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        pg.goto(base + "/#/booking"); pg.wait_for_selector("#m_id")
        pg.fill("#m_id", str(gid)); pg.fill("#m_phone", ph); pg.click("form.lookup button[type=submit]"); pg.wait_for_selector(".conf")
        guest_flag = pg.evaluate("window.__xss || null")
        guest_shows = "<img src=x" in pg.inner_text(".conf") or "&lt;img" in pg.content()
        injected_guest = pg.locator(".conf img[src='x'], .conf script").count()
        s = b.new_context(viewport={"width": 1300, "height": 900}).new_page()
        s.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        s.goto(sbase + "/staff"); s.fill("#u", "manager"); s.fill("#p", A.PW); s.click("button[type=submit]"); s.wait_for_selector("#navlist a")
        s.goto(f"{sbase}/staff#/bookings"); s.wait_for_selector("main h1"); s.fill("#f_q", str(gid)) if s.locator("#f_q").count() else None
        s.wait_for_timeout(1200)
        s.goto(f"{sbase}/staff#/desk"); s.wait_for_selector("#d_code"); s.fill("#d_code", str(gid)); s.press("#d_code", "Enter"); s.wait_for_selector("text=Guest ID"); s.wait_for_timeout(300)
        staff_flag = s.evaluate("window.__xss || null")
        injected_staff = s.locator("main img[src='x'], main script").count()
        return not dialogs and guest_flag is None and staff_flag is None and injected_guest == 0 and injected_staff == 0 and guest_shows, f"alerts {dialogs}, guest flag {guest_flag}, staff flag {staff_flag}, injected nodes {injected_guest}/{injected_staff}, shown as text {guest_shows}"
    check("Production", "Stored script injection never runs: guest booking page and staff front desk show it as text", "browser: load pages with the <img onerror>/<script> name; watch dialogs, globals and injected nodes", xss)

    # ---- mobile layout of the booking flow, and the desktop header ----
    def mobile():
        out = {}
        ctx = b.new_context(viewport={"width": 375, "height": 700}, is_mobile=True, has_touch=True)
        pg = ctx.new_page()
        watch(pg, "mobile")
        over = lambda: pg.evaluate("[document.documentElement.scrollWidth, innerWidth]")
        pg.goto(base + "/"); pg.wait_for_selector("#s_in")
        out["home"] = over()
        pg.fill("#s_in", D(20)); pg.fill("#s_out", D(22)); pg.click("form#search button[type=submit]"); pg.wait_for_selector(".rt .btn"); pg.wait_for_timeout(500)
        out["results"] = over()
        pg.click(".rt .btn >> nth=0"); pg.wait_for_selector("#bdlg[open]"); pg.wait_for_timeout(800)
        out["dialog"] = [pg.evaluate("document.querySelector('#bdlg').scrollWidth"), pg.evaluate("document.querySelector('#bdlg').clientWidth")]
        sub = pg.locator("#b_submit")
        sub.scroll_into_view_if_needed()
        box = sub.bounding_box()
        out["submit button reachable"] = bool(box and box["x"] >= 0 and box["x"] + box["width"] <= 376 and box["width"] >= 44 and box["height"] >= 44)
        back = pg.locator("#b_back").bounding_box()
        out["back button"] = bool(back and back["x"] >= 0 and back["x"] + back["width"] <= 376)
        small = pg.eval_on_selector_all("#bdlg button, #bdlg input:not([type=hidden]):not([tabindex='-1']), #bdlg select", "els => els.filter(e => { const box = (['checkbox', 'radio'].includes(e.type) && e.closest('label')) || e; const r = box.getBoundingClientRect(); return r.width > 0 && (r.height < 43.5) && !e.closest('[inert]') }).map(e => (e.id || e.name || e.tagName) + ':' + e.type)")  # a checkbox is tapped through its label
        out["small tap targets"] = small
        pg.click("#b_back")
        for r in ("booking", "policy", "contact", "festivals", "subscribe", "faq"):
            pg.goto(f"{base}/#/{r}"); pg.wait_for_selector("main h1"); pg.wait_for_timeout(300)
            out[r] = over()
        wide = b.new_context(viewport={"width": 1100, "height": 800}).new_page()
        wide.goto(base + "/"); wide.wait_for_selector("#s_in")
        out["desktop header"] = wide.evaluate("[document.documentElement.scrollWidth, innerWidth, Math.max(...[...document.querySelectorAll('#nav a, #nav button, #nav select')].map(e => e.getBoundingClientRect().right))]")
        bad = {k: v for k, v in out.items() if (isinstance(v, list) and len(v) == 2 and v[0] > v[1] + 1) or v is False or (k == "small tap targets" and v) or (k == "desktop header" and (v[0] > v[1] + 1 or v[2] > v[1]))}
        return not bad, f"overflow or unreachable: {bad}; all: {out}"
    check("Content", "Mobile (375 px): no sideways scroll on the booking flow and content pages, buttons reachable and at least 44 px; desktop header fits", "browser at 375 and 1100 px: scrollWidth, button boxes, tap-target sizes", mobile)

    # ---- link checker ----
    def links():
        pg = b.new_context(viewport={"width": 1200, "height": 900}).new_page()
        watch(pg, "links")
        pg.goto(base + "/"); pg.wait_for_selector("#s_in")
        routes = sorted(set(pg.eval_on_selector_all("a[href^='#/']", "els => els.map(e => e.getAttribute('href'))")))
        broken, ext, checked = [], set(), 0
        seen = set()
        for r in routes + ["#/festivals", "#/subscribe", "#/unsubscribe", "#/feedback", "#/account", "#/booking"]:
            pg.goto(base + "/" + r); pg.wait_for_timeout(700)
            if len(pg.eval_on_selector("#view", "e => e.textContent").strip()) < 20:
                broken.append((r, "page did not render"))
            for sel, attr in (("a[href]", "href"), ("img[src]", "src"), ("link[href]", "href"), ("script[src]", "src"), ("iframe[src]", "src")):
                for u in pg.eval_on_selector_all(sel, f"els => els.map(e => e.{attr}).filter(Boolean)"):
                    if u in seen or u.startswith(("tel:", "mailto:", "javascript:", "data:", "blob:")):
                        continue
                    seen.add(u)
                    if u.startswith(("https://wa.me/",)) or not u.startswith(base) and not u.startswith(sbase):
                        ext.add(u)
                        continue
                    if "#" in u.split("/", 3)[-1] and u.startswith(base + "/#"):
                        continue
                    checked += 1
                    st = guest(uniq_ip()).req("GET", u.replace(base, ""))[0] if u.startswith(base) else A.Client(A.SP).req("GET", u.replace(sbase, ""))[0]
                    if st >= 400 and not (u.startswith(sbase) and st == 401):
                        broken.append((u, st))
        malformed = [u for u in ext if not re.match(r"https?://[\w.-]+(:\d+)?(/\S*)?$", u)]
        return not broken and not malformed, f"{len(routes)} routes rendered, {checked} same-site URLs fetched, broken {broken[:5]}; {len(ext)} external links (not fetched): {sorted(ext)[:6]}, malformed {malformed}"
    check("Content", "Link checker: every route renders; every same-site link, image, stylesheet and script resolves; external links are well-formed", "browser crawl of all nav/footer routes; GET every same-origin URL found", links)
    rec("Content", "External links reachable (OpenStreetMap, Google Fonts, Razorpay Checkout, wa.me)", "not fetched: no internet access is assumed in the audit", "blocked", "the links are checked for form only; open them once from a connected machine")

    # ---- accessibility (axe) ----
    def a11y():
        if not os.environ.get("AUDIT_AXE") or not (Path(os.environ["AUDIT_AXE"]) / "node_modules" / "axe-core" / "axe.min.js").exists():
            return "blocked", "axe-core not installed (set AUDIT_AXE to a folder with node_modules/axe-core)"
        pg = b.new_context(viewport={"width": 1200, "height": 900}).new_page()
        watch(pg, "axe")
        for label, path in (("home", "/"), ("policy", "/#/policy"), ("contact", "/#/contact"), ("faq", "/#/faq"), ("festivals", "/#/festivals"), ("subscribe", "/#/subscribe"), ("my booking", "/#/booking"), ("account", "/#/account")):
            pg.goto(base + path); pg.wait_for_selector("main h1, #s_in"); pg.wait_for_timeout(500)
            axe_all[label] = axe_scan(pg, label, axe_bad)
        pg.goto(base + "/"); pg.wait_for_selector("#s_in"); pg.fill("#s_in", D(20)); pg.fill("#s_out", D(22)); pg.click("form#search button[type=submit]"); pg.wait_for_selector(".rt .btn"); pg.wait_for_timeout(600)
        axe_all["results"] = axe_scan(pg, "results", axe_bad)
        pg.click(".rt .btn >> nth=0"); pg.wait_for_selector("#bdlg[open]"); pg.wait_for_timeout(600)
        axe_all["booking dialog"] = axe_scan(pg, "booking dialog", axe_bad)
        pg.click("#b_back")
        pg.evaluate("localStorage.setItem('a11y', JSON.stringify({text: 1, contrast: 1}))"); pg.goto(base + "/"); pg.wait_for_selector("#s_in")
        axe_all["home (high contrast)"] = axe_scan(pg, "home (high contrast)", axe_bad)
        pg.evaluate("localStorage.removeItem('a11y')")
        s = b.new_context(viewport={"width": 1300, "height": 900}).new_page()
        s.goto(sbase + "/staff"); s.wait_for_selector("#u"); axe_all["staff login"] = axe_scan(s, "staff login", axe_bad)
        s.fill("#u", "manager"); s.fill("#p", A.PW); s.click("button[type=submit]"); s.wait_for_selector("#navlist a")
        for r in ("desk", "housekeeping", "calendar", "reports", "callbacks"):
            s.goto(f"{sbase}/staff#/{r}"); s.wait_for_selector("main h1"); s.wait_for_timeout(900)
            axe_all["staff " + r] = axe_scan(s, "staff " + r, axe_bad)
        total = sum(len(v or []) for v in axe_all.values())
        return not axe_bad, f"{len(axe_all)} pages scanned ({total} findings of any level); serious/critical: " + "; ".join(f"{l}: {v['id']} x{v['n']} ({v['sample']})" for l, v in axe_bad[:6])
    check("Content", "Accessibility scan (axe-core, WCAG 2 A/AA): no serious or critical issues on key guest and staff pages", "axe-core injected into 15+ pages and dialogs, including high-contrast mode", a11y)
    json.dump({k: v for k, v in axe_all.items()}, open(A.TMP / "axe.json", "w"), indent=1)
    b.close()
    return errs
