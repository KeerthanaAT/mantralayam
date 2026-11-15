"""Self-check for the staff API. Runs against a COPY of the real database, with a known test password.
Run: python tools/test_mantralayam_staff.py   (or pytest)
"""
import hashlib
import re
import shutil
import sqlite3
import tempfile
from datetime import date as date_, timedelta
from pathlib import Path

import mantralayam_rooms as g
import mantralayam_staff as s

SRC = Path(__file__).resolve().parent.parent / ".tmp" / "mantralayam.db"
TMP = Path(tempfile.mkdtemp())
g.DB = TMP / "copy.db"
g.OTP_REQUIRED = False  # guest bookings made by these tests skip the SMS code (covered in test_mantralayam_features.py)
g.BACKUP_MAX_HOURS = 0
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
    db.execute("UPDATE users SET pw_hash = ?, salt = ?", (h, salt))
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('checkin_requires_id', '0')"); db.commit()  # these tests check guests in directly; the ID rule has its own tests (test_staff_desk.py)
    before = hashlib.md5(SRC.read_bytes()).hexdigest()

    # login, sessions, throttling
    assert call("admin", "GET", "/api/meta")[0] == 401
    assert login("frontdesk", "wrong")[0] == 401 and login("nobody")[0] == 401
    for _ in range(5): login("frontdesk", "wrong")
    assert login("frontdesk")[0] == 429, "5 failures must lock this username"
    g.reset_throttle()
    for u in ("admin", "manager", "frontdesk", "housekeeping", "viewer"):
        st, m = login(u)
        assert st == 200 and m["user"]["username"] == u, (u, m)
    can = call("viewer", "GET", "/api/meta")[1]["can"]
    assert {k: can[k] for k in ("book", "status", "clean", "audit", "admin")} == dict(book=False, status=False, clean=False, audit=False, admin=False) and not any(can.values()), can

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
    assert pages("housekeeping") == ["today", "rooms", "housekeeping", "notifications", "settings"]  # Room status is part of Rooms now
    assert "housekeeping" in pages("frontdesk") and "housekeeping" not in pages("viewer")  # the Housekeeping tab is for the roles that may clean
    REMOVED = {"desk", "callbacks", "payments", "messages", "notices", "feedback", "broadcasts"}  # taken off the menu on request; their routes and permissions still work and are covered by test_staff_desk.py and test_extras.py
    assert not any(REMOVED & set(pages(u)) for u in ("admin", "manager", "frontdesk", "housekeeping", "viewer")), "removed pages must not be in any menu"
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
    assert call("frontdesk", "POST", "/api/guests/edit", dict(phone="9222222222", guest_name="Renamed", new_phone="9333333333"))[0] == 403, "the mobile number is the guest's key: managers only"
    assert call("manager", "POST", "/api/guests/edit", dict(phone="9222222222", guest_name="Renamed", new_phone="9333333333"))[1]["updated"] == 1
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
    assert call("manager", "POST", "/api/room_types", dict(name="Mgr Suite", capacity=3, tariff=1))[0] == 403, "room types and tariffs are shared by every property: super admin only"
    assert call("manager", "POST", "/api/room_types/1/delete")[0] == 403
    st, t = call("admin", "POST", "/api/room_types", dict(name="Test Suite", capacity=3, tariff=999, description="x"))
    assert st == 200 and call("admin", "POST", "/api/room_types", dict(name="Test Suite", capacity=3, tariff=1))[0] == 409
    assert call("admin", "POST", "/api/room_types", dict(id=t["id"], name="Test Suite 2", capacity=0, tariff=1))[0] == 400
    assert call("admin", "POST", "/api/room_types", dict(id=t["id"], name="Test Suite 2", capacity=4, tariff="abc"))[0] == 400
    assert call("admin", "POST", "/api/room_types", dict(id=t["id"], name="Test Suite 2", capacity=4, tariff=1200))[0] == 200
    assert call("admin", "POST", "/api/room_types/1/delete")[0] == 409, "types in use cannot be deleted"
    assert call("admin", "POST", f"/api/room_types/{t['id']}/delete")[0] == 200 and call("admin", "POST", f"/api/room_types/{t['id']}/delete")[0] == 404

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
    g.reset_throttle()
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


def test_site_config_admin():
    db = sqlite3.connect(g.DB)
    for u in ("admin", "manager", "frontdesk", "viewer"):
        assert login(u)[0] == 200, u
    post = lambda u, **b: call(u, "POST", "/api/admin/site_config", b)
    for u in ("manager", "frontdesk", "viewer"):  # permission: super admin only
        assert post(u, key="rooms_phone", lang="en", value="1")[0] == 403, u
    items = lambda: {x["key"]: x for x in call("admin", "GET", "/api/admin/data")[1]["site_config"]}
    assert items()["rooms_phone"]["todo"] and items()["rooms_phone"]["values"] == {}, "a fresh database has no invented facts"
    assert post("admin", key="nope", lang="en", value="x")[0] == 400 and post("admin", key="darshan_timings", lang="xx", value="x")[0] == 400
    assert post("admin", key="rooms_phone", lang="te", value="x")[0] == 400, "non-translatable items take English only"
    assert post("admin", key="darshan_timings", lang="en", value="x" * 2001)[0] == 400
    assert post("admin", key="darshan_timings", lang="en", value="TODO")[0] == 200 and items()["darshan_timings"]["todo"]
    assert post("admin", key="darshan_timings", lang="en", value="Set by the office")[0] == 200 and not items()["darshan_timings"]["todo"]
    assert post("admin", key="darshan_timings", lang="ta", value="Tamil text")[0] == 200 and items()["darshan_timings"]["values"]["ta"] == "Tamil text"
    assert post("admin", key="darshan_timings", lang="ta", value="")[0] == 200 and "ta" not in items()["darshan_timings"]["values"]
    assert post("admin", key="darshan_timings", lang="en", value="")[0] == 200 and items()["darshan_timings"]["todo"]
    assert {"admin.site_config"} <= {r[0] for r in db.execute("SELECT action FROM audit_log")}
    db.execute("DELETE FROM site_config"); db.commit(); db.close()


def test_quick_callback_in_queue():
    db = sqlite3.connect(g.DB)
    for u in ("admin", "frontdesk", "viewer"):
        assert login(u)[0] == 200, u
    db.execute("DELETE FROM callback_requests"); db.commit()
    g.reset_throttle()
    assert g.dispatch("POST", "/api/public/callback", None, dict(name="Elder", phone="9876500123", call_time="morning", lang="ta"), "6.6.6.6")[0] == 200
    for u in ("admin", "frontdesk", "viewer"):
        rows = call(u, "GET", "/api/callbacks")[1]
        assert len(rows) == 1 and rows[0]["people"] is None and rows[0]["check_in"] is None and rows[0]["call_time"] == "morning" and rows[0]["lang"] == "ta", (u, rows)
    cid = call("admin", "GET", "/api/callbacks")[1][0]["id"]
    assert call("viewer", "POST", f"/api/callbacks/{cid}/status", {"status": "contacted"})[0] == 403
    st, r = call("frontdesk", "POST", f"/api/callbacks/{cid}/status", {"status": "contacted"})
    assert st == 200 and r["status"] == "contacted" and r["people"] is None
    db.execute("DELETE FROM callback_requests"); db.commit(); db.close()


