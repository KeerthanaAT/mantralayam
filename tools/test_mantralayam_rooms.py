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
    s = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    assert s.execute("SELECT COUNT(*) FROM bookings WHERE source='online'").fetchone()[0] == 0
    s.close()


def _free(pid, t):
    return m.free_rooms(m.connect(), m.date.fromisoformat(CI), m.date.fromisoformat(CO), pid, t["room_type_id"])


if __name__ == "__main__":
    try:
        test_all()
        print("ok")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
