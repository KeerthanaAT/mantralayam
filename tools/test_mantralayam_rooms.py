"""Self-check for the guest booking API. Runs against a COPY of the real database.
Run: python tools/test_mantralayam_rooms.py   (or pytest)
"""
import contextlib
import io
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import mantralayam_rooms as m

SRC = Path(__file__).resolve().parent.parent / ".tmp" / "mantralayam.db"
TMP = Path(tempfile.mkdtemp())
m.DB = TMP / "copy.db"
m.OTP_REQUIRED = False  # the OTP flow has its own tests (test_mantralayam_features.py); here bookings go straight through
m.BACKUP_MAX_HOURS = 0  # backup recency is tested separately
shutil.copy(SRC, m.DB)
D = lambda n: (m.today() + timedelta(days=n)).isoformat()
CI, CO = D(30), D(32)
N = [0]


def real_online_count():
    s = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    try:
        return s.execute("SELECT COUNT(*) FROM bookings WHERE source='online'").fetchone()[0]
    finally:
        s.close()


REAL_ONLINE = real_online_count()


def call(method, path, data=None):
    m.reset_throttle()
    q, body = (data, None) if method == "GET" else (None, data)
    return m.dispatch(method, path, q, body, "127.0.0.1")


def guest(**kw):
    N[0] += 1
    return dict(guest_name="Test Guest", phone=f"98765{N[0]:05d}", id_type="Aadhaar", id_number="1234", payment_method="pay_at_counter",
                check_in=CI, check_out=CO, rooms=1, guests=2, **kw)


def pick(guests=2):
    st, r = call("GET", "/api/public/search", dict(check_in=CI, check_out=CO, guests=guests))
    assert st == 200 and r["nights"] == 2, r
    p = r["properties"][0]
    return p["property_id"], p["types"][0]


def test_health_and_backup():
    assert call("GET", "/health", {}) == (200, {"status": "ok", "checks": {"database": "ok", "backup": "disabled"}})
    m.BACKUPS = TMP / "backups"
    (m.BACKUPS).mkdir()
    for d in range(1, 17):  # 16 older backups, plus files that rotation must never touch
        (m.BACKUPS / f"mantralayam-2026-01-{d:02d}-0300.db").write_bytes(b"old")
    (m.BACKUPS / "notes.txt").write_text("keep me")
    dest = m.backup()
    names = sorted(p.name for p in m.BACKUPS.glob(m.BACKUP_GLOB))
    assert re.fullmatch(r"mantralayam-\d{4}-\d\d-\d\d-\d{4}\.db", dest.name) and dest.name == names[-1], dest
    assert len(names) == 14 and names[0] == "mantralayam-2026-01-04-0300.db", names  # exactly the newest 14 remain
    assert (m.BACKUPS / "notes.txt").exists() and not list(m.BACKUPS.glob("*.part"))
    c = sqlite3.connect(dest)
    assert c.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert c.execute("SELECT COUNT(*) FROM bookings").fetchone() == sqlite3.connect(m.DB).execute("SELECT COUNT(*) FROM bookings").fetchone()
    c.close()
    last = m.last_backup()
    assert last["ok"] and last["detail"] == dest.name, last

    # a failure is reported, logged, never rotates, and the command exits non-zero with a message
    real_db, m.DB = m.DB, TMP / "missing.db"
    try:
        try:
            m.backup()
            raise AssertionError("backup of a missing database must fail")
        except m.BackupError as e:
            assert "not found" in str(e)
        assert len(list(m.BACKUPS.glob(m.BACKUP_GLOB))) == 14 and m.last_backup()["ok"] is False
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert m.main(["backup"]) == 1
        assert "Backup FAILED" in err.getvalue()
    finally:
        m.DB = real_db
    with contextlib.redirect_stdout(io.StringIO()):
        assert m.main(["backup"]) == 0 and m.last_backup()["ok"]  # same minute replaces the file, still 14
    assert len(list(m.BACKUPS.glob(m.BACKUP_GLOB))) == 14


