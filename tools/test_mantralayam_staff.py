"""Self-check for the staff API. Runs against a COPY of the real database, with a known test password.
Run: python tools/test_mantralayam_staff.py   (or pytest)
"""
import hashlib
import shutil
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path

import mantralayam_rooms as g
import mantralayam_staff as s

SRC = Path(__file__).resolve().parent.parent / ".tmp" / "mantralayam.db"
TMP = Path(tempfile.mkdtemp())
g.DB = TMP / "copy.db"
shutil.copy(SRC, g.DB)
PW = "Staff#1234"
TODAY = g.today()
D = lambda n: (TODAY + timedelta(days=n)).isoformat()
TOKENS = {}


def call(user, method, path, data=None):
    q, body = (data, None) if method == "GET" else (None, data)
    st, out, _ = s.dispatch(method, path, q, body, "127.0.0.1", TOKENS.get(user))
    return st, out


def login(user, pw=PW):
    st, out, tok = s.dispatch("POST", "/api/login", None, {"username": user, "password": pw}, "127.0.0.1")
    if st == 200:
        TOKENS[user] = tok
    return st, out


def guest(room, **kw):
    return dict(property_id=room["property_id"], room_id=room["id"], guest_name="Walk In", phone="9876543210", id_type="Aadhaar", id_number="123412341234",
                guests=1, check_in=D(0), check_out=D(1)) | kw


def free_room(user, pid=1, skip=()):
    """A room of property pid, sleeping 2+, that is free for the whole test window (today to D(12))."""
    conn = g.connect()
    try:
        free = {r["id"] for r in g.free_rooms(conn, TODAY, TODAY + timedelta(days=12), pid)}
    finally:
        conn.close()
    for r in call(user, "GET", "/api/rooms", dict(property_id=pid))[1]:
        if r["id"] in free and r["id"] not in skip and r["capacity"] >= 2:
            return r


