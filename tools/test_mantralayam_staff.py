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
    assert call("viewer", "GET", "/api/meta")[1]["can"] == dict(book=False, status=False, clean=False, audit=False, admin=False)

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
    ev = [e for e in call("housekeeping", "GET", "/api/room_events")[1] if e["room_id"] == r1["id"] and e["kind"] in s.CLEANING]  # other (older) blocks on the live data are not this test's business
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

    # menu pages by role: the server decides, and enforces it on every route
    pages = lambda u: [p["key"] for p in call(u, "GET", "/api/meta")[1]["pages"]]
    assert set(pages("admin")) == set(pages("manager")) | {"admin"} and "reports" in pages("admin") and "rates" in pages("admin")
    assert "reports" not in pages("frontdesk") and "rates" not in pages("frontdesk") and "audit" not in pages("frontdesk") and "settings" in pages("frontdesk")
    assert pages("housekeeping") == ["today", "rooms", "status", "notifications", "settings"]
    for u in ("frontdesk", "housekeeping", "viewer"):
        for path in ("/api/reports", "/api/room_types", "/api/settings"):
            assert call(u, "GET", path)[0] == 403, (u, path)
    assert call("housekeeping", "GET", "/api/bookings")[0] == 403 and call("housekeeping", "GET", "/api/guests")[0] == 403 and call("housekeeping", "GET", "/api/calendar")[0] == 403
    assert call("frontdesk", "POST", "/api/settings/times", dict(checkin_time="13:00", checkout_time="10:00"))[0] == 403
    assert call("manager", "POST", "/api/settings/times", dict(checkin_time="13:00", checkout_time="10:00"))[0] == 403
    assert call("admin", "POST", "/api/settings/times", dict(checkin_time="25:00", checkout_time="10:00"))[0] == 400

    # edit and reschedule: never double-book
    ra = free_room("frontdesk", skip=(r1["id"], r2["id"], r3["id"]))
    st, e1 = call("frontdesk", "POST", "/api/bookings", guest(ra, check_in=D(2), check_out=D(4), guest_name="Edit Me", phone="9111111111"))
    assert st == 200, e1
    assert call("viewer", "POST", f"/api/bookings/{e1['id']}/edit", dict(guest_name="X", phone="9111111111", guests=1))[0] == 403
    assert call("frontdesk", "POST", f"/api/bookings/{e1['id']}/edit", dict(guest_name="", phone="9111111111", guests=1))[0] == 400
    assert call("frontdesk", "POST", f"/api/bookings/{e1['id']}/edit", dict(guest_name="Edited", phone="1", guests=1))[0] == 400
    assert call("frontdesk", "POST", f"/api/bookings/{e1['id']}/edit", dict(guest_name="Edited", phone="9222222222", guests=99))[0] == 400
    out = call("frontdesk", "POST", f"/api/bookings/{e1['id']}/edit", dict(guest_name="Edited", phone="9222222222", guests=2, remarks="late"))[1]
    assert out["guest_name"] == "Edited" and out["guests"] == 2 and out["remarks"] == "late"
    st, e2 = call("frontdesk", "POST", "/api/bookings", guest(ra, check_in=D(6), check_out=D(8)))
    assert st == 200
    st, out = call("frontdesk", "POST", f"/api/bookings/{e2['id']}/reschedule", dict(check_in=D(3), check_out=D(7)))
    assert st == 409 and "already booked" in out["error"], out
    assert call("frontdesk", "POST", f"/api/bookings/{e2['id']}/reschedule", dict(check_in=D(-1), check_out=D(7)))[0] == 400
    assert call("frontdesk", "POST", f"/api/bookings/{e2['id']}/reschedule", dict(check_in=D(7), check_out=D(7)))[0] == 400
    out = call("frontdesk", "POST", f"/api/bookings/{e2['id']}/reschedule", dict(check_in=D(5), check_out=D(7)))[1]
    assert (out["check_in"], out["check_out"]) == (D(5), D(7)), out
    assert call("frontdesk", "POST", f"/api/bookings/{e2['id']}/reschedule", dict(check_in=D(5), check_out=D(7)))[0] == 200, "own dates do not conflict with itself"

    # calendar, guests, notifications (before e2 is cancelled)
    cal = call("frontdesk", "GET", "/api/calendar", dict(start=D(0), days=7, property_id=ra["property_id"]))[1]
    row_ = next(r for r in cal["rooms"] if r["id"] == ra["id"])
    assert len(cal["days"]) == 7 and row_["cells"][2]["status"] == "booked" and row_["cells"][0] is None and row_["cells"][4] is None
    assert call("frontdesk", "GET", "/api/calendar", dict(days=99))[0] == 400
    assert call("frontdesk", "POST", f"/api/bookings/{e2['id']}/cancel", dict(reason="test"))[0] == 200
    assert call("frontdesk", "POST", f"/api/bookings/{e2['id']}/reschedule", dict(check_in=D(5), check_out=D(7)))[0] == 409, "cancelled bookings cannot be moved"
    assert call("frontdesk", "POST", f"/api/bookings/{e2['id']}/edit", dict(guest_name="Z", phone="9222222222", guests=1))[0] == 409
    gl = call("frontdesk", "GET", "/api/guests", dict(q="edited"))[1]
    assert [x["phone"] for x in gl] == ["9222222222"], gl
    prof = call("frontdesk", "GET", "/api/guests/profile", dict(phone="9222222222"))[1]
    assert len(prof["bookings"]) == 1 and call("frontdesk", "GET", "/api/guests/profile", dict(phone="9000000000"))[0] == 404
    assert call("frontdesk", "POST", "/api/guests/edit", dict(phone="9222222222", guest_name="Renamed", new_phone="9333333333"))[1]["updated"] == 1
    assert call("viewer", "POST", "/api/guests/edit", dict(phone="9333333333", guest_name="Hack"))[0] in (401, 403)
    nt = call("frontdesk", "GET", "/api/notifications")[1]
    assert any(i["kind"] == "new" for i in nt["items"]) and any(i["kind"] == "cancelled" for i in nt["items"]) and nt["unread"] > 0
    assert call("frontdesk", "POST", "/api/notifications/seen")[0] == 200 and call("frontdesk", "GET", "/api/notifications")[1]["unread"] == 0
    assert all(i["kind"] == "cleaning" for i in call("housekeeping", "GET", "/api/notifications")[1]["items"])

    # reports
    rp = call("manager", "GET", "/api/reports", {"from": D(-5), "to": D(30)})[1]
    assert rp["bookings"] >= 3 and rp["revenue"] > 0 and sum(d["bookings"] for d in rp["by_day"]) == rp["bookings"] and "Cancelled" in rp["by_status"]
    assert call("manager", "GET", "/api/reports", {"from": D(5), "to": D(1)})[0] == 400 and call("manager", "GET", "/api/reports", {"from": D(-400), "to": D(1)})[0] == 400

    # rooms & rates
    st, t = call("manager", "POST", "/api/room_types", dict(name="Test Suite", capacity=3, tariff=999, description="x"))
    assert st == 200 and call("manager", "POST", "/api/room_types", dict(name="Test Suite", capacity=3, tariff=1))[0] == 409
    assert call("manager", "POST", "/api/room_types", dict(id=t["id"], name="Test Suite 2", capacity=0, tariff=1))[0] == 400
    assert call("manager", "POST", "/api/room_types", dict(id=t["id"], name="Test Suite 2", capacity=4, tariff="abc"))[0] == 400
    assert call("manager", "POST", "/api/room_types", dict(id=t["id"], name="Test Suite 2", capacity=4, tariff=1200))[0] == 200
    assert call("manager", "POST", "/api/room_types/1/delete")[0] == 409, "types in use cannot be deleted"
    assert call("manager", "POST", f"/api/room_types/{t['id']}/delete")[0] == 200 and call("manager", "POST", f"/api/room_types/{t['id']}/delete")[0] == 404

    # settings and own profile
    st, cfg = call("manager", "GET", "/api/settings")
    assert st == 200 and {p["id"] for p in cfg["properties"]} == {1, 2}
    assert call("manager", "POST", "/api/settings/property", dict(id=3, phone="9440000103"))[0] == 403
    assert call("manager", "POST", "/api/settings/property", dict(id=1, phone="abc"))[0] == 400
    assert call("manager", "POST", "/api/settings/property", dict(id=1, phone="9440000101", address="Temple Road", contact_person="X", description="d", facilities="f"))[0] == 200
    assert call("admin", "POST", "/api/settings/times", dict(checkin_time="13:00", checkout_time="10:00"))[0] == 200
    assert call("frontdesk", "POST", "/api/me/profile", dict(full_name=""))[0] == 400
    assert call("frontdesk", "POST", "/api/me/profile", dict(full_name="New Name"))[1]["user"]["full_name"] == "New Name"
    assert call("frontdesk", "POST", "/api/me/password", dict(current="bad", new="LongEnough1"))[0] == 400
    assert call("frontdesk", "POST", "/api/me/password", dict(current=PW, new="short"))[0] == 400
    TOKENS["fd2"] = s.dispatch("POST", "/api/login", None, {"username": "frontdesk", "password": PW}, "127.0.0.1")[2]
    assert call("frontdesk", "POST", "/api/me/password", dict(current=PW, new="LongEnough1"))[0] == 200
    assert call("fd2", "GET", "/api/meta")[0] == 401, "other sessions end after a password change"
    assert call("frontdesk", "GET", "/api/meta")[0] == 200
    s.FAILS.clear()
    assert login("frontdesk", PW)[0] == 401 and login("frontdesk", "LongEnough1")[0] == 200


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


