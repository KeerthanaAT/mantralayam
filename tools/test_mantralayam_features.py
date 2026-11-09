"""Self-check for production hardening, the booking-flow features and phone OTP. Runs against a COPY of the real database.
Run: python tools/test_mantralayam_features.py   (or pytest)
"""
import http.client
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import mantralayam_rooms as m
import mantralayam_staff as st_app
import providers

SRC = Path(__file__).resolve().parent.parent / ".tmp" / "mantralayam.db"
TMP = Path(tempfile.mkdtemp())
m.DB = TMP / "copy.db"
shutil.copy(SRC, m.DB)
m.BACKUPS = TMP / "backups"
m.BACKUP_MAX_HOURS = 30
D = lambda n: (m.today() + timedelta(days=n)).isoformat()
N = [0]
IP = "127.0.0.1"


def db():
    c = sqlite3.connect(m.DB, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def reset():
    m.reset_throttle()
    providers.OUTBOX.clear()


def call(method, path, data=None, session=None, csrf=None, ip=IP, keep=False):
    if not keep:
        m.reset_throttle()
    q, body = (data, None) if method == "GET" else (None, data)
    return m.dispatch(method, path, q, body, ip, session, csrf)


def setting(**kw):
    c = db()
    for k, v in kw.items():
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
    c.close()


def phone():
    N[0] += 1
    return f"97654{N[0]:05d}"


def req(room_type=1, **kw):
    """A quote/booking request for a type that has free rooms on D(40)-D(42)."""
    c = db()
    pid = c.execute("SELECT property_id FROM rooms WHERE room_type_id = ? AND active = 1 GROUP BY property_id ORDER BY COUNT(*) DESC LIMIT 1", (room_type,)).fetchone()[0]
    cap = c.execute("SELECT capacity FROM room_types WHERE id = ?", (room_type,)).fetchone()[0]
    c.close()
    return dict(property_id=pid, room_type_id=room_type, check_in=D(40), check_out=D(42), rooms=1, guests=min(2, cap)) | kw


def book(proof=None, **kw):
    base = req(**{k: kw.pop(k) for k in ("room_type",) if k in kw})
    return call("POST", "/api/public/book", base | dict(guest_name="Test Guest", phone=phone(), id_type="Aadhaar", id_number="1234", payment_method="pay_at_counter") | ({"otp_proof": proof} if proof else {}) | kw)


def block(room_ids, a, b):
    c = db()
    ids = [c.execute("INSERT INTO room_status_events (room_id, kind, reason, start_date, end_date, active, created_at) VALUES (?, 'internal_block', 'test', ?, ?, 1, 'now')", (r, a, b)).lastrowid for r in room_ids]
    c.close()
    return ids


def unblock(ids):
    c = db()
    c.executemany("UPDATE room_status_events SET active = 0 WHERE id = ?", [(i,) for i in ids])
    c.close()


def rooms_of(room_type, pid):
    c = db()
    try:
        return [r[0] for r in c.execute("SELECT id FROM rooms WHERE room_type_id = ? AND property_id = ? AND active = 1 ORDER BY id", (room_type, pid))]
    finally:
        c.close()


def sms_code():
    msgs = [x for x in providers.OUTBOX if x["kind"] == "sms"]
    return re.match(r"(\d{6}) is your", msgs[-1]["text"])[1]


def setup():
    m.OTP_REQUIRED = False  # the booking tests go straight through; the OTP tests switch it on
    m.Handler.log_message = st_app.Handler.log_message = lambda *a: None
    m.connect().close()  # migrates the copy
    c = db()
    c.execute("DELETE FROM peak_periods")
    c.close()
    setting(guest_max_rooms=3, guest_advance_days=200)


# ---------------------------------------------------------------- production hardening
def test_rate_limits_in_database():
    reset()
    for _ in range(3):
        m.throttle(("probe", "a"), 3, 60, "Too many.")
    try:
        m.throttle(("probe", "a"), 3, 60, "Too many.")
        raise AssertionError("fourth hit must be refused")
    except m.UserError as e:
        assert e.status == 429 and e.retry_after and "Please try again in" in str(e)
    m.MIGRATED.clear()  # a "restart": nothing in memory, the counters are in the database
    assert db().execute("SELECT COUNT(*) FROM rate_hits WHERE key = 'probe|a'").fetchone()[0] == 3
    try:
        m.throttle(("probe", "a"), 3, 60, "Too many.")
        raise AssertionError("still limited after a restart")
    except m.UserError:
        pass
    m.throttle(("probe", "b"), 3, 60, "Too many.")  # other keys are independent
    db().execute("UPDATE rate_hits SET ts = ts - 120 WHERE key = 'probe|a'")
    m.throttle(("probe", "a"), 3, 60, "Too many.")  # the window moved on
    # the staff login lock-out is stored the same way, and is cleared by a good login
    h, salt = st_app.make_hash("Pw#12345")
    db().execute("UPDATE users SET pw_hash = ?, salt = ? WHERE username = 'frontdesk'", (h, salt))
    for _ in range(5):
        st_app.dispatch("POST", "/api/login", None, {"username": "frontdesk", "password": "bad"}, IP)
    assert st_app.dispatch("POST", "/api/login", None, {"username": "frontdesk", "password": "Pw#12345"}, IP)[0] == 429
    assert db().execute("SELECT COUNT(*) FROM rate_hits WHERE key LIKE 'staff-login|%'").fetchone()[0] == 5
    reset()
    assert st_app.dispatch("POST", "/api/login", None, {"username": "frontdesk", "password": "Pw#12345"}, IP)[0] == 200


def test_health_and_alerts():
    reset()
    c = db()
    c.execute("DELETE FROM alerts")  # the development database may hold an open alert from an earlier dashboard visit; the test starts clean
    c.close()
    m.BACKUPS.mkdir(exist_ok=True)
    for f in m.BACKUPS.glob("*"):
        f.unlink()
    status, out = call("GET", "/health", {})
    assert status == 503 and out["checks"] == {"database": "ok", "backup": "none"}, out  # no backup has ever run
    assert "path" not in json.dumps(out)
    with open(m.BACKUPS / "backup.log", "w") as f:
        f.write(f"{(datetime.now() - timedelta(hours=40)).isoformat(timespec='seconds')}\tOK\tmantralayam-old.db\n")
    status, out = call("GET", "/health", {})
    assert status == 503 and out["checks"]["backup"] == "stale" and "backup_age_hours" not in out["checks"]
    with open(m.BACKUPS / "backup.log", "a") as f:
        f.write(f"{m.now_iso()}\tFAILED\tdisk full\n")
    assert call("GET", "/health", {})[1]["checks"]["backup"] == "failed"

    # a failed backup opens one alert, emails the staff once, and the next good backup clears it
    os.environ["STAFF_ALERT_EMAILS"] = "owner@example.org, manager@example.org"
    providers.OUTBOX.clear()
    c = m.connect()
    open_ = m.check_alerts(c)
    assert [a["kind"] for a in open_] == ["backup"] and "FAILED" in open_[0]["message"]
    mails = [x for x in providers.OUTBOX if x["kind"] == "email"]
    assert sorted(x["to"] for x in mails) == ["manager@example.org", "owner@example.org"], mails
    m.check_alerts(c)
    assert len([x for x in providers.OUTBOX if x["kind"] == "email"]) == 2, "an open alert is emailed once"
    assert c.execute("SELECT COUNT(*) FROM alerts WHERE resolved_at IS NULL").fetchone()[0] == 1
    c.close()
    io_stderr(lambda: m.backup())
    status, out = call("GET", "/health", {})
    assert status == 200 and out["status"] == "ok" and out["checks"]["backup"] == "ok" and "backup_age_hours" not in out["checks"], out
    c = m.connect()
    assert m.alerts_open(c) == [], "a good backup resolves the backup alert"

    # holds: many rooms held at once, and many holds lapsing unbooked
    total = c.execute("SELECT COUNT(*) FROM rooms WHERE active = 1").fetchone()[0]
    exp = (m.now() + timedelta(minutes=1)).isoformat(timespec="seconds")
    ids = [r[0] for r in c.execute("SELECT id FROM rooms WHERE active = 1 LIMIT ?", (total // 3,))]
    for i, rid in enumerate(ids):
        c.execute("INSERT INTO holds (token, room_id, check_in, check_out, expires_at, ip) VALUES (?,?,?,?,?,?)", (f"tok{i}", rid, D(60), D(61), exp, "1.1.1.1"))
    kinds = {(a["kind"], a["key"]) for a in m.check_alerts(c)}
    assert ("holds", "pressure") in kinds, kinds
    c.execute("DELETE FROM holds")
    past = (m.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
    for i in range(m.HOLD_LAPSE_ALERT):
        c.execute("INSERT INTO holds (token, room_id, check_in, check_out, expires_at, ip) VALUES (?,?,?,?,?,?)", (f"old{i}", ids[0], D(60), D(61), past, "1.1.1.1"))
    kinds = {(a["kind"], a["key"]) for a in m.check_alerts(c)}
    assert kinds == {("holds", "lapsed")}, kinds  # the pressure alert cleared by itself
    c.execute("DELETE FROM holds")
    assert m.check_alerts(c) == []
    c.close()
    os.environ.pop("STAFF_ALERT_EMAILS")
    m.DB, real = TMP / "gone.db", m.DB
    try:
        assert call("GET", "/health", {})[0] == 503 and call("GET", "/health", {})[1]["checks"]["database"] == "fail"
    finally:
        m.DB = real


def io_stderr(fn):
    import contextlib
    import io
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        return fn()


def test_security_baseline():
    # input shape
    for bad in ({"a": "x" * 3000}, {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}, {f"k{i}": 1 for i in range(70)}, {"a": object()}):
        try:
            m.clean_body(bad)
            raise AssertionError(bad)
        except m.UserError:
            pass
    assert m.clean_body({"a": "ok\x00bad\x07", "n": 1, "l": [True, None]}) == {"a": "okbad", "n": 1, "l": [True, None]}
    assert call("POST", "/api/public/callback", {"name": "x" * 3000, "phone": "9876543210"})[0] == 400
    # CSRF token belongs to the session
    tok = "session-token"
    assert m.csrf_ok(tok, m.csrf_token(tok)) and not m.csrf_ok(tok, m.csrf_token("other")) and not m.csrf_ok(tok, None) and not m.csrf_ok(None, m.csrf_token(None))
    # proxy headers are only believed from a trusted proxy, and read from the right
    m.TRUST_PROXY = True
    try:
        h = {"X-Forwarded-For": "6.6.6.6, 203.0.113.9"}
        assert m.client_ip(h, "127.0.0.1") == "203.0.113.9"  # the left part is whatever the visitor wrote
        assert m.client_ip(h, "198.51.100.1") == "198.51.100.1"  # not from our proxy: ignored
        assert m.client_ip({"X-Forwarded-For": "not-an-ip"}, "127.0.0.1") == "127.0.0.1"
        assert m.is_https({"X-Forwarded-Proto": "https"}) and not m.is_https({"X-Forwarded-Proto": "http"})
    finally:
        m.TRUST_PROXY = False
    assert m.client_ip({"X-Forwarded-For": "6.6.6.6"}, "127.0.0.1") == "127.0.0.1" and not m.is_https({"X-Forwarded-Proto": "https"})
    # origin and host checks
    assert m.request_ok({"Host": "rooms.example", "Origin": "https://rooms.example"}, "POST") is None
    assert m.request_ok({"Host": "rooms.example", "Origin": "https://evil.example"}, "POST")[0] == 403
    assert m.request_ok({"Host": "rooms.example", "Origin": "null"}, "POST")[0] == 403
    assert m.request_ok({"Host": "rooms.example"}, "POST") is None and m.request_ok({"Host": "rooms.example", "Origin": "https://evil.example"}, "GET") is None
    m.ALLOWED_HOSTS = {"rooms.example"}
    try:
        assert m.request_ok({"Host": "other.example"}, "GET")[0] == 400 and m.request_ok({"Host": "rooms.example:8001"}, "GET") is None
    finally:
        m.ALLOWED_HOSTS = set()
    # headers
    hs = dict(m.security_headers({}))
    assert "Strict-Transport-Security" not in hs and hs["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in hs["Content-Security-Policy"]
    m.HTTPS_ONLY = True
    try:
        assert "max-age=31536000" in dict(m.security_headers({}))["Strict-Transport-Security"]
    finally:
        m.HTTPS_ONLY = False
    # staff: a signed-in POST needs the CSRF header when check_csrf is on (the HTTP handler always turns it on)
    h, salt = st_app.make_hash("Pw#12345")
    db().execute("UPDATE users SET pw_hash = ?, salt = ? WHERE username = 'admin'", (h, salt))
    reset()
    status, out, tok = st_app.dispatch("POST", "/api/login", None, {"username": "admin", "password": "Pw#12345"}, IP)
    assert status == 200 and out["csrf"] == m.csrf_token(tok)
    body = {"name": "Q", "phone": "9876543210"}
    assert st_app.dispatch("POST", "/api/me/profile", None, {"full_name": "Admin"}, IP, tok, None, check_csrf=True)[0] == 403
    assert st_app.dispatch("POST", "/api/me/profile", None, {"full_name": "Admin"}, IP, tok, "wrong", check_csrf=True)[0] == 403
    assert st_app.dispatch("POST", "/api/me/profile", None, {"full_name": "Admin"}, IP, tok, out["csrf"], check_csrf=True)[0] == 200
    assert st_app.dispatch("GET", "/api/meta", None, None, IP, tok, None, check_csrf=True)[0] == 200  # reads need no token


def test_captcha_flag():
    assert providers.captcha_config() is None and call("GET", "/api/public/info")[1]["captcha"] is None
    os.environ["CAPTCHA_PROVIDER"] = "mock"
    try:
        assert call("GET", "/api/public/info")[1]["captcha"] == {"provider": "mock", "site_key": ""}
        cb = dict(name="Asha", phone=phone(), call_time="any")
        assert call("POST", "/api/public/callback", cb)[0] == 400
        assert call("POST", "/api/public/callback", cb | {"captcha_token": "wrong"})[0] == 400
        assert call("POST", "/api/public/callback", cb | {"captcha_token": "mock-pass"})[0] == 200
        assert call("POST", "/api/public/otp/send", {"phone": phone(), "purpose": "booking"})[0] == 400
        assert call("POST", "/api/public/waitlist", {"name": "A", "phone": phone(), "check_in": D(40), "check_out": D(41), "guests": 1})[0] == 400
        os.environ["CAPTCHA_PROVIDER"] = "turnstile"
        assert "challenges.cloudflare.com" in m.csp()
        os.environ["CAPTCHA_PROVIDER"] = "bogus"
        try:
            providers.captcha_config()
            raise AssertionError("unknown provider")
        except providers.ProviderError:
            pass
    finally:
        os.environ.pop("CAPTCHA_PROVIDER")
    assert "challenges.cloudflare.com" not in m.csp()


def test_http_layer():
    """The real handler: headers, HSTS and Secure cookies behind a TLS proxy, origin check, HEAD for monitors."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def get(path, method="GET", headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data
    try:
        m.reset_throttle()
        r, _ = get("/api/public/info")
        assert r.status == 200 and r.getheader("X-Frame-Options") == "DENY" and r.getheader("Strict-Transport-Security") is None
        m.HTTPS_ONLY = True
        r, _ = get("/api/public/info")
        assert r.getheader("Strict-Transport-Security").startswith("max-age=")
        m.HTTPS_ONLY = False
        r, data = get("/health", "HEAD")
        assert r.status in (200, 503) and data == b""
        r, _ = get("/api/public/callback", "POST", {"Content-Type": "application/json", "Origin": "https://evil.example"}, {"name": "x"})
        assert r.status == 403
        r, _ = get("/api/public/callback", "POST", {"Content-Type": "text/plain"}, {"name": "x"})
        assert r.status == 415
        # the sign-in cookie of a guest is Secure + HttpOnly behind HTTPS
        db().execute("INSERT INTO bookings (property_id, room_id, guest_name, phone, id_type, id_number, guests, check_in, check_out, status, created_at, status_at, source, payment_status, amount) "
                     "SELECT 1, id, 'Cookie', '9111111111', 'Aadhaar', '1', 1, ?, ?, 'Confirmed', 'now', 'now', 'online', 'due', 1 FROM rooms WHERE property_id = 1 LIMIT 1", (D(80), D(81)))
        m.reset_throttle()
        get("/api/public/otp/send", "POST", {"Content-Type": "application/json"}, {"phone": "9111111111", "purpose": "login"})
        code = sms_code()
        m.HTTPS_ONLY = True
        r, data = get("/api/public/otp/verify", "POST", {"Content-Type": "application/json"}, {"phone": "9111111111", "purpose": "login", "code": code})
        assert r.status == 403, "over HTTPS a POST without an Origin header is refused"
        m.reset_throttle()
        get("/api/public/otp/send", "POST", {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}, {"phone": "9111111111", "purpose": "login"})
        code = sms_code()
        r, data = get("/api/public/otp/verify", "POST", {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}, {"phone": "9111111111", "purpose": "login", "code": code})
        cookie = r.getheader("Set-Cookie")
        assert r.status == 200 and cookie.startswith("guest_session=") and "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Strict" in cookie, cookie
        assert "_session" not in json.loads(data) and json.loads(data)["csrf"]
        tok = cookie.split(";")[0].split("=", 1)[1]
        # no CSRF header: the POST is refused; with it, accepted
        r, _ = get("/api/me/logout", "POST", {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}", "Cookie": f"guest_session={tok}"}, {})
        assert r.status == 403
        r, _ = get("/api/me/logout", "POST", {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}", "Cookie": f"guest_session={tok}", "X-CSRF-Token": m.csrf_token(tok)}, {})
        assert r.status == 200 and "Max-Age=0" in r.getheader("Set-Cookie")
        m.HTTPS_ONLY = False
        # staff app: Secure cookie behind HTTPS, health route, CSRF at the handler
        ssrv = ThreadingHTTPServer(("127.0.0.1", 0), st_app.Handler)
        threading.Thread(target=ssrv.serve_forever, daemon=True).start()
        c = http.client.HTTPConnection("127.0.0.1", ssrv.server_address[1])
        origin = f"http://127.0.0.1:{ssrv.server_address[1]}"
        h, salt = st_app.make_hash("Pw#12345")
        db().execute("UPDATE users SET pw_hash = ?, salt = ? WHERE username = 'manager'", (h, salt))
        m.reset_throttle()
        m.HTTPS_ONLY = True
        c.request("POST", "/api/login", json.dumps({"username": "manager", "password": "Pw#12345"}), {"Content-Type": "application/json", "Origin": origin})
        r = c.getresponse()
        data = json.loads(r.read())
        cookie = r.getheader("Set-Cookie")
        assert r.status == 200 and "Secure" in cookie and "HttpOnly" in cookie and r.getheader("Strict-Transport-Security"), cookie
        stoken = cookie.split(";")[0].split("=", 1)[1]
        c.request("POST", "/api/me/profile", json.dumps({"full_name": "M"}), {"Content-Type": "application/json", "Origin": origin, "Cookie": f"staff_session={stoken}"})
        r = c.getresponse(); r.read()
        assert r.status == 403
        c.request("POST", "/api/me/profile", json.dumps({"full_name": "M"}), {"Content-Type": "application/json", "Origin": origin, "Cookie": f"staff_session={stoken}", "X-CSRF-Token": data["csrf"]})
        r = c.getresponse(); r.read()
        assert r.status == 200
        c.request("GET", "/health")
        r = c.getresponse(); r.read()
        assert r.status in (200, 503)
        ssrv.shutdown()
    finally:
        m.HTTPS_ONLY = False
        srv.shutdown()


# ---------------------------------------------------------------- booking flow
def test_pricing_breakdown():
    from decimal import ROUND_HALF_UP, Decimal
    r = req()
    st, q = call("POST", "/api/public/quote", r)
    c = db()
    tariff = Decimal(str(c.execute("SELECT tariff FROM room_types WHERE id = 1").fetchone()[0]))
    assert st == 200 and q["room_charge"] == float(tariff * 2) and q["total"] == q["room_charge"] and q["tax"] == 0 and q["advance"] == 0 and q["discount"] is None, q  # no rule configured: no extras
    # rules are configured, not invented
    setting(tax_percent="12", advance_percent="30", extra_bed_price="200", extra_bed_max="1", child_price="100")
    cap = c.execute("SELECT capacity FROM room_types WHERE id = 1").fetchone()[0]
    r2 = r | {"guests": cap + 1, "children": 1, "extra_beds": 1}
    st, q = call("POST", "/api/public/quote", r2)
    room = tariff * 2
    beds, kids = Decimal(400), Decimal(200)
    tax = ((room + beds + kids) * Decimal("0.12")).quantize(Decimal("0.01"), ROUND_HALF_UP)
    total = room + beds + kids + tax
    assert st == 200 and (q["extra_bed_charge"], q["child_charge"], q["tax"], q["total"]) == (400.0, 200.0, float(tax), float(total)), q
    assert q["advance"] == float((total * Decimal("0.3")).quantize(Decimal("0.01"), ROUND_HALF_UP)) and round(q["advance"] + q["balance"], 2) == q["total"]
    # rules: beds only when offered and within the maximum per room; children within guests
    assert call("POST", "/api/public/quote", r2 | {"extra_beds": 2})[0] == 400
    assert call("POST", "/api/public/quote", r | {"children": 5})[0] == 400
    # booking it stores that exact breakdown, the rows add up to the total, and the booking view carries it
    st, b = book(**{k: r2[k] for k in ("guests", "children", "extra_beds")})
    assert st == 200 and b["amount"] == float(total) and b["price"]["total"] == float(total) and b["advance_due"] == q["advance"] and b["children"] == 1 and b["extra_beds"] == 1, b
    # more guests than rooms + beds hold is refused
    assert book(guests=cap + 2, children=0, extra_beds=1)[0] == 400
    # child_counts_capacity=0: children share the beds already there
    setting(child_counts_capacity="0")
    assert book(guests=cap + 1, children=1)[0] == 200
    setting(child_counts_capacity="1", tax_percent="", advance_percent="", extra_bed_price="", child_price="")
    c.close()


def test_peak_prices_and_calendar():
    c = db()
    c.execute("INSERT INTO peak_periods (name, start_date, end_date, max_nights, max_rooms, surcharge_percent) VALUES ('Festival', ?, ?, NULL, NULL, 50)", (D(41), D(41)))
    tariff = c.execute("SELECT tariff FROM room_types WHERE id = 1").fetchone()[0]
    st, q = call("POST", "/api/public/quote", req())
    assert [x["price"] for x in q["night_prices"]] == [tariff, tariff * 1.5] and q["night_prices"][1]["peak"] == "Festival" and q["room_charge"] == tariff * 2.5, q
    st, s = call("GET", "/api/public/search", dict(check_in=D(40), check_out=D(42), guests=2))
    ac = [t for p in s["properties"] for t in p["types"] if t["room_type_id"] == 1][0]
    assert ac["total_per_room"] == tariff * 2.5 and ac["peak_prices"] is True
    month = D(41)[:7]
    st, cal = call("GET", "/api/public/calendar", dict(month=month, room_type_id=1))
    assert st == 200 and cal["month"] == month
    day = {d["date"]: d for d in cal["days"]}
    assert day[D(41)]["peak"] == "Festival" and day[D(41)]["price"] == tariff * 1.5 and day[D(40)]["price"] == tariff and day[D(40)]["peak"] is None
    assert day[D(40)]["status"] in ("available", "few") and day[(m.today() - timedelta(days=1)).isoformat()]["status"] == "closed" if (m.today() - timedelta(days=1)).isoformat()[:7] == month else True
    # a night with every matching room blocked is sold out in the calendar; the next night is not
    pid = req()["property_id"]
    ids = block(rooms_of(1, pid), D(50), D(51))
    st, cal = call("GET", "/api/public/calendar", dict(month=D(50)[:7], room_type_id=1, property_id=pid))
    day = {d["date"]: d for d in cal["days"]}
    assert day[D(50)]["status"] == "soldout" and day[D(50)]["free"] == 0 and day[D(50)]["price"] is None and day[D(51)]["status"] != "soldout"
    unblock(ids)
    assert call("GET", "/api/public/calendar", dict(month="nonsense"))[0] == 400
    assert call("GET", "/api/public/calendar", dict(month="2020-01"))[0] == 400
    c.execute("DELETE FROM peak_periods")
    c.close()


def test_room_filters():
    c = db()
    ground = [r[0] for r in c.execute("SELECT id FROM rooms WHERE floor = 0 AND room_type_id = 1 AND active = 1")]
    upper = [r[0] for r in c.execute("SELECT id FROM rooms WHERE floor > 0 AND room_type_id = 1 AND active = 1")]
    assert upper, "the data has AC rooms above the ground floor"
    c.execute("UPDATE rooms SET senior_friendly = 0")
    c.execute("UPDATE rooms SET senior_friendly = 1 WHERE id = ?", (upper[0],))
    q = dict(check_in=D(40), check_out=D(42), guests=2, room_type_id=1)
    free = lambda **kw: {x["id"] for x in m.free_rooms(m.connect(), m.today() + timedelta(40), m.today() + timedelta(42), None, 1, **kw)}
    assert free(ground=True) <= set(ground) and free(senior=True) == {upper[0]} and free(senior=True, ground=True) == set()
    st, s = call("GET", "/api/public/search", q | {"senior": "1"})
    types = [t for p in s["properties"] for t in p["types"]]
    assert st == 200 and len(types) == 1 and types[0]["senior_friendly"] and types[0]["can_book"] == 1
    st, s = call("GET", "/api/public/search", q | {"ground_floor": "1"})
    assert all(t["ground_floor"] for p in s["properties"] for t in p["types"])
    st, s = call("GET", "/api/public/search", q)
    assert any(t["senior_friendly"] for p in s["properties"] for t in p["types"])
    # the booking honours the preference: it lands on the senior-friendly room
    pid = c.execute("SELECT property_id FROM rooms WHERE id = ?", (upper[0],)).fetchone()[0]
    st, b = book(property_id=pid, senior="1")
    assert st == 200
    assert c.execute("SELECT r.id FROM bookings x JOIN rooms r ON r.id = x.room_id WHERE x.group_id = ?", (b["booking_id"],)).fetchone()[0] == upper[0]
    st, b2 = book(property_id=pid, senior="1")
    assert st == 409, "only one senior-friendly room exists and it is taken"
    assert call("GET", "/api/public/info")[1]["filters"] == {"senior": True, "ground": True}
    c.execute("UPDATE rooms SET senior_friendly = 0")
    assert call("GET", "/api/public/info")[1]["filters"]["senior"] is False
    c.close()


def test_booking_on_behalf():
    st, b = book(booker_name="Ravi (son)", arrival_contact_name="Driver Kumar", arrival_contact_phone="98 765-43210", guest_name="Grandma Sita")
    assert st == 200 and b["guest_name"] == "Grandma Sita" and b["booker_name"] == "Ravi (son)" and b["arrival_contact_name"] == "Driver Kumar" and b["arrival_contact_phone"] == "9876543210", b
    assert call("POST", "/api/public/booking/lookup", {"booking_id": b["booking_id"], "phone": b["mobile"]})[1]["booker_name"] == "Ravi (son)"  # the booker's mobile is the key
    assert book(arrival_contact_phone="12")[0] == 400
    assert book(booker_name="x" * 101)[0] == 400
    st, plain = book()
    assert plain["booker_name"] is None and plain["arrival_contact_phone"] is None


def test_promo_codes():
    c = db()
    c.execute("DELETE FROM promo_codes")
    mk = lambda **kw: c.execute("INSERT INTO promo_codes (code, label, auto, kind, value, created_at, max_discount, min_rooms, min_guests, min_nights, valid_from, valid_to, max_uses, per_phone_limit, exclude_peak, property_id, room_type_id) "
                                "VALUES (:code, :label, :auto, :kind, :value, 'now', :max_discount, :min_rooms, :min_guests, :min_nights, :valid_from, :valid_to, :max_uses, :per_phone_limit, :exclude_peak, :property_id, :room_type_id)",
                                dict(code=None, label="x", auto=0, kind="percent", value=10, max_discount=None, min_rooms=None, min_guests=None, min_nights=None, valid_from=None, valid_to=None, max_uses=None,
                                     per_phone_limit=None, exclude_peak=1, property_id=None, room_type_id=None) | kw).lastrowid
    tariff = c.execute("SELECT tariff FROM room_types WHERE id = 1").fetchone()[0]
    room = tariff * 2
    quote = lambda code, **kw: call("POST", "/api/public/quote", req() | {"promo_code": code} | kw)
    mk(code="DONOR10", label="Donor 10%")
    mk(code="FLAT500", label="Flat", kind="flat", value=500)
    mk(code="BIGCAP", label="Capped", value=50, max_discount=100)
    st, q = quote("donor10")
    assert st == 200 and q["discount"]["amount"] == room * 0.1 and q["discount"]["code"] == "DONOR10" and q["total"] == room - room * 0.1, q  # case-insensitive
    assert quote("FLAT500")[1]["discount"]["amount"] == 500 and quote("BIGCAP")[1]["discount"]["amount"] == 100
    assert quote("NOSUCH")[0] == 400 and quote("NOSUCH")[1]["error"] == "This code is not valid."
    assert quote("")[1]["discount"] is None
    # tax applies after the discount
    setting(tax_percent="10")
    q = quote("FLAT500")[1]
    assert q["tax"] == round((room - 500) * 0.1, 2) and q["total"] == round((room - 500) * 1.1, 2)
    setting(tax_percent="")
    # expiry, start date, scope and minimums are explained
    mk(code="OLD", label="old", valid_to=D(-1))
    mk(code="SOON", label="soon", valid_from=D(5))
    mk(code="TWOROOMS", label="rooms", min_rooms=2)
    mk(code="OTHERTYPE", label="type", room_type_id=2)
    mk(code="LONGSTAY", label="nights", min_nights=5)
    assert quote("OLD")[1]["error"] == "This code has expired." and quote("SOON")[1]["error"] == "This code is not valid yet."
    assert quote("TWOROOMS")[1]["error"] == "This code does not apply to your rooms, dates or party size." and quote("TWOROOMS", rooms=2, guests=4)[0] == 200
    assert quote("OTHERTYPE")[0] == 400 and quote("LONGSTAY")[0] == 400
    # inactive codes do not exist
    c.execute("UPDATE promo_codes SET active = 0 WHERE code = 'DONOR10'")
    assert quote("DONOR10")[0] == 400
    c.execute("UPDATE promo_codes SET active = 1 WHERE code = 'DONOR10'")
    # usage limits: total and per mobile; a cancelled booking gives its use back
    mk(code="ONCE", label="once", max_uses=1)
    mk(code="PERPHONE", label="per phone", per_phone_limit=1)
    p1 = phone()
    st, b = book(promo_code="ONCE")
    assert st == 200 and b["promo_code"] == "ONCE" and b["amount"] == room * 0.9, b
    assert quote("ONCE")[1]["error"] == "This code has reached its usage limit."
    assert book(promo_code="ONCE")[0] == 400, book(promo_code="ONCE")
    assert call("POST", "/api/public/booking/cancel", {"booking_id": b["booking_id"], "phone": b["mobile"], "reason": "test"})[0] == 200
    assert quote("ONCE")[0] == 200, "cancelling frees the use"
    assert book(phone=p1, promo_code="PERPHONE")[0] == 200
    assert quote("PERPHONE", phone=p1)[1]["error"] == "You have already used this code." and quote("PERPHONE", phone=phone())[0] == 200
    # peak dates are excluded unless the rule says otherwise
    c.execute("INSERT INTO peak_periods (name, start_date, end_date) VALUES ('Utsav', ?, ?)", (D(40), D(41)))
    assert quote("DONOR10")[1]["error"] == "This code cannot be used on peak dates."
    c.execute("UPDATE promo_codes SET exclude_peak = 0 WHERE code = 'DONOR10'")
    assert quote("DONOR10")[0] == 200
    c.execute("DELETE FROM peak_periods")
    # an automatic pilgrim group rate: no code needed once the party qualifies; the better of code and group rate applies
    mk(label="Pilgrim group rate", auto=1, value=15, min_guests=6)
    st, q = call("POST", "/api/public/quote", req(rooms=3, guests=6))
    assert q["discount"]["label"] == "Pilgrim group rate" and q["discount"]["code"] is None and q["discount"]["amount"] == round(tariff * 2 * 3 * 0.15, 2), q
    assert call("POST", "/api/public/quote", req(rooms=3, guests=5))[1]["discount"] is None
    assert quote("FLAT500", rooms=3, guests=6)[1]["discount"]["amount"] == round(tariff * 6 * 0.15, 2), "the larger discount wins; they do not stack"
    assert quote("DONOR10", rooms=3, guests=6)[1]["discount"]["label"] == "Pilgrim group rate"
    # guessing is throttled
    reset()
    codes = [call("POST", "/api/public/quote", req() | {"promo_code": f"GUESS{i}"}, keep=True)[0] for i in range(17)]
    assert codes[:15] == [400] * 15 and codes[15] == 429, codes
    reset()
    c.execute("DELETE FROM promo_codes")
    c.close()


def test_change_dates():
    setting(tax_percent="", advance_percent="")
    c = db()
    c.execute("DELETE FROM promo_codes")
    c.execute("INSERT INTO promo_codes (code, label, kind, value, exclude_peak, created_at) VALUES ('KEEP10', 'k', 'percent', 10, 1, 'now')")
    st, b = book(promo_code="KEEP10")
    gid, ph = b["booking_id"], b["mobile"]
    tariff = c.execute("SELECT tariff FROM room_types WHERE id = 1").fetchone()[0]
    chg = lambda **kw: call("POST", "/api/public/booking/change", {"booking_id": gid, "phone": ph, "check_in": D(60), "check_out": D(63)} | kw)
    st, p = chg()
    assert st == 200 and p["changed"] is False and p["old"]["total"] == tariff * 2 * 0.9 and p["new"]["quote"]["total"] == tariff * 3 * 0.9 and p["difference"] == round(tariff * 0.9, 2), p
    assert c.execute("SELECT check_in FROM bookings WHERE group_id = ?", (gid,)).fetchone()[0] == D(40), "a preview changes nothing"
    # peak dates drop an expired-for-this-stay code and say so
    c.execute("INSERT INTO peak_periods (name, start_date, end_date) VALUES ('Utsav', ?, ?)", (D(60), D(61)))
    st, p = chg()
    assert p["promo_dropped"] is True and p["new"]["quote"]["discount"] is None and p["new"]["quote"]["total"] == tariff * 3
    c.execute("DELETE FROM peak_periods")
    st, out = chg(confirm=True)
    assert st == 200 and out["changed"] and out["booking"]["check_in"] == D(60) and out["booking"]["amount"] == tariff * 3 * 0.9 and out["booking"]["price"]["nights"] == 3, out
    assert out["booking"]["promo_code"] == "KEEP10"
    rows = c.execute("SELECT check_in, check_out FROM bookings WHERE group_id = ?", (gid,)).fetchall()
    assert [tuple(r) for r in rows] == [(D(60), D(63))]
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'booking.online_change_dates'").fetchone()[0] == 1
    assert chg(check_in=D(60), check_out=D(63))[1]["error"] == "The new dates are the same as your current dates."
    assert chg(check_in=D(-3), check_out=D(-1))[0] == 400 and chg(check_in=D(62), check_out=D(61))[0] == 400
    assert call("POST", "/api/public/booking/change", {"booking_id": gid, "phone": "9000000000", "check_in": D(60), "check_out": D(63)})[0] == 404, "wrong mobile"
    # no free room of the type on the new dates: refused, and nothing changed
    pid = c.execute("SELECT property_id FROM bookings WHERE group_id = ?", (gid,)).fetchone()[0]
    ids = block(rooms_of(1, pid), D(70), D(72))
    st, out = chg(check_in=D(70), check_out=D(72), confirm=True)
    assert st == 409 and "not available" in out["error"] and c.execute("SELECT check_in FROM bookings WHERE group_id = ?", (gid,)).fetchone()[0] == D(60)
    unblock(ids)
    # the booking's own room does not block overlapping new dates (D(60)-D(63) to D(61)-D(64))
    assert chg(check_in=D(61), check_out=D(64), confirm=True)[0] == 200
    # moved to another room when the old one is taken on the new dates
    old_room = c.execute("SELECT room_id FROM bookings WHERE group_id = ?", (gid,)).fetchone()[0]
    ids = block([old_room], D(80), D(82))
    st, out = chg(check_in=D(80), check_out=D(82), confirm=True)
    assert st == 200 and c.execute("SELECT room_id FROM bookings WHERE group_id = ?", (gid,)).fetchone()[0] != old_room
    unblock(ids)
    # after the cancellation cutoff, or once a payment is recorded, only the property can change it
    c.execute("UPDATE bookings SET payment_status = 'paid' WHERE group_id = ?", (gid,))
    assert chg(check_in=D(90), check_out=D(91))[1]["error"].startswith("A payment is recorded")
    c.execute("UPDATE bookings SET payment_status = 'due' WHERE group_id = ?", (gid,))
    c.execute("UPDATE settings SET value = '100000' WHERE key = 'guest_cancel_cutoff_hours'") if c.execute("SELECT 1 FROM settings WHERE key='guest_cancel_cutoff_hours'").fetchone() else c.execute("INSERT INTO settings VALUES ('guest_cancel_cutoff_hours', '100000')")
    assert chg(check_in=D(90), check_out=D(91))[1]["error"].startswith("Online changes closed")
    setting(guest_cancel_cutoff_hours=24)
    c.execute("UPDATE bookings SET status = 'Cancelled' WHERE group_id = ?", (gid,))
    assert chg(check_in=D(90), check_out=D(91))[1]["error"] == "This booking can no longer be changed online."
    c.execute("DELETE FROM promo_codes")
    c.close()


def test_waitlist():
    c = db()
    pid = req()["property_id"]
    all_ac = rooms_of(1, pid)
    ids = block(all_ac, D(100), D(102))
    wl = lambda **kw: call("POST", "/api/public/waitlist", dict(name="Meena", phone=phone(), check_in=D(100), check_out=D(102), guests=2, property_id=pid, room_type_id=1, lang="en") | kw)
    # only offered when sold out
    st, s = call("GET", "/api/public/search", dict(check_in=D(100), check_out=D(102), guests=2, property_id=pid, room_type_id=1))
    assert s["properties"] == [] and s["waitlist"] is True
    st, s = call("GET", "/api/public/search", dict(check_in=D(40), check_out=D(42), guests=2))
    assert s["waitlist"] is False
    assert wl(check_in=D(40), check_out=D(42), property_id="", room_type_id="")[1]["error"].startswith("Rooms are available")
    p1, p2 = phone(), phone()
    st, a = wl(phone=p1)
    st, b2 = wl(phone=p2)
    assert st == 200 and a["id"] != b2["id"]
    assert wl(phone=p1)[1]["id"] == a["id"], "the same request twice is one entry"
    assert wl(phone="12")[0] == 400 and wl(name="")[0] == 400 and wl(check_in=D(-2), check_out=D(-1))[0] == 400
    reset()
    for i in (1, 2):  # a mobile number may wait for 3 stays
        ids2 = block(all_ac, D(110 + i), D(111 + i))
        assert wl(phone=p1, check_in=D(110 + i), check_out=D(111 + i))[0] == 200
        unblock(ids2)
    ids3 = block(all_ac, D(120), D(121))
    assert wl(phone=p1, check_in=D(120), check_out=D(121))[0] == 409
    unblock(ids3)
    c.execute("DELETE FROM waitlist WHERE check_in != ?", (D(100),))  # keep the two requests for D(100)
    cn = m.connect()
    providers.OUTBOX.clear()
    assert m.waitlist_tick(cn) == 0, "still full: nobody is told"
    # one room frees up: the guest who asked first is told; the second is not (the room is already promised), and nobody is told twice
    unblock(ids[:1])
    assert m.waitlist_tick(cn) == 1
    sms = [x for x in providers.OUTBOX if x["kind"] == "sms"]
    assert len(sms) == 1 and sms[0]["to"] == p1 and "may now be free" in sms[0]["text"] and D(100) in sms[0]["text"], sms
    assert c.execute("SELECT status FROM waitlist WHERE id = ?", (a["id"],)).fetchone()[0] == "notified" and c.execute("SELECT status FROM waitlist WHERE id = ?", (b2["id"],)).fetchone()[0] == "waiting"
    assert m.waitlist_tick(cn) == 0
    unblock(ids[1:])
    assert m.waitlist_tick(cn) == 1 and [x["to"] for x in providers.OUTBOX if x["kind"] == "sms"][-1] == p2
    # booking the dates closes the request
    assert c.execute("SELECT status FROM waitlist WHERE id = ?", (b2["id"],)).fetchone()[0] == "notified"
    st, bk = call("POST", "/api/public/book", req(check_in=D(100), check_out=D(102)) | dict(guest_name="Meena", phone=p2, id_type="Aadhaar", id_number="1", payment_method="pay_at_counter"))
    assert st == 200 and c.execute("SELECT status FROM waitlist WHERE id = ?", (b2["id"],)).fetchone()[0] == "booked"
    # a cancellation (guest or staff) triggers the check: fill the type, wait, cancel, and the waiter is told at once
    c.execute("DELETE FROM waitlist")
    st, full = call("POST", "/api/public/book", req(check_in=D(130), check_out=D(131), rooms=1, guests=2) | dict(guest_name="F", phone=phone(), id_type="Aadhaar", id_number="1", payment_method="pay_at_counter"))
    held = [r[0] for r in c.execute("SELECT room_id FROM bookings WHERE group_id = ?", (full["booking_id"],))]
    others = [r for r in all_ac if r not in held]
    ids4 = block(others, D(130), D(131))
    providers.OUTBOX.clear()
    pw = phone()
    assert wl(phone=pw, check_in=D(130), check_out=D(131))[0] == 200
    assert call("POST", "/api/public/booking/cancel", {"booking_id": full["booking_id"], "phone": full["mobile"], "reason": "plans changed"})[0] == 200
    assert sorted(x["to"] for x in providers.OUTBOX if x["kind"] == "sms") == sorted([full["mobile"], pw])  # the cancelled booking's guest is told, and so is the waiting guest
    unblock(ids4)
    # cancelling a request: right mobile only
    ids5 = block(all_ac, D(140), D(141))
    p3 = phone()
    st, w3 = wl(phone=p3, check_in=D(140), check_out=D(141))
    assert call("POST", "/api/public/waitlist/cancel", {"id": w3["id"], "phone": "9000000000"})[0] == 404
    assert call("POST", "/api/public/waitlist/cancel", {"id": w3["id"], "phone": p3})[0] == 200
    assert c.execute("SELECT status FROM waitlist WHERE id = ?", (w3["id"],)).fetchone()[0] == "cancelled"
    unblock(ids5)
    # old requests expire
    c.execute("INSERT INTO waitlist (name, phone, phone10, check_in, check_out, guests, created_at) VALUES ('Old', '9000000001', '9000000001', ?, ?, 1, 'now')", (D(-3), D(-2)))
    m.waitlist_tick(cn)
    assert c.execute("SELECT status FROM waitlist WHERE phone10 = '9000000001'").fetchone()[0] == "expired"
    cn.close()
    c.close()


# ---------------------------------------------------------------- phone OTP, My bookings, saved details
def send_otp(ph, purpose="booking", **kw):
    return call("POST", "/api/public/otp/send", {"phone": ph, "purpose": purpose} | kw)


def verify(ph, code, purpose="booking", **kw):
    return call("POST", "/api/public/otp/verify", {"phone": ph, "purpose": purpose, "code": code} | kw)


def test_otp_booking():
    m.OTP_REQUIRED = True
    c = db()
    try:
        ph = phone()
        # a booking without proof is refused
        st, out = book(phone=ph)
        assert st == 403 and "verify your mobile number" in out["error"]
        providers.OUTBOX.clear()
        assert send_otp(ph) == (200, {"ok": True, "expires_in": 300, "resend_after": 30})
        assert len(providers.OUTBOX) == 1 and providers.OUTBOX[0]["to"] == ph
        code = sms_code()
        stored = c.execute("SELECT code_hash, salt FROM otp_codes WHERE phone10 = ?", (ph[-10:],)).fetchone()
        assert code not in stored["code_hash"] and code not in json.dumps([dict(r) for r in c.execute("SELECT * FROM audit_log")]), "the code is stored hashed and never audited"
        wrong = "000000" if code != "000000" else "111111"
        # wrong codes: attempt limit, then the code is dead even if right
        for i in range(4):
            assert verify(ph, wrong)[1]["error"] == "That code is not correct. Please check it and try again."
        st, out = verify(ph, wrong)
        assert st == 429 and "Too many wrong codes" in out["error"]
        assert verify(ph, code)[0] == 429, "a locked code stays locked"
        # resend: cooldown, then a new code replaces the old
        reset()
        c.execute("DELETE FROM otp_codes")
        send_otp(ph)
        st, out = call("POST", "/api/public/otp/send", {"phone": ph, "purpose": "booking"}, keep=True)
        assert st == 429 and out["retry_after"] <= 30 and "wait a little" in out["error"]
        c.execute("DELETE FROM rate_hits WHERE key LIKE 'otp-cool|%'")
        old = sms_code()
        providers.OUTBOX.clear()
        assert call("POST", "/api/public/otp/send", {"phone": ph, "purpose": "booking"}, keep=True)[0] == 200
        new = sms_code()
        assert c.execute("SELECT COUNT(*) FROM otp_codes WHERE phone10 = ?", (ph[-10:],)).fetchone()[0] == 1
        if old != new:
            assert verify(ph, old)[0] == 400
        # expiry
        c.execute("UPDATE otp_codes SET expires_at = ?", ((m.now() - timedelta(seconds=1)).isoformat(timespec="seconds"),))
        assert verify(ph, new)[1]["error"].startswith("The code has expired")
        reset()
        send_otp(ph)
        code = sms_code()
        assert verify(ph, "12345")[0] == 400 and verify(ph, "abcdef")[0] == 400
        st, v = verify(ph, code)
        assert st == 200 and v["proof"] and v["profile"] is None
        proof = v["proof"]
        # the proof belongs to that mobile number, is single use, and a failed booking keeps it
        assert book(proof=proof, phone=phone())[0] == 403
        assert book(proof=proof, phone=ph, id_type="Nonsense")[0] == 400
        st, b = book(proof=proof, phone=ph)
        assert st == 200 and b["mobile"] == ph
        assert book(proof=proof, phone=ph)[0] == 403, "single use"
        # a proof expires
        reset()
        send_otp(ph)
        p2 = verify(ph, sms_code())[1]["proof"]
        c.execute("UPDATE otp_codes SET proof_expires = ?", ((m.now() - timedelta(seconds=1)).isoformat(timespec="seconds"),))
        assert book(proof=p2, phone=ph)[0] == 403
        # a login code is not a booking proof
        reset()
        send_otp(ph, purpose="login")
        st, lv = verify(ph, sms_code(), purpose="login")
        assert st == 200 and "proof" not in lv
        # limits: per mobile number per hour, per device per hour
        reset()
        pn = phone()
        res = []
        for _ in range(7):
            c.execute("DELETE FROM rate_hits WHERE key LIKE 'otp-cool|%'")
            res.append(call("POST", "/api/public/otp/send", {"phone": pn, "purpose": "booking"}, keep=True)[0])
        assert res == [200] * 5 + [429] * 2, res
        reset()
        res = [call("POST", "/api/public/otp/send", {"phone": phone(), "purpose": "booking"}, keep=True)[0] for _ in range(11)]
        assert res == [200] * 10 + [429]
        reset()
        assert call("POST", "/api/public/otp/send", {"phone": "123", "purpose": "booking"})[0] == 400
        assert call("POST", "/api/public/otp/send", {"phone": phone(), "purpose": "x"})[0] == 400
        # codes are never printed outside the mock provider, and a production deployment cannot use the mock
        m.HTTPS_ONLY = True
        try:
            assert send_otp(phone())[0] == 503
            assert m.serve_checks()
            os.environ["SMS_PROVIDER"] = "http"
            assert any("ID_ENCRYPTION_KEY" in x for x in m.serve_checks())  # a public deployment needs the ID encryption key too
            os.environ["ID_ENCRYPTION_KEY"] = "k" * 24
            assert any("shorter than 32" in x for x in m.serve_checks())
            os.environ["ID_ENCRYPTION_KEY"] = "k" * 32
            assert any("APP_SECRET" in x for x in m.serve_checks())
            os.environ["APP_SECRET"] = "s" * 32
            assert any("ALLOWED_HOSTS" in x for x in m.serve_checks())
            m.ALLOWED_HOSTS = {"rooms.example.org"}
            assert m.serve_checks() == []
        finally:
            m.HTTPS_ONLY = False
            os.environ.pop("SMS_PROVIDER", None)
            os.environ.pop("ID_ENCRYPTION_KEY", None)
            os.environ.pop("APP_SECRET", None)
            m.ALLOWED_HOSTS = set()
        os.environ["SMS_PROVIDER"] = "bogus"
        try:
            assert send_otp(phone())[0] in (500, 503)
        finally:
            os.environ.pop("SMS_PROVIDER")
    finally:
        m.OTP_REQUIRED = False
        c.close()


def test_account_and_profile():
    m.OTP_REQUIRED = True
    c = db()
    try:
        ph = phone()
        reset()
        providers.OUTBOX.clear()
        # login for a number with no bookings: same answer, no SMS spent
        assert send_otp(ph, purpose="login") == (200, {"ok": True, "expires_in": 300, "resend_after": 30}) and providers.OUTBOX == []
        # a booking with saved details (opt-in)
        send_otp(ph)
        proof = verify(ph, sms_code())[1]["proof"]
        st, b = book(proof=proof, phone=ph, guest_name="<script>alert(1)</script> Lakshmi", email="l@example.org", id_number="9999 8888 7777", save_details=True)
        assert st == 200
        prof = c.execute("SELECT * FROM guest_profiles WHERE phone10 = ?", (ph[-10:],)).fetchone()
        assert prof["email"] == "l@example.org" and prof["id_type"] == "Aadhaar" and "9999" not in json.dumps(dict(prof)), "the ID number is never saved"
        # no opt-in: nothing saved
        ph2 = phone()
        reset(); send_otp(ph2)
        assert book(proof=verify(ph2, sms_code())[1]["proof"], phone=ph2)[0] == 200
        assert c.execute("SELECT COUNT(*) FROM guest_profiles WHERE phone10 = ?", (ph2[-10:],)).fetchone()[0] == 0
        # the next booking offers the saved details after the code is verified
        reset(); send_otp(ph)
        v = verify(ph, sms_code())[1]
        assert v["profile"]["email"] == "l@example.org" and "id_number" not in v["profile"]
        # sign in
        reset(); providers.OUTBOX.clear()
        send_otp(ph, purpose="login")
        assert len(providers.OUTBOX) == 1
        st, lv = verify(ph, sms_code(), purpose="login")
        tok, csrf = lv.pop("_session"), lv["csrf"]
        assert st == 200 and csrf == m.csrf_token(tok) and c.execute("SELECT COUNT(*) FROM guest_sessions WHERE token_hash = ?", (m.sha(tok),)).fetchone()[0] == 1
        assert tok not in json.dumps([dict(r) for r in c.execute("SELECT * FROM guest_sessions")]), "only a hash of the session token is stored"
        me = lambda method, path, data=None, **kw: call(method, path, data, session=kw.get("session", tok), csrf=kw.get("csrf", csrf), keep=True)
        assert call("GET", "/api/me/bookings")[0] == 401 and call("GET", "/api/me/bookings", session="bogus")[0] == 401
        st, s = me("GET", "/api/me/session")
        assert st == 200 and s["phone_last4"] == ph[-4:] and s["profile"]["name"]
        st, lst = me("GET", "/api/me/bookings")
        assert st == 200 and [x["booking_id"] for x in lst["bookings"]] == [b["booking_id"]] and lst["profile"]
        # CSRF on every POST
        assert me("POST", "/api/me/booking/cancel", {"booking_id": b["booking_id"], "reason": "x"}, csrf="wrong")[0] == 403
        assert me("POST", "/api/me/booking/cancel", {"booking_id": b["booking_id"], "reason": "x"}, csrf="")[0] == 403
        # another number's booking is invisible
        po = phone()
        st, ob = call("POST", "/api/public/book", req() | dict(guest_name="O", phone=po, id_type="Aadhaar", id_number="1", payment_method="pay_at_counter", otp_proof=mkproof(po)))
        assert st == 200
        assert me("POST", "/api/me/booking/cancel", {"booking_id": ob["booking_id"], "reason": "x"})[0] == 404
        # modify details, change dates, receipt, cancel
        st, v = me("POST", "/api/me/booking/update", {"booking_id": b["booking_id"], "arrival_time": "06:30", "darshan_date": D(41), "special_requests": "ground floor", "arrival_contact_name": "Sam", "arrival_contact_phone": "9812345678"})
        assert st == 200 and v["arrival_time"] == "06:30" and v["arrival_contact_phone"] == "9812345678" and v["special_requests"] == "ground floor", v
        assert me("POST", "/api/me/booking/update", {"booking_id": b["booking_id"], "arrival_time": "25:00"})[0] == 400
        st, p = me("POST", "/api/me/booking/change", {"booking_id": b["booking_id"], "check_in": D(45), "check_out": D(47)})
        assert st == 200 and p["changed"] is False
        st, ch = me("POST", "/api/me/booking/change", {"booking_id": b["booking_id"], "check_in": D(45), "check_out": D(47), "confirm": True})
        assert st == 200 and ch["booking"]["check_in"] == D(45)
        st, rc = me("GET", "/api/me/receipt", {"booking_id": b["booking_id"], "lang": "en"})
        assert st == 200 and rc["_filename"] == f"receipt-{b['booking_id']}.html" and "&lt;script&gt;" in rc["_html"] and "<script>alert" not in rc["_html"] and "Booking receipt" in rc["_html"]
        assert f"#{b['booking_id']}" in rc["_html"] and "₹" in rc["_html"]
        assert me("GET", "/api/me/receipt", {"booking_id": ob["booking_id"]})[0] == 404
        st, cv = me("POST", "/api/me/booking/cancel", {"booking_id": b["booking_id"], "reason": "plans changed"})
        assert st == 200 and cv["status"] == "Cancelled"
        # saved details can be deleted
        assert me("POST", "/api/me/profile/delete", {})[0] == 200 and c.execute("SELECT COUNT(*) FROM guest_profiles WHERE phone10 = ?", (ph[-10:],)).fetchone()[0] == 0
        # waiting list entries show up and can be removed
        ids = block(rooms_of(1, req()["property_id"]), D(150), D(151))
        w = call("POST", "/api/public/waitlist", dict(name="Lakshmi", phone=ph, check_in=D(150), check_out=D(151), guests=2, property_id=req()["property_id"], room_type_id=1))[1]
        assert [x["id"] for x in me("GET", "/api/me/bookings")[1]["waitlist"]] == [w["id"]]
        assert me("POST", "/api/me/waitlist/cancel", {"id": w["id"]})[0] == 200 and me("GET", "/api/me/bookings")[1]["waitlist"] == []
        unblock(ids)
        # sign out ends the session
        assert me("POST", "/api/me/logout", {})[0] == 200
        assert me("GET", "/api/me/session")[0] == 401
        # sessions expire
        send_otp(ph, purpose="login")
        tok2 = verify(ph, sms_code(), purpose="login")[1]["_session"]
        c.execute("UPDATE guest_sessions SET expires_at = ?", ((m.now() - timedelta(seconds=1)).isoformat(timespec="seconds"),))
        assert call("GET", "/api/me/session", session=tok2)[0] == 401
    finally:
        m.OTP_REQUIRED = False
        c.close()


def mkproof(ph=None):
    """A verified booking proof for a (new) mobile number, using the real OTP flow."""
    ph = ph or phone()
    send_otp(ph)
    return verify(ph, sms_code())[1]["proof"]


def test_sms_text_uses_locales():
    assert m.tr("en", "sms.otp", code="123456", minutes=5).startswith("123456 is your Mantralayam Rooms")
    assert m.tr("te", "sms.otp", code="123456", minutes=5) == m.tr("en", "sms.otp", code="123456", minutes=5), "a TODO translation falls back to English"
    assert m.tr("en", "no.such.key") == "no.such.key"


if __name__ == "__main__":
    try:
        setup()
        for name, fn in list(globals().items()):
            if name.startswith("test_") and callable(fn):
                try:
                    c = db()
                    c.execute("DELETE FROM bookings WHERE source = 'online'")  # each test starts with the rooms free again (this is a copy of the database)
                    c.execute("DELETE FROM holds")
                    c.close()
                    fn()
                except BaseException:
                    print("FAILED:", name, file=sys.stderr)
                    raise
        print("ok")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