def test_all():
    db = sqlite3.connect(g.DB)
    h, salt = s.make_hash(PW)
    db.execute("UPDATE users SET pw_hash = ?, salt = ?", (h, salt)); db.commit()
    before = hashlib.md5(SRC.read_bytes()).hexdigest()

    # login, sessions, throttling
    assert call("admin", "GET", "/api/meta")[0] == 401
    assert login("frontdesk", "wrong")[0] == 401 and login("nobody")[0] == 401
    for _ in range(5): login("frontdesk", "wrong")
    assert login("frontdesk")[0] == 429, "5 failures must lock this username"
    s.FAILS.clear()
    for u in ("admin", "manager", "frontdesk", "housekeeping", "viewer"):
        st, m = login(u)
        assert st == 200 and m["user"]["username"] == u, (u, m)
    assert call("viewer", "GET", "/api/meta")[1]["can"] == dict(book=False, status=False, clean=False, audit=False)

    # property scoping: front desk has properties 1,2 only
    assert call("frontdesk", "GET", "/api/dashboard", dict(property_id=3))[0] == 403
    assert {b["property_id"] for b in call("frontdesk", "GET", "/api/bookings")[1]} <= {1, 2}
    assert {p["id"] for p in call("admin", "GET", "/api/meta")[1]["properties"]} == {1, 2, 3, 4, 5}

    # walk-in booking rules and roles
    r1 = free_room("frontdesk")
    assert call("viewer", "POST", "/api/bookings", guest(r1))[0] == 403
    assert call("housekeeping", "POST", "/api/bookings", guest(r1))[0] == 403
    for patch, msg in [(dict(check_in=D(-1)), "past"), (dict(phone="12"), "10 to 13"), (dict(guests=99), "sleeps at most"), (dict(id_type="Library"), "ID proof")]:
        st, out = call("frontdesk", "POST", "/api/bookings", guest(r1) | patch)
        assert st == 400 and msg in out["error"], (patch, out)
    st, b = call("frontdesk", "POST", "/api/bookings", guest(r1))
    assert st == 200 and b["status"] == "Confirmed" and b["source"] == "front_desk" and b["id_tail"] == "…1234", b
    assert "id_number" not in b
    assert call("frontdesk", "POST", "/api/bookings", guest(r1))[0] == 409, "double booking"
    assert call("frontdesk", "GET", "/api/dashboard")[1]["arrivals"], "arrival shows on dashboard"

    # lifecycle: check-in, no second check-in, check-out creates a cleaning task that blocks same-day rebooking
    assert call("viewer", "POST", f"/api/bookings/{b['id']}/checkin")[0] == 403
    assert call("frontdesk", "POST", f"/api/bookings/{b['id']}/checkin")[1]["status"] == "Checked-In"
    assert call("frontdesk", "POST", f"/api/bookings/{b['id']}/checkin")[0] == 409
    assert next(r for r in call("frontdesk", "GET", "/api/rooms", dict(property_id=1))[1] if r["id"] == r1["id"])["status"] == "occupied"
    st, c = call("frontdesk", "POST", f"/api/bookings/{b['id']}/cancel", dict(reason="x"))
    assert st == 409 and "Check-out" in c["error"], c
    assert call("frontdesk", "POST", f"/api/bookings/{b['id']}/checkout")[1]["status"] == "Checked-Out"
    ev = [e for e in call("housekeeping", "GET", "/api/room_events")[1] if e["room_id"] == r1["id"]]
    assert [e["kind"] for e in ev] == ["cleaning_required"], ev
    assert call("frontdesk", "POST", "/api/bookings", guest(r1))[0] == 409, "room awaiting cleaning"
    assert call("frontdesk", "POST", f"/api/room_events/{ev[0]['id']}/start_cleaning")[0] == 200
    assert call("housekeeping", "POST", f"/api/room_events/{ev[0]['id']}/start_cleaning")[0] == 409
    assert call("viewer", "POST", f"/api/room_events/{ev[0]['id']}/release")[0] == 403
    assert call("housekeeping", "POST", f"/api/room_events/{ev[0]['id']}/release")[0] == 200
    assert next(r for r in call("frontdesk", "GET", "/api/rooms", dict(property_id=1))[1] if r["id"] == r1["id"])["status"] == "available"

    # check-in needs the date to have arrived, no-show needs it too, cancel needs a reason
    r2 = free_room("frontdesk", skip=(r1["id"],))
    st, fut = call("frontdesk", "POST", "/api/bookings", guest(r2, check_in=D(3), check_out=D(4)))
    assert st == 200
    assert call("frontdesk", "POST", f"/api/bookings/{fut['id']}/checkin")[0] == 409
    assert call("frontdesk", "POST", f"/api/bookings/{fut['id']}/noshow")[0] == 409
    assert call("frontdesk", "POST", f"/api/bookings/{fut['id']}/cancel", dict(reason=""))[0] == 400
    out = call("frontdesk", "POST", f"/api/bookings/{fut['id']}/cancel", dict(reason="Guest called"))[1]
    assert out["status"] == "Cancelled" and out["cancel_reason"] == "Guest called"
    st, today_b = call("frontdesk", "POST", "/api/bookings", guest(r2))
    assert call("frontdesk", "POST", f"/api/bookings/{today_b['id']}/noshow")[1]["status"] == "No-Show"
    assert call("frontdesk", "POST", "/api/bookings/999999/checkin")[0] == 404
    assert call("frontdesk", "POST", "/api/bookings/1/cancel", dict(reason="x"))[0] in (200, 404, 409)  # own-property booking; no crash

    # room status: block conflicts with bookings, roles, release rules
    r3 = free_room("manager", skip=(r1["id"], r2["id"]))
    st, tb = call("manager", "POST", "/api/bookings", guest(r3, check_in=D(5), check_out=D(7)))
    assert st == 200, tb
    blk = dict(room_id=r3["id"], kind="maintenance", issue_category="Plumbing", reason="Leak", start_date=D(6), end_date=D(8))
    assert call("viewer", "POST", "/api/room_events", blk)[0] == 403
    assert call("housekeeping", "POST", "/api/room_events", blk)[0] == 403
    st, out = call("manager", "POST", "/api/room_events", blk)
    assert st == 409 and "booking" in out["error"], out
    assert call("manager", "POST", "/api/room_events", blk | dict(end_date=None))[0] == 400
    assert call("manager", "POST", "/api/room_events", blk | dict(issue_category="Magic"))[0] == 400
    ok = blk | dict(start_date=D(9), end_date=D(11))
    st, ev = call("frontdesk", "POST", "/api/room_events", ok)
    assert st == 200, ev
    assert next(r for r in call("frontdesk", "GET", "/api/rooms", dict(property_id=r3["property_id"], date=D(10)))[1] if r["id"] == r3["id"])["status"] == "maintenance"
    assert call("housekeeping", "POST", f"/api/room_events/{ev['id']}/release")[0] == 403, "housekeeping cannot clear maintenance"
    assert call("frontdesk", "POST", f"/api/room_events/{ev['id']}/release")[0] == 200
    assert call("frontdesk", "POST", f"/api/room_events/{ev['id']}/release")[0] == 404

    # audit: managers only, scoped, records the actions above
    assert call("frontdesk", "GET", "/api/audit")[0] == 403 and call("viewer", "GET", "/api/audit")[0] == 403
    actions = {a["action"] for a in call("manager", "GET", "/api/audit")[1]}
    assert actions >= {"booking.create", "booking.checkin", "booking.checkout", "booking.cancel", "booking.noshow", "room.cleaning_start", "room.cleaning_done",
                       "room.block", "room.release"}, actions
    assert not {a for a in actions if a.startswith("auth.")}, "property-less entries are for super admins only"
    assert {"auth.login", "auth.login_failed"} <= {a["action"] for a in call("admin", "GET", "/api/audit")[1]}
    assert all(a["property_id"] in (1, 2) for a in call("manager", "GET", "/api/audit")[1] if a["property_id"])

    # logout kills the session; sessions of deactivated users die too
    assert call("viewer", "POST", "/api/logout")[0] == 200 and call("viewer", "GET", "/api/meta")[0] == 401
    db.execute("UPDATE users SET active = 0 WHERE username = 'housekeeping'"); db.commit()
    assert call("housekeeping", "GET", "/api/meta")[0] == 401

    # password helpers: legacy rows (bare salt) verify with the configured rounds; wrong password fails
    legacy_salt = "ab" * 16
    old = s.derive("demo-pass", legacy_salt.encode(), s.LEGACY_ROUNDS)
    assert s.check_pw("demo-pass", old, legacy_salt) and not s.check_pw("nope", old, legacy_salt)
    assert s.check_pw("demo-pass", s.derive("demo-pass", bytes.fromhex(legacy_salt), s.LEGACY_ROUNDS), legacy_salt)

    db.close()
    assert hashlib.md5(SRC.read_bytes()).hexdigest() == before, "the real database must not change"


if __name__ == "__main__":
    try:
        test_all()
        print("ok")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