def test_all():
    st, i = call("GET", "/api/public/info")
    assert st == 200 and i["settings"]["guest_booking_enabled"] and i["properties"] and i["room_types"]

    for bad, msg in [(dict(check_in=D(-1), check_out=D(1)), "past"), (dict(check_in=CI, check_out=CI), "after check-in"),
                     (dict(check_in=D(200), check_out=D(201)), "days ahead"), (dict(check_in=CI, check_out=D(50)), "at most"),
                     (dict(check_in="nope", check_out=CO), "valid date")]:
        st, r = call("GET", "/api/public/search", dict(bad, guests=2))
        assert st == 400 and msg in r["error"], (bad, r)

    pid, t = pick()
    assert t["tariff"] * 2 == t["total_per_room"] and t["can_book"] >= 1

    # book one room, then look it up, then the same room is gone from search
    free_before = len(_free(pid, t))
    st, b = call("POST", "/api/public/book", guest(property_id=pid, room_type_id=t["room_type_id"]))
    assert st == 200 and b["status"] == "Confirmed" and b["amount"] == t["total_per_room"] and b["can_cancel"], b
    assert len(_free(pid, t)) == free_before - 1

    phone = b["mobile"]
    assert call("POST", "/api/public/booking/lookup", dict(booking_id=b["booking_id"], phone="+91" + phone[-10:]))[0] == 200
    assert call("POST", "/api/public/booking/lookup", dict(booking_id=b["booking_id"], phone="9000000000"))[0] == 404

    # validation
    for patch, msg in [(dict(phone="123"), "10 to 13"), (dict(id_type="Library card"), "ID proof"), (dict(payment_method="card"), "payment"),
                       (dict(email="nope"), "email"), (dict(guests=0), "guests"), (dict(rooms=9), "rooms")]:
        st, r = call("POST", "/api/public/book", guest(property_id=pid, room_type_id=t["room_type_id"]) | patch)
        assert st == 400 and msg.lower() in r["error"].lower(), (patch, r)

    # cancel: reason required, works once, frees the room, can't repeat
    assert call("POST", "/api/public/booking/cancel", dict(booking_id=b["booking_id"], phone=phone, reason=""))[0] == 400
    st, c = call("POST", "/api/public/booking/cancel", dict(booking_id=b["booking_id"], phone=phone, reason="Plans changed"))
    assert st == 200 and c["status"] == "Cancelled" and not c["can_cancel"], c
    assert len(_free(pid, t)) == free_before
    assert call("POST", "/api/public/booking/cancel", dict(booking_id=b["booking_id"], phone=phone, reason="again"))[0] == 409

    # group booking: 2 rooms, 3 guests spread 2 + 1, one booking id
    st, g = call("POST", "/api/public/book", guest(property_id=pid, room_type_id=t["room_type_id"], ) | dict(rooms=2, guests=3))
    assert st == 200 and g["rooms"] == 2 and g["guests"] == 3 and g["amount"] == 2 * t["total_per_room"], g
    db = sqlite3.connect(m.DB)
    assert sorted(x[0] for x in db.execute("SELECT guests FROM bookings WHERE group_id=?", (g["booking_id"],))) == [1, 2]

    # no double booking: keep booking single rooms until the type sells out
    left, done = len(_free(pid, t)), 0
    while True:
        st, r = call("POST", "/api/public/book", guest(property_id=pid, room_type_id=t["room_type_id"]))
        if st != 200:
            assert st == 409 and "no longer available" in r["error"], r
            break
        done += 1
        assert done <= left
    assert done == left
    assert db.execute("SELECT COUNT(*) FROM (SELECT room_id FROM bookings WHERE status='Confirmed' AND check_in=? GROUP BY room_id HAVING COUNT(*)>1)", (CI,)).fetchone()[0] == 0

    # cancel cutoff: with a huge cutoff the booking can no longer be cancelled online
    pid2, t2 = pick()
    st, b2 = call("POST", "/api/public/book", guest(property_id=pid2, room_type_id=t2["room_type_id"]))
    assert st == 200
    db.execute("UPDATE settings SET value='1000000' WHERE key='guest_cancel_cutoff_hours'"); db.commit()
    st, r = call("POST", "/api/public/booking/cancel", dict(booking_id=b2["booking_id"], phone=b2["mobile"], reason="late"))
    assert st == 409 and "cancellation closed" in r["error"], r

    # online booking switched off
    db.execute("UPDATE settings SET value='0' WHERE key='guest_booking_enabled'"); db.commit()
    assert call("GET", "/api/public/search", dict(check_in=CI, check_out=CO, guests=2))[0] == 403

    # audit trail written, original database untouched
    assert {r[0] for r in db.execute("SELECT action FROM audit_log")} >= {"booking.online_create", "booking.online_cancel"}
    db.close()
    assert real_online_count() == REAL_ONLINE  # tests must never write to the real database


def block_rooms(db, a, b, where="1=1"):
    """Test helper: every active room (matching `where`) blocked for [D(a), D(b)) by an internal block. Removed with unblock()."""
    db.execute("INSERT INTO room_status_events (room_id, kind, reason, start_date, end_date, created_at) "
               f"SELECT r.id, 'internal_block', 'ALT BLOCK', ?, ?, ? FROM rooms r WHERE r.active = 1 AND {where}", (D(a), D(b), m.now_iso()))
    db.commit()


def unblock(db):
    db.execute("DELETE FROM room_status_events WHERE reason = 'ALT BLOCK'")
    db.commit()