def test_admin():
    db = sqlite3.connect(g.DB)
    h, salt = s.make_hash(PW)
    db.execute("UPDATE users SET pw_hash = ?, salt = ?, active = 1", (h, salt)); db.commit()
    s.FAILS.clear()
    for u in ("admin", "manager", "frontdesk", "viewer"):
        assert login(u)[0] == 200, u
    # only super_admin may touch the admin screens
    for u in ("manager", "frontdesk", "viewer"):
        assert call(u, "GET", "/api/admin/data")[0] == 403 and call(u, "POST", "/api/admin/rooms", {})[0] == 403, u
        assert call(u, "POST", "/api/admin/settings", {"guest_max_rooms": 2})[0] == 403
    assert "admin" not in [p["key"] for p in call("manager", "GET", "/api/meta")[1]["pages"]]
    d = call("admin", "GET", "/api/admin/data")[1]
    assert d["properties"] and d["rooms"] and d["roles"] and d["settings"]["guest_max_rooms"]

    # properties, room types, rooms: create, edit, validation
    st, p = call("admin", "POST", "/api/admin/properties", {"name": "Test Mutt", "phone": "08512 280000"})
    assert st == 200
    assert call("admin", "POST", "/api/admin/properties", {"name": "Test Mutt"})[0] == 409
    assert call("admin", "POST", "/api/admin/properties", {"name": "X", "phone": "abc"})[0] == 400
    assert call("admin", "POST", "/api/admin/properties", {"id": p["id"], "name": "Test Mutt 2"})[0] == 200
    st, t = call("admin", "POST", "/api/admin/room_types", {"name": "Test Suite", "capacity": 3, "tariff": 999})
    assert st == 200 and call("admin", "POST", "/api/admin/room_types", {"name": "Test Suite", "capacity": 3, "tariff": 1})[0] == 409
    room = dict(property_id=p["id"], number="T1", floor=1, room_type_id=t["id"], capacity=3, amenities="Fan")
    assert call("admin", "POST", "/api/admin/rooms", room | {"capacity": 0})[0] == 400
    st, r = call("admin", "POST", "/api/admin/rooms", room)
    assert st == 200 and call("admin", "POST", "/api/admin/rooms", room)[0] == 409
    assert call("admin", "POST", "/api/admin/rooms", room | {"id": r["id"], "number": "T1A"})[0] == 200
    assert call("admin", "POST", "/api/admin/rooms", room | {"id": r["id"], "property_id": 1})[0] == 400  # cannot move property

    # deactivate refuses while a current or future booking exists, and the guest site stops showing it afterwards
    rm = next(x for x in call("admin", "GET", "/api/rooms", dict(property_id=p["id"]))[1] if x["id"] == r["id"])
    st, bk = call("admin", "POST", "/api/bookings", guest(rm, check_in=D(40), check_out=D(41)))
    assert st == 200, bk
    for kind, id_ in (("rooms", r["id"]), ("room_types", t["id"]), ("properties", p["id"])):
        st, out = call("admin", "POST", f"/api/admin/{kind}/{id_}/active", {"active": False})
        assert st == 409 and "booking" in out["error"], (kind, out)
    assert call("admin", "POST", "/api/admin/rooms", room | {"id": r["id"], "number": "T1A", "room_type_id": 1})[0] == 409  # type change blocked too
    assert call("admin", "POST", f"/api/bookings/{bk['id']}/cancel", {"reason": "test"})[0] == 200
    assert call("admin", "POST", f"/api/admin/rooms/{r['id']}/active", {"active": "no"})[0] == 400
    for kind, id_ in (("rooms", r["id"]), ("room_types", t["id"]), ("properties", p["id"])):
        assert call("admin", "POST", f"/api/admin/{kind}/{id_}/active", {"active": False})[0] == 200, kind
    assert call("admin", "POST", "/api/admin/rooms", room | {"number": "T2"})[0] == 400  # inactive property or type
    st, info = g.dispatch("GET", "/api/public/info", {}, None, "127.0.0.1")
    assert t["id"] not in [x["id"] for x in info["room_types"]] and p["id"] not in [x["id"] for x in info["properties"] if x["type_ids"]]
    assert call("admin", "POST", f"/api/admin/room_types/{t['id']}/active", {"active": True})[0] == 200

    # users: validation, property assignment, self-protection, password reset signs the user out
    u = dict(username="newdesk", full_name="New Desk", role="front_desk", password="Longenough1", property_ids=[1])
    assert call("admin", "POST", "/api/admin/users", u | {"property_ids": []})[0] == 400
    assert call("admin", "POST", "/api/admin/users", u | {"password": "short"})[0] == 400
    assert call("admin", "POST", "/api/admin/users", u | {"role": "boss"})[0] == 400
    assert call("admin", "POST", "/api/admin/users", u | {"property_ids": [p["id"]]})[0] == 400  # inactive property
    st, nu = call("admin", "POST", "/api/admin/users", u)
    assert st == 200 and call("admin", "POST", "/api/admin/users", u | {"username": "NEWDESK"})[0] == 409
    assert login("newdesk", "Longenough1")[0] == 200 and call("newdesk", "GET", "/api/meta")[1]["properties"][0]["id"] == 1
    assert call("admin", "POST", "/api/admin/users", u | {"id": nu["id"], "password": "", "role": "viewer"})[0] == 200
    assert call("newdesk", "GET", "/api/meta")[1]["user"]["role"] == "viewer"
    assert call("admin", "POST", "/api/admin/users", u | {"id": nu["id"], "password": "Another#Pass1"})[0] == 200
    assert call("newdesk", "GET", "/api/meta")[0] == 401 and login("newdesk", "Another#Pass1")[0] == 200
    me = next(x for x in d["users"] if x["username"] == "admin")
    assert call("admin", "POST", "/api/admin/users", dict(username="admin", full_name="A", role="viewer", password="", property_ids=[1], id=me["id"]))[0] == 409
    assert call("admin", "POST", f"/api/admin/users/{me['id']}/active", {"active": False})[0] == 409
    assert call("admin", "POST", f"/api/admin/users/{nu['id']}/active", {"active": False})[0] == 200 and call("newdesk", "GET", "/api/meta")[0] == 401

    # settings: ranges, persistence, the guest site obeys them
    for bad in ({"guest_max_rooms": 0}, {"guest_max_rooms": "x"}, {"guest_advance_days": 5000}, {"checkin_time": "25:00"}, {"guest_booking_enabled": "yes"}):
        assert call("admin", "POST", "/api/admin/settings", bad)[0] == 400, bad
    assert call("admin", "POST", "/api/admin/settings", {"guest_max_rooms": 2, "guest_max_nights": 5, "checkin_time": "13:00", "guest_booking_enabled": True})[0] == 200
    cfg = g.settings(g.connect())
    assert (cfg["max_rooms"], cfg["max_nights"], cfg["checkin_time"], cfg["enabled"]) == (2, 5, "13:00", True)
    assert call("admin", "POST", "/api/admin/settings", {"guest_booking_enabled": False})[0] == 200
    assert g.dispatch("GET", "/api/public/search", dict(check_in=D(30), check_out=D(31), guests=1), None, "127.0.0.1")[0] == 403
    assert call("admin", "POST", "/api/admin/settings", {"guest_booking_enabled": True, "guest_max_rooms": 3, "guest_max_nights": 15, "checkin_time": "14:00"})[0] == 200  # leave the copy as found

    # every admin action is in the audit log
    actions = {r[0] for r in db.execute("SELECT action FROM audit_log WHERE action LIKE 'admin.%' OR action LIKE 'rate.%'")}
    assert {"admin.property_create", "admin.room_create", "admin.user_create", "admin.user_edit", "admin.room_deactivate", "admin.settings", "rate.create"} <= actions, actions
    db.close()


