"""Regression tests for the security audit (docs/SECURITY.md): each test fails on the code as it was before the fixes. Runs against a COPY of the database.
Run: python tools/test_security.py   (or pytest)
"""
import hashlib
import hmac
import http.client
import json
import os
import shutil
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import idvault
import mantralayam_rooms as g
import mantralayam_staff as s
import test_staff_desk as td
import totp

ROOT = Path(__file__).resolve().parent.parent
GUEST = (ROOT / "web" / "guest.html").read_text(encoding="utf-8")
STAFF = (ROOT / "web" / "staff.html").read_text(encoding="utf-8")
FAKE = "9" * 10


def serve(handler, bounded=True):
    srv = (g.BoundedServer if bounded else g.ThreadingHTTPServer)(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def http_get(port, path, method="GET", headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r, data


def test_ip_keys():
    assert g.ip_key("203.0.113.5") == "203.0.113.5" and g.ip_key("::ffff:1.2.3.4") == "1.2.3.4"
    assert g.ip_key("2001:db8:1:2:3:4:5:6") == g.ip_key("2001:db8:1:2:ffff::1") == "2001:db8:1:2::/64", "one IPv6 /64 is one visitor"
    assert g.ip_key("2001:db8:1:3::1") != g.ip_key("2001:db8:1:2::1") and g.ip_key("garbage") == "garbage"


def test_image_names_and_static_paths():
    for bad in ("..\\hero.png", "..\\..\\web\\images\\logo.png", "C:\\Users\\x\\logo.png", "a/b.png", ".hidden.png", "..", ""):
        assert not g.IMAGE_NAME.fullmatch(bad), bad
    assert g.IMAGE_NAME.fullmatch("logo.png") and g.IMAGE_NAME.fullmatch("hero-final-safe5.png")
    srv, port = serve(g.Handler)
    try:
        assert http_get(port, "/images/logo.png")[0].status == 200
        for p in ("/images/..\\images\\logo.png", "/images/C:\\Users\\keerthana\\Desktop\\claude\\web\\images\\logo.png", "/images/..%5cimages%5clogo.png", "/images/%2e%2e/%2e%2e/.env"):
            assert http_get(port, p)[0].status == 404, p
    finally:
        srv.shutdown()


def test_body_length_rules_and_slow_clients():
    H = lambda v: {"Content-Length": v}
    assert g.content_length({}, 10) == 0 and g.content_length(H("10"), 10) == 10
    for bad, status in (("-1", 400), ("abc", 400), ("1e3", 400), ("11", 413)):
        try:
            g.content_length(H(bad), 10)
            raise AssertionError(bad)
        except g.UserError as e:
            assert e.status == status, (bad, e.status)
    old = g.Handler.timeout
    g.Handler.timeout = 1  # a stalled client is dropped, not waited for
    srv, port = serve(g.Handler)
    try:
        for raw in (b"POST /api/public/callback HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 50\r\n\r\n{\"a\":",  # promised 50 bytes, sent 5
                    b"GET /health HTTP/1.1\r\nHost: x\r\n"):  # headers never finished
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.sendall(raw)
            t0 = time.time()
            try:
                sock.recv(1000)
            except socket.timeout:
                raise AssertionError("the server kept waiting")
            assert time.time() - t0 < 4
            sock.close()
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)  # a negative length is refused at once
        sock.sendall(b"POST /api/public/callback HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: -1\r\n\r\n")
        assert b" 400 " in sock.recv(200).split(b"\r\n")[0]
        sock.close()
    finally:
        g.Handler.timeout = old
        srv.shutdown()


def test_connection_cap():
    old = g.MAX_CONNECTIONS
    g.MAX_CONNECTIONS = 1
    srv, port = serve(g.Handler)
    try:
        first = socket.create_connection(("127.0.0.1", port), timeout=5)  # holds the only slot (sends nothing)
        time.sleep(0.3)
        second = socket.create_connection(("127.0.0.1", port), timeout=3)
        try:
            second.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
            got = second.recv(100)
        except ConnectionError:  # Windows reports the close as a reset
            got = b""
        assert got == b"", "beyond the cap a connection is closed at once"
        first.close()
        time.sleep(0.5)
        assert http_get(port, "/health")[0].status in (200, 503), "the slot is free again"
    finally:
        g.MAX_CONNECTIONS = old
        srv.shutdown()


def test_sms_destinations_budget_and_alert():
    for ok in ("9876543210", "+919876543210", "09876543210", "919876543210"):
        g.sms_destination(g.norm_phone(ok))
    for bad in ("+447911123456", "+12025550123", "1234567890", "+919876543210"[:0] or "5876543210"):
        try:
            g.sms_destination(g.norm_phone(bad))
            raise AssertionError(bad)
        except g.UserError as e:
            assert e.status == 400
    assert g.dispatch("POST", "/api/public/otp/send", None, {"phone": "+447911123456", "purpose": "booking"}, "9.9.9.1")[0] == 400
    old = g.SMS_HOURLY_LIMIT
    g.SMS_HOURLY_LIMIT = 3
    g.reset_throttle()
    try:
        res = [g.dispatch("POST", "/api/public/otp/send", None, {"phone": f"98765000{i:02d}", "purpose": "booking"}, f"9.9.9.{i}")[0] for i in range(5)]
        assert res == [200, 200, 200, 503, 503], res
        assert any(a["kind"] == "sms" for a in g.alerts_open(td.db())), "the staff are told when the SMS budget is used up"
        g.reset_throttle()
        g.check_alerts(g.connect())
        assert not any(a["kind"] == "sms" for a in g.alerts_open(td.db())), "and the alert clears when it recovers"
    finally:
        g.SMS_HOURLY_LIMIT = old
        g.reset_throttle()


def test_lookup_failures_are_counted_per_mobile_across_addresses():
    b = td.online(start=40)
    gid, phone = b["booking_id"], td.db().execute("SELECT phone FROM bookings WHERE coalesce(group_id, id) = ?", (b["booking_id"],)).fetchone()[0]
    g.reset_throttle()
    assert g.dispatch("POST", "/api/public/booking/lookup", None, {"booking_id": gid, "phone": phone}, "8.8.8.0")[0] == 200
    codes = [g.dispatch("POST", "/api/public/booking/lookup", None, {"booking_id": gid + 1000 + i, "phone": phone}, f"8.8.8.{i + 1}")[0] for i in range(g.LOOKUP_FAIL_LIMIT)]
    assert codes == [404] * g.LOOKUP_FAIL_LIMIT
    st, out = g.dispatch("POST", "/api/public/booking/lookup", None, {"booking_id": gid, "phone": phone}, "8.8.9.9")
    assert st == 429 and out["retry_after"], "a scan with a known mobile is stopped even when every guess comes from a new address"
    assert g.dispatch("POST", "/api/public/booking/cancel", None, {"booking_id": gid, "phone": phone, "reason": "x"}, "8.8.9.10")[0] == 429
    assert g.dispatch("POST", "/api/public/booking/lookup", None, {"booking_id": gid, "phone": "9000000099"}, "8.8.9.11")[0] == 404, "other mobiles are not affected"
    g.reset_throttle()


def test_site_wide_hold_cap():
    rooms = td.db().execute("SELECT COUNT(*) FROM rooms WHERE active = 1").fetchone()[0]
    old = g.HOLD_GLOBAL_PERCENT
    g.HOLD_GLOBAL_PERCENT = 100 // rooms + 1  # room for exactly one room on hold
    g.reset_throttle()
    try:
        base = dict(property_id=1, room_type_id=td.room_type(1), check_in=td.D(70), check_out=td.D(71), rooms=1, guests=1)
        assert g.dispatch("POST", "/api/public/hold", None, base, "6.6.6.1")[0] == 200
        st, out = g.dispatch("POST", "/api/public/hold", None, base, "6.6.6.2")
        assert st == 429 and "Many rooms are being chosen" in out["error"]
    finally:
        g.HOLD_GLOBAL_PERCENT = old
        g.reset_throttle()
        td.db().execute("DELETE FROM holds")


def test_id_number_is_cut_to_four_characters_at_booking():
    b = td.online(start=45)
    row = td.db().execute("SELECT id_number FROM bookings WHERE coalesce(group_id, id) = ?", (b["booking_id"],)).fetchone()
    assert row[0] == "****7777", row[0]
    walk = td.walkin(td.room_ids(1, start=3)[0], id_number="555566667777")[1]
    assert td.db().execute("SELECT id_number FROM bookings WHERE id = ?", (walk["id"],)).fetchone()[0] == "****7777"
    assert "999988887777" not in "".join(str(r[0]) for r in td.db().execute("SELECT id_number FROM bookings"))
    assert 'maxlength="4"' in GUEST and 'name="id_number"' in GUEST


def test_vault_formats_and_rekey():
    key = "k" * 40

    def seal_v1(plain, aad):  # how the vault wrote values before the scrypt change
        enc, mac = hmac.new(key.encode(), b"mantralayam|enc", "sha256").digest(), hmac.new(key.encode(), b"mantralayam|mac", "sha256").digest()
        nonce = b"n" * 16
        ks = b"".join(hmac.new(enc, nonce + struct.pack(">Q", i), "sha256").digest() for i in range(len(plain) // 32 + 1))
        ct = bytes(a ^ b for a, b in zip(plain, ks))
        return b"\x01" + nonce + ct + hmac.new(mac, b"\x01" + nonce + aad + ct, "sha256").digest()
    old = seal_v1(b"123412341234", b"id:1")
    assert idvault.open_(key, old, b"id:1") == b"123412341234", "version 1 values still open"
    new = idvault.seal(key, b"123412341234", b"id:1")
    assert new[:1] == idvault.V2 and idvault.open_(key, new, b"id:1") == b"123412341234"
    for bad_args in ((key, new, b"id:2"), ("x" * 40, new, b"id:1")):
        try:
            idvault.open_(*bad_args)
            raise AssertionError("opened")
        except idvault.VaultError:
            pass
    tampered = new[:20] + bytes([new[20] ^ 1]) + new[21:]
    try:
        idvault.open_(key, tampered, b"id:1")
        raise AssertionError("tampered value opened")
    except idvault.VaultError:
        pass
    os.environ["ID_ENCRYPTION_KEY"] = key
    c = td.db()
    try:
        bid = c.execute("SELECT id FROM bookings LIMIT 1").fetchone()[0]
        c.execute("DELETE FROM id_records WHERE booking_id = ?", (bid,))
        c.execute("INSERT INTO id_records (booking_id, id_type, last4, number_enc, captured_name, captured_at) VALUES (?,?,?,?,?,?)", (bid, "Aadhaar", "1234", seal_v1(b"123412341234", f"id:{bid}".encode()), "t", "now"))
        conn = g.connect()
        assert g.rekey(conn) >= 1
        blob = bytes(c.execute("SELECT number_enc FROM id_records WHERE booking_id = ?", (bid,)).fetchone()[0])
        assert blob[:1] == idvault.V2 and idvault.open_(key, blob, f"id:{bid}".encode()) == b"123412341234"
        assert g.rekey(conn) == 0, "nothing left to rewrite"
        conn.close()
    finally:
        os.environ.pop("ID_ENCRYPTION_KEY", None)
        c.close()


def test_scrub_moves_old_full_numbers_into_the_vault():
    os.environ["ID_ENCRYPTION_KEY"] = "s" * 40
    c = td.db()
    try:
        room = td.room_ids(1, start=5)[0]
        st, w = td.walkin(room, check_in=td.D(5), check_out=td.D(6))
        c.execute("UPDATE bookings SET id_number = '555566667777' WHERE id = ?", (w["id"],))  # how an old row looked
        conn = g.connect()
        moved, _ = g.scrub_ids(conn)
        conn.close()
        assert moved >= 1
        assert c.execute("SELECT id_number FROM bookings WHERE id = ?", (w["id"],)).fetchone()[0] == "****7777"
        rec = c.execute("SELECT number_enc, last4 FROM id_records WHERE booking_id = ?", (w["id"],)).fetchone()
        assert rec["last4"] == "7777" and idvault.open_("s" * 40, bytes(rec["number_enc"]), f"id:{w['id']}".encode()) == b"555566667777"
        assert td.call("manager", "POST", f"/api/bookings/{w['id']}/id_reveal", {"reason": "audit check"})[1]["id_number"] == "555566667777"
    finally:
        os.environ.pop("ID_ENCRYPTION_KEY", None)
        c.close()


def test_erasure_and_retention():
    c = td.db()
    ph = "9811100001"
    room = td.room_ids(1, start=8)[0]
    for i, (ci, co, st) in enumerate((("2025-01-10", "2025-01-12", "Checked-Out"),)):
        c.execute("INSERT INTO bookings (property_id, room_id, guest_name, phone, id_type, id_number, guests, check_in, check_out, status, created_at, status_at, source, payment_status, amount, email, remarks) "
                  "VALUES (1, ?, 'Old Guest', ?, 'Aadhaar', '****1111', 1, ?, ?, ?, 'now', 'now', 'online', 'due', 1, 'old@example.org', 'secret note')", (room["id"], ph, ci, co, st))
    c.execute("INSERT OR REPLACE INTO guest_profiles (phone10, name, email, id_type, consented_at, updated_at) VALUES (?, 'Old Guest', 'old@example.org', 'Aadhaar', 'now', 'now')", (ph,))
    c.execute("INSERT INTO callback_requests (name, phone, status, created_at) VALUES ('Old Guest', ?, 'new', 'now')", (ph,))
    assert td.call("manager", "POST", "/api/admin/guest_erase", {"phone": ph, "reason": "asked"})[0] == 403, "super admin only"
    assert td.call("admin", "POST", "/api/admin/guest_erase", {"phone": ph})[0] == 400, "a reason is required"
    st, out = td.call("admin", "POST", "/api/admin/guest_erase", {"phone": ph, "reason": "Guest asked by phone"})
    assert st == 200 and out["anonymised"] == 1, out
    row = c.execute("SELECT guest_name, phone, email, remarks, id_number, amount, check_in FROM bookings WHERE guest_name = 'Erased guest' AND check_in = '2025-01-10'").fetchone()
    assert row and row["phone"] == "" and row["email"] is None and row["remarks"] is None and row["id_number"] == "" and row["amount"] == 1, "the money record stays, the person does not"
    assert not c.execute("SELECT 1 FROM guest_profiles WHERE phone10 = ?", (ph,)).fetchone() and c.execute("SELECT phone, name FROM callback_requests WHERE name = 'Erased guest'").fetchone()["phone"] == ""
    assert any(r[0] == "admin.guest_erase" for r in c.execute("SELECT action FROM audit_log")) and any(r[0] == "guest.erase" for r in c.execute("SELECT action FROM audit_log"))
    assert g.same_phone("", "") is False, "an erased booking cannot be looked up with an empty number"
    assert td.call("admin", "POST", "/api/admin/guest_erase", {"phone": ph, "reason": "again"})[0] == 404
    # a guest with a stay still to come is not erased
    b = td.online(start=50)
    phone = c.execute("SELECT phone FROM bookings WHERE coalesce(group_id, id) = ?", (b["booking_id"],)).fetchone()[0]
    assert td.call("admin", "POST", "/api/admin/guest_erase", {"phone": phone, "reason": "test"})[0] == 409
    # retention: off until set, then erases guests whose last stay ended long ago
    ph2, room2 = "9811100002", td.room_ids(1, start=9)[0]
    c.execute("INSERT INTO bookings (property_id, room_id, guest_name, phone, id_type, id_number, guests, check_in, check_out, status, created_at, status_at, source, payment_status, amount) "
              "VALUES (1, ?, 'Retained', ?, 'Aadhaar', '****2222', 1, '2025-02-01', '2025-02-02', 'Checked-Out', 'now', 'now', 'online', 'due', 1)", (room2["id"], ph2))
    conn = g.connect()
    assert g.purge_guests(conn) == 0
    td.settings_(guest_retention_days=365)
    assert g.purge_guests(conn) == 1 and c.execute("SELECT guest_name FROM bookings WHERE check_in = '2025-02-01'").fetchone()[0] == "Erased guest"
    assert g.purge_guests(conn) == 0
    conn.execute("DELETE FROM settings WHERE key = 'guest_retention_days'")
    conn.close()
    c.close()


def test_totp_codes():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238's test key
    assert totp.code_at(secret, 1) == "287082" and totp.code_at(secret, 37037036) == "081804"
    assert totp.verify(secret, "287082", now=59) == 1 and totp.verify(secret, "287082", now=59 + 30) == 1, "one step of clock drift is allowed"
    assert totp.verify(secret, "287082", now=59 + 90) is None and totp.verify(secret, "287082", after=1, now=59) is None, "late or used codes fail"
    assert totp.verify(secret, "28708", now=59) is None and totp.verify(secret, "abcdef", now=59) is None and totp.verify(secret, None) is None
    assert totp.uri(secret, "a b").startswith("otpauth://totp/Mantralayam%20Rooms:a%20b?secret=" + secret)


def login(user, **extra):
    return s.dispatch("POST", "/api/login", None, {"username": user, "password": td.PW} | extra, "127.0.0.1")


def test_two_factor_sign_in():
    g.reset_throttle()
    assert td.call("frontdesk", "POST", "/api/me/totp/setup", {"password": td.PW})[0] == 403, "front desk does not use it"
    assert td.call("manager", "POST", "/api/me/totp/setup", {"password": "wrong"})[0] == 403
    st, out = td.call("manager", "POST", "/api/me/totp/setup", {"password": td.PW})
    assert st == 200 and out["uri"].startswith("otpauth://") and len(out["secret"]) >= 32
    secret = out["secret"]
    raw = td.db().execute("SELECT totp_secret, totp_enabled FROM users WHERE username = 'manager'").fetchone()
    assert raw["totp_enabled"] == 0 and secret.encode() not in bytes(raw["totp_secret"]), "the secret is stored encrypted"
    assert login("manager")[1].get("need_totp") is None, "not on yet: a password is enough"
    now_step = int(time.time() // totp.STEP)
    assert td.call("manager", "POST", "/api/me/totp/enable", {"code": "000000"})[0] == 400
    assert td.call("manager", "POST", "/api/me/totp/enable", {"code": totp.code_at(secret, now_step)})[0] == 200
    assert td.call("manager", "POST", "/api/me/totp/setup", {"password": td.PW})[0] == 409, "no quiet re-enrolment over a working one"
    # now the password alone gives nothing
    st, out, tok = login("manager")
    assert st == 200 and out == {"need_totp": True} and tok is None
    g.reset_throttle()
    assert login("manager", totp="000000")[0] == 401 and login("manager", totp=totp.code_at(secret, now_step))[0] == 401, "wrong, and the code used to turn it on, are refused"
    code = totp.code_at(secret, now_step + 1)
    st, out, tok = login("manager", totp=code)
    assert st == 200 and tok and out["user"]["username"] == "manager" and out["totp"]["enabled"]
    assert login("manager", totp=code)[0] == 401, "a code works once"
    td.TOKENS["manager"] = tok
    # lost phone: a super admin resets it
    me = td.db().execute("SELECT id, username, full_name, role FROM users WHERE username = 'manager'").fetchone()
    u = dict(username=me["username"], full_name=me["full_name"], role=me["role"], password="", property_ids=[1, 2], id=me["id"], reset_totp=True)
    assert td.call("manager", "POST", "/api/admin/users", u)[0] == 403
    assert td.call("admin", "POST", "/api/admin/users", u)[0] == 200
    assert td.call("manager", "GET", "/api/meta")[0] == 401, "their sessions end"
    assert td.db().execute("SELECT totp_enabled FROM users WHERE username = 'manager'").fetchone()[0] == 0
    assert any(r[0] == "admin.totp_reset" for r in td.db().execute("SELECT action FROM audit_log"))
    td.TOKENS["manager"] = login("manager")[2]
    g.reset_throttle()


def test_two_factor_must_be_set_up_when_required():
    s.TOTP_REQUIRED = True
    try:
        g.reset_throttle()
        tok = login("manager")[2]
        td.TOKENS["m2"] = tok
        meta = td.call("m2", "GET", "/api/meta")[1]
        assert meta["totp"] == {"applies": True, "enabled": False, "required": True}
        st, out = td.call("m2", "GET", "/api/bookings")
        assert st == 403 and "two-factor" in out["error"].lower()
        assert td.call("m2", "POST", "/api/me/totp/setup", {"password": td.PW})[0] == 200
        assert td.call("m2", "POST", "/api/logout", {})[0] == 200
        fd = login("frontdesk")[2]
        td.TOKENS["fd3"] = fd
        assert td.call("fd3", "GET", "/api/bookings")[0] == 200, "front desk is not affected"
        assert td.call("admin", "GET", "/api/bookings")[0] == 403 or td.call("admin", "GET", "/api/meta")[1]["totp"]["required"], "super admins must set it up too"
    finally:
        s.TOTP_REQUIRED = False
        td.db().execute("UPDATE users SET totp_secret = NULL, totp_enabled = 0, totp_step = 0")
        g.reset_throttle()


def test_sign_in_limits_cover_spraying_and_spread_out_guessing():
    g.reset_throttle()
    codes = [s.dispatch("POST", "/api/login", None, {"username": f"nobody{i}", "password": "x"}, "4.4.4.4")[0] for i in range(22)]
    assert codes[:20] == [401] * 20 and codes[20] == 429, "one address cannot try more than 20 wrong sign-ins in 10 minutes, whatever the usernames"
    g.reset_throttle()
    codes = [s.dispatch("POST", "/api/login", None, {"username": "frontdesk", "password": "x"}, f"4.4.5.{i}")[0] for i in range(17)]
    assert codes[:15] == [401] * 15 and codes[15] == 429, "one account is locked after 15 wrong tries in an hour from any addresses"
    assert any(a["kind"] == "login" for a in g.alerts_open(td.db())), "and the staff are told"
    assert s.dispatch("POST", "/api/login", None, {"username": "frontdesk", "password": td.PW}, "4.4.6.1")[0] == 429
    g.reset_throttle()
    g.check_alerts(g.connect())
    assert not any(a["kind"] == "login" for a in g.alerts_open(td.db()))


def test_old_password_hashes_are_upgraded_and_sessions_expire():
    c = td.db()
    c.execute("UPDATE users SET pw_hash = ?, salt = 'abcd' WHERE username = 'viewer'", (s.derive(td.PW, b"abcd", s.LEGACY_ROUNDS),))
    g.reset_throttle()
    assert login("viewer")[0] == 200
    assert c.execute("SELECT salt FROM users WHERE username = 'viewer'").fetchone()[0].startswith(f"{s.ROUNDS}$") and s.ROUNDS >= 600_000
    assert login("viewer")[0] == 200, "and the new hash works"
    tok = login("viewer")[2]
    uid, idle, born = s.SESSIONS[tok]
    s.SESSIONS[tok] = (uid, idle, born - s.ABS_TTL - 1)
    td.TOKENS["v2"] = tok
    assert td.call("v2", "GET", "/api/meta")[0] == 401, "a session ends 12 hours after sign-in however busy it is"
    assert td.call("frontdesk", "POST", "/api/me/password", dict(current=td.PW, new="Short#123"))[0] == 400, "9 characters is too short"
    g.reset_throttle()


def test_scripts_carry_a_nonce_and_the_policy_has_no_unsafe_inline_script():
    for page in (GUEST, STAFF):
        assert "<script>" not in page and page.count("<script nonce=\"__NONCE__\">") >= 1
    data, nonce = g.with_nonce(b"<script nonce=\"__NONCE__\">x</script>", "text/html")
    assert nonce and nonce.encode() in data and b"__NONCE__" not in data and g.with_nonce(b"{}", "application/json") == (b"{}", None)
    script_src = [d for d in g.csp(nonce).split(";") if d.strip().startswith("script-src")][0]
    assert f"'nonce-{nonce}'" in script_src and "unsafe-inline" not in script_src
    srv, port = serve(g.Handler)
    try:
        r, body = http_get(port, "/")
        n = r.getheader("Content-Security-Policy").split("'nonce-")[1].split("'")[0]
        assert f'nonce="{n}"'.encode() in body, "the header and the page agree"
        r2, body2 = http_get(port, "/")
        assert f'nonce="{n}"'.encode() not in body2, "a new nonce for every response"
    finally:
        srv.shutdown()


def test_post_origin_rules():
    old = g.HTTPS_ONLY, g.ALLOWED_HOSTS
    try:
        g.HTTPS_ONLY, g.ALLOWED_HOSTS = False, {"rooms.example.org", "staff.example.org"}
        assert g.request_ok({"Host": "rooms.example.org"}, "POST", "/api/public/book") is None, "without HTTPS a missing Origin passes (curl, tests)"
        g.HTTPS_ONLY = True
        assert g.request_ok({"Host": "rooms.example.org"}, "POST", "/api/public/book")[0] == 403
        assert g.request_ok({"Host": "rooms.example.org"}, "POST", "/api/public/payment/webhook") is None, "the payment webhook is signed instead"
        assert g.request_ok({"Host": "rooms.example.org", "Origin": "https://rooms.example.org"}, "POST", "/api/public/book") is None
        assert g.request_ok({"Host": "rooms.example.org", "Origin": "https://staff.example.org"}, "POST", "/api/public/book")[0] == 403, "the other app's page may not call this one"
        assert g.request_ok({"Host": "evil.example"}, "GET", "/")[0] == 400
    finally:
        g.HTTPS_ONLY, g.ALLOWED_HOSTS = old


def test_staff_uploads_are_refused_before_the_body_is_read():
    srv, port = serve(s.Handler)
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        sock.sendall(b"POST /api/bookings/1/id_image HTTP/1.1\r\nHost: x\r\nContent-Type: image/png\r\nContent-Length: 2000000\r\n\r\n")
        assert b" 401 " in sock.recv(200).split(b"\r\n")[0], "no session: refused at once, the 2 MB is never waited for"
        sock.close()
        tok = td.TOKENS["frontdesk"]
        sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        sock.sendall(f"POST /api/bookings/1/id_image HTTP/1.1\r\nHost: x\r\nContent-Type: image/png\r\nCookie: staff_session={tok}\r\nContent-Length: 2000000\r\n\r\n".encode())
        assert b" 401 " in sock.recv(200).split(b"\r\n")[0], "a session without the CSRF token is refused too"
        sock.close()
    finally:
        srv.shutdown()


def test_access_log_has_no_query_string():
    import io
    h = g.Handler.__new__(g.Handler)
    h.client_address = ("1.2.3.4", 1)
    err, sys.stderr = sys.stderr, io.StringIO()
    try:
        g.Handler.log_message(h, '"%s" %s', "GET /api/guests/profile?phone=9876543210 HTTP/1.1", "200")
        out = sys.stderr.getvalue()
    finally:
        sys.stderr = err
    assert "9876543210" not in out and "/api/guests/profile" in out


def test_backups_are_private():
    old = g.BACKUPS
    g.BACKUPS = td.TMP / "bk"
    try:
        dest = g.backup()
        if os.name == "posix":
            assert oct(dest.stat().st_mode & 0o777) == "0o600" and oct(g.BACKUPS.stat().st_mode & 0o777) == "0o700"
        assert dest.exists() and dest.stat().st_size > 0
    finally:
        g.BACKUPS = old


def test_production_settings_are_checked():
    old = g.HTTPS_ONLY, g.ALLOWED_HOSTS, g.TRUST_PROXY
    env = {k: os.environ.get(k) for k in ("PAYMENT_PROVIDER", "ID_ENCRYPTION_KEY", "APP_SECRET", "SMS_PROVIDER")}
    try:
        g.HTTPS_ONLY, g.TRUST_PROXY, g.ALLOWED_HOSTS = False, False, set()
        os.environ["PAYMENT_PROVIDER"] = "mock"
        assert not g.serve_checks(), "mock payments are fine on a development machine"
        g.TRUST_PROXY = True
        assert any("mock" in x for x in g.serve_checks()), "but not behind a proxy, which means a public server"
        g.TRUST_PROXY = False
        g.ALLOWED_HOSTS = {"rooms.example.org"}
        assert any("mock" in x for x in g.serve_checks())
    finally:
        g.HTTPS_ONLY, g.ALLOWED_HOSTS, g.TRUST_PROXY = old
        for k, v in env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


if __name__ == "__main__":
    try:
        td.setup()
        g.UPLOADS = td.TMP / "uploads"
        for name, fn in list(globals().items()):
            if name.startswith("test_") and callable(fn):
                try:
                    fn()
                except BaseException:
                    print("FAILED:", name, file=sys.stderr)
                    raise
        print("ok")
    finally:
        shutil.rmtree(td.TMP, ignore_errors=True)