def test_alternatives():
    db = sqlite3.connect(m.DB)
    db.execute("DELETE FROM peak_periods")
    db.commit()
    search = lambda a, b, **kw: call("GET", "/api/public/search", dict(check_in=D(a), check_out=D(b), guests=2, **kw))
    st, r = search(40, 42)
    assert st == 200 and r["properties"] and r["alternatives"] == [], "rooms free: no alternatives offered"

    # everything is sold out on exactly D(40)-D(42): the alternatives are the nearest windows that are really free
    block_rooms(db, 40, 42)
    st, r = search(40, 42)
    assert st == 200 and not r["properties"], r
    alts = r["alternatives"]
    assert [a["days_away"] for a in alts] == [-2, 2, -3], alts  # nearest first; D(39)-D(41) and D(41)-D(43) still overlap the blocked nights
    assert all(a["nights"] == 2 for a in alts) and len({a["check_in"] for a in alts}) == 3
    for a in alts:  # each is genuinely bookable: search finds rooms
        st, r2 = call("GET", "/api/public/search", dict(check_in=a["check_in"], check_out=a["check_out"], guests=2))
        assert st == 200 and r2["properties"], a
    a = alts[0]
    st, r2 = call("GET", "/api/public/search", dict(check_in=a["check_in"], check_out=a["check_out"], guests=2))
    pid, t = r2["properties"][0]["property_id"], r2["properties"][0]["types"][0]
    st, b = call("POST", "/api/public/book", guest(property_id=pid, room_type_id=t["room_type_id"]) | dict(check_in=a["check_in"], check_out=a["check_out"]))
    assert st == 200 and b["check_in"] == a["check_in"], b  # ...and a real booking goes through

    # the property filter is kept: the alternatives must be free in that property
    unblock(db)
    block_rooms(db, 40, 42, "r.property_id = 1")
    st, r = search(40, 42, property_id=1)
    conn = m.connect()
    assert not r["properties"] and r["alternatives"] and all(
        m.free_rooms(conn, m.date.fromisoformat(a["check_in"]), m.date.fromisoformat(a["check_out"]), 1) for a in r["alternatives"])
    conn.close()
    unblock(db)

    # nothing free within 14 days either side: no alternatives at all
    block_rooms(db, 20, 70)
    st, r = search(40, 42)
    assert st == 200 and r["properties"] == [] and r["alternatives"] == [], r
    unblock(db)

    # the party must fit: 200 guests need more rooms than anyone may book, so no date helps
    st, r = call("GET", "/api/public/search", dict(check_in=CI, check_out=CO, guests=200))
    assert st == 200 and r["properties"] == [] and r["alternatives"] == []

    # never in the past, never beyond the advance window, never over the night limit
    block_rooms(db, 1, 3)
    st, r = search(1, 3)
    assert r["alternatives"] and all(D(0) <= a["check_in"] <= D(90) for a in r["alternatives"]), r
    unblock(db)
    block_rooms(db, 88, 90)
    st, r = search(88, 90)
    assert r["alternatives"] and all(a["check_in"] <= D(90) for a in r["alternatives"]), r
    unblock(db)
    conn = m.connect()
    assert m.alternatives(conn, m.today() + timedelta(days=40), m.today() + timedelta(days=56), 2) == [], "16 nights exceeds guest_max_nights (15)"
    conn.close()
    db.close()


def test_callback():
    db = sqlite3.connect(m.DB)
    db.execute("DELETE FROM peak_periods")
    db.commit()  # an open transaction here would block the server's own writes
    N0 = db.execute("SELECT COUNT(*) FROM callback_requests").fetchone()[0]
    good = dict(name="Group Leader", phone="9876500001", people=40, check_in=D(40), check_out=D(43), note="Need ground floor")
    count = lambda: db.execute("SELECT COUNT(*) FROM callback_requests").fetchone()[0]

    # every field is validated on the server
    for patch, msg in [(dict(name=""), "name"), (dict(name="x" * 101), "name"), (dict(phone="12"), "10 to 13"), (dict(phone="abcdefghijk"), "10 to 13"),
                       (dict(people=0), "people"), (dict(people=501), "people"), (dict(people="many"), "people"), (dict(people=None), "people"),
                       (dict(check_in=D(-1)), "past"), (dict(check_in="soon"), "dates"), (dict(check_in=None), "dates"), (dict(check_out=D(40)), "after check-in"),
                       (dict(check_out=D(39)), "after check-in"), (dict(check_in=D(400), check_out=D(401)), "within"), (dict(check_out=D(120)), "nights"),
                       (dict(note="x" * 501), "500"), (dict(property_id=9999), "property"), (dict(property_id="abc"), "property")]:
        st, r = call("POST", "/api/public/callback", good | patch)
        assert st == 400 and msg.lower() in r["error"].lower(), (patch, st, r)
    assert count() == N0, "nothing is stored for invalid input"

    # honeypot: a filled hidden field is refused with the generic message and nothing is stored
    st, r = call("POST", "/api/public/callback", good | dict(website="http://spam.example"))
    assert st == 400 and "not valid" in r["error"] and count() == N0, r

    # a valid request is stored (property optional) and audited
    st, r = call("POST", "/api/public/callback", good)
    assert st == 200 and r == {"ok": True}, r
    st, r = call("POST", "/api/public/callback", good | dict(phone="9876500002", property_id=3, note=""))
    assert st == 200 and count() == N0 + 2
    row = db.execute("SELECT property_id, name, phone, people, check_in, check_out, note, status, handled_by FROM callback_requests ORDER BY id DESC LIMIT 1 OFFSET 1").fetchone()
    assert row == (None, "Group Leader", "9876500001", 40, D(40), D(43), "Need ground floor", "new", None), row
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'callback.request'").fetchone()[0] == 2

    # the guest rate limit applies: 5 per 10 minutes per address, then 429 with Retry-After
    m.reset_throttle()
    codes = [m.dispatch("POST", "/api/public/callback", None, good | dict(phone=f"98765001{i:02d}"), "5.6.7.8")[0] for i in range(6)]
    assert codes == [200] * 5 + [429], codes
    st, r = m.dispatch("POST", "/api/public/callback", None, good | dict(phone="9876500199"), "5.6.7.8")
    assert st == 429 and r["retry_after"] > 0, r
    m.reset_throttle()
    assert [m.dispatch("POST", "/api/public/callback", None, good, "9.9.9.1")[0] for _ in range(4)] == [200, 200, 200, 429], "3 per mobile number"

    # search tells the page when to offer the form
    m.reset_throttle()
    search = lambda a, b, g=2: call("GET", "/api/public/search", dict(check_in=D(a), check_out=D(b), guests=g))[1]
    assert search(40, 42)["callback"] is None
    assert search(40, 42, 200)["callback"] == "group", "rooms are free but the party needs more than the online room limit"
    block_rooms(db, 40, 42)
    r = search(40, 42)
    assert r["callback"] is None and r["alternatives"], "sold out but other dates are on offer"
    unblock(db)
    block_rooms(db, 20, 70)
    r = search(40, 42)
    assert r["callback"] == "soldout" and r["alternatives"] == []
    unblock(db)
    db.close()