def test_content_admin():
    db = sqlite3.connect(g.DB)
    db.row_factory = sqlite3.Row
    for u in ("admin", "manager", "frontdesk", "viewer"):
        assert login(u)[0] == 200, u
    post = lambda u, path, b: call(u, "POST", path, b)
    # permissions: only a super admin edits FAQs, refund settings and site items
    for u in ("manager", "frontdesk", "viewer"):
        assert post(u, "/api/admin/faqs", {"texts": {"en": {"question": "Q", "answer": "A"}}})[0] == 403, u
        assert post(u, "/api/admin/settings", {"refund_percent": 10})[0] == 403, u
        assert post(u, "/api/admin/faqs/1/active", {"active": False})[0] == 403, u
    # refund settings: optional, bounded, and cleared by an empty value
    for bad in ({"refund_percent": 101}, {"refund_percent": -1}, {"refund_percent": "abc"}, {"refund_days": 91}, {"refund_days": -1}):
        assert post("admin", "/api/admin/settings", bad)[0] == 400, bad
    assert post("admin", "/api/admin/settings", {"refund_percent": 50, "refund_days": 7})[0] == 200
    assert g.settings(db)["refund_percent"] == 50 and g.settings(db)["refund_days"] == 7
    assert post("admin", "/api/admin/settings", {"refund_percent": "", "refund_days": ""})[0] == 200
    assert g.settings(db)["refund_percent"] is None and g.settings(db)["refund_days"] is None
    # site items are validated by kind
    for key, val in (("rooms_phone", "abc"), ("whatsapp_number", "123"), ("map_embed_url", "https://evil.example/x"), ("map_url", "http://insecure.example")):
        assert post("admin", "/api/admin/site_config", dict(key=key, lang="en", value=val))[0] == 400, key
    assert post("admin", "/api/admin/site_config", dict(key="map_embed_url", lang="en", value="https://www.google.com/maps/embed?pb=1"))[0] == 200
    assert post("admin", "/api/admin/site_config", dict(key="map_embed_url", lang="en", value=""))[0] == 200
    # FAQs: English is required, question and answer come as a pair, other languages optional
    texts = lambda **kw: {"texts": kw}
    assert post("admin", "/api/admin/faqs", texts(te={"question": "Q", "answer": "A"}))[0] == 400, "English required"
    assert post("admin", "/api/admin/faqs", texts(en={"question": "Q", "answer": ""}))[0] == 400
    assert post("admin", "/api/admin/faqs", texts(xx={"question": "Q", "answer": "A"}, en={"question": "Q", "answer": "A"}))[0] == 400
    assert post("admin", "/api/admin/faqs", {"texts": "no"})[0] == 400
    st, f = post("admin", "/api/admin/faqs", dict(position=3, **texts(en={"question": "Is parking available?", "answer": "TODO"}, te={"question": "", "answer": ""})))
    assert st == 200, f
    listed = lambda: {x["id"]: x for x in call("admin", "GET", "/api/admin/data")[1]["faqs"]}
    assert listed()[f["id"]]["texts"] == {"en": {"question": "Is parking available?", "answer": "TODO"}} and listed()[f["id"]]["active"] == 1
    assert post("admin", "/api/admin/faqs", dict(id=f["id"], position=1, **texts(en={"question": "Is parking available?", "answer": "Confirmed by the office"}, ta={"question": "q", "answer": "a"})))[0] == 200
    assert set(listed()[f["id"]]["texts"]) == {"en", "ta"}
    assert post("admin", "/api/admin/faqs", dict(id=99999, **texts(en={"question": "Q", "answer": "A"})))[0] == 404
    assert post("admin", f"/api/admin/faqs/{f['id']}/active", {"active": "no"})[0] == 400 and post("admin", "/api/admin/faqs/99999/active", {"active": False})[0] == 404
    assert post("admin", f"/api/admin/faqs/{f['id']}/active", {"active": False})[0] == 200 and listed()[f["id"]]["active"] == 0
    assert {"admin.faq_create", "admin.faq_edit", "admin.faq_deactivate"} <= {r[0] for r in db.execute("SELECT action FROM audit_log")}
    db.execute("DELETE FROM faq_text"); db.execute("DELETE FROM faqs"); db.execute("DELETE FROM site_config"); db.commit(); db.close()


def test_admin():
    db = sqlite3.connect(g.DB)
    h, salt = s.make_hash(PW)
    db.execute("UPDATE users SET pw_hash = ?, salt = ?, active = 1", (h, salt)); db.commit()
    g.reset_throttle()
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


def test_backup_status():
    g.BACKUPS = TMP / "backups"
    db = sqlite3.connect(g.DB)
    h, salt = s.make_hash(PW)
    db.execute("UPDATE users SET pw_hash = ?, salt = ?, active = 1", (h, salt)); db.commit()
    g.reset_throttle()
    for u in ("admin", "manager"):
        assert login(u)[0] == 200
    assert call("admin", "GET", "/api/dashboard")[1]["backup"] is None  # nothing has run yet
    g.backup()
    assert call("admin", "GET", "/api/dashboard")[1]["backup"]["ok"] is True
    g.DB, real = TMP / "missing.db", g.DB
    try:
        g.backup()
    except g.BackupError:
        pass
    finally:
        g.DB = real
    assert call("admin", "GET", "/api/dashboard")[1]["backup"]["ok"] is False
    assert "backup" not in call("manager", "GET", "/api/dashboard")[1], "only super_admin sees backup status"
    db.close()


def test_alternatives_scope():
    db = sqlite3.connect(g.DB)
    h, salt = s.make_hash(PW)
    db.execute("UPDATE users SET pw_hash = ?, salt = ?, active = 1", (h, salt)); db.commit()
    g.reset_throttle()
    for u in ("admin", "frontdesk", "housekeeping", "viewer"):
        assert login(u)[0] == 200
    q = dict(check_in=D(40), check_out=D(42), guests=2)

    def block(a, b, where):
        db.execute("INSERT INTO bookings (property_id, room_id, guest_name, phone, id_type, id_number, guests, check_in, check_out, status, created_at, status_at) "
                   f"SELECT r.property_id, r.id, 'ALT BLOCK', '9000000001', 'Other', 'x', 1, ?, ?, 'Confirmed', ?, ? FROM rooms r WHERE r.active = 1 AND {where}",
                   (D(a), D(b), g.now_iso(), g.now_iso()))
        db.commit()

    def unblock():
        db.execute("DELETE FROM bookings WHERE guest_name = 'ALT BLOCK'"); db.commit()

    assert call("housekeeping", "GET", "/api/availability", q)[0] == 403 and call("viewer", "GET", "/api/availability", q)[0] == 403
    assert call("frontdesk", "GET", "/api/availability", q | dict(property_id=3))[0] == 403, "another property"
    out = call("frontdesk", "GET", "/api/availability", q)[1]
    assert out["free"] > 0 and out["alternatives"] == [], "rooms are free: nothing to suggest"
    for bad in (dict(check_in=D(-1)), dict(check_out=D(40)), dict(check_in="nope"), dict(guests=0)):
        assert call("frontdesk", "GET", "/api/availability", q | bad)[0] == 400, bad

    # the front desk's own properties (1, 2) are full for 50 days; properties 3 to 5 are free
    block(20, 70, "r.property_id IN (1, 2)")
    assert call("frontdesk", "GET", "/api/availability", q)[1] == {"free": 0, "alternatives": []}, "other properties' rooms must not be offered to this user"
    assert call("admin", "GET", "/api/availability", q)[1]["free"] > 0, "the super admin sees every property"
    assert call("admin", "GET", "/api/availability", q | dict(property_id=1))[1] == {"free": 0, "alternatives": []}
    unblock()

    # a gap in their own properties: the alternatives are bookable there, and nothing outside 1 and 2 counts
    block(38, 44, "r.property_id IN (1, 2)")
    block(36, 46, "r.property_id IN (3, 4, 5)")  # the others are not free near the dates either, so any suggestion must come from 1 or 2
    out = call("frontdesk", "GET", "/api/availability", q)[1]
    assert out["free"] == 0 and out["alternatives"], out
    conn = g.connect()
    for a in out["alternatives"]:
        free = [r for r in g.free_rooms(conn, g.date.fromisoformat(a["check_in"]), g.date.fromisoformat(a["check_out"])) if r["property_id"] in (1, 2) and r["capacity"] >= 2]
        assert free and a["nights"] == 2, a
    a = out["alternatives"][0]
    st, b = call("frontdesk", "POST", "/api/bookings", guest(free[0], check_in=a["check_in"], check_out=a["check_out"], guests=2))
    assert st == 200, b  # the suggested dates can really be booked
    conn.close()
    unblock()
    db.close()


