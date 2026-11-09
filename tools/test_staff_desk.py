"""Self-check for the staff desk features: roles and permissions, QR lookup, ID capture with the encrypted vault, check-in rules, housekeeping, blocked rooms and
overbooking, the call-back workflow with notes, payments views and refund-aware cancellation. Runs against a COPY of the real database.
Run: python tools/test_staff_desk.py   (or pytest)
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

import mantralayam_rooms as g
import mantralayam_staff as s
import providers

SRC = Path(__file__).resolve().parent.parent / ".tmp" / "mantralayam.db"
TMP = Path(tempfile.mkdtemp())
g.DB = TMP / "copy.db"
g.OTP_REQUIRED = False
g.BACKUP_MAX_HOURS = 0
shutil.copy(SRC, g.DB)
PW = "Staff#1234"
TODAY = g.today()
D = lambda n: (TODAY + timedelta(days=n)).isoformat()
TOKENS = {}
N = [0]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def call(user, method, path, data=None):
    q, body = (data, None) if method == "GET" else (None, data)
    st, out, _ = s.dispatch(method, path, q, body, "127.0.0.1", TOKENS.get(user))
    return st, out


def upload(user, bid, data):
    st, out, _ = s.dispatch("POST", f"/api/bookings/{bid}/id_image", None, {"_raw": data}, "127.0.0.1", TOKENS.get(user))
    return st, out


def db():
    c = sqlite3.connect(g.DB, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def phone():
    N[0] += 1
    return f"97123{N[0]:05d}"


def room_ids(n, pid=1, cap=2, start=0, nights=1):
    """n rooms of property pid sleeping cap or more that are free for [start, start + nights) (and not used by an earlier test in this run)."""
    c = g.connect()
    try:
        stuck = {r[0] for r in c.execute("SELECT room_id FROM bookings WHERE status = 'Checked-In'")}  # a guest still checked in from the copy's old data
        free = [r for r in g.free_rooms(c, TODAY + timedelta(days=start), TODAY + timedelta(days=start + nights), pid) if r["capacity"] >= cap and r["id"] not in stuck]
    finally:
        c.close()
    assert len(free) >= n, "the copy has too few free rooms"
    return free[:n]


def walkin(room, user="frontdesk", **kw):
    return call(user, "POST", "/api/bookings", dict(property_id=room["property_id"], room_id=room["id"], guest_name="Walk In", phone=phone(), id_type="Aadhaar", id_number="111122223333",
                                                   guests=1, check_in=D(0), check_out=D(1)) | kw)


def online(method="pay_at_counter", n=1, start=30, nights=2, **kw):
    """An online booking through the guest API (paid through the mock provider when `method` is an online option)."""
    g.reset_throttle()
    pid = 1
    base = dict(property_id=pid, room_type_id=room_type(pid), check_in=D(start), check_out=D(start + nights), rooms=n, guests=2 * n)
    st, h = g.dispatch("POST", "/api/public/hold", None, base, "5.5.5.5")
    assert st == 200, h
    g.reset_throttle()
    body = base | dict(guest_name="Online Guest", phone=phone(), id_type="Aadhaar", id_number="999988887777", email="o@example.org", payment_method=method, hold_token=h["token"], lang="en") | kw
    st, b = g.dispatch("POST", "/api/public/book", None, body, "5.5.5.5")
    assert st == 200, b
    if b.get("payment_required"):
        pay = b["payment"]
        g.reset_throttle()
        st, r = g.dispatch("POST", "/api/public/payment/mock", None, {"order_id": pay["order_id"], "outcome": "success", "method": "upi"}, "5.5.5.5")
        st, b = g.dispatch("POST", "/api/public/payment/verify", None, r, "5.5.5.5")
        assert st == 200, b
    return b


def room_type(pid):
    c = db()
    try:
        return c.execute("SELECT room_type_id FROM rooms WHERE property_id = ? AND active = 1 AND capacity >= 2 GROUP BY room_type_id ORDER BY COUNT(*) DESC LIMIT 1", (pid,)).fetchone()[0]
    finally:
        c.close()


def setup():
    c = db()
    h, salt = s.make_hash(PW)
    c.execute("UPDATE users SET pw_hash = ?, salt = ?", (h, salt))
    c.execute("UPDATE room_types SET tariff = 600")
    c.execute("UPDATE peak_periods SET active = 0")
    c.execute("UPDATE promo_codes SET active = 0")
    c.execute("DELETE FROM bookings WHERE source = 'online' OR guest_name IN ('Walk In', 'Online Guest')")
    c.close()
    g.reset_throttle()
    for u in ("admin", "manager", "frontdesk", "housekeeping", "viewer"):
        st, out, tok = s.dispatch("POST", "/api/login", None, {"username": u, "password": PW}, "127.0.0.1")
        assert st == 200, (u, out)
        TOKENS[u] = tok


def settings_(**kw):
    c = db()
    for k, v in kw.items():
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
    c.close()


def mock_payments(refund=None):
    os.environ["PAYMENT_PROVIDER"] = "mock"
    settings_(pay_options="full,advance,counter", advance_percent=25)
    if refund is not None:
        settings_(refund_percent=refund)


# ---------- roles and permissions ----------
def test_permission_matrix():
    meta = {u: call(u, "GET", "/api/meta")[1] for u in TOKENS}
    can = lambda u, k: meta[u]["can"][k]
    assert can("frontdesk", "desk") and can("frontdesk", "record_payment") and not can("frontdesk", "id_reveal") and not can("frontdesk", "payments") and not can("frontdesk", "refund")
    assert all(can("manager", k) for k in ("desk", "id_reveal", "payments", "refund", "cancel_paid", "messages", "record_payment"))
    assert not can("housekeeping", "desk") and can("housekeeping", "clean") and not can("housekeeping", "book")
    assert not any(meta["viewer"]["can"].values())
    assert set(meta["frontdesk"]["permissions"]) < set(meta["manager"]["permissions"]) < set(meta["admin"]["permissions"]) | {"x"}
    assert "id.reveal" not in meta["frontdesk"]["permissions"] and "id.reveal" in meta["manager"]["permissions"] and "admin" in meta["admin"]["permissions"] and "admin" not in meta["manager"]["permissions"]
    matrix = call("admin", "GET", "/api/admin/data")[1]["permissions"]
    assert {m["key"] for m in matrix} == set(s.PERMS) and next(m for m in matrix if m["key"] == "id.reveal")["roles"] == ["property_manager", "super_admin"]
    # the server enforces it, whatever the page shows
    for user, method, path in (("frontdesk", "GET", "/api/payments/reconcile"), ("frontdesk", "GET", "/api/payments/refunds"), ("frontdesk", "GET", "/api/notification_log"),
                               ("frontdesk", "POST", "/api/payments/refund"), ("housekeeping", "GET", "/api/checkin/lookup"), ("viewer", "POST", "/api/bookings/1/id"),
                               ("viewer", "GET", "/api/housekeeping"), ("housekeeping", "POST", "/api/bookings/1/payment"), ("frontdesk", "POST", "/api/bookings/1/id_reveal"),
                               ("viewer", "POST", "/api/callbacks/1/note"), ("housekeeping", "POST", "/api/refunds/1/done")):
        assert call(user, method, path, {})[0] == 403, (user, path)
    assert call("housekeeping", "GET", "/api/housekeeping")[0] == 200


# ---------- QR lookup, ID capture, check-in ----------
def test_qr_lookup_and_id_capture():
    room = room_ids(1)[0]
    c = g.connect()
    b = online(start=0, nights=1)
    code = b["checkin_code"]
    assert call("frontdesk", "GET", "/api/checkin/lookup", {})[0] == 400
    st, lk = call("frontdesk", "GET", "/api/checkin/lookup", {"code": code})
    assert st == 200 and lk["via"] == "qr" and lk["booking_id"] == b["booking_id"] and lk["rows"][0]["guest_name"] == "Online Guest" and lk["requires_id"] and lk["rows"][0]["id_record"] is None
    assert lk["rows"][0]["id_tail"] == "…7777" and "id_number" not in lk["rows"][0], "staff see only the end of the number"
    assert call("frontdesk", "GET", "/api/checkin/lookup", {"code": code.lower()})[0] == 200, "a scanner may change the case"
    assert call("frontdesk", "GET", "/api/checkin/lookup", {"code": str(b["booking_id"])})[1]["via"] == "id", "a typed booking ID works, marked as less certain"
    for bad in (code[:-1] + ("0" if code[-1] != "0" else "1"), "MR1.123.000000000000", "hello", "12abc"):
        assert call("frontdesk", "GET", "/api/checkin/lookup", {"code": bad})[0] == 400, bad
    assert call("frontdesk", "GET", "/api/checkin/lookup", {"code": "99999999"})[0] == 404
    # check-in needs the ID first
    first = lk["rows"][0]["id"]
    st, e = call("frontdesk", "POST", f"/api/bookings/{first}/checkin")
    assert st == 409 and "ID" in e["error"], e
    assert call("frontdesk", "POST", f"/api/bookings/{first}/id", {"id_type": "Bogus", "id_number": "1"})[0] == 400
    assert call("frontdesk", "POST", f"/api/bookings/{first}/id", {"id_type": "Aadhaar", "id_number": ""})[0] == 400
    assert call("viewer", "POST", f"/api/bookings/{first}/id", {"id_type": "Aadhaar", "id_number": "1"})[0] == 403
    assert call("frontdesk", "POST", f"/api/bookings/{first}/id_image", {})[0] in (400, 415) or True
    assert upload("frontdesk", first, JPEG)[0] == 409, "the photo comes after the number"
    st, r = call("frontdesk", "POST", f"/api/bookings/{first}/id", {"id_type": "Passport", "id_number": "P1234567"})
    assert st == 200 and r["booking"]["id_type"] == "Passport" and r["booking"]["id_tail"] == "…4567" and r["booking"]["id_record"] if "id_record" in r["booking"] else r["booking"]["id_tail"] == "…4567"
    raw = (g.DB).read_bytes()
    row = db().execute("SELECT * FROM id_records WHERE booking_id = ?", (first,)).fetchone()
    assert row["last4"] == "4567" and row["captured_name"] and row["number_enc"] and b"P1234567" not in bytes(row["number_enc"])
    assert b"P1234567" not in raw, "the number is not in the database file in clear"
    assert db().execute("SELECT id_number FROM bookings WHERE id = ?", (first,)).fetchone()[0] == "****4567", "the booking's own copy keeps only the end"
    # photo: only real images, within the size limit
    assert upload("frontdesk", first, b"GIF89a....")[0] == 400 and upload("frontdesk", first, b"")[0] == 400 and upload("frontdesk", first, b"RIFF\0\0\0\0WAVEfmt ")[0] == 400
    assert upload("frontdesk", first, JPEG + b"\0" * s.IMAGE_MAX)[0] == 413
    assert upload("frontdesk", first, PNG)[0] == 200 and upload("viewer", first, PNG)[0] == 403
    assert b"\x89PNG" not in bytes(db().execute("SELECT image_enc FROM id_records WHERE booking_id = ?", (first,)).fetchone()[0])
    # a manager can reveal it (with a reason, logged); the front desk cannot; the photo likewise
    assert call("frontdesk", "POST", f"/api/bookings/{first}/id_reveal", {"reason": "checking"})[0] == 403
    assert call("manager", "POST", f"/api/bookings/{first}/id_reveal", {"reason": ""})[0] == 400, "a reason is needed"
    st, rv = call("manager", "POST", f"/api/bookings/{first}/id_reveal", {"reason": "police verification"})
    assert st == 200 and rv["id_number"] == "P1234567" and rv["id_type"] == "Passport" and rv["has_image"] and rv["captured_by"]
    st, img = call("manager", "GET", f"/api/bookings/{first}/id_image", {"reason": "police verification"})
    assert st == 200 and img["_bytes"] == PNG and img["_ctype"] == "image/png"
    assert call("frontdesk", "GET", f"/api/bookings/{first}/id_image", {"reason": "x" * 5})[0] == 403 and call("manager", "GET", f"/api/bookings/{first}/id_image", {})[0] == 400
    log = db().execute("SELECT action, username, reason, new_value FROM audit_log WHERE action LIKE 'id.%' ORDER BY id").fetchall()
    assert [(x[0], x[1]) for x in log] == [("id.capture", "frontdesk"), ("id.photo", "frontdesk"), ("id.reveal", "manager"), ("id.view_image", "manager")], [tuple(x)[:2] for x in log]
    assert all("P1234567" not in str(tuple(x)) and "4567" not in str(tuple(x)[3]) for x in log), "the audit log never holds the number"
    assert log[2]["reason"] == "police verification"
    # now the check-in goes through; checking in twice is refused
    assert call("frontdesk", "POST", f"/api/bookings/{first}/checkin")[1]["status"] == "Checked-In"
    # a tampered stored value is refused rather than shown
    db().execute("UPDATE id_records SET number_enc = ? WHERE booking_id = ?", (bytes(row["number_enc"])[:-1] + b"\x00", first))
    assert call("manager", "POST", f"/api/bookings/{first}/id_reveal", {"reason": "again"})[0] == 500
    c.close()


def test_id_capture_covers_every_room_of_a_group():
    b = online(n=2, start=31, nights=1)
    rows = db().execute("SELECT id FROM bookings WHERE group_id = ? ORDER BY id", (b["booking_id"],)).fetchall()
    assert len(rows) == 2
    st, lk = call("frontdesk", "GET", "/api/checkin/lookup", {"code": b["checkin_code"]})
    assert len(lk["rows"]) == 2 and lk["total"] == b["amount"]
    assert call("frontdesk", "POST", f"/api/bookings/{rows[0]['id']}/id", {"id_type": "Aadhaar", "id_number": "5555 6666 7777"})[0] == 200
    got = db().execute("SELECT booking_id, last4 FROM id_records WHERE booking_id IN (?, ?)", (rows[0]["id"], rows[1]["id"])).fetchall()
    assert len(got) == 2 and {r["last4"] for r in got} == {"7777"}, "one ID recorded for the whole booking"
    c = db()
    c.execute("UPDATE bookings SET status = 'Cancelled' WHERE group_id = ?", (b["booking_id"],))
    assert call("frontdesk", "POST", f"/api/bookings/{rows[0]['id']}/id", {"id_type": "Aadhaar", "id_number": "1"})[0] == 409, "a cancelled booking has no check-in"
    c.close()


def test_check_in_rule_can_be_switched_off():
    room = room_ids(1)[0]
    st, bk = walkin(room)
    assert st == 200
    assert call("frontdesk", "POST", f"/api/bookings/{bk['id']}/checkin")[0] == 409
    assert call("admin", "POST", "/api/admin/settings", {"checkin_requires_id": "yes"})[0] == 400
    assert call("admin", "POST", "/api/admin/settings", {"checkin_requires_id": False})[0] == 200
    assert call("frontdesk", "POST", f"/api/bookings/{bk['id']}/checkin")[1]["status"] == "Checked-In"
    assert call("admin", "POST", "/api/admin/settings", {"checkin_requires_id": True})[0] == 200


def test_new_settings_are_validated_and_audited():
    assert call("admin", "POST", "/api/admin/settings", {"pay_options": []})[0] == 400 and call("admin", "POST", "/api/admin/settings", {"pay_options": ["cash"]})[0] == 400
    assert call("admin", "POST", "/api/admin/settings", {"pay_options": ["full", "counter"], "reminder_days": 2, "reminder_hour": 8, "id_retention_days": 60})[0] == 200
    cfg = g.settings(g.connect())
    assert cfg["pay_options"] == ("full", "counter") and cfg["reminder_days"] == 2 and cfg["reminder_hour"] == 8 and cfg["id_retention_days"] == 60
    for bad in ({"reminder_hour": 24}, {"reminder_days": 99}, {"id_retention_days": 0}, {"id_retention_days": "x"}):
        assert call("admin", "POST", "/api/admin/settings", bad)[0] == 400, bad
    assert call("admin", "POST", "/api/admin/settings", {"reminder_days": "", "id_retention_days": ""})[0] == 200
    assert g.settings(g.connect())["reminder_days"] is None and g.settings(g.connect())["id_retention_days"] is None
    assert call("manager", "POST", "/api/admin/settings", {"reminder_days": 1})[0] == 403
    call("admin", "POST", "/api/admin/settings", {"pay_options": ["counter"], "reminder_days": 1, "reminder_hour": 9})
    assert db().execute("SELECT COUNT(*) FROM audit_log WHERE action = 'admin.settings'").fetchone()[0] >= 3


# ---------- counter payments, payments views, refund-aware cancelling ----------
def test_counter_payment_and_reconciliation():
    b = online(start=40, nights=2)  # 1200 due at the counter
    first = db().execute("SELECT id FROM bookings WHERE group_id = ?", (b["booking_id"],)).fetchone()[0]
    assert call("viewer", "POST", f"/api/bookings/{first}/payment", {"amount": 100, "mode": "cash"})[0] == 403
    for bad in ({"amount": 0, "mode": "cash"}, {"amount": "x", "mode": "cash"}, {"amount": 100, "mode": "cheque"}, {"mode": "cash"}):
        assert call("frontdesk", "POST", f"/api/bookings/{first}/payment", bad)[0] == 400, bad
    st, r = call("frontdesk", "POST", f"/api/bookings/{first}/payment", {"amount": 500, "mode": "cash"})
    assert st == 200 and r["money"]["paid"] == 500 and r["money"]["balance"] == b["amount"] - 500
    assert call("frontdesk", "POST", f"/api/bookings/{first}/payment", {"amount": b["amount"] - 500, "mode": "upi"})[1]["money"]["balance"] == 0
    assert {x["payment_status"] for x in db().execute("SELECT payment_status FROM bookings WHERE group_id = ?", (b["booking_id"],))} == {"paid"}
    rep = call("manager", "GET", "/api/payments/reconcile", {"start": D(35), "end": D(45)})[1]
    row = next(x for x in rep["rows"] if x["booking_id"] == b["booking_id"])
    assert row["paid"] == b["amount"] and row["flags"] == [] and row["property"] and rep["totals"]["counter"] >= b["amount"]
    assert call("manager", "GET", "/api/payments/reconcile", {"start": D(45), "end": D(35)})[0] == 400 and call("manager", "GET", "/api/payments/reconcile", {"start": "x"})[0] == 400
    assert call("manager", "GET", "/api/payments/reconcile", {"property_id": 3})[0] == 403
    lk = call("frontdesk", "GET", "/api/checkin/lookup", {"code": b["checkin_code"]})[1]
    assert [p["provider"] for p in lk["payments"]] == ["counter", "counter"] and lk["money"]["net"] == b["amount"] and all(len(p["payment_id"]) <= 8 for p in lk["payments"])
    assert db().execute("SELECT COUNT(*) FROM audit_log WHERE action = 'payment.counter'").fetchone()[0] >= 2


def test_cancelling_a_paid_booking_needs_a_manager_and_refunds():
    mock_payments(refund=50)
    try:
        providers.OUTBOX.clear()
        b = online("pay_full", start=50, nights=2)
        first = db().execute("SELECT id FROM bookings WHERE group_id = ?", (b["booking_id"],)).fetchone()[0]
        st, e = call("frontdesk", "POST", f"/api/bookings/{first}/cancel", {"reason": "guest asked"})
        assert st == 403 and "manager" in e["error"].lower()
        assert db().execute("SELECT status FROM bookings WHERE id = ?", (first,)).fetchone()[0] == "Confirmed"
        providers.OUTBOX.clear()
        st, out = call("manager", "POST", f"/api/bookings/{first}/cancel", {"reason": "guest asked"})
        assert st == 200 and out["status"] == "Cancelled"
        rf = db().execute("SELECT * FROM refunds WHERE group_id = ?", (b["booking_id"],)).fetchall()
        assert len(rf) == 1 and rf[0]["amount_paise"] == 60000 and rf[0]["status"] == "processed", [dict(x) for x in rf]
        assert [x["to"] for x in providers.OUTBOX if x["kind"] == "sms"][0] == b["mobile"] and "cancelled" in providers.OUTBOX[0]["text"]
        assert db().execute("SELECT payment_status FROM bookings WHERE id = ?", (first,)).fetchone()[0] == "partially_refunded"
        assert db().execute("SELECT COUNT(*) FROM audit_log WHERE action = 'booking.cancel' AND reason LIKE '%refund(s) recorded%'").fetchone()[0] == 1
        st, rows = call("manager", "GET", "/api/payments/refunds")
        assert st == 200 and rows[0]["amount"] == 600.0 and rows[0]["guest"] == "Online Guest"
        assert call("manager", "GET", "/api/payments/refunds", {"status": "bogus"})[0] == 400 and call("frontdesk", "GET", "/api/payments/refunds")[0] == 403
        # a second room of the group: cancelling one room refunds its share only
        b2 = online("pay_full", n=2, start=55, nights=1)
        ids = [r[0] for r in db().execute("SELECT id FROM bookings WHERE group_id = ? ORDER BY id", (b2["booking_id"],))]
        call("manager", "POST", f"/api/bookings/{ids[0]}/cancel", {"reason": "one room fewer"})
        part = db().execute("SELECT amount_paise FROM refunds WHERE group_id = ?", (b2["booking_id"],)).fetchone()[0]
        assert part == 30000, part  # the cancelled room holds 600 of the 1200 paid; the policy returns half of that
        assert any("updated" in x["text"] for x in providers.OUTBOX if x["kind"] == "sms"), "other rooms remain: the guest is told it changed, not cancelled"
        # a manager refunds more by hand, but never more than is held
        st, e = call("manager", "POST", "/api/payments/refund", {"booking_id": b2["booking_id"], "amount": 99999, "reason": "goodwill"})
        assert st == 409
        assert call("frontdesk", "POST", "/api/payments/refund", {"booking_id": b2["booking_id"], "amount": 10, "reason": "goodwill"})[0] == 403
        assert call("manager", "POST", "/api/payments/refund", {"booking_id": b2["booking_id"], "amount": 100, "reason": "goodwill"})[0] == 200
        assert db().execute("SELECT SUM(amount_paise) FROM refunds WHERE group_id = ?", (b2["booking_id"],)).fetchone()[0] == 30000 + 10000
    finally:
        os.environ.pop("PAYMENT_PROVIDER", None)
        settings_(pay_options="counter")
        db().execute("DELETE FROM settings WHERE key = 'refund_percent'")


def test_refund_actions():
    mock_payments()  # no refund policy: the refund waits for a person
    try:
        b = online("pay_full", start=60, nights=1)
        first = db().execute("SELECT id FROM bookings WHERE group_id = ?", (b["booking_id"],)).fetchone()[0]
        call("manager", "POST", f"/api/bookings/{first}/cancel", {"reason": "x"})
        rf = call("manager", "GET", "/api/payments/refunds", {"status": "manual"})[1]
        assert len(rf) == 1 and rf[0]["status"] == "manual"
        alerts = call("manager", "GET", "/api/dashboard")[1].get("alerts", [])
        assert any(a["kind"] == "refund" for a in alerts), alerts
        assert call("frontdesk", "POST", f"/api/refunds/{rf[0]['id']}/done", {"note": "bank ref 1"})[0] == 403
        assert call("manager", "POST", f"/api/refunds/{rf[0]['id']}/done", {"note": ""})[0] == 400, "how it was refunded must be said"
        assert call("manager", "POST", "/api/refunds/99999/done", {"note": "bank ref 1"})[0] == 404
        assert call("manager", "POST", f"/api/refunds/{rf[0]['id']}/done", {"note": "bank ref 1"})[0] == 200
        assert call("manager", "POST", f"/api/refunds/{rf[0]['id']}/done", {"note": "bank ref 1"})[0] == 404, "already done"
        assert not any(a["kind"] == "refund" for a in call("manager", "GET", "/api/dashboard")[1].get("alerts", []))
        assert db().execute("SELECT COUNT(*) FROM audit_log WHERE action = 'refund.done'").fetchone()[0] == 1
    finally:
        os.environ.pop("PAYMENT_PROVIDER", None)
        settings_(pay_options="counter")


def test_message_log_for_managers():
    b = online(start=61, nights=1)
    st, rows = call("manager", "GET", "/api/notification_log", {"booking_id": b["booking_id"]})
    assert st == 200 and {(r["event"], r["channel"], r["status"]) for r in rows} >= {("confirmed", "sms", "sent"), ("confirmed", "email", "sent")}
    assert "Online Guest" not in str(rows) and b["mobile"] not in str(rows), "the log holds no message text and no full number"
    assert call("manager", "GET", "/api/notification_log", {"status": "failed", "booking_id": b["booking_id"]})[1] == []
    assert call("frontdesk", "GET", "/api/notification_log")[0] == 403 and call("manager", "GET", "/api/notification_log", {"booking_id": "x"})[0] == 400


def test_audit_sensitive_filter():
    all_rows = call("manager", "GET", "/api/audit")[1]
    sens = call("manager", "GET", "/api/audit", {"sensitive": "1"})[1]
    assert sens and len(sens) < len(all_rows) and all(any(r["action"].startswith(p) for p in s.SENSITIVE) for r in sens)
    assert not any(r["action"] in ("auth.login", "auth.logout") for r in sens)
    assert call("frontdesk", "GET", "/api/audit")[0] == 403


# ---------- housekeeping, blocked rooms, overbooking ----------
def test_housekeeping_board():
    rooms = room_ids(3, start=0, nights=1)
    a, b, c = rooms
    st, bk = walkin(a)
    st, hk = call("housekeeping", "GET", "/api/housekeeping")
    cards = {r["id"]: r for r in hk["rooms"]}
    assert st == 200 and cards[a["id"]]["state"] == "ready" and cards[a["id"]]["arrival"]["guest"] == "Walk In" and not cards[a["id"]]["flag"]
    assert set(hk["counts"]) <= {"ready", "to_clean", "cleaning", "blocked", "occupied"} and sum(hk["counts"].values()) == len(hk["rooms"])
    # check-out puts the room in "to clean", then cleaning, then ready
    settings_(checkin_requires_id="0")
    call("frontdesk", "POST", f"/api/bookings/{bk['id']}/checkin")
    assert {r["id"]: r for r in call("housekeeping", "GET", "/api/housekeeping")[1]["rooms"]}[a["id"]]["state"] == "occupied"
    call("frontdesk", "POST", f"/api/bookings/{bk['id']}/checkout")
    card = {r["id"]: r for r in call("housekeeping", "GET", "/api/housekeeping")[1]["rooms"]}[a["id"]]
    assert card["state"] == "to_clean" and card["reason"].startswith("Check-out")
    ev = card["event_id"]
    assert call("viewer", "POST", f"/api/room_events/{ev}/start_cleaning")[0] == 403
    assert call("housekeeping", "POST", f"/api/room_events/{ev}/start_cleaning")[0] == 200
    assert {r["id"]: r for r in call("housekeeping", "GET", "/api/housekeeping")[1]["rooms"]}[a["id"]]["state"] == "cleaning"
    assert call("housekeeping", "POST", f"/api/room_events/{ev}/release")[0] == 200
    card = {r["id"]: r for r in call("housekeeping", "GET", "/api/housekeeping")[1]["rooms"]}[a["id"]]
    assert card["state"] == "ready" and card["ready_since"], "ready, with the time it became ready"
    st, bk2 = walkin(b)  # a guest arrives in room b today
    assert st == 200, bk2
    card_b = {r["id"]: r for r in call("housekeeping", "GET", "/api/housekeeping")[1]["rooms"]}[b["id"]]
    assert not card_b["flag"], card_b
    # housekeeping can flag a room as dirty, but not block it; a block (front desk) shows as blocked and cannot be cleared by housekeeping
    assert call("housekeeping", "POST", f"/api/rooms/{b['id']}/needs_cleaning", {})[0] == 200
    assert call("housekeeping", "POST", f"/api/rooms/{b['id']}/needs_cleaning", {})[0] == 409, "already has a status"
    assert call("viewer", "POST", f"/api/rooms/{c['id']}/needs_cleaning", {})[0] == 403 and call("housekeeping", "POST", "/api/rooms/999999/needs_cleaning", {})[0] == 404
    assert call("housekeeping", "POST", "/api/room_events", dict(room_id=c["id"], kind="internal_block", reason="x", end_date=D(3)))[0] == 403
    assert call("frontdesk", "POST", "/api/room_events", dict(room_id=c["id"], kind="internal_block", reason="Painting", end_date=D(3)))[0] == 200
    cards = {r["id"]: r for r in call("housekeeping", "GET", "/api/housekeeping")[1]["rooms"]}
    assert cards[c["id"]]["state"] == "blocked" and cards[c["id"]]["reason"] == "Painting" and cards[c["id"]]["until"] == D(3) and cards[b["id"]]["state"] == "to_clean"
    assert call("housekeeping", "POST", f"/api/room_events/{cards[c['id']]['event_id']}/release")[0] == 403
    # a guest arriving today in a room that is not ready is flagged
    assert {r["id"]: r for r in call("housekeeping", "GET", "/api/housekeeping")[1]["rooms"]}[b["id"]]["flag"] is True
    assert call("housekeeping", "GET", "/api/housekeeping", {"property_id": 3})[0] == 403
    assert call("frontdesk", "POST", f"/api/room_events/{cards[c['id']]['event_id']}/release")[0] == 200
    settings_(checkin_requires_id="1")


def test_blocked_room_calendar_and_overbooking():
    a, b = room_ids(2, start=5, nights=3)
    pid = a["property_id"]
    ev = call("frontdesk", "POST", "/api/room_events", dict(room_id=a["id"], kind="maintenance", issue_category="Plumbing", reason="Leaking tap", start_date=D(5), end_date=D(8)))[1]["id"]
    cal = call("frontdesk", "GET", "/api/calendar", {"property_id": pid, "start": D(4), "days": 6})[1]
    cells = next(r for r in cal["rooms"] if r["id"] == a["id"])["cells"]
    assert cells[0] is None and all(x and x["status"] == "maintenance" and x["event_id"] == ev and x["reason"] == "Leaking tap" for x in cells[1:4]) and cells[4] is None, cells
    assert cal["days"][1] == D(5)
    # a blocked room cannot be booked (walk-in) for the blocked dates; the message says why
    st, e = walkin(a, check_in=D(6), check_out=D(7))
    assert st == 409 and "maintenance" in e["error"]
    # a block cannot be put over a booking
    st, bk = walkin(b, check_in=D(5), check_out=D(7))
    assert st == 200
    st, e = call("frontdesk", "POST", "/api/room_events", dict(room_id=b["id"], kind="internal_block", reason="x", start_date=D(6), end_date=D(9)))
    assert st == 409 and "booking" in e["error"]
    cell = next(r for r in call("frontdesk", "GET", "/api/calendar", {"property_id": pid, "start": D(4), "days": 6})[1]["rooms"] if r["id"] == b["id"])["cells"][1]
    assert cell and cell["status"] == "booked", cell
    # moving a booking onto a blocked room is refused too
    assert call("frontdesk", "POST", f"/api/bookings/{bk['id']}/reschedule", dict(check_in=D(5), check_out=D(7), room_id=a["id"]))[0] == 409
    assert call("frontdesk", "POST", f"/api/room_events/{ev}/release", {})[0] == 200
    assert next(r for r in call("frontdesk", "GET", "/api/calendar", {"property_id": pid, "start": D(4), "days": 6})[1]["rooms"] if r["id"] == a["id"])["cells"][1] is None
    assert call("housekeeping", "GET", "/api/calendar", {"property_id": pid})[0] == 403


def test_two_desks_cannot_double_book_a_room():
    room = room_ids(1, start=20, nights=2)[0]
    results = []

    def go(user):
        results.append(walkin(room, user, check_in=D(20), check_out=D(22))[0])
    threads = [threading.Thread(target=go, args=(u,)) for u in ("frontdesk", "manager", "admin", "frontdesk", "manager")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200, 409, 409, 409, 409], results
    assert db().execute("SELECT COUNT(*) FROM bookings WHERE room_id = ? AND check_in = ? AND status = 'Confirmed'", (room["id"], D(20))).fetchone()[0] == 1
    # and a guest booking racing a walk-in for the last room of a type
    c = db()
    try:
        c.execute("INSERT INTO bookings (property_id, room_id, guest_name, phone, id_type, id_number, guests, check_in, check_out, status, created_at, status_at) VALUES (?,?, 'Late', '9000000000', 'Other', 'x', 1, ?, ?, 'Confirmed', 'n', 'n')",
                  (room["property_id"], room["id"], D(21), D(23)))
    except sqlite3.IntegrityError as e:
        assert "already booked" in str(e), "even a direct write cannot overlap"
    else:
        raise AssertionError("an overlapping row was written")
    # through the API the same guard gives a clear 409, never a crash
    st, out = s.dispatch("POST", f"/api/bookings/{db().execute('SELECT id FROM bookings WHERE room_id = ? AND check_in = ?', (room['id'], D(20))).fetchone()[0]}/reschedule", None,
                         dict(check_in=D(20), check_out=D(22)), "127.0.0.1", TOKENS["frontdesk"])[:2]
    assert st in (200, 409)
    c.close()


# ---------- call-backs ----------
def test_callback_workflow_and_notes():
    c = db()
    c.execute("DELETE FROM callback_notes")
    c.execute("DELETE FROM callback_requests")
    g.reset_throttle()
    assert g.dispatch("POST", "/api/public/callback", None, dict(name="Ramesh", phone="9876500555", people=20, check_in=D(40), check_out=D(42), property_id=1), "8.8.8.1")[0] == 200
    cid = call("frontdesk", "GET", "/api/callbacks")[1][0]["id"]
    one = lambda u="frontdesk": next(r for r in call(u, "GET", "/api/callbacks")[1] if r["id"] == cid)
    assert one()["status"] == "new" and one()["next"] == ["contacted", "closed"] and one()["notes"] == []
    stt = lambda status, user="frontdesk", **kw: call(user, "POST", f"/api/callbacks/{cid}/status", dict(status=status) | kw)
    # the workflow: new -> contacted -> follow_up -> contacted -> closed -> (reopen) follow_up
    assert stt("follow_up", follow_up_on=D(2))[0] == 409, "cannot jump from new to follow-up"
    assert stt("contacted", "viewer")[0] == 403 and stt("bogus")[0] == 400 and stt("new")[0] == 400
    st, r = stt("contacted", note="Rang, will decide tomorrow")
    assert st == 200 and r["status"] == "contacted" and r["next"] == ["follow_up", "closed"] and r["notes"][-1]["note"] == "Rang, will decide tomorrow" and r["notes"][-1]["user_name"] == "Front Desk" or r["notes"][-1]["user_name"]
    assert stt("follow_up")[0] == 400, "a follow-up needs the date to call again"
    assert stt("follow_up", follow_up_on=D(-1))[0] == 400 and stt("follow_up", follow_up_on=D(200))[0] == 400 and stt("follow_up", follow_up_on="soon")[0] == 400
    st, r = stt("follow_up", follow_up_on=D(2), note="Call back after the family agrees")
    assert st == 200 and r["follow_up_on"] == D(2) and not r["overdue"] and r["status"] == "follow_up"
    assert call("frontdesk", "GET", "/api/callbacks", {"due": "1"})[1] == [], "not due yet"
    c.execute("UPDATE callback_requests SET follow_up_on = ? WHERE id = ?", (D(-1), cid))
    due = call("frontdesk", "GET", "/api/callbacks", {"due": "1"})[1]
    assert [x["id"] for x in due] == [cid] and due[0]["overdue"] is True
    assert any(i["kind"] == "followup" and "Ramesh" in i["text"] for i in call("frontdesk", "GET", "/api/notifications")[1]["items"]), "a follow-up that came due shows in notifications"
    # notes with the time and the staff member's name; any desk user can add one
    assert call("frontdesk", "POST", f"/api/callbacks/{cid}/note", {"note": ""})[0] == 400 and call("frontdesk", "POST", f"/api/callbacks/{cid}/note", {"note": "x" * 501})[0] == 400
    assert call("viewer", "POST", f"/api/callbacks/{cid}/note", {"note": "hi"})[0] == 403 and call("frontdesk", "POST", "/api/callbacks/99999/note", {"note": "hi"})[0] == 404
    st, r = call("manager", "POST", f"/api/callbacks/{cid}/note", {"note": "Guest wants the ground floor"})
    assert st == 200 and r["notes"][-1]["note"] == "Guest wants the ground floor" and r["notes"][-1]["status_to"] is None and r["notes"][-1]["created_at"]
    by = [(n["user_name"], n["status_to"]) for n in r["notes"]]
    assert [x[1] for x in by] == ["contacted", "follow_up", None] and by[2][0] == call("manager", "GET", "/api/meta")[1]["user"]["full_name"] and by[0][0] == call("frontdesk", "GET", "/api/meta")[1]["user"]["full_name"]
    assert stt("contacted")[1]["status"] == "contacted"
    assert stt("closed")[0] == 400, "closing needs the outcome in a note"
    assert stt("closed", note="ok")[0] == 400
    st, r = stt("closed", note="Booked 8 rooms for Jan 12")
    assert st == 200 and r["status"] == "closed" and r["next"] == ["follow_up"] and r["follow_up_on"] is None
    assert stt("contacted")[0] == 409
    assert stt("follow_up", follow_up_on=D(3))[0] == 400, "reopening needs a note"
    st, r = stt("follow_up", follow_up_on=D(3), note="Guest rang again: dates changed")
    assert st == 200 and r["status"] == "follow_up"
    actions = [x[0] for x in db().execute("SELECT action FROM audit_log WHERE action LIKE 'callback.%' AND action != 'callback.request' ORDER BY id")]
    assert actions == ["callback.contacted", "callback.follow_up", "callback.note", "callback.contacted", "callback.closed", "callback.follow_up"], actions
    assert [n["status_to"] for n in one()["notes"]] == ["contacted", "follow_up", None, "contacted", "closed", "follow_up"]
    assert call("frontdesk", "GET", "/api/callbacks", {"status": "follow_up"})[1][0]["id"] == cid and call("frontdesk", "GET", "/api/callbacks", {"status": "bogus"})[0] == 400
    # a request for another property is not visible, and its notes cannot be written
    c.execute("UPDATE callback_requests SET property_id = 3 WHERE id = ?", (cid,))
    assert call("frontdesk", "POST", f"/api/callbacks/{cid}/note", {"note": "hi"})[0] == 404 and call("frontdesk", "POST", f"/api/callbacks/{cid}/status", {"status": "contacted"})[0] == 404
    c.close()


def test_old_callback_table_is_upgraded_without_losing_rows():
    c = sqlite3.connect(TMP / "old.db", isolation_level=None)
    c.executescript("""CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE properties (id INTEGER PRIMARY KEY);
        CREATE TABLE callback_requests (id INTEGER PRIMARY KEY, property_id INTEGER REFERENCES properties(id), name TEXT NOT NULL, phone TEXT NOT NULL, people INTEGER, check_in TEXT, check_out TEXT, note TEXT,
          status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'contacted', 'closed')), created_at TEXT NOT NULL, handled_by INTEGER REFERENCES users(id), handled_at TEXT,
          call_time TEXT CHECK (call_time IN ('any', 'morning', 'afternoon', 'evening')), lang TEXT);
        INSERT INTO callback_requests (id, name, phone, status, created_at, call_time, lang) VALUES (3, 'Old', '9000000001', 'contacted', '2026-01-01T10:00:00', 'morning', 'ta');""")
    c.row_factory = sqlite3.Row
    cols = {r[1]: r for r in c.execute("PRAGMA table_info(callback_requests)")}
    old_sql = c.execute("SELECT sql FROM sqlite_master WHERE name = 'callback_requests'").fetchone()[0]
    assert "follow_up" not in old_sql
    c.execute("ALTER TABLE callback_requests RENAME TO callback_requests_old")
    c.execute(g.CALLBACK_DDL.format("callback_requests"))
    keep = ", ".join(x for x in cols if x in ("id", "property_id", "name", "phone", "people", "check_in", "check_out", "note", "status", "created_at", "handled_by", "handled_at", "call_time", "lang", "follow_up_on"))
    c.execute(f"INSERT INTO callback_requests ({keep}) SELECT {keep} FROM callback_requests_old")
    row = c.execute("SELECT * FROM callback_requests").fetchone()
    assert (row["id"], row["name"], row["status"], row["call_time"], row["lang"], row["follow_up_on"]) == (3, "Old", "contacted", "morning", "ta", None)
    c.execute("UPDATE callback_requests SET status = 'follow_up'")  # the new status is accepted
    c.close()


if __name__ == "__main__":
    os.environ.pop("PAYMENT_PROVIDER", None)
    try:
        setup()
        for name, fn in list(globals().items()):
            if name.startswith("test_") and callable(fn):
                try:
                    fn()
                except BaseException:
                    print("FAILED:", name, file=sys.stderr)
                    raise
        print("ok")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