def test_arrival_details():
    db = sqlite3.connect(m.DB)
    db.execute("DELETE FROM peak_periods")
    db.commit()
    pid, t = pick()
    book = lambda **kw: call("POST", "/api/public/book", guest(property_id=pid, room_type_id=t["room_type_id"]) | kw)
    cols = {r[1] for r in db.execute("PRAGMA table_info(bookings)")}
    assert {"arrival_time", "darshan_date", "remarks"} <= cols and "arrival_notes" not in cols, "arrival time and darshan date are new; the one note field stays remarks"

    # the migration is safe to run again and never rewrites data
    conn = m.connect()
    n_book, n_cb = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0], conn.execute("SELECT COUNT(*) FROM callback_requests").fetchone()[0]
    m.write(conn, lambda: m.migrate(conn))
    m.write(conn, lambda: m.migrate(conn))
    assert [r[1] for r in conn.execute("PRAGMA table_info(bookings)")].count("arrival_time") == 1
    assert (conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0], conn.execute("SELECT COUNT(*) FROM callback_requests").fetchone()[0]) == (n_book, n_cb)
    conn.close()

    # bad values are rejected and nothing is booked
    before = db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    for patch, msg in [(dict(arrival_time="25:00"), "arrival time"), (dict(arrival_time="9:5"), "arrival time"), (dict(arrival_time="noon"), "arrival time"), (dict(arrival_time="14:60"), "arrival time"),
                       (dict(darshan_date=D(-1)), "past"), (dict(darshan_date="tomorrow"), "valid date"), (dict(darshan_date="2026-02-30"), "valid date")]:
        st, r = book(**patch)
        assert st == 400 and msg in r["error"], (patch, r)
    assert db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == before

    # stored, returned on booking and on lookup; the note is a separate, unchanged field; both are optional
    note = "<script>alert(1)</script> <b>ground floor</b>"
    st, b = book(arrival_time="06:30", darshan_date=D(31), remarks=note)
    assert st == 200 and (b["arrival_time"], b["darshan_date"], b["special_requests"]) == ("06:30", D(31), note), b
    st, v = call("POST", "/api/public/booking/lookup", dict(booking_id=b["booking_id"], phone=b["mobile"]))
    assert st == 200 and (v["arrival_time"], v["darshan_date"], v["special_requests"]) == ("06:30", D(31), note), v
    assert db.execute("SELECT arrival_time, darshan_date, remarks FROM bookings WHERE group_id = ?", (b["booking_id"],)).fetchone() == ("06:30", D(31), note)
    st, b2 = book(darshan_date=D(0))  # today is allowed
    assert st == 200 and b2["arrival_time"] is None and b2["darshan_date"] == D(0), b2
    st, b3 = book(arrival_time="", darshan_date="")
    assert st == 200 and b3["arrival_time"] is None and b3["darshan_date"] is None, b3
    st, g2 = book(arrival_time="23:59", rooms=2, guests=3)  # a group booking carries them on every room
    assert st == 200 and [r[0] for r in db.execute("SELECT arrival_time FROM bookings WHERE group_id = ?", (g2["booking_id"],))] == ["23:59", "23:59"]

    # display is by text only: the pages never build HTML from data, so a tag in the note shows as typed
    web = Path(__file__).resolve().parent.parent / "web"
    for page in ("guest.html", "staff.html"):
        src = (web / page).read_text(encoding="utf-8")
        assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", src), page
    db.close()


def _free(pid, t):
    return m.free_rooms(m.connect(), m.date.fromisoformat(CI), m.date.fromisoformat(CO), pid, t["room_type_id"])


def call_ip(ip, method, path, data=None):
    q, body = (data, None) if method == "GET" else (None, data)
    return m.dispatch(method, path, q, body, ip)