def test_callbacks():
    db = sqlite3.connect(g.DB)
    h, salt = s.make_hash(PW)
    db.execute("UPDATE users SET pw_hash = ?, salt = ?, active = 1", (h, salt)); db.commit()
    g.reset_throttle()
    for u in ("admin", "manager", "frontdesk", "housekeeping", "viewer"):
        assert login(u)[0] == 200
    db.execute("DELETE FROM callback_requests"); db.commit()
    mk = lambda i, **kw: g.dispatch("POST", "/api/public/callback", None, dict(name=f"Caller {i}", phone=f"98700000{i:02d}", people=30, check_in=D(40), check_out=D(42)) | kw, f"7.7.7.{i}")
    assert [mk(1, property_id=1)[0], mk(2, property_id=3)[0], mk(3)[0]] == [200, 200, 200]  # property 1, property 3 and no property
    db.execute("UPDATE callback_requests SET created_at = '2026-01-01T10:00:00' WHERE name = 'Caller 1'"); db.commit()  # make Caller 1 the oldest

    # who sees what: own properties plus unassigned; newest first; read-only for viewers; none for housekeeping
    names = lambda u, **q: [r["name"] for r in call(u, "GET", "/api/callbacks", q)[1]]
    assert call("housekeeping", "GET", "/api/callbacks")[0] == 403 and call("housekeeping", "POST", "/api/callbacks/1/status", dict(status="closed"))[0] == 403
    assert sorted(names("frontdesk")) == ["Caller 1", "Caller 3"], "front desk (properties 1 and 2) never sees property 3 requests"
    assert names("frontdesk")[-1] == "Caller 1", "newest first"
    assert sorted(names("manager")) == ["Caller 1", "Caller 3"]
    assert sorted(names("admin")) == ["Caller 1", "Caller 2", "Caller 3"] and sorted(names("viewer")) == ["Caller 1", "Caller 2", "Caller 3"]
    assert call("frontdesk", "GET", "/api/callbacks", dict(property_id=3))[0] == 403
    assert names("admin", property_id=3) == ["Caller 2"] and names("admin", property_id=1) == ["Caller 1"], "a property filter is exact"
    assert call("frontdesk", "GET", "/api/callbacks", dict(status="bogus"))[0] == 400
    row = next(r for r in call("frontdesk", "GET", "/api/callbacks")[1] if r["name"] == "Caller 1")
    assert (row["status"], row["people"], row["phone"], row["handled_by"]) == ("new", 30, "9870000001", None) and row["property"]
    ids = {r["name"]: r["id"] for r in call("admin", "GET", "/api/callbacks")[1]}

    # status changes: role and property checked on the server, saved, audited
    st = lambda u, name, status: call(u, "POST", f"/api/callbacks/{ids[name]}/status", dict(status=status, note="Spoke to the guest"))
    assert st("viewer", "Caller 1", "contacted")[0] == 403, "viewers are read-only"
    assert st("frontdesk", "Caller 2", "contacted")[0] == 404, "another property request cannot be changed"
    assert st("manager", "Caller 2", "closed")[0] == 404
    assert st("frontdesk", "Caller 1", "new")[0] == 400 and st("frontdesk", "Caller 1", "bogus")[0] == 400
    assert call("frontdesk", "POST", "/api/callbacks/99999/status", dict(status="closed"))[0] == 404
    code, out = st("frontdesk", "Caller 1", "contacted")
    assert code == 200 and out["status"] == "contacted" and out["handled_at"] and out["handled_by_name"], out
    assert st("frontdesk", "Caller 1", "contacted")[0] == 409
    assert st("manager", "Caller 3", "closed")[1]["status"] == "closed", "requests with no property are open to every property user"
    assert st("frontdesk", "Caller 3", "contacted")[0] == 409, "closed is final"
    assert names("frontdesk", status="contacted") == ["Caller 1"] and names("frontdesk", status="new") == [] and names("admin", status="new") == ["Caller 2"]
    saved = db.execute("SELECT status, handled_by, handled_at FROM callback_requests WHERE id = ?", (ids["Caller 1"],)).fetchone()
    assert saved[0] == "contacted" and saved[1] == db.execute("SELECT id FROM users WHERE username = 'frontdesk'").fetchone()[0] and saved[2]
    audit = db.execute("SELECT action, username, property_id, old_value, new_value FROM audit_log WHERE action LIKE 'callback.%' AND action != 'callback.request' ORDER BY id").fetchall()
    assert audit == [("callback.contacted", "frontdesk", 1, "new", "contacted"), ("callback.closed", "manager", None, "new", "closed")], audit
    assert not any("callbacks" in [p["key"] for p in call(u, "GET", "/api/meta")[1]["pages"]] for u in ("viewer", "housekeeping", "frontdesk"))  # off the menu; the call-back routes still work
    db.close()


def test_arrival_details():
    db = sqlite3.connect(g.DB)
    h, salt = s.make_hash(PW)
    db.execute("UPDATE users SET pw_hash = ?, salt = ?, active = 1", (h, salt)); db.commit()
    g.reset_throttle()
    for u in ("frontdesk", "viewer"):
        assert login(u)[0] == 200
    room = free_room("frontdesk")
    stay = dict(check_in=D(20), check_out=D(21))
    for patch, msg in [(dict(arrival_time="24:00"), "arrival time"), (dict(arrival_time="7pm"), "arrival time"), (dict(darshan_date=D(-1)), "past"), (dict(darshan_date="soon"), "valid date")]:
        st, out = call("frontdesk", "POST", "/api/bookings", guest(room, **stay) | patch)
        assert st == 400 and msg in out["error"], (patch, out)
    note = "<img src=x onerror=alert(1)> late train"
    st, b = call("frontdesk", "POST", "/api/bookings", guest(room, **stay, arrival_time="05:45", darshan_date=D(21), remarks=note))
    assert st == 200 and (b["arrival_time"], b["darshan_date"], b["remarks"]) == ("05:45", D(21), note), b

    # returned on every staff list that shows bookings (bookings page, arrivals on the dashboard)
    row = next(r for r in call("frontdesk", "GET", "/api/bookings", dict(q=str(b["id"])))[1] if r["id"] == b["id"])
    assert (row["arrival_time"], row["darshan_date"], row["remarks"]) == ("05:45", D(21), note)
    assert call("viewer", "GET", "/api/bookings", dict(q=str(b["id"])))[1][0]["arrival_time"] == "05:45"
    st, today_b = call("frontdesk", "POST", "/api/bookings", guest(free_room("frontdesk", skip=(room["id"],)), arrival_time="09:15", darshan_date=D(0)))
    assert st == 200
    arr = next(r for r in call("frontdesk", "GET", "/api/dashboard")[1]["arrivals"] if r["id"] == today_b["id"])
    assert (arr["arrival_time"], arr["darshan_date"]) == ("09:15", D(0)), arr
    st, plain = call("frontdesk", "POST", "/api/bookings", guest(free_room("frontdesk", skip=(room["id"], today_b["room_id"])), **stay))
    assert st == 200 and plain["arrival_time"] is None and plain["darshan_date"] is None and plain["remarks"] is None

    # edit: changes them only when asked, and validates
    ed = lambda **kw: call("frontdesk", "POST", f"/api/bookings/{b['id']}/edit", dict(guest_name="Walk In", phone="9876543210", guests=1, remarks=note) | kw)
    assert ed(arrival_time="bad")[0] == 400 and ed(darshan_date=D(-2))[0] == 400
    out = ed()[1]
    assert (out["arrival_time"], out["darshan_date"]) == ("05:45", D(21)), "an edit without the new fields leaves them alone"
    out = ed(arrival_time="07:00", darshan_date="")[1]
    assert (out["arrival_time"], out["darshan_date"]) == ("07:00", None), out
    assert db.execute("SELECT arrival_time, darshan_date FROM bookings WHERE id = ?", (b["id"],)).fetchone() == ("07:00", None)

    # text only on screen
    web = Path(__file__).resolve().parent.parent / "web"
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", (web / "staff.html").read_text(encoding="utf-8"))
    db.close()


def test_hold_blocks_walkin():
    db = sqlite3.connect(g.DB)
    h, salt = s.make_hash(PW)
    db.execute("UPDATE users SET pw_hash = ?, salt = ?, active = 1", (h, salt)); db.commit()
    g.reset_throttle()
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
    g.reset_throttle()
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
    g.reset_throttle()
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