def test_hold_blocks_walkin():
    db = sqlite3.connect(g.DB)
    h, salt = s.make_hash(PW)
    db.execute("UPDATE users SET pw_hash = ?, salt = ?, active = 1", (h, salt)); db.commit()
    s.FAILS.clear()
    assert login("frontdesk")[0] == 200
    room = free_room("frontdesk")
    stay = dict(check_in=D(50), check_out=D(51))
    exp = (g.now() + timedelta(minutes=10)).isoformat(timespec="seconds")
    db.execute("INSERT INTO holds (token, room_id, check_in, check_out, expires_at, ip) VALUES ('t', ?, ?, ?, ?, '1.1.1.1')", (room["id"], stay["check_in"], stay["check_out"], exp))
    db.commit()
    st, out = call("frontdesk", "POST", "/api/bookings", guest(room, **stay))
    assert st == 409 and "held for a guest who is booking online" in out["error"], out
    db.execute("DELETE FROM holds"); db.commit()
    assert call("frontdesk", "POST", "/api/bookings", guest(room, **stay))[0] == 200
    db.close()


def test_peak_admin():
    db = sqlite3.connect(g.DB)
    h, salt = s.make_hash(PW)
    db.execute("UPDATE users SET pw_hash = ?, salt = ?, active = 1", (h, salt)); db.commit()
    s.FAILS.clear()
    for u in ("admin", "manager", "frontdesk"):
        assert login(u)[0] == 200
    pp = dict(name="Test Festival", start_date=D(40), end_date=D(44), max_nights="2", max_rooms="1", note="Book early.")
    for u in ("manager", "frontdesk"):  # only super_admin manages peak periods
        assert call(u, "POST", "/api/admin/peak_periods", pp)[0] == 403
    assert call("admin", "GET", "/api/admin/data")[1]["peak_periods"] == []
    for bad in ({"name": ""}, {"start_date": "nope"}, {"end_date": D(39)}, {"end_date": D(500)}, {"max_nights": "0"}, {"max_nights": "x"}, {"max_rooms": "99"}, {"note": "x" * 501}):
        assert call("admin", "POST", "/api/admin/peak_periods", pp | bad)[0] == 400, bad
    st, r = call("admin", "POST", "/api/admin/peak_periods", pp)
    assert st == 200
    row = call("admin", "GET", "/api/admin/data")[1]["peak_periods"][0]
    assert (row["name"], row["max_nights"], row["max_rooms"], row["note"], row["active"]) == ("Test Festival", 2, 1, "Book early.", 1)

    # the guest site picks it up at once, and edits and deactivation take effect at once
    guest_search = lambda a, b: g.dispatch("GET", "/api/public/search", dict(check_in=D(a), check_out=D(b), guests=1), None, "127.0.0.1")
    g.HITS.clear()
    st, out = guest_search(40, 43)
    assert st == 400 and "Test Festival" in out["error"], out
    assert call("admin", "POST", "/api/admin/peak_periods", pp | {"id": r["id"], "max_nights": "", "max_rooms": ""})[0] == 200  # empty means no limit
    st, out = guest_search(40, 43)
    assert st == 200 and out["peak"][0]["note"] == "Book early."
    assert call("admin", "POST", "/api/admin/peak_periods", pp | {"id": 9999})[0] == 404
    assert call("admin", "POST", f"/api/admin/peak_periods/{r['id']}/active", {"active": "no"})[0] == 400
    assert call("admin", "POST", f"/api/admin/peak_periods/{r['id']}/active", {"active": False})[0] == 200
    assert guest_search(40, 43)[1]["peak"] == []
    assert call("admin", "POST", f"/api/admin/peak_periods/{r['id']}/active", {"active": True})[0] == 200
    assert call("manager", "POST", f"/api/admin/peak_periods/{r['id']}/active", {"active": False})[0] == 403
    actions = {x[0] for x in db.execute("SELECT action FROM audit_log WHERE action LIKE 'admin.peak%'")}
    assert actions == {"admin.peak_period_create", "admin.peak_period_edit", "admin.peak_period_deactivate", "admin.peak_period_activate"}, actions
    db.close()


if __name__ == "__main__":
    try:
        test_all()
        test_admin()
        test_hold_blocks_walkin()
        test_peak_admin()
        print("ok")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