def test_holds_and_limits():
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    H1, H2 = D(60), D(62)
    stay = dict(check_in=H1, check_out=H2)
    m.reset_throttle()
    st, r = call_ip("1.1.1.1", "GET", "/api/public/search", dict(stay, guests=2))
    pid, t = r["properties"][0]["property_id"], r["properties"][0]["types"][0]
    sel = dict(stay, property_id=pid, room_type_id=t["room_type_id"], rooms=1)

    def free(hold=None):
        conn = m.connect()
        try:
            return len(m.free_rooms(conn, m.date.fromisoformat(H1), m.date.fromisoformat(H2), pid, t["room_type_id"], hold))
        finally:
            conn.close()

    def book_for(ip, **kw):
        m.reset_throttle()
        N[0] += 1
        return call_ip(ip, "POST", "/api/public/book", dict(guest_name="Racer", phone=f"97654{N[0]:05d}", id_type="Aadhaar", id_number="1234",
                                                              payment_method="pay_at_counter", guests=1, **sel, **kw))

    # sell the type down to exactly one room
    while free() > 1:
        assert book_for("9.9.9.9")[0] == 200
    assert free() == 1

    # two guests race for the last room: the first hold wins, the second cannot hold or book it
    m.reset_throttle()
    st, a = call_ip("1.1.1.1", "POST", "/api/public/hold", sel)
    assert st == 200 and a["seconds"] == 600 and a["rooms"] == 1, a
    assert free() == 0 and free(a["token"]) == 1  # held rooms are unavailable to everyone else, not to the holder
    m.reset_throttle()
    st, r = call_ip("2.2.2.2", "POST", "/api/public/hold", sel)
    assert st == 409 and "no longer available" in r["error"], r
    assert book_for("2.2.2.2")[0] == 409
    assert book_for("2.2.2.2", hold_token="not-the-token")[0] == 409
    q = dict(stay, guests=1, property_id=pid, room_type_id=t["room_type_id"])
    assert not call_ip("2.2.2.2", "GET", "/api/public/search", q)[1]["properties"]
    assert call_ip("1.1.1.1", "GET", "/api/public/search", q | {"hold": a["token"]})[1]["properties"], "the holder still sees their own room"
    # the holder books; the hold is consumed
    st, b = book_for("1.1.1.1", hold_token=a["token"])
    assert st == 200 and b["status"] == "Confirmed", b
    db = sqlite3.connect(m.DB)
    assert db.execute("SELECT COUNT(*) FROM holds WHERE token = ?", (a["token"],)).fetchone()[0] == 0
    call("POST", "/api/public/booking/cancel", dict(booking_id=b["booking_id"], phone=b["mobile"], reason="test"))
    assert free() == 1

    # hold expiry: after 10 minutes the room is free again, with no cleanup job needed
    m.reset_throttle()
    st, a = call_ip("1.1.1.1", "POST", "/api/public/hold", sel)
    assert st == 200 and free() == 0
    real_now = m.now
    m.now = lambda: real_now() + timedelta(minutes=m.HOLD_MINUTES, seconds=5)
    try:
        assert free() == 1
        st, b = book_for("2.2.2.2")
        assert st == 200, b  # the second guest gets it once the hold has lapsed
    finally:
        m.now = real_now
    call("POST", "/api/public/booking/cancel", dict(booking_id=b["booking_id"], phone=b["mobile"], reason="test"))
    m.reset_throttle()
    db.execute("DELETE FROM holds"); db.commit()

    # release frees it at once; re-holding with the old token replaces rather than stacks
    st, a1 = call_ip("3.3.3.3", "POST", "/api/public/hold", sel)
    assert st == 200 and free() == 0
    m.reset_throttle()
    st, a2 = call_ip("3.3.3.3", "POST", "/api/public/hold", sel | {"token": a1["token"]})
    assert st == 200 and db.execute("SELECT COUNT(DISTINCT token) FROM holds WHERE ip = '3.3.3.3'").fetchone()[0] == 1
    assert call_ip("3.3.3.3", "POST", "/api/public/release", {"token": a2["token"]})[0] == 200 and free() == 1

    # one address cannot lock up the whole property
    cap = m.MAX_HOLDS_PER_IP
    st, r = call_ip("4.4.4.4", "GET", "/api/public/search", dict(check_in=D(70), check_out=D(71), guests=1))
    big = max(((p["property_id"], x) for p in r["properties"] for x in p["types"]), key=lambda px: px[1]["can_book"])
    osel = dict(check_in=D(70), check_out=D(71), property_id=big[0], room_type_id=big[1]["room_type_id"], rooms=1)
    assert big[1]["can_book"] >= 3, "test data needs a room type with several free rooms"
    cap = min(cap, 3)
    m.MAX_HOLDS_PER_IP = cap
    try:
        for _ in range(cap):
            m.reset_throttle()
            assert call_ip("5.5.5.5", "POST", "/api/public/hold", osel)[0] == 200
        m.reset_throttle()
        st, r = call_ip("5.5.5.5", "POST", "/api/public/hold", osel)
        assert st == 429 and r["retry_after"] > 0, r
    finally:
        m.MAX_HOLDS_PER_IP = 3
    db.execute("DELETE FROM holds"); db.commit()

    # honeypot: a filled hidden field is refused and nothing is booked
    before = db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    st, r = book_for("6.6.6.6", website="http://spam.example")
    assert st == 400 and db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == before, r

    # rate limits per IP: 429 with a wait time, and a Retry-After header over real HTTP
    m.reset_throttle()
    codes = [call_ip("7.7.7.7", "GET", "/api/public/search", dict(stay, guests=1))[0] for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429
    st, r = call_ip("7.7.7.7", "GET", "/api/public/search", dict(stay, guests=1))
    assert st == 429 and 1 <= r["retry_after"] <= 60 and "try again in" in r["error"], r
    assert call_ip("8.8.8.8", "GET", "/api/public/search", dict(stay, guests=1))[0] == 200  # other addresses are unaffected
    m.reset_throttle()
    assert [call_ip("7.7.7.7", "POST", "/api/public/book", {})[0] for _ in range(11)][-1] == 429
    m.reset_throttle()
    assert [call_ip("7.7.7.7", "POST", "/api/public/booking/lookup", dict(booking_id=1, phone="9000000000"))[0] for _ in range(21)][-1] == 429
    m.reset_throttle()
    assert [call_ip("7.7.7.7", "POST", "/api/public/booking/cancel", dict(booking_id=1, phone="9000000000", reason="x"))[0] for _ in range(21)][-1] == 429
    m.Handler.log_message = lambda *a: None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        m.reset_throttle()
        last = None
        for _ in range(21):
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/public/booking/lookup", json.dumps(dict(booking_id=1, phone="9000000000")).encode(),
                                         {"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req)
            except urllib.error.HTTPError as e:
                last = e
        assert last.code == 429 and int(last.headers["Retry-After"]) > 0, last.headers
        assert json.loads(last.read())["retry_after"] == int(last.headers["Retry-After"])
    finally:
        srv.shutdown()
    m.reset_throttle()
    db.close()


def test_peak_periods():
    db = sqlite3.connect(m.DB)
    db.execute("DELETE FROM peak_periods")
    ins = lambda name, a, b, nights=None, rooms=None, note=None, active=1: db.execute(
        "INSERT INTO peak_periods (name, start_date, end_date, max_nights, max_rooms, note, active) VALUES (?,?,?,?,?,?,?)", (name, D(a), D(b), nights, rooms, note, active))
    ins("Brahmotsavam", 80, 84, nights=2, rooms=1, note="Very crowded. Dormitory only on request.")
    ins("Loose cap", 80, 84, nights=30, rooms=10)       # a limit above the normal one never loosens it
    ins("Switched off", 70, 79, nights=1, rooms=1, active=0)  # inactive periods are ignored
    db.commit()
    search = lambda a, b, **kw: call("GET", "/api/public/search", dict(check_in=D(a), check_out=D(b), guests=2, **kw))

    # which stays touch the period: its end date is the last night, so D(84)->D(85) is in and D(85)->D(86) is out
    for a, b, inside in [(80, 82, True), (79, 81, True), (84, 85, True), (79, 80, False), (85, 86, False), (86, 89, False)]:
        st, r = search(a, b)
        assert st == 200 and bool(r["peak"]) == inside, (a, b, r["peak"])
    st, r = search(80, 82)
    assert r["peak"][0]["name"] == "Brahmotsavam" and "crowded" in r["peak"][0]["note"] and len(r["peak"]) == 2
    assert all(t["can_book"] <= 1 for p in r["properties"] for t in p["types"]), "peak max_rooms limits what a guest may pick"
    assert any(t["can_book"] > 1 for p in search(86, 87)[1]["properties"] for t in p["types"]), "outside the period the normal limit applies"

    # max_nights enforced on search, hold and book, naming the period; normal stays outside are untouched
    st, r = search(80, 83)
    assert st == 400 and "Brahmotsavam" in r["error"] and "at most 2 nights" in r["error"], r
    st, r = search(86, 89)
    assert st == 200 and not r["peak"]
    pid, t = r["properties"][0]["property_id"], r["properties"][0]["types"][0]
    body = lambda a, b, **kw: dict(guest(property_id=pid, room_type_id=t["room_type_id"]), check_in=D(a), check_out=D(b)) | kw
    st, r = call("POST", "/api/public/book", body(80, 83))
    assert st == 400 and "at most 2 nights" in r["error"], r
    st, r = call("POST", "/api/public/hold", dict(check_in=D(80), check_out=D(83), property_id=pid, room_type_id=t["room_type_id"], rooms=1))
    assert st == 400 and "at most 2 nights" in r["error"], r

    # max_rooms enforced on book and hold with a clear message
    st, r = call("POST", "/api/public/book", body(80, 82, rooms=2, guests=2))
    assert st == 400 and "Brahmotsavam" in r["error"] and "at most 1 room " in r["error"], r
    st, r = call("POST", "/api/public/hold", dict(check_in=D(80), check_out=D(82), property_id=pid, room_type_id=t["room_type_id"], rooms=2))
    assert st == 400 and "at most 1 room" in r["error"], r
    st, r = call("POST", "/api/public/book", body(80, 82))
    assert st == 200, r  # within the limits it books normally
    st, r = call("POST", "/api/public/book", body(86, 88, rooms=2, guests=2))
    assert st == 200 and r["rooms"] == 2, r  # outside the period two rooms are fine

    # overlapping periods: the strictest limit wins and is the one named
    ins("Festival day", 81, 81, nights=1)
    db.commit()
    st, r = search(80, 82)
    assert st == 400 and "Festival day" in r["error"] and "at most 1 nights" in r["error"], r
    assert search(80, 81)[0] == 200

    # public info lists upcoming active periods for the booking form
    names = [p["name"] for p in call("GET", "/api/public/info")[1]["peak_periods"]]
    assert names == ["Brahmotsavam", "Loose cap", "Festival day"] and "Switched off" not in names, names
    db.execute("DELETE FROM peak_periods"); db.commit()
    assert search(80, 83)[0] == 200  # with the periods gone, the normal rules return
    db.close()


def test_site_config():
    call("GET", "/health", {})  # the first connection runs migrate(), which creates site_config
    db = sqlite3.connect(m.DB)
    db.execute("DELETE FROM site_config"); db.commit()
    assert call("GET", "/api/public/info")[1]["site"] == {}, "nothing is invented: unset items are absent"
    put = lambda k, lang, v: db.execute("INSERT OR REPLACE INTO site_config (key, lang, value, updated_at) VALUES (?,?,?,'now')", (k, lang, v))
    put("darshan_timings", "en", "Set by the office"); put("darshan_timings", "te", "TODO"); put("directions", "en", "TODO: fill in"); put("seva_timings", "en", "English seva")
    put("seva_timings", "hi", "Hindi seva"); put("not_a_key", "en", "ignored"); db.commit()
    site = lambda lang: call("GET", "/api/public/info", dict(lang=lang))[1]["site"]
    assert site("en") == {"darshan_timings": "Set by the office", "seva_timings": "English seva"}, site("en")  # TODO and unknown keys never reach guests
    assert site("te")["darshan_timings"] == "Set by the office", "a TODO translation falls back to English"
    assert site("hi")["seva_timings"] == "Hindi seva" and site("xx") == site("en")
    db.execute("DELETE FROM site_config"); db.commit(); db.close()


def test_policy_and_content():
    # money: refunds round half up to the paisa, never exceed what was paid, and are 0 when no policy is set
    assert m.refund_for(1000, 50) == 500 and m.refund_for(999, 33) == 329.67 and m.refund_for(1000, 100) == 1000
    assert m.refund_for(0.05, 50) == 0.03 and m.refund_for(1000, 0) == 0 and m.refund_for(1000, None) == 0 and m.refund_for(0, 100) == 0 and m.refund_for(None, 100) == 0
    call("GET", "/health", {})
    db = sqlite3.connect(m.DB)
    cfg = lambda: call("GET", "/api/public/info")[1]["settings"]
    assert cfg()["refund_percent"] is None and cfg()["refund_days"] is None, "no refund policy is assumed"
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('refund_percent', '50'), ('refund_days', '7')"); db.commit()
    assert (cfg()["refund_percent"], cfg()["refund_days"]) == (50, 7)

    # the cutoff the page shows is the one enforced; the booking carries the same refund values; refund = paid x percent
    pid, t = pick()
    st, b = call("POST", "/api/public/book", guest(property_id=pid, room_type_id=t["room_type_id"]))
    assert st == 200 and b["refund"] == {"percent": 50, "days": 7, "amount": 0}, b  # nothing is paid online, so nothing to refund
    cutoff = cfg()["guest_cancel_cutoff_hours"]
    assert datetime.fromisoformat(b["cancel_deadline"]) == datetime.fromisoformat(f"{CI}T{cfg()['checkin_time']}") - timedelta(hours=cutoff)
    db.execute("UPDATE bookings SET payment_status = 'paid' WHERE id = ? OR group_id = ?", (b["booking_id"], b["booking_id"])); db.commit()
    v = call("POST", "/api/public/booking/lookup", dict(booking_id=b["booking_id"], phone=b["mobile"]))[1]
    assert v["refund"]["amount"] == v["amount"] / 2, v
    st, c = call("POST", "/api/public/booking/cancel", dict(booking_id=b["booking_id"], phone=b["mobile"], reason="plans changed"))
    assert st == 200 and c["status"] == "Cancelled" and c["refund"]["amount"] == v["amount"] / 2
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'booking.online_cancel' AND reason LIKE '%refund policy 50%'").fetchone()[0] == 1
    db.execute("DELETE FROM settings WHERE key IN ('refund_percent', 'refund_days')"); db.commit()
    assert cfg()["refund_percent"] is None

    # site items are checked by kind; a bad value written straight to the database is still not served
    bad = [("rooms_phone", "abc"), ("whatsapp_number", "12345"), ("support_email", "x"), ("map_url", "http://x.example"), ("official_site_url", "javascript:alert(1)"),
           ("map_embed_url", "https://evil.example/maps/embed"), ("map_embed_url", "https://www.google.com/maps/embed?x=1 onload=1")]
    for k, v in bad:
        try:
            m.site_check(k, v)
        except m.UserError:
            continue
        raise AssertionError((k, v))
    for k, v in [("rooms_phone", "+91 98765-43210"), ("whatsapp_number", "+91 98765 43210"), ("support_email", "a@b.in"), ("map_url", "https://maps.example/x"),
                 ("map_embed_url", "https://www.google.com/maps/embed?pb=abc"), ("map_embed_url", "https://www.openstreetmap.org/export/embed.html?bbox=1")]:
        m.site_check(k, v)
    db.execute("DELETE FROM site_config")
    db.execute("INSERT INTO site_config (key, lang, value, updated_at) VALUES ('map_embed_url', 'en', 'https://evil.example/x', 'now'), ('rooms_phone', 'en', 'abc', 'now'), ('map_url', 'en', 'https://maps.example/x', 'now')"); db.commit()
    assert call("GET", "/api/public/info")[1]["site"] == {"map_url": "https://maps.example/x"}
    db.execute("DELETE FROM site_config"); db.commit()

    # FAQ: TODO answers and inactive drafts are hidden, a translation is used only when complete, order follows position
    db.execute("DELETE FROM faq_text"); db.execute("DELETE FROM faqs")
    for fid, pos, active, texts in [(1, 2, 1, {"en": ("Q1", "A1"), "te": ("Q1te", "A1te")}), (2, 1, 1, {"en": ("Q2", "TODO")}), (3, 3, 0, {"en": ("Q3", "A3")}),
                                    (4, 4, 1, {"en": ("Q4", "A4"), "hi": ("Q4hi", "TODO")})]:
        db.execute("INSERT INTO faqs (id, position, active) VALUES (?,?,?)", (fid, pos, active))
        db.executemany("INSERT INTO faq_text (faq_id, lang, question, answer) VALUES (?,?,?,?)", [(fid, lang, q, a) for lang, (q, a) in texts.items()])
    db.commit()
    qs = lambda lang: [x["question"] for x in call("GET", "/api/public/info", dict(lang=lang))[1]["faqs"]]
    assert qs("en") == ["Q1", "Q4"] and qs("te") == ["Q1te", "Q4"] and qs("hi") == ["Q1", "Q4"] and qs("xx") == ["Q1", "Q4"], (qs("en"), qs("te"), qs("hi"))

    # seeding: call-back notes become inactive drafts with a TODO answer, once each
    db.execute("DELETE FROM faq_text"); db.execute("DELETE FROM faqs")
    for note in ("Need ground floor", "Need ground floor", "   ", None):
        db.execute("INSERT INTO callback_requests (name, phone, people, check_in, check_out, note, created_at) VALUES ('T', '9876543210', 5, ?, ?, ?, 'now')", (CI, CO, note))
    db.commit()
    assert m.seed_faqs_from_callbacks(db) == 1 and m.seed_faqs_from_callbacks(db) == 0
    assert db.execute("SELECT f.active, x.question, x.answer FROM faqs f JOIN faq_text x ON x.faq_id = f.id").fetchall() == [(0, "Need ground floor", "TODO")]
    db.commit()  # the throttle counters live in the same database: do not hold its write lock
    assert call("GET", "/api/public/info")[1]["faqs"] == []
    db.execute("DELETE FROM faq_text"); db.execute("DELETE FROM faqs"); db.execute("DELETE FROM callback_requests WHERE name = 'T'"); db.commit(); db.close()


def test_quick_callback():
    """The Call me back button sends only a name and phone (plus best time and language): it uses the same queue, with no people or dates."""
    call("GET", "/health", {})
    db = sqlite3.connect(m.DB)
    n0 = db.execute("SELECT COUNT(*) FROM callback_requests").fetchone()[0]
    quick = dict(name="Elder Guest", phone="9876500077", call_time="evening", lang="te")
    st, r = call("POST", "/api/public/callback", quick)
    assert st == 200 and r == {"ok": True}, r
    row = db.execute("SELECT name, phone, people, check_in, check_out, status, call_time, lang FROM callback_requests ORDER BY id DESC LIMIT 1").fetchone()
    assert row == ("Elder Guest", "9876500077", None, None, None, "new", "evening", "te"), row
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'callback.request' AND new_value LIKE '%quick call-back, evening, te%'").fetchone()[0] == 1
    # defaults, language is optional and unknown languages are ignored, call time is checked
    assert call("POST", "/api/public/callback", dict(name="A", phone="9876500078"))[0] == 200
    assert call("POST", "/api/public/callback", dict(name="B", phone="9876500079", lang="xx"))[0] == 200
    assert db.execute("SELECT call_time, lang FROM callback_requests WHERE name IN ('A', 'B') ORDER BY name").fetchall() == [("any", None), ("any", None)]
    assert call("POST", "/api/public/callback", quick | dict(call_time="midnight"))[0] == 400
    # the same checks as the long form: name, phone, honeypot, and a half-filled booking request is not a quick one
    for patch in (dict(name=""), dict(phone="12"), dict(website="x"), dict(check_in=D(40)), dict(people=3), dict(note="x" * 501)):
        assert call("POST", "/api/public/callback", quick | patch)[0] == 400, patch
    assert db.execute("SELECT COUNT(*) FROM callback_requests").fetchone()[0] == n0 + 3
    # per-number throttle still applies
    m.reset_throttle()
    codes = [m.dispatch("POST", "/api/public/callback", None, dict(name="T", phone="9876500099"), f"9.9.9.{i}")[0] for i in range(5)]
    assert codes == [200, 200, 200, 429, 429], codes
    db.execute("DELETE FROM callback_requests WHERE name IN ('Elder Guest', 'A', 'B', 'T')"); db.commit(); db.close()


def test_callback_table_migration():
    """An old callback_requests table (people and dates required) is rebuilt with every row kept, and then accepts empty people and dates."""
    path = TMP / "old.db"
    shutil.copy(m.DB, path)
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("DROP TABLE callback_requests")
    c.execute("CREATE TABLE callback_requests (id INTEGER PRIMARY KEY, property_id INTEGER REFERENCES properties(id), name TEXT NOT NULL, phone TEXT NOT NULL, people INTEGER NOT NULL, "
              "check_in TEXT NOT NULL, check_out TEXT NOT NULL, note TEXT, status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'contacted', 'closed')), created_at TEXT NOT NULL, "
              "handled_by INTEGER REFERENCES users(id), handled_at TEXT)")
    c.execute("INSERT INTO callback_requests VALUES (7, 1, 'Old Row', '9876500001', 12, '2027-01-01', '2027-01-03', 'a note', 'contacted', '2026-10-01T10:00:00', 1, '2026-10-02T09:00:00')")
    c.execute("CREATE INDEX idx_callbacks_status ON callback_requests(status, created_at)")
    m.write(c, lambda: m.migrate(c))
    assert c.execute("SELECT * FROM callback_requests").fetchall() == [(7, 1, "Old Row", "9876500001", 12, "2027-01-01", "2027-01-03", "a note", "contacted", "2026-10-01T10:00:00", 1, "2026-10-02T09:00:00", None, None, None)]
    c.execute("INSERT INTO callback_requests (name, phone, created_at, call_time, lang) VALUES ('Quick', '9876500002', 'now', 'any', 'hi')")  # no people or dates
    assert c.execute("SELECT name FROM sqlite_master WHERE name = 'callback_requests_old'").fetchone() is None
    m.write(c, lambda: m.migrate(c))  # running it again changes nothing
    assert c.execute("SELECT COUNT(*) FROM callback_requests").fetchone()[0] == 2
    c.close()


if __name__ == "__main__":
    try:
        test_callback_table_migration()
        test_site_config()
        test_policy_and_content()
        test_health_and_backup()
        test_alternatives()
        test_callback()
        test_quick_callback()  # after test_callback, which counts the audit rows (the audit log is append-only)
        test_arrival_details()
        test_holds_and_limits()
        test_peak_periods()
        test_all()
        print("ok")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