def test_staff_features():
    """Promo codes, pricing rules, peak surcharge, senior flag, alerts banner and the waitlist hook, as the super admin sees them."""
    db = sqlite3.connect(g.DB)
    h, salt = s.make_hash(PW)
    db.execute("UPDATE users SET pw_hash = ?, salt = ?", (h, salt)); db.commit()
    g.reset_throttle()
    for u in ("admin", "manager", "frontdesk"):
        assert login(u)[0] == 200
    promo = dict(code="pilgrim 15", label="Pilgrim 15%", kind="percent", value="15", min_guests="6", valid_to=D(300), max_uses="50", per_phone_limit="2", exclude_peak=True, max_discount="")
    st, out = call("admin", "POST", "/api/admin/promos", promo)
    assert st == 200, out
    row = db.execute("SELECT code, label, kind, value, min_guests, max_uses, per_phone_limit, exclude_peak, auto, active FROM promo_codes WHERE id = ?", (out["id"],)).fetchone()
    assert row == ("PILGRIM15", "Pilgrim 15%", "percent", 15.0, 6, 50, 2, 1, 0, 1), row  # stored upper-case, spaces removed
    pid = out["id"]
    assert call("manager", "POST", "/api/admin/promos", promo | {"code": "OTHER1"})[0] == 403 and call("frontdesk", "GET", "/api/admin/data")[0] == 403
    assert call("admin", "POST", "/api/admin/promos", promo)[0] == 409
    for bad in (dict(code="a!"), dict(code=""), dict(kind="bogus"), dict(value="101", kind="percent"), dict(value="0"), dict(valid_from=D(10), valid_to=D(5), code="X1X1"), dict(label=""), dict(min_rooms="0", code="Y2Y2")):
        assert call("admin", "POST", "/api/admin/promos", promo | {"code": "ZZZ111"} | bad)[0] == 400, bad
    st, auto = call("admin", "POST", "/api/admin/promos", dict(label="Group rate", kind="flat", value="500", auto=True, min_rooms="3", exclude_peak=False))
    assert st == 200 and db.execute("SELECT code, auto, exclude_peak FROM promo_codes WHERE id = ?", (auto["id"],)).fetchone() == (None, 1, 0)
    st, edit = call("admin", "POST", "/api/admin/promos", promo | {"id": pid, "value": "20"})
    assert st == 200 and db.execute("SELECT value FROM promo_codes WHERE id = ?", (pid,)).fetchone()[0] == 20
    data = call("admin", "GET", "/api/admin/data")[1]
    assert {p["id"] for p in data["promos"]} >= {pid, auto["id"]} and all("uses" in p for p in data["promos"])
    # a guest sees the discount in the quote; deactivating removes it
    guest_q = lambda code: g.dispatch("POST", "/api/public/quote", None, dict(property_id=1, room_type_id=2, check_in=D(30), check_out=D(32), rooms=2, guests=6, promo_code=code), "9.9.9.9")
    st, q = guest_q("pilgrim15")
    assert st == 200 and q["discount"]["code"] == "PILGRIM15" and q["discount"]["amount"] > 0, q
    assert call("admin", "POST", f"/api/admin/promo_codes/{pid}/active", {"active": False})[0] == 200 and guest_q("PILGRIM15")[0] == 400
    assert call("admin", "POST", f"/api/admin/promo_codes/{pid}/active", {"active": True})[0] == 200
    actions = {x[0] for x in db.execute("SELECT action FROM audit_log WHERE action LIKE 'admin.promo%'")}
    assert {"admin.promo_create", "admin.promo_edit", "admin.promo_code_deactivate", "admin.promo_code_activate"} <= actions, actions
    # pricing rules: numbers with at most 2 decimals, empty clears
    ok = lambda **kw: call("admin", "POST", "/api/admin/settings", kw)
    assert ok(tax_percent="12.5", advance_percent="30", extra_bed_price="250", extra_bed_max="2", child_price="100", child_counts_capacity=False)[0] == 200
    now = g.settings(g.connect())
    assert now["tax_percent"] == g.Decimal("12.5") and now["child_counts_capacity"] is False and now["extra_bed_max"] == 2
    for bad in (dict(tax_percent="abc"), dict(tax_percent="101"), dict(tax_percent="1.234"), dict(extra_bed_price="-5"), dict(extra_bed_max="9"), dict(child_counts_capacity="yes")):
        assert ok(**bad)[0] == 400, bad
    assert ok(tax_percent="", advance_percent="", extra_bed_price="", child_price="", extra_bed_max="1", child_counts_capacity=True)[0] == 200
    assert g.settings(g.connect())["tax_percent"] is None and call("manager", "POST", "/api/admin/settings", {"tax_percent": "5"})[0] == 403
    # peak price increase and the senior-citizen flag
    pp = dict(name="Aradhana", start_date=D(400), end_date=D(402), max_nights="", max_rooms="", note="", surcharge_percent="25")
    st, pk = call("admin", "POST", "/api/admin/peak_periods", pp)
    assert st == 200 and db.execute("SELECT surcharge_percent FROM peak_periods WHERE id = ?", (pk["id"],)).fetchone()[0] == 25
    assert call("admin", "POST", "/api/admin/peak_periods", pp | {"surcharge_percent": "501"})[0] == 400 and call("admin", "POST", "/api/admin/peak_periods", pp | {"surcharge_percent": "x"})[0] == 400
    assert call("admin", "POST", "/api/admin/peak_periods", pp | {"id": pk["id"], "surcharge_percent": ""})[0] == 200 and db.execute("SELECT surcharge_percent FROM peak_periods WHERE id = ?", (pk["id"],)).fetchone()[0] is None
    room = call("admin", "GET", "/api/admin/data")[1]["rooms"][0]
    body = {k: room[k] for k in ("property_id", "number", "floor", "room_type_id", "capacity", "amenities", "id")}
    assert call("admin", "POST", "/api/admin/rooms", body | {"senior_friendly": True})[0] == 200 and db.execute("SELECT senior_friendly FROM rooms WHERE id = ?", (room["id"],)).fetchone()[0] == 1
    assert call("admin", "POST", "/api/admin/rooms", body)[0] == 200 and db.execute("SELECT senior_friendly FROM rooms WHERE id = ?", (room["id"],)).fetchone()[0] == 0
    # the dashboard banner: managers and the super admin see open alerts, the front desk does not
    db.execute("DELETE FROM alerts"); db.commit()
    g.BACKUP_MAX_HOURS = 30
    g.BACKUPS = TMP / "no-backups"
    try:
        for who, sees in (("admin", True), ("manager", True), ("frontdesk", False)):
            d = call(who, "GET", "/api/dashboard")[1]
            assert ("alerts" in d) == sees, who
        d = call("admin", "GET", "/api/dashboard")[1]
        assert [a["kind"] for a in d["alerts"]] == ["backup"] and "No backup" in d["alerts"][0]["message"] and d["alerts"][0]["emailed_at"] is None and "STAFF_ALERT_EMAILS" in d["alerts"][0]["email_note"]
    finally:
        g.BACKUP_MAX_HOURS = 0
    db.execute("DELETE FROM alerts"); db.commit()
    # cancelling at the desk tells a guest who was waiting for that room
    conn = g.connect()
    ac = [r["id"] for r in conn.execute("SELECT id FROM rooms WHERE room_type_id = 1 AND property_id = 3 AND active = 1")]
    conn.close()
    db.execute("DELETE FROM bookings WHERE source = 'online'"); db.commit()
    st, bk = g.dispatch("POST", "/api/public/book", None, dict(property_id=3, room_type_id=1, check_in=D(60), check_out=D(61), rooms=1, guests=2, guest_name="Held", phone="9111222333", id_type="Aadhaar", id_number="1", payment_method="pay_at_counter"), "8.8.8.8")
    assert st == 200, bk
    held = db.execute("SELECT room_id FROM bookings WHERE group_id = ?", (bk["booking_id"],)).fetchone()[0]
    for r in ac:
        if r != held:
            db.execute("INSERT INTO room_status_events (room_id, kind, reason, start_date, end_date, active, created_at) VALUES (?, 'internal_block', 't', ?, ?, 1, 'now')", (r, D(60), D(61)))
    db.commit()
    st, w = g.dispatch("POST", "/api/public/waitlist", None, dict(name="Waiter", phone="9444555666", check_in=D(60), check_out=D(61), guests=2, property_id=3, room_type_id=1), "8.8.8.9")
    assert st == 200, w
    g.providers.OUTBOX.clear()
    bid = db.execute("SELECT id FROM bookings WHERE group_id = ?", (bk["booking_id"],)).fetchone()[0]
    assert call("admin", "POST", f"/api/bookings/{bid}/cancel", {"reason": "Guest called"})[0] == 200
    assert [x["to"] for x in g.providers.OUTBOX if x["kind"] == "sms"] == ["9111222333", "9444555666"], g.providers.OUTBOX  # the cancelled booking's guest is told, then the waiting guest
    assert db.execute("SELECT status FROM waitlist WHERE id = ?", (w["id"],)).fetchone()[0] == "notified"
    db.close()


