"""Self-check for the guest booking API. Runs against a COPY of the real database.
Run: python tools/test_mantralayam_rooms.py   (or pytest)
"""
import shutil
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path

import mantralayam_rooms as m

SRC = Path(__file__).resolve().parent.parent / ".tmp" / "mantralayam.db"
TMP = Path(tempfile.mkdtemp())
m.DB = TMP / "copy.db"
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
    m.HITS.clear()
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
    assert call("GET", "/health", {}) == (200, {"ok": True})
    dest = m.backup(keep=1)
    assert dest.is_file() and sqlite3.connect(dest).execute("SELECT COUNT(*) FROM bookings").fetchone()[0] >= 0
    assert len(list(dest.parent.glob("*.db"))) == 1


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
    m.HITS.clear()
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
        m.HITS.clear()
        N[0] += 1
        return call_ip(ip, "POST", "/api/public/book", dict(guest_name="Racer", phone=f"97654{N[0]:05d}", id_type="Aadhaar", id_number="1234",
                                                              payment_method="pay_at_counter", guests=1, **sel, **kw))

    # sell the type down to exactly one room
    while free() > 1:
        assert book_for("9.9.9.9")[0] == 200
    assert free() == 1

    # two guests race for the last room: the first hold wins, the second cannot hold or book it
    m.HITS.clear()
    st, a = call_ip("1.1.1.1", "POST", "/api/public/hold", sel)
    assert st == 200 and a["seconds"] == 600 and a["rooms"] == 1, a
    assert free() == 0 and free(a["token"]) == 1  # held rooms are unavailable to everyone else, not to the holder
    m.HITS.clear()
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
    m.HITS.clear()
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
    m.HITS.clear()
    db.execute("DELETE FROM holds"); db.commit()

    # release frees it at once; re-holding with the old token replaces rather than stacks
    st, a1 = call_ip("3.3.3.3", "POST", "/api/public/hold", sel)
    assert st == 200 and free() == 0
    m.HITS.clear()
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
            m.HITS.clear()
            assert call_ip("5.5.5.5", "POST", "/api/public/hold", osel)[0] == 200
        m.HITS.clear()
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
    m.HITS.clear()
    codes = [call_ip("7.7.7.7", "GET", "/api/public/search", dict(stay, guests=1))[0] for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429
    st, r = call_ip("7.7.7.7", "GET", "/api/public/search", dict(stay, guests=1))
    assert st == 429 and 1 <= r["retry_after"] <= 60 and "try again in" in r["error"], r
    assert call_ip("8.8.8.8", "GET", "/api/public/search", dict(stay, guests=1))[0] == 200  # other addresses are unaffected
    m.HITS.clear()
    assert [call_ip("7.7.7.7", "POST", "/api/public/book", {})[0] for _ in range(11)][-1] == 429
    m.HITS.clear()
    assert [call_ip("7.7.7.7", "POST", "/api/public/booking/lookup", dict(booking_id=1, phone="9000000000"))[0] for _ in range(21)][-1] == 429
    m.HITS.clear()
    assert [call_ip("7.7.7.7", "POST", "/api/public/booking/cancel", dict(booking_id=1, phone="9000000000", reason="x"))[0] for _ in range(21)][-1] == 429
    m.Handler.log_message = lambda *a: None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        m.HITS.clear()
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
    m.HITS.clear()
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


if __name__ == "__main__":
    try:
        test_health_and_backup()
        test_holds_and_limits()
        test_peak_periods()
        test_all()
        print("ok")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
