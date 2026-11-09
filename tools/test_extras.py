"""Self-check for sevas and packages, the festival and panchanga calendar linked to peak pricing, weather and river notices, post-stay feedback, the opt-in festival
broadcast, and the PWA files. Runs on a COPY of the real database. Run: python tools/test_extras.py   (or pytest)
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

import test_staff_desk as td
import mantralayam_rooms as g
import providers

D, call = td.D, td.call
ROOT = Path(__file__).resolve().parent.parent
N = [0]


def admin(path, body, user="admin"):
    return call(user, "POST", path, body)


def mkseva(name="Abhisheka", price=500, cap=None, per_guest=False):
    st, r = admin("/api/admin/sevas", dict(name=name, description="d", price=price, max_per_day=cap, per_guest=per_guest))
    assert st == 200, r
    return r["id"]


def quote(**kw):
    g.reset_throttle()
    base = dict(property_id=1, room_type_id=td.room_type(1), check_in=D(20), check_out=D(22), rooms=1, guests=2)
    return g.dispatch("POST", "/api/public/quote", None, base | kw, "4.4.4.4")


def otp_proof(phone, purpose):
    g.reset_throttle()
    providers.OUTBOX.clear()
    assert g.dispatch("POST", "/api/public/otp/send", None, {"phone": phone, "purpose": purpose}, "3.3.3.3")[0] == 200
    code = re.match(r"(\d{6}) is your", [x for x in providers.OUTBOX if x["kind"] == "sms"][-1]["text"])[1]
    g.reset_throttle()
    st, r = g.dispatch("POST", "/api/public/otp/verify", None, {"phone": phone, "purpose": purpose, "code": code}, "3.3.3.3")
    assert st == 200, r
    return r["proof"]


def pub(path, body, ip="2.2.2.2", method="POST"):
    g.reset_throttle()
    return g.dispatch(method, path, body if method == "GET" else None, None if method == "GET" else body, ip)


def cleanup():
    c = td.db()
    for t in ("seva_bookings", "package_items", "packages", "sevas", "notices", "feedback", "broadcast_log", "broadcasts", "subscribers", "festivals", "panchanga"):
        c.execute(f"DELETE FROM {t}")
    c.execute("DELETE FROM bookings WHERE source = 'online'")
    c.execute("DELETE FROM holds")
    c.execute("DELETE FROM alerts")
    c.execute("DELETE FROM notification_log")
    c.execute("DELETE FROM peak_periods WHERE name LIKE 'TEST %'")
    c.close()
    providers.OUTBOX.clear()
    os.environ.pop("WHATSAPP_PROVIDER", None)


# ---------- sevas and packages ----------
def test_seva_admin_validation_and_catalog():
    for user in ("manager", "frontdesk", "viewer"):
        assert admin("/api/admin/sevas", dict(name="x", price=1), user)[0] == 403
    for bad in (dict(name="", price=10), dict(name="x", price=-1), dict(name="x", price="abc"), dict(name="x", price=10, max_per_day=0), dict(name="x" * 101, price=10)):
        assert admin("/api/admin/sevas", bad)[0] == 400, bad
    a = mkseva("Abhisheka", 500, cap=3)
    b = mkseva("Archana", 100)
    cat = g.dispatch("GET", "/api/public/info", {}, None, "1.1.1.1")[1]["catalog"]
    assert [x["name"] for x in cat["sevas"]] == ["Abhisheka", "Archana"] and cat["packages"] == []
    assert admin(f"/api/admin/extras/sevas/{b}/active", {"active": False})[0] == 200
    assert [x["name"] for x in g.dispatch("GET", "/api/public/info", {}, None, "1.1.1.1")[1]["catalog"]["sevas"]] == ["Abhisheka"], "a switched-off seva is not offered"
    assert admin("/api/admin/extras/sevas/9999/active", {"active": False})[0] == 404
    assert admin("/api/admin/sevas", dict(id=a, name="Abhisheka", price=550, max_per_day=3))[0] == 200
    assert call("admin", "GET", "/api/admin/data")[1]["extras"]["sevas"][0]["price"] == 550


def test_seva_is_priced_into_the_booking():
    sid = mkseva("Abhisheka", 500)
    st, q = quote(sevas=[{"seva_id": sid, "date": D(21), "qty": 2}])
    assert st == 200 and q["room_charge"] == 1200 and q["seva_charge"] == 1000 and q["total"] == 2200 and q["tax"] == 0
    assert q["sevas"][0] | {} == {"seva_id": sid, "name": "Abhisheka", "date": D(21), "qty": 2, "unit": 500.0, "amount": 1000.0, "package_id": None, "package": None}
    g.dispatch("POST", "/api/admin/settings", None, None, "x")
    td.settings_(tax_percent=10, advance_percent=25)
    try:
        q = quote(sevas=[{"seva_id": sid, "date": D(21), "qty": 2}])[1]
        assert q["tax"] == 120 and q["total"] == 1200 + 120 + 1000 and q["advance"] == q["total"] / 4, "tax is on the room only; the advance is on the whole total"
    finally:
        c = td.db()
        c.execute("DELETE FROM settings WHERE key IN ('tax_percent', 'advance_percent')")
        c.close()
    for bad, msg in (([{"seva_id": sid, "date": D(19), "qty": 1}], "within your stay"), ([{"seva_id": sid, "date": D(23), "qty": 1}], "within your stay"), ([{"seva_id": 999, "date": D(21), "qty": 1}], "not available"),
                     ([{"seva_id": sid, "date": "x", "qty": 1}], "date"), ([{"seva_id": sid, "date": D(21), "qty": 0}], "how many"), ([{"seva_id": sid, "date": D(21), "qty": 99}], "how many"), (["junk"], "not valid"),
                     ([{"seva_id": sid, "date": D(21), "qty": 1}] * 11, "at most 10")):
        st, e = quote(sevas=bad)
        assert st == 400 and msg in e["error"], (bad, e)
    assert quote(sevas=[{"seva_id": sid, "date": D(22), "qty": 1}])[0] == 200, "the check-out day is allowed (a morning seva)"
    # booked with the stay, shown on the booking and the receipt, carried by the confirmation
    b = td.online(start=20, nights=2, sevas=[{"seva_id": sid, "date": D(21), "qty": 2}])
    assert b["amount"] == 2200 and b["sevas"] == [{"date": D(21), "qty": 2, "amount": 1000.0, "status": "Confirmed", "name": "Abhisheka"}]
    assert any("Abhisheka" in str(ln) for ln in g.receipt_lines(b, "en")) and "Abhisheka" in g.receipt_html(b, "en")
    assert td.db().execute("SELECT COUNT(*) FROM seva_bookings WHERE group_id = ? AND status = 'Confirmed'", (b["booking_id"],)).fetchone()[0] == 1
    assert "Abhisheka" in "".join(x["text"] for x in providers.OUTBOX if x["kind"] == "email")
    # the schedule report (for the priests and the desk)
    st, t = call("manager", "GET", "/api/reports/data", {"report": "seva_schedule", "from": D(20), "to": D(22)})
    assert st == 200 and [r[1:3] for r in t["rows"]] == [["Abhisheka", 2]] and t["rows"][0][4] == "Online Guest"
    # the booking's dates cannot be changed online or by staff while it holds sevas
    st, e = g.dispatch("POST", "/api/public/booking/change", None, dict(booking_id=b["booking_id"], phone=b["mobile"], check_in=D(30), check_out=D(32)), "4.4.4.4")
    assert st == 409 and "sevas" in e["error"]
    first = td.db().execute("SELECT id FROM bookings WHERE group_id = ?", (b["booking_id"],)).fetchone()[0]
    assert call("manager", "POST", f"/api/bookings/{first}/reschedule", dict(check_in=D(30), check_out=D(32)))[0] == 409


def test_seva_capacity_is_checked_inside_the_booking():
    sid = mkseva("Rudrabhisheka", 300, cap=5)
    want = lambda qty: [{"seva_id": sid, "date": D(25), "qty": qty}]
    assert quote(check_in=D(25), check_out=D(26), sevas=want(6))[0] == 409, "more than the places a day"
    b1 = td.online(start=25, nights=1, sevas=want(3))
    st, e = quote(check_in=D(25), check_out=D(26), sevas=want(3))
    assert st == 409 and "full" in e["error"], "3 of 5 taken: 3 more do not fit"
    assert quote(check_in=D(25), check_out=D(26), sevas=want(2))[0] == 200
    # two guests race for the last places: the transaction lets one through
    g.reset_throttle()
    tok = [g.dispatch("POST", "/api/public/hold", None, dict(property_id=1, room_type_id=td.room_type(1), check_in=D(25), check_out=D(26), rooms=1), f"9.9.9.{i}")[1]["token"] for i in (1, 2)]
    body = lambda i: dict(property_id=1, room_type_id=td.room_type(1), check_in=D(25), check_out=D(26), rooms=1, guests=2, guest_name="G", phone=td.phone(), id_type="Aadhaar", id_number="1", payment_method="pay_at_counter",
                          hold_token=tok[i], sevas=want(2))
    g.reset_throttle()
    r1 = g.dispatch("POST", "/api/public/book", None, body(0), "9.9.9.1")
    st2, e2 = g.dispatch("POST", "/api/public/book", None, body(1), "9.9.9.2")[:2]
    assert r1[0] == 200 and st2 == 409 and "full" in e2["error"], "3 + 2 fill the 5 places; the third guest's 2 do not fit"
    c = td.db()
    assert c.execute("SELECT SUM(qty) FROM seva_bookings WHERE seva_id = ? AND status = 'Confirmed'", (sid,)).fetchone()[0] == 5
    # a direct place() with no room left is refused whatever the quote said
    cn = g.connect()
    q = quote(check_in=D(26), check_out=D(27), sevas=[{"seva_id": sid, "date": D(26), "qty": 5}])[1]
    td.online(start=26, nights=1, sevas=[{"seva_id": sid, "date": D(26), "qty": 5}])
    p = dict(pid=1, tid=td.room_type(1), n=1, guests=2, children=0, beds=0, ci=D(26), co=D(27), flt={"senior": False, "ground": False}, name="G", phone=td.phone(), email="", remarks="", id_type="Aadhaar", id_no="1", booker=None,
             arrival_name=None, arrival_phone=None, arrival=None, darshan=None, promo="", sevas=None, package=None, token="", save=False, lang="en", proof="verified", verified=True)
    try:
        g.write(cn, lambda: g.place(cn, p, q, "pay_at_counter", use_otp=False))
    except g.UserError as e:
        assert e.status == 409 and "full" in str(e)
    else:
        raise AssertionError("an over-full seva was booked")
    # cancelling the booking frees its places
    c.execute("UPDATE bookings SET check_in = ?, check_out = ? WHERE group_id = ?", (D(25), D(26), b1["booking_id"]))
    st, out = g.dispatch("POST", "/api/public/booking/cancel", None, dict(booking_id=b1["booking_id"], phone=b1["mobile"], reason="x"), "9.9.9.9")
    assert st == 200 and c.execute("SELECT status FROM seva_bookings WHERE group_id = ?", (b1["booking_id"],)).fetchone()[0] == "Cancelled"
    assert quote(check_in=D(25), check_out=D(26), sevas=want(3))[0] == 200
    assert out["sevas"] == [], "a cancelled booking lists no live sevas"
    cn.close()
    c.close()


def test_packages():
    a, b = mkseva("Abhisheka", 500), mkseva("Archana", 100)
    for bad in (dict(name="P", items=[]), dict(name="P", items=[{"seva_id": 999}]), dict(name="P", discount_percent=150, items=[{"seva_id": a}]), dict(name="", items=[{"seva_id": a}]), dict(name="P", items="x"),
                dict(name="P", items=[{"seva_id": a, "day_offset": 99}])):
        assert admin("/api/admin/packages", bad)[0] == 400, bad
    assert admin("/api/admin/packages", dict(name="P", items=[{"seva_id": a}]), "manager")[0] == 403
    st, r = admin("/api/admin/packages", dict(name="Room + Seva", description="d", discount_percent=20, min_nights=2, items=[{"seva_id": a, "day_offset": 0, "per_guest": True}, {"seva_id": b, "day_offset": 1, "per_guest": False}]))
    assert st == 200
    pk = r["id"]
    cat = g.dispatch("GET", "/api/public/info", {}, None, "1.1.1.1")[1]["catalog"]
    assert cat["packages"][0]["name"] == "Room + Seva" and len(cat["packages"][0]["items"]) == 2
    st, q = quote(package_id=pk, guests=3, rooms=2)
    assert st == 200, q
    by = {x["name"]: x for x in q["sevas"]}
    assert by["Abhisheka"]["date"] == D(20) and by["Abhisheka"]["qty"] == 3 and by["Abhisheka"]["unit"] == 400.0 and by["Abhisheka"]["amount"] == 1200.0 and by["Abhisheka"]["package"] == "Room + Seva", "20% off, one place per guest, on the first day"
    assert by["Archana"]["date"] == D(21) and by["Archana"]["qty"] == 1 and by["Archana"]["amount"] == 80.0
    assert q["total"] == q["room_charge"] + 1280, q
    # a package plus an extra seva; a one-night stay is too short; an unknown or switched-off package is refused
    assert quote(package_id=pk, sevas=[{"seva_id": a, "date": D(21), "qty": 1}])[1]["seva_charge"] == 400 * 2 + 80 + 500
    st, e = quote(package_id=pk, check_in=D(30), check_out=D(31))
    assert st == 400 and "at least 2 nights" in e["error"]
    assert quote(package_id=999)[0] == 400
    assert admin(f"/api/admin/extras/packages/{pk}/active", {"active": False})[0] == 200 and quote(package_id=pk)[0] == 400
    assert admin(f"/api/admin/extras/packages/{pk}/active", {"active": True})[0] == 200
    # booked through the whole flow, and the money adds up for a refund
    os.environ["PAYMENT_PROVIDER"] = "mock"
    try:
        td.settings_(pay_options="full,counter", refund_percent=50)
        b = td.online("pay_full", start=40, nights=2, package_id=pk, guests=2)
        assert b["amount"] == 1200 + 2 * 400 + 80 and b["payment"]["paid"] == b["amount"] and len(b["sevas"]) == 2
        first = td.db().execute("SELECT id FROM bookings WHERE group_id = ?", (b["booking_id"],)).fetchone()[0]
        out = call("manager", "POST", f"/api/bookings/{first}/cancel", {"reason": "x"})[1]
        assert out["status"] == "Cancelled" and td.db().execute("SELECT SUM(amount_paise) FROM refunds WHERE group_id = ?", (b["booking_id"],)).fetchone()[0] == int(b["amount"] * 100 / 2), "sevas are refunded under the same policy"
        assert td.db().execute("SELECT status FROM seva_bookings WHERE group_id = ?", (b["booking_id"],)).fetchone()[0] == "Cancelled"
    finally:
        os.environ.pop("PAYMENT_PROVIDER", None)
        td.settings_(pay_options="counter")
        c = td.db()
        c.execute("DELETE FROM settings WHERE key = 'refund_percent'")
        c.close()


# ---------- festivals, panchanga, peak pricing ----------
def test_festival_drives_peak_pricing():
    base = quote(check_in=D(55), check_out=D(57))[1]["total"]
    for bad in (dict(name="", kind="festival", start_date=D(55)), dict(name="F", kind="nope", start_date=D(55)), dict(name="F", kind="festival", start_date="x"), dict(name="F", kind="festival", start_date=D(57), end_date=D(55)),
                dict(name="F", kind="festival", start_date=D(55), make_peak=True, surcharge_percent=999)):
        assert admin("/api/admin/festivals", bad)[0] == 400, bad
    assert admin("/api/admin/festivals", dict(name="F", kind="festival", start_date=D(55)), "manager")[0] == 403
    st, r = admin("/api/admin/festivals", dict(name="TEST Aradhane", kind="festival", start_date=D(55), end_date=D(56), note="Annual", make_peak=True, surcharge_percent=50, max_nights=3, max_rooms=2))
    assert st == 200
    fid = r["id"]
    q = quote(check_in=D(55), check_out=D(57))[1]
    assert q["total"] == base + 600 and q["night_prices"][0]["price"] == 900.0 and q["night_prices"][0]["peak"] == "TEST Aradhane" and q["night_prices"][1]["price"] == 900.0, "the festival's dates are priced as peak"
    assert quote(check_in=D(55), check_out=D(59))[0] == 400, "the festival's night limit applies"
    pub_f = pub("/api/public/festivals", {}, method="GET")[1]
    f = pub_f["festivals"][0]
    assert f["name"] == "TEST Aradhane" and f["peak"] is True and f["start_date"] == D(55) and "peak_period_id" not in f
    assert g.dispatch("GET", "/api/public/info", {}, None, "1.1.1.1")[1]["festivals"][0]["name"] == "TEST Aradhane"
    # edit: dates move, the peak period moves with them; untick: pricing returns to normal
    assert admin("/api/admin/festivals", dict(id=fid, name="TEST Aradhane", kind="festival", start_date=D(58), end_date=D(59), make_peak=True, surcharge_percent=50))[0] == 200
    assert quote(check_in=D(55), check_out=D(57))[1]["total"] == base and quote(check_in=D(58), check_out=D(59))[1]["night_prices"][0]["price"] == 900.0
    assert td.db().execute("SELECT COUNT(*) FROM peak_periods WHERE name = 'TEST Aradhane'").fetchone()[0] == 1, "one peak period per festival"
    assert admin("/api/admin/festivals", dict(id=fid, name="TEST Aradhane", kind="festival", start_date=D(58), end_date=D(59), make_peak=False))[0] == 200
    assert quote(check_in=D(58), check_out=D(59))[1]["night_prices"][0]["price"] == 600.0 and pub("/api/public/festivals", {}, method="GET")[1]["festivals"][0]["peak"] is False
    admin("/api/admin/festivals", dict(id=fid, name="TEST Aradhane", kind="festival", start_date=D(58), end_date=D(59), make_peak=True, surcharge_percent=10))
    assert admin(f"/api/admin/extras/festivals/{fid}/active", {"active": False})[0] == 200
    assert quote(check_in=D(58), check_out=D(59))[1]["night_prices"][0]["price"] == 600.0 and pub("/api/public/festivals", {}, method="GET")[1]["festivals"] == [], "a switched-off festival takes its peak pricing with it"
    assert admin("/api/admin/extras/festivals/999/active", {"active": False})[0] == 404
    ex = call("admin", "GET", "/api/admin/data")[1]["extras"]["festivals"][0]
    assert ex["peak"]["active"] == 0 and ex["active"] == 0


def test_panchanga_entries():
    for bad in (dict(day="x", tithi="a"), dict(day=D(60)), dict(day=D(60), tithi="x" * 101), dict(delete="x")):
        assert admin("/api/admin/panchanga", bad)[0] == 400, bad
    assert admin("/api/admin/panchanga", dict(day=D(60), tithi="Shukla Dashami"), "manager")[0] == 403
    assert admin("/api/admin/panchanga", dict(day=D(60), tithi="Shukla Dashami", nakshatra="Rohini", note=""))[0] == 200
    st, e = admin("/api/admin/panchanga", {"import": f"{D(61)},Shukla Ekadashi,Mrigashira,\n{D(62)},Dwadashi,,\nbad line"})
    assert st == 400 and "Line 3" in e["error"] and td.db().execute("SELECT COUNT(*) FROM panchanga").fetchone()[0] == 1, "a bad line imports nothing"
    assert admin("/api/admin/panchanga", {"import": f"{D(61)},Shukla Ekadashi,Mrigashira,\n{D(62)},Dwadashi,,Fast ends"})[0] == 200
    p = pub("/api/public/festivals", {"from": D(60), "to": D(62)}, method="GET")[1]["panchanga"]
    assert [x["day"] for x in p] == [D(60), D(61), D(62)] and p[2]["note"] == "Fast ends" and p[0]["nakshatra"] == "Rohini"
    assert pub("/api/public/festivals", {"from": D(62), "to": D(60)}, method="GET")[0] == 400 and pub("/api/public/festivals", {"from": "x"}, method="GET")[0] == 400
    assert admin("/api/admin/panchanga", {"delete": D(60)})[0] == 200 and len(pub("/api/public/festivals", {"from": D(60), "to": D(62)}, method="GET")[1]["panchanga"]) == 2
    assert pub("/api/public/festivals", {"from": D(0), "to": D(60)}, method="GET")[0] == 200
    assert td.db().execute("SELECT COUNT(*) FROM audit_log WHERE action = 'admin.panchanga'").fetchone()[0] >= 3


# ---------- notices ----------
def test_notices_reach_the_site_and_the_confirmation():
    assert call("viewer", "POST", "/api/notices", dict(kind="river", level="warning", text="River in spate"))[0] == 403 and call("housekeeping", "GET", "/api/notices")[0] == 403
    for bad in (dict(kind="x", level="info", text="long enough text"), dict(kind="river", level="x", text="long enough text"), dict(kind="river", level="info", text="abc"), dict(kind="river", level="info", text="x" * 301),
                dict(kind="river", level="info", text="long enough text", starts_on=D(5), ends_on=D(1)), dict(kind="river", level="info", text="long enough text", starts_on=D(0), ends_on=D(200))):
        assert call("frontdesk", "POST", "/api/notices", bad)[0] == 400, bad
    st, r = call("frontdesk", "POST", "/api/notices", dict(kind="river", level="warning", text="Tungabhadra is high: do not bathe at the ghat", starts_on=D(0), ends_on=D(10)))
    assert st == 200
    call("manager", "POST", "/api/notices", dict(kind="weather", level="info", text="Light rain expected this week", starts_on=D(0)))
    call("manager", "POST", "/api/notices", dict(kind="weather", level="watch", text="Heat later in the month", starts_on=D(40), ends_on=D(45)))
    info = g.dispatch("GET", "/api/public/info", {}, None, "1.1.1.1")[1]["notices"]
    assert [n["level"] for n in info] == ["warning", "info"], "worst first; the future one is not shown yet"
    providers.OUTBOX.clear()
    b = td.online(start=5, nights=2)
    assert [n["kind"] for n in b["notices"]] == ["river", "weather"] and b["notices"][0]["text"].startswith("Tungabhadra")
    sms = [x for x in providers.OUTBOX if x["kind"] == "sms"][0]["text"]
    mail = [x for x in providers.OUTBOX if x["kind"] == "email"][0]
    assert "Notice: Tungabhadra" in sms and "Light rain" not in sms, "only a warning goes in the SMS"
    assert "Tungabhadra" in mail["text"] and "Light rain" in mail["text"] and "Tungabhadra" in mail["html"], "the email lists every notice for the stay"
    assert "Tungabhadra" in g.receipt_html(b, "en")
    late = td.online(start=41, nights=2)
    assert [n["text"] for n in late["notices"]] == ["Heat later in the month", "Light rain expected this week"], "each stay gets the notices that cover its dates (the river warning ended on D10; the rain notice has no end)"
    # switching off or expiring removes it
    assert call("frontdesk", "POST", "/api/notices", dict(id=r["id"], kind="river", level="warning", text="Tungabhadra is high", starts_on=D(0), ends_on=D(10), active=False))[0] == 200
    assert [n["kind"] for n in g.dispatch("GET", "/api/public/info", {}, None, "1.1.1.1")[1]["notices"]] == ["weather"]
    assert call("frontdesk", "POST", "/api/notices", dict(id=9999, kind="river", level="info", text="long enough text", starts_on=D(0)))[0] == 404
    assert any(n["text"].startswith("Heat") for n in call("viewer", "GET", "/api/notices")[1])


# ---------- feedback ----------
def checked_out_booking(**kw):
    b = td.online(start=30, nights=2, **kw)
    c = td.db()
    c.execute("UPDATE bookings SET status = 'Checked-Out', check_in = ?, check_out = ? WHERE group_id = ?", (D(-3), D(-1), b["booking_id"]))
    c.close()
    return b


def test_feedback_flow():
    b = checked_out_booking()
    td.settings_(feedback_days="")
    cn = g.connect()
    assert g.extras.feedback_requests(cn) == 0, "off while the setting is empty"
    td.settings_(feedback_days=1)
    providers.OUTBOX.clear()
    assert g.extras.feedback_requests(cn) == 1 and g.extras.feedback_requests(cn) == 0, "asked once"
    sms = [x for x in providers.OUTBOX if x["kind"] == "sms"][0]["text"]
    link = re.search(r"(http\S+#/feedback\?c=(FB1\.\d+\.[0-9a-f]{12}))", sms)
    assert link and "FB1." in [x for x in providers.OUTBOX if x["kind"] == "email"][0]["text"] and not [x for x in providers.OUTBOX if x["kind"] == "email"][0]["attachments"]
    code = link[2]
    assert td.db().execute("SELECT event FROM notification_log WHERE group_id = ? AND event = 'feedback' AND channel = 'sms'", (b["booking_id"],)).fetchone()
    st, info = pub("/api/public/feedback/info", {"code": code}, method="GET")
    assert st == 200 and info["done"] is False and info["check_out"] == D(-1)
    for bad in ("FB1.1.000000000000", code[:-1] + ("0" if code[-1] != "0" else "1"), "junk", ""):
        assert pub("/api/public/feedback/info", {"code": bad}, method="GET")[0] == 404, bad
    for bad in (dict(rating=0), dict(rating=6), dict(rating="x"), dict(rating=5, cleanliness=9), dict(rating=5, comment="x" * 1001), dict(rating=5, website="bot")):
        assert pub("/api/public/feedback", dict(code=code) | bad)[0] == 400, bad
    assert pub("/api/public/feedback", dict(code=code, rating=2, cleanliness=3, service=1, comment="Room was not clean"))[0] == 200
    st, e = pub("/api/public/feedback", dict(code=code, rating=5))
    assert st == 409 and "already" in e["error"] and pub("/api/public/feedback/info", {"code": code}, method="GET")[1]["done"] is True
    assert [a for a in g.alerts_open(cn) if a["kind"] == "feedback"], "a low rating alerts the staff"
    # the admin view
    assert call("frontdesk", "GET", "/api/feedback")[0] == 403
    st, d = call("manager", "GET", "/api/feedback")
    assert st == 200 and d["rows"][0]["rating"] == 2 and d["rows"][0]["guest"] == "Online Guest" and d["rows"][0]["comment"] == "Room was not clean" and d["rows"][0]["status"] == "new"
    assert d["summary"]["count"] == 1 and d["summary"]["rating"] == 2.0 and d["summary"]["distribution"][2] == 1 and d["summary"]["cleanliness"] == 3.0
    assert call("manager", "GET", "/api/feedback", {"min": "3"})[1]["rows"] == [] and len(call("manager", "GET", "/api/feedback", {"max": "2", "status": "new"})[1]["rows"]) == 1
    assert call("manager", "GET", "/api/feedback", {"min": "9"})[0] == 400 and call("manager", "GET", "/api/feedback", {"property_id": 3})[0] == 403
    fid = d["rows"][0]["id"]
    assert call("frontdesk", "POST", f"/api/feedback/{fid}/review", {"staff_note": "x"})[0] == 403 and call("manager", "POST", "/api/feedback/999/review", {"staff_note": "x"})[0] == 404
    assert call("manager", "POST", f"/api/feedback/{fid}/review", {"staff_note": "Spoke to housekeeping"})[0] == 200
    row = call("manager", "GET", "/api/feedback")[1]["rows"][0]
    assert row["status"] == "reviewed" and row["staff_note"] == "Spoke to housekeeping" and row["reviewed_by"]
    assert not [a for a in g.alerts_open(cn) if a["kind"] == "feedback"], "reviewing clears the alert"
    st, t = call("manager", "GET", "/api/reports/data", {"report": "feedback", "from": D(-1), "to": D(0)})
    assert st == 200 and t["rows"][0][3] == 2
    # not asked for walk-ins, cancelled stays or stays that are too old
    b2 = checked_out_booking()
    c = td.db()
    c.execute("UPDATE bookings SET check_in = ?, check_out = ? WHERE group_id = ?", (D(-62), D(-60), b2["booking_id"]))
    assert g.extras.feedback_requests(cn) == 0
    cn.close()
    c.close()


# ---------- newsletter and broadcasts ----------
def test_subscription_needs_consent_and_proof():
    ph = td.phone()
    ok = dict(name="Devotee", phone=ph, sms=True, whatsapp=True, consent=True, lang="en")
    assert pub("/api/public/subscribe", ok | dict(consent=False))[0] == 400 and pub("/api/public/subscribe", {k: v for k, v in ok.items() if k != "consent"})[0] == 400
    assert pub("/api/public/subscribe", dict(consent=True, sms=True))[0] == 400, "nothing to contact"
    assert pub("/api/public/subscribe", dict(consent=True, email_ok=True))[0] == 400 and pub("/api/public/subscribe", dict(consent=True, phone=ph))[0] == 400, "no channel"
    st, e = pub("/api/public/subscribe", ok)
    assert st == 403 and "verify your mobile" in e["error"], "SMS and WhatsApp need a verified number: nobody can sign up someone else"
    assert pub("/api/public/subscribe", ok | dict(otp_proof="made-up"))[0] == 403
    booking_proof = otp_proof(ph, "booking")
    assert pub("/api/public/subscribe", ok | dict(otp_proof=booking_proof))[0] == 403, "a booking proof is not a subscription proof"
    proof = otp_proof(ph, "subscribe")
    assert pub("/api/public/subscribe", ok | dict(otp_proof=proof, website="bot"))[0] == 400
    st, r = pub("/api/public/subscribe", ok | dict(otp_proof=proof))
    assert st == 200 and r["pending_email"] is False
    row = td.db().execute("SELECT * FROM subscribers WHERE phone10 = ?", (g.p10(ph),)).fetchone()
    assert row["ph_ok"] == 1 and row["sms"] == 1 and row["wa"] == 1 and "agree to receive" in row["consent_text"] and row["consent_at"]
    assert pub("/api/public/subscribe", ok | dict(otp_proof=proof))[0] == 403, "a proof works once"
    # email: pending until the link is opened
    providers.OUTBOX.clear()
    st, r = pub("/api/public/subscribe", dict(name="E", email="e@example.org", email_ok=True, consent=True, lang="en"))
    assert st == 200 and r["pending_email"] is True
    mail = [x for x in providers.OUTBOX if x["kind"] == "email"][0]
    tok = re.search(r"subscribe-confirm\?t=([\w-]+)", mail["text"])[1]
    assert not [s for s in __import__("extras").audience(g.connect(), ["email"])["email"]], "not on the list before confirming"
    assert pub("/api/public/subscribe/confirm", {"token": "nope"})[0] == 404 and pub("/api/public/subscribe/confirm", {"token": tok})[0] == 200
    assert len(__import__("extras").audience(g.connect(), ["email"])["email"]) == 1


def test_broadcast_to_opted_in_only_with_unsubscribe():
    os.environ["WHATSAPP_PROVIDER"] = "mock"
    people = {}
    for name, sms, wa in (("A", True, True), ("B", True, False), ("C", False, True)):
        ph = td.phone()
        proof = otp_proof(ph, "subscribe")
        assert pub("/api/public/subscribe", dict(name=name, phone=ph, sms=sms, whatsapp=wa, consent=True, lang="te" if name == "B" else "en", otp_proof=proof))[0] == 200
        people[name] = ph
    st, r = pub("/api/public/subscribe", dict(name="E", email="e2@example.org", email_ok=True, consent=True, lang="en"))
    tok = re.search(r"t=([\w-]+)", [x for x in providers.OUTBOX if x["kind"] == "email"][-1]["text"])[1]
    pub("/api/public/subscribe/confirm", {"token": tok})
    td.db().execute("INSERT INTO subscribers (name, phone, phone10, email, sms, wa, em, ph_ok, em_ok, token, consent_at) VALUES ('Nobody', '9000000009', '9000000009', 'n@example.org', 1, 1, 1, 0, 0, 'tok-never', 'now')")  # never verified
    b = call("manager", "GET", "/api/broadcasts")[1]
    assert b["counts"] == {"sms": 2, "whatsapp": 2, "email": 1} and len(b["subscribers"]) == 5 and "9000" not in str(b["subscribers"]) and "e2@example.org" not in str(b["subscribers"]), "contacts are masked"
    assert call("frontdesk", "GET", "/api/broadcasts")[0] == 403 and call("frontdesk", "POST", "/api/broadcasts", {})[0] == 403
    good = dict(title="Vaikunta Ekadashi", texts={"en": "Rooms fill early for Ekadashi. Book now.", "te": "TELUGU TEXT HERE ok"}, channels=["sms", "whatsapp", "email"], confirm=True)
    for bad in (dict(title=""), dict(texts={"en": "short"}), dict(texts={"te": "no english text at all"}), dict(channels=[]), dict(channels=["fax"]), dict(confirm=False), dict(texts={"en": "x" * 601})):
        assert call("manager", "POST", "/api/broadcasts", good | bad)[0] == 400, bad
    providers.OUTBOX.clear()
    st, out = call("manager", "POST", "/api/broadcasts", good)
    assert st == 200 and out["recipients"] == 4 and out["sent"] == 5 and out["failed"] == 0, out
    sms, wa, mail = [x for x in providers.OUTBOX if x["kind"] == "sms"], [x for x in providers.OUTBOX if x["kind"] == "whatsapp"], [x for x in providers.OUTBOX if x["kind"] == "email"]
    assert sorted(x["to"] for x in sms) == sorted([people["A"], people["B"]]) and sorted(x["to"] for x in wa) == sorted([people["A"], people["C"]]) and [x["to"] for x in mail] == ["e2@example.org"]
    assert "9000000009" not in str([x["to"] for x in providers.OUTBOX]), "someone who never verified gets nothing"
    by = {x["to"]: x["text"] for x in sms}
    assert by[people["B"]].startswith("TELUGU TEXT") and by[people["A"]].startswith("Rooms fill early"), "each in their own language, English as the fallback"
    assert all("#/unsubscribe?t=" in x["text"] for x in providers.OUTBOX), "every message carries an unsubscribe link"
    assert td.db().execute("SELECT COUNT(*) FROM broadcast_log WHERE status = 'sent'").fetchone()[0] == 5
    assert td.db().execute("SELECT COUNT(*) FROM audit_log WHERE action = 'broadcast.send'").fetchone()[0] == 1
    # unsubscribe by the link, by staff, and once more by signing up again
    tokA = td.db().execute("SELECT token FROM subscribers WHERE phone10 = ?", (g.p10(people["A"]),)).fetchone()[0]
    assert pub("/api/public/unsubscribe", {"token": "nope"})[0] == 404 and pub("/api/public/unsubscribe", {"token": tokA})[0] == 200 and pub("/api/public/unsubscribe", {"token": tokA})[0] == 200
    sid = td.db().execute("SELECT id FROM subscribers WHERE phone10 = ?", (g.p10(people["B"]),)).fetchone()[0]
    assert call("frontdesk", "POST", f"/api/subscribers/{sid}/unsubscribe", {})[0] == 403 and call("manager", "POST", f"/api/subscribers/{sid}/unsubscribe", {})[0] == 200 and call("manager", "POST", "/api/subscribers/999/unsubscribe", {})[0] == 404
    assert call("manager", "GET", "/api/broadcasts")[1]["counts"] == {"sms": 0, "whatsapp": 1, "email": 1}
    providers.OUTBOX.clear()
    assert call("manager", "POST", "/api/broadcasts", good | dict(channels=["sms"]))[0] == 400, "nobody left on SMS"
    out = call("manager", "POST", "/api/broadcasts", good | dict(channels=["whatsapp"]))[1]
    assert out["sent"] == 1 and [x["to"] for x in providers.OUTBOX] == [people["C"]]
    # the daily limit
    call("manager", "POST", "/api/broadcasts", good | dict(channels=["whatsapp"]))
    st, e = call("manager", "POST", "/api/broadcasts", good | dict(channels=["whatsapp"]))
    assert st == 429 and "At most 3" in e["error"]
    # a failing gateway is counted, not hidden
    td.db().execute("UPDATE broadcasts SET created_at = '2020-01-01T00:00:00'")
    del os.environ["WHATSAPP_PROVIDER"]
    st, e = call("manager", "POST", "/api/broadcasts", good | dict(channels=["whatsapp"]))
    assert st == 200 and e["failed"] == 1 and e["sent"] == 0


# ---------- PWA ----------
def test_pwa_files_and_page():
    import mantralayam_rooms as m
    man = json.loads((ROOT / "web" / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert man["display"] == "standalone" and man["start_url"] and man["scope"] == "/" and {i["sizes"] for i in man["icons"]} >= {"192x192", "512x512"} and any(i.get("purpose") == "maskable" for i in man["icons"])
    for i in man["icons"]:
        f = ROOT / "web" / i["src"].lstrip("/")
        assert f.is_file() and f.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", i
    assert any(s["url"].endswith("#/rebook") for s in man["shortcuts"]), "the quick re-book shortcut"
    assert m.STATIC["/manifest.webmanifest"][1] == "application/manifest+json" and m.STATIC["/sw.js"][0] == "sw.js"
    sw = (ROOT / "web" / "sw.js").read_text(encoding="utf-8")
    assert '"/api/"' in sw or "/api/" in sw and "startsWith(\"/api/\")" in sw, "the API is never cached"
    page = (ROOT / "web" / "guest.html").read_text(encoding="utf-8")
    for needle in ('rel="manifest"', "serviceWorker.register", "beforeinstallprompt", "#/rebook", 'id="offline"'):
        assert needle in page, needle
    assert "worker-src" not in m.csp() and "'self'" in m.csp(), "the worker is allowed by script-src 'self'"
    # the quick re-book keeps no personal data
    assert 'saveRebook({ property_id' in page and "guest_name" not in re.search(r"function saveRebook.*", page)[0]


if __name__ == "__main__":
    os.environ.pop("PAYMENT_PROVIDER", None)
    try:
        td.setup()
        for name, fn in list(globals().items()):
            if name.startswith("test_") and callable(fn) and not name.startswith("test_staff_desk"):
                try:
                    cleanup()
                    fn()
                except BaseException:
                    print("FAILED:", name, file=sys.stderr)
                    raise
        print("ok")
    finally:
        shutil.rmtree(td.TMP, ignore_errors=True)