def test_rooms_page():
    """The merged Rooms page: one state per room (GET /api/rooms/board), editing a block, a room's history, the roles, and the old Room status link."""
    # the state rule on its own: a block hides cleaning, cleaning hides the guest; what is underneath comes second
    L, ev = s.room_layers, lambda *kinds: [{"kind": k} for k in kinds]
    assert L([], [], None) == ["available"] and L([], [], {"status": "Confirmed"}) == ["booked"] and L([], [], {"status": "Checked-In"}) == ["checked_in"]
    assert L([], ev("cleaning_required"), None) == ["to_clean", "available"]
    assert L([], ev("cleaning_required", "in_cleaning"), {"status": "Checked-In"}) == ["cleaning", "checked_in"]
    assert L(ev("maintenance"), ev("cleaning_required"), {"status": "Confirmed"}) == ["blocked", "to_clean", "booked"]

    board = lambda user, **q: call(user, "GET", "/api/rooms/board", q)
    at = lambda rid, **q: next(r for r in board("frontdesk", **q)[1]["rooms"] if r["id"] == rid)
    post = lambda user, path, data=None: call(user, "POST", path, data or {})
    # every role reads it, inside its own properties; each room has exactly one of the six states
    assert board("nobody")[0] == 401 and board("frontdesk", property_id=3)[0] == 403 and board("frontdesk", date="soon")[0] == 400
    for u in ("admin", "manager", "frontdesk", "housekeeping", "viewer"):
        st, out = board(u)
        assert st == 200 and out["date"] == out["today"] == D(0) and sum(out["counts"].values()) == len(out["rooms"]) > 0, (u, out)
        assert {r["state"] for r in out["rooms"]} <= {"available", "booked", "checked_in", "to_clean", "cleaning", "blocked"}
    assert {r["property_id"] for r in board("frontdesk")[1]["rooms"]} <= {1, 2}
    # it never disagrees with the two views it replaces
    same = {"available": ("available",), "booked": ("booked",), "occupied": ("checked_in",), "cleaning": ("to_clean", "cleaning"), "blocked": ("blocked",), "maintenance": ("blocked",), "out_of_service": ("blocked",)}
    new = {r["id"]: r for r in board("frontdesk")[1]["rooms"]}
    old = call("frontdesk", "GET", "/api/rooms")[1]
    assert len(old) == len(new) and all(new[r["id"]]["state"] in same[r["status"]] for r in old), [(r["number"], r["status"], new[r["id"]]["state"]) for r in old if new[r["id"]]["state"] not in same[r["status"]]]
    assert all(new[r["id"]]["state"] == r["state"] for r in call("frontdesk", "GET", "/api/housekeeping")[1]["rooms"] if r["state"] in ("blocked", "to_clean", "cleaning"))

    # one room through a stay: booked, checked in, flagged on check-out, being cleaned, available
    room = free_room("frontdesk")
    rid = room["id"]
    assert at(rid)["state"] == "available" and at(rid)["under"] is None
    st, b = post("frontdesk", "/api/bookings", guest(room, arrival_time="23:30"))
    assert st == 200, b
    r = at(rid)
    assert (r["state"], r["guest"], r["booking_id"], r["flag"]) == ("booked", "Walk In", b["id"], False) and r["arrival"]["guest"] == "Walk In", r
    assert post("frontdesk", f"/api/bookings/{b['id']}/checkin")[0] == 200 and at(rid)["state"] == "checked_in" and at(rid)["arrival"] is None
    assert post("viewer", f"/api/rooms/{rid}/needs_cleaning")[0] == 403, "the viewer changes nothing"
    assert post("housekeeping", f"/api/rooms/{rid}/needs_cleaning")[0] == 200
    r = at(rid)
    assert (r["state"], r["under"], r["kind"], r["guest"]) == ("to_clean", "checked_in", "cleaning_required", "Walk In"), r
    assert post("housekeeping", f"/api/room_events/{r['event_id']}/release")[0] == 200 and at(rid)["state"] == "checked_in"
    assert post("frontdesk", f"/api/bookings/{b['id']}/checkout")[0] == 200
    r = at(rid)
    assert (r["state"], r["under"], r["guest"]) == ("to_clean", "available", None), "check-out flags the room by itself"
    assert at(rid, date=D(3))["state"] == "available", "cleaning describes today: another day shows the room as it will be"
    assert "to_clean" not in board("frontdesk", date=D(3))[1]["counts"] and "cleaning" not in board("frontdesk", date=D(3))[1]["counts"]
    assert post("housekeeping", f"/api/room_events/{r['event_id']}/start_cleaning")[0] == 200 and at(rid)["state"] == "cleaning"
    assert post("housekeeping", f"/api/room_events/{r['event_id']}/release")[0] == 200
    assert at(rid)["state"] == "available" and at(rid)["ready_since"], "marked ready from the Rooms page"

    # a block: shown with kind, category, reason and dates; only on the days it covers
    blk = dict(room_id=rid, kind="maintenance", issue_category="Plumbing", reason="Tap", start_date=D(0), end_date=D(2))
    st, e = post("frontdesk", "/api/room_events", blk)
    assert st == 200, e
    r = at(rid)
    assert (r["state"], r["kind"], r["category"], r["reason"], r["since"], r["until"], r["under"]) == ("blocked", "maintenance", "Plumbing", "Tap", D(0), D(2), "available"), r
    assert at(rid, date=D(2))["state"] == "available"
    assert post("housekeeping", f"/api/rooms/{rid}/needs_cleaning")[0] == 409, "a room blocked today is not flagged for cleaning"

    # editing it: the roles that may block, the same checks as a new block, an audit entry
    edit = lambda user, **kw: post(user, f"/api/room_events/{e['id']}/edit", dict(kind="maintenance", issue_category="Plumbing", reason="Tap", end_date=D(2)) | kw)
    assert edit("viewer")[0] == 403 and edit("housekeeping")[0] == 403 and post("frontdesk", "/api/room_events/999999/edit", blk)[0] == 404
    for patch in (dict(kind="cleaning_required"), dict(issue_category="Magic"), dict(end_date=D(0)), dict(end_date=None), dict(reason=" "), dict(reason="x" * 301)):
        assert edit("frontdesk", **patch)[0] == 400, patch
    st, later = post("frontdesk", "/api/bookings", guest(room, check_in=D(4), check_out=D(5)))
    assert st == 200, later
    st, out = edit("frontdesk", end_date=D(6))
    assert st == 409 and "booking" in out["error"], "a block cannot be stretched over a booking"
    assert post("frontdesk", f"/api/bookings/{later['id']}/cancel", dict(reason="test"))[0] == 200
    assert edit("frontdesk", kind="out_of_service", issue_category="Electrical", reason="Wiring", end_date=None)[0] in (200, 409)  # open-ended: refused when the room has a booking later on live data
    assert edit("frontdesk", kind="out_of_service", issue_category="Electrical", reason="Wiring", end_date=D(8))[0] == 200
    r = at(rid, date=D(7))
    assert (r["state"], r["kind"], r["category"], r["reason"], r["since"], r["until"]) == ("blocked", "out_of_service", "Electrical", "Wiring", D(0), D(8)), r
    assert edit("manager", kind="internal_block", issue_category="", reason="Mutt use", end_date=D(1))[0] == 200 and at(rid)["category"] is None
    other = free_room("admin", pid=3)
    st, far = post("admin", "/api/room_events", blk | dict(room_id=other["id"]))
    assert st == 200, far
    assert post("frontdesk", f"/api/room_events/{far['id']}/edit", blk)[0] == 404, "another property's block"
    assert post("housekeeping", f"/api/room_events/{e['id']}/release")[0] == 403 and post("frontdesk", f"/api/room_events/{e['id']}/release")[0] == 200
    assert post("housekeeping", f"/api/rooms/{rid}/needs_cleaning")[0] == 200
    cid = at(rid)["event_id"]
    assert post("frontdesk", f"/api/room_events/{cid}/edit", blk)[0] == 409, "a cleaning task is not edited"
    assert post("housekeeping", f"/api/room_events/{cid}/release")[0] == 200

    # a block that ended but was never cleared is not a status today: the board shows the room as usable, so it can be flagged
    db = sqlite3.connect(g.DB)
    db.execute("INSERT INTO room_status_events (room_id, kind, reason, start_date, end_date, created_at) VALUES (?, 'internal_block', 'Old', ?, ?, ?)", (rid, D(-5), D(-1), s.now_iso()))
    db.commit()
    db.close()
    assert at(rid)["state"] == "available" and post("housekeeping", f"/api/rooms/{rid}/needs_cleaning")[0] == 200
    assert post("housekeeping", f"/api/room_events/{at(rid)['event_id']}/release")[0] == 200

    # history: every role, own properties only, room.* entries only, and nothing but who, what and when
    assert call("nobody", "GET", f"/api/rooms/{rid}/history")[0] == 401 and call("frontdesk", "GET", f"/api/rooms/{other['id']}/history")[0] == 404 and call("frontdesk", "GET", "/api/rooms/999999/history")[0] == 404
    for u in ("manager", "frontdesk", "housekeeping", "viewer"):
        st, rows = call(u, "GET", f"/api/rooms/{rid}/history")
        assert st == 200 and 0 < len(rows) <= 8 and all(set(x) == {"ts", "username", "action"} and x["action"].startswith("room.") for x in rows), (u, rows)
    assert [x["action"] for x in rows][:3] == ["room.cleaning_done", "room.cleaning_required", "room.cleaning_done"] and rows[0]["username"] == "housekeeping", rows
    log = [a for a in call("manager", "GET", "/api/audit")[1] if a["room_id"] == rid]
    assert {"booking.create", "booking.checkin"} <= {a["action"] for a in log}, "the room's booking entries exist in the audit log, and stay out of its history"
    edits = [a for a in log if a["action"] == "room.block_edit"]
    assert len(edits) == 2 and edits[-1]["username"] == "frontdesk" and edits[-1]["old_value"] == f"maintenance to {D(2)}" and edits[-1]["new_value"] == f"out_of_service to {D(8)}" and edits[-1]["reason"] == "Wiring", edits
    assert post("admin", f"/api/room_events/{far['id']}/release")[0] == 200

    # menu and old links: Room status is gone for every role, Rooms stays for everyone, #/status opens Rooms
    for u in ("admin", "manager", "frontdesk", "housekeeping", "viewer"):
        keys = [p["key"] for p in call(u, "GET", "/api/meta")[1]["pages"]]
        assert "status" not in keys and keys.count("rooms") == 1, (u, keys)
    assert not any(k == "status" for k, _, _ in s.PAGES)
    html = (Path(__file__).resolve().parent.parent / "web" / "staff.html").read_text(encoding="utf-8")  # the redirect runs in the browser: check that the page still carries it
    assert 'const MOVED = { status: "rooms" };' in html and 'if (MOVED[name]) { location.replace("#/" + MOVED[name]); return; }' in html
    assert "statusView" not in html and '"Room status"' not in html and html.count("async function roomsView(") == 1 and 'rooms: ["Rooms", roomsView]' in html
    assert "/api/rooms/board?" in html and "hk-board" not in html[html.index("async function housekeepingView("):html.index("// ---------- rooms:")], "Housekeeping keeps the team panel only"


def test_staff_dashboard():
    """GET /api/staff/dashboard: role-shaped output, property scope, availability, money totals, sources."""
    dash = lambda user, **q: call(user, "GET", "/api/staff/dashboard", q)
    money_keys = {"month", "cash_today", "revenue_series"}
    ops_keys = {"today", "occupancy", "availability", "can_act"}
    assert call("nobody", "GET", "/api/staff/dashboard")[0] == 401
    outs = {}
    for u in ("admin", "manager", "frontdesk", "housekeeping", "viewer"):
        st, outs[u] = dash(u)
        assert st == 200 and ops_keys <= set(outs[u]), (u, st)
    # who sees what: money for managers and super admin only, guest lists for roles that may see bookings
    for u in ("admin", "manager"):
        assert money_keys <= set(outs[u]), u
    for u in ("frontdesk", "housekeeping", "viewer"):
        assert not money_keys & set(outs[u]), (u, "money must be absent, not zeroed")
    assert {"arrivals", "departures", "recent", "sources"} <= set(outs["frontdesk"]) and {"arrivals", "departures"} <= set(outs["viewer"])
    assert not {"arrivals", "departures", "recent", "sources"} & set(outs["housekeeping"]), "housekeeping sees no guest data"
    assert not any("setup" in outs[u] for u in outs), "the setup checklist is gone"
    assert outs["viewer"]["can_act"] is False and outs["frontdesk"]["can_act"] is True
    assert "alerts" in outs["admin"] and "alerts" in outs["manager"] and "backup" in outs["admin"]
    assert not any(k in outs[u] for u in ("frontdesk", "housekeeping", "viewer") for k in ("alerts", "backup")) and "backup" not in outs["manager"]
    assert {"/i18n.js", "/locales/en.json"} <= set(s.STATIC) and not any(p.startswith("/locales/") and p != "/locales/en.json" for p in s.STATIC)
    assert len(outs["manager"]["revenue_series"]) == 30 and outs["manager"]["revenue_series"][-1]["date"] == TODAY.isoformat()
    # property scope: front desk has properties 1 and 2 only
    assert dash("frontdesk", property_id=3)[0] == 403 and set(outs["frontdesk"]["property_ids"]) == {1, 2}
    assert dash("frontdesk", property_id=1)[1]["property_ids"] == [1]
    assert {r["property_id"] for r in outs["frontdesk"]["availability"]["rooms"]} <= {1, 2}
    # date range rules
    for q in (dict(**{"from": D(2), "to": D(2)}), dict(**{"from": D(2), "to": D(1)}), dict(**{"from": D(0), "to": D(40)}), dict(**{"from": "x"})):
        assert dash("frontdesk", **q)[0] == 400, q
    av = outs["frontdesk"]["availability"]
    assert av["nights"] == 1 and av["available"] + av["occupied"] + av["blocked"] == av["total"] == len(av["rooms"])
    assert sum(x["total"] for x in av["by_type"]) == av["total"] and sum(x["available"] for x in av["by_type"]) == av["available"]

    # a walk-in booking: availability drops by one, arrivals, sources and dues move; the double booking stays refused
    room = free_room("frontdesk", pid=1)
    nights = 2
    before = dash("manager", property_id=1, **{"from": D(0), "to": D(nights)})[1]
    st, b = call("frontdesk", "POST", "/api/bookings", guest(room, check_in=D(0), check_out=D(nights)))
    assert st == 200, b
    assert call("frontdesk", "POST", "/api/bookings", guest(room, check_in=D(1), check_out=D(3)))[0] == 409
    after = dash("manager", property_id=1, **{"from": D(0), "to": D(nights)})[1]
    assert after["availability"]["available"] == before["availability"]["available"] - 1 and after["availability"]["occupied"] == before["availability"]["occupied"] + 1
    assert next(r for r in after["availability"]["rooms"] if r["id"] == room["id"])["state"] == "occupied"
    assert after["arrivals"]["count"] == before["arrivals"]["count"] + 1 and after["arrivals"]["checked_in"] == before["arrivals"]["checked_in"]
    walk = lambda o: next((x["count"] for x in o["sources"]["items"] if x["source"] == "walk_in"), 0)
    assert walk(after) == walk(before) + 1
    assert after["month"]["outstanding_bookings"] == before["month"]["outstanding_bookings"] + 1 and after["month"]["outstanding"] > before["month"]["outstanding"]
    assert after["month"]["expenses"] == 0 and after["month"]["profit"] == 0 and after["month"]["expenses_module"] is False
    # check-in moves the arrivals card; occupancy tonight counts the room either way
    assert call("frontdesk", "POST", f"/api/bookings/{b['id']}/checkin")[0] == 200
    ci = dash("frontdesk", property_id=1)[1]
    assert ci["arrivals"]["checked_in"] == before["arrivals"]["checked_in"] + 1 and ci["occupancy"]["occupied"] >= 1
    assert all(r["status"] in ("Confirmed", "Checked-In", "Checked-Out") for r in ci["arrivals"]["list"]) and {"id", "guest_name", "room"} <= set(ci["arrivals"]["list"][0])

    # money: a counter payment raises cash today and the month; a processed refund comes off both; dues fall as the booking is paid
    pay = lambda amt: call("admin", "POST", f"/api/bookings/{b['id']}/payment", dict(amount=amt, mode="cash"))
    cash0, m0 = after["cash_today"], after["month"]
    assert pay(500)[0] == 200
    m1 = dash("manager", property_id=1)[1]
    assert round(m1["cash_today"] - cash0, 2) == 500 and round(m1["month"]["net_revenue"] - m0["net_revenue"], 2) == 500
    assert round(m0["outstanding"] - m1["month"]["outstanding"], 2) == 500, "dues fall by what was paid"
    assert m1["revenue_series"][-1]["collected"] >= 500
    db = sqlite3.connect(g.DB)
    prow = db.execute("SELECT id FROM payments WHERE group_id = ? ORDER BY id DESC", (b["id"],)).fetchone()[0]
    ts = g.now_iso()
    db.execute("INSERT INTO refunds (payment_row, group_id, amount_paise, status, created_at, updated_at) VALUES (?,?,20000,'processed',?,?)", (prow, b["id"], ts, ts))
    db.execute("INSERT INTO refunds (payment_row, group_id, amount_paise, status, created_at, updated_at) VALUES (?,?,99900,'failed',?,?)", (prow, b["id"], ts, ts))  # a failed refund moved no money
    db.commit(); db.close()
    m2 = dash("manager", property_id=1)[1]
    assert round(m1["cash_today"] - m2["cash_today"], 2) == 200 and round(m1["month"]["net_revenue"] - m2["month"]["net_revenue"], 2) == 200
    assert m2["revenue_series"][-1]["refunded"] >= 200
    # property scope for money: a manager of properties 1,2 sees none of property 3's payments
    assert dash("manager", property_id=3)[0] in (200, 403)
    for u in ("frontdesk", "viewer", "housekeeping"):
        assert "cash_today" not in dash(u, property_id=1)[1]

    # a guest still checked in after their check-out date keeps the room occupied for any range that includes today, and does not for a later range
    late = free_room("frontdesk", pid=1, skip=(room["id"],))
    db = sqlite3.connect(g.DB)
    db.execute("INSERT INTO bookings (property_id, room_id, guest_name, phone, id_type, id_number, guests, check_in, check_out, status, source, created_at, status_at, amount) VALUES (1,?,'Late Leaver','9876500000','Aadhaar','****1',1,?,?,'Checked-In','front_desk',?,?,0)",
               (late["id"], D(-5), D(-3), g.now_iso(), g.now_iso()))
    db.commit(); db.close()
    state = lambda **q: next(r["state"] for r in dash("admin", property_id=1, **q)[1]["availability"]["rooms"] if r["id"] == late["id"])
    assert state(**{"from": D(0), "to": D(1)}) == "occupied" and state(**{"from": D(-1), "to": D(2)}) == "occupied"
    assert state(**{"from": D(5), "to": D(6)}) == "available"
    a2 = dash("admin", property_id=1, **{"from": D(0), "to": D(1)})[1]["availability"]
    assert a2["available"] + a2["occupied"] + a2["blocked"] == a2["total"]

def test_dashboard_recent_noshow():
    """The stored status is 'No-Show' (capital S). The dashboard's recent list must include such a booking: it once filtered on 'No-show', which matches nothing."""
    room = free_room("frontdesk", pid=1)
    db = sqlite3.connect(g.DB)
    assert db.execute("SELECT COUNT(*) FROM bookings WHERE status = 'No-show'").fetchone()[0] == 0, "the only spelling in the data is No-Show"
    cur = db.execute("INSERT INTO bookings (property_id, room_id, guest_name, phone, id_type, id_number, guests, check_in, check_out, status, source, created_at, status_at, amount) VALUES (1,?,'No Show Guest','9876500001','Aadhaar','****1',1,?,?,'No-Show','front_desk',?,?,0)",
                     (room["id"], D(-1), D(0), g.now_iso(), g.now_iso()))
    db.commit(); db.close()
    rec = call("frontdesk", "GET", "/api/staff/dashboard", dict(property_id=1))[1]["recent"]
    assert cur.lastrowid in [b["id"] for b in rec], "a No-Show booking must appear in recent bookings"
    assert next(b for b in rec if b["id"] == cur.lastrowid)["status"] == "No-Show"

def test_reservations_list():
    """GET /api/bookings (paged shape): tab counts and filtering, the five sorts, pagination, both response shapes, roles and scope, a hostile sort value, and /api/bookings/csv."""
    import csv, io
    db = sqlite3.connect(g.DB)
    rooms1 = [r[0] for r in db.execute("SELECT id FROM rooms WHERE property_id = 1 AND active = 1 ORDER BY id")]
    room3 = db.execute("SELECT id FROM rooms WHERE property_id = 3 AND active = 1 ORDER BY id").fetchone()[0]
    statuses = ["Confirmed"] * 4 + ["Checked-In"] * 3 + ["Checked-Out"] * 2 + ["Cancelled"] * 2 + ["No-Show"]
    fx = []  # the fixtures: 12 rows of property 1 whose check-in, check-out and booking date all order differently; rows 0 and 1 tie on check-in
    for i, st in enumerate(statuses):
        off = 0 if i == 1 else i * 3
        ci, co = D(200 + off), D(200 + off + 1 + (11 - i) % 4)
        created = f"2026-{1 + (i * 5) % 9:02d}-{10 + i:02d}T09:00:00"
        cur = db.execute("INSERT INTO bookings (property_id, room_id, guest_name, phone, id_type, id_number, guests, check_in, check_out, status, source, created_at, status_at, amount, payment_status) "
                         "VALUES (1,?,?,?,'Aadhaar','****1234',2,?,?,?,'front_desk',?,?,?,?)", (rooms1[i % len(rooms1)], f"ZZRes {i:02d}", f"98000000{i:02d}", ci, co, st, created, created, 600 * (1 + i % 3), "paid" if i % 2 else "due"))
        fx.append(dict(id=cur.lastrowid, status=st, check_in=ci, check_out=co, created=created))
    other = db.execute("INSERT INTO bookings (property_id, room_id, guest_name, phone, id_type, id_number, guests, check_in, check_out, status, source, created_at, status_at, amount) "
                       "VALUES (3,?,'ZZRes Other Property','9800000099','Aadhaar','****1',1,?,?,'Confirmed','front_desk',?,?,0)", (room3, D(210), D(211), g.now_iso(), g.now_iso())).lastrowid
    db.commit()
    api = lambda user, **q: call(user, "GET", "/api/bookings", q)
    mine = {"q": "ZZRes", "property_id": 1}
    ids = lambda items: [b["id"] for b in items]
    SORT_KEYS = ("checkin_desc", "checkin_asc", "checkout_desc", "checkout_asc", "booked_desc")

    # tab counts: five tabs plus No-Show, under the search; "all" holds everything, No-Show included
    st, out = api("frontdesk", page_size=100, **mine)
    assert st == 200 and set(out) == {"items", "total", "page", "page_size", "pages", "sort", "counts"}, out.keys()
    assert out["counts"] == {"all": 12, "checked_in": 3, "checked_out": 2, "confirmed": 4, "cancelled": 2, "no_show": 1}, out["counts"]
    assert out["counts"]["all"] == sum(v for k, v in out["counts"].items() if k != "all") and out["total"] == 12 and out["sort"] == "checkin_desc"
    # tab filtering: each status shows only itself, its total is its count, the counts do not move, and No-Show is only ever in "all"
    for key, status in (("checked_in", "Checked-In"), ("checked_out", "Checked-Out"), ("confirmed", "Confirmed"), ("cancelled", "Cancelled"), ("no_show", "No-Show")):
        st, o = api("frontdesk", status=status, page_size=100, **mine)
        assert st == 200 and {b["status"] for b in o["items"]} == {status} and o["total"] == out["counts"][key] == len(o["items"]) and o["counts"] == out["counts"], status
    assert "No-Show" in {b["status"] for b in out["items"]} and len(out["items"]) == 12
    # counts follow the date and search filters
    lo, hi = D(200), D(209)
    st, o = api("frontdesk", page_size=100, **mine, **{"from": lo, "to": hi})
    want = [f for f in fx if lo <= f["check_in"] <= hi]
    assert st == 200 and sorted(ids(o["items"])) == sorted(f["id"] for f in want) and o["counts"]["all"] == len(want)
    assert o["counts"]["confirmed"] == sum(f["status"] == "Confirmed" for f in want) and o["counts"]["no_show"] == sum(f["status"] == "No-Show" for f in want)
    assert api("frontdesk", page_size=100, **mine, **{"from": hi, "to": lo})[0] == 400 and api("frontdesk", page_size=100, **mine, **{"from": "nope"})[0] == 400
    o = api("frontdesk", page_size=100, q="ZZRes 03", property_id=1)[1]
    assert o["counts"]["all"] == 1 and o["items"][0]["guest_name"] == "ZZRes 03"
    assert api("frontdesk", page_size=100, q=str(fx[4]["id"]), property_id=1)[1]["counts"]["all"] == 1, "search by booking id"
    assert api("frontdesk", page_size=100, q="9800000005", property_id=1)[1]["counts"]["all"] == 1, "search by phone"
    assert api("frontdesk", page_size=100, date=fx[5]["check_in"], **mine)[1]["counts"]["all"] >= 1, "the old staying-on date still works"

    # each sort, against the order computed here; ties on the key fall back to the booking id, in the same direction
    key = lambda f, k: {"checkin": f["check_in"], "checkout": f["check_out"], "booked": f["created"]}[k]
    for sort in SORT_KEYS:
        k, rev = sort.split("_")[0], sort.endswith("desc")
        want = [f["id"] for f in sorted(fx, key=lambda f: (key(f, k), f["id"]), reverse=rev)]
        st, o = api("frontdesk", sort=sort, page_size=100, **mine)
        assert st == 200 and ids(o["items"]) == want and o["sort"] == sort, (sort, ids(o["items"]), want)
    # pagination keeps the sort: three pages of 5 rebuild the whole sorted list with no repeats or gaps
    for sort in SORT_KEYS:
        full = ids(api("frontdesk", sort=sort, page_size=100, **mine)[1]["items"])
        parts = [api("frontdesk", sort=sort, page=n, page_size=5, **mine)[1] for n in (1, 2, 3)]
        assert [len(x["items"]) for x in parts] == [5, 5, 2] and sum((ids(x["items"]) for x in parts), []) == full and {x["pages"] for x in parts} == {3} and parts[0]["total"] == 12, sort
    assert api("frontdesk", page=4, page_size=5, **mine)[1]["items"] == []
    # the tab and the page together: page 2 of the Confirmed tab
    o = api("frontdesk", status="Confirmed", page=2, page_size=3, **mine)[1]
    assert o["total"] == 4 and len(o["items"]) == 1 and o["pages"] == 2
    for bad in (dict(page_size=101), dict(page_size=0), dict(page=0), dict(page="x"), dict(page_size="x")):
        assert api("frontdesk", **mine, **bad)[0] == 400, bad

    # a hostile or unknown sort is refused and never reaches SQL; the table is intact
    n = db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    for bad in ("nope", "b.id; DROP TABLE bookings; --", "checkin_desc, (SELECT 1)", "CHECKIN_DESC", "b.check_in"):
        for path in ("/api/bookings", "/api/bookings/csv"):
            st, o = call("frontdesk", "GET", path, dict(sort=bad, page_size=5))
            assert st == 400 and "Unknown sort" in o["error"], (path, bad, st)
    assert db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == n

    # both response shapes: no paging parameters keeps the old plain list; page_size, page or counts gives the object; the old filters work in both
    old = api("frontdesk", **mine)[1]
    assert isinstance(old, list) and len(old) == 12 and all("guest_name" in b for b in old) and ids(old) == ids(api("frontdesk", sort="checkin_desc", page_size=100, **mine)[1]["items"])
    assert ids(api("frontdesk", sort="booked_desc", **mine)[1]) == [f["id"] for f in sorted(fx, key=lambda f: (f["created"], f["id"]), reverse=True)], "sort also applies to the plain list"
    assert [b["status"] for b in api("frontdesk", status="Cancelled", **mine)[1]] == ["Cancelled"] * 2
    assert isinstance(api("frontdesk", counts=1, **mine)[1], dict) and isinstance(api("frontdesk", page=1, **mine)[1], dict) and isinstance(api("frontdesk", page_size=5, **mine)[1], dict)
    assert api("frontdesk", counts=1, **mine)[1]["page_size"] == 20, "default page size"

    # what a row carries: room type, nights and payment state; only the ID tail
    b = next(x for x in api("frontdesk", page_size=100, **mine)[1]["items"] if x["id"] == fx[0]["id"])
    assert b["room_type"] and b["nights"] == (date_.fromisoformat(b["check_out"]) - date_.fromisoformat(b["check_in"])).days and b["payment_status"] == "due" and b["id_tail"] == "…1234" and "id_number" not in b
    assert b["amount"] == 600 and {"room", "property", "phone", "guests", "status", "created_at"} <= set(b)

    # roles and scope
    assert api("housekeeping", page_size=5)[0] == 403 and api("housekeeping")[0] == 403 and call("nobody", "GET", "/api/bookings", dict(page_size=5))[0] == 401
    assert api("viewer", page_size=5, **mine)[0] == 200 and api("manager", page_size=5, **mine)[0] == 200
    assert api("frontdesk", property_id=3, page_size=5)[0] == 403
    fd = api("frontdesk", page_size=100, q="ZZRes")[1]
    assert other not in ids(fd["items"]) and fd["counts"]["all"] == 12 and {b["property_id"] for b in fd["items"]} <= {1, 2}, "front desk never sees property 3"
    ad = api("admin", page_size=100, q="ZZRes")[1]
    assert other in ids(ad["items"]) and ad["counts"]["all"] == 13 and ad["counts"]["confirmed"] == 5

    # the export: managers and front desk, not viewers or housekeeping; same filters and sort, every page, audited, formula-safe
    for u, want in (("manager", 200), ("frontdesk", 200), ("admin", 200), ("viewer", 403), ("housekeeping", 403)):
        assert call(u, "GET", "/api/bookings/csv", dict(q="ZZRes", property_id=1))[0] == want, u
    assert call("nobody", "GET", "/api/bookings/csv")[0] == 401 and call("frontdesk", "GET", "/api/bookings/csv", dict(property_id=3))[0] == 403
    st, out = call("frontdesk", "GET", "/api/bookings/csv", dict(q="ZZRes", property_id=1, status="Cancelled", sort="checkin_asc"))
    assert st == 200 and out["_ctype"] == "text/csv" and out["_filename"] == f"reservations-{TODAY.isoformat()}.csv" and out["_bytes"].startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(out["_bytes"].decode("utf-8-sig"))))
    assert rows[0][:3] == ["Booking ID", "Guest", "Mobile"] and len(rows) == 3 and {r[12] for r in rows[1:]} == {"Cancelled"} and not any("ID proof" in c or "Aadhaar" in c for r in rows for c in r)
    full = list(csv.reader(io.StringIO(call("manager", "GET", "/api/bookings/csv", dict(q="ZZRes", property_id=1))[1]["_bytes"].decode("utf-8-sig"))))
    assert len(full) == 13 and [int(r[0]) for r in full[1:]] == ids(old), "the export is every row, in the page's default order, not one page"
    log = db.execute("SELECT username, new_value FROM audit_log WHERE action = 'booking.export' ORDER BY id DESC LIMIT 1").fetchone()
    assert log[0] == "manager" and "12 booking(s)" in log[1] and "q=ZZRes" in log[1]
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'booking.export'").fetchone()[0] == 5, "every successful download is logged, refusals are not"
    assert call("frontdesk", "GET", "/api/meta")[1]["can"]["booking_export"] is True and call("viewer", "GET", "/api/meta")[1]["can"]["booking_export"] is False
    db.close()


if __name__ == "__main__":
    try:
        test_all()
        test_admin()
        test_site_config_admin()
        test_content_admin()
        test_backup_status()
        test_alternatives_scope()
        test_callbacks()
        test_quick_callback_in_queue()  # after test_callbacks, which counts the audit rows (the audit log is append-only)
        test_arrival_details()
        test_hold_blocks_walkin()
        test_peak_admin()
        test_staff_features()
        test_rooms_page()
        test_staff_dashboard()
        test_dashboard_recent_noshow()
        test_reservations_list()
        print("ok")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
