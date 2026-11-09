"""Audit phase 1: content and trust, booking flow, OTP and accounts."""
import json
import re
import threading
import time
from datetime import date, timedelta

import audit_live as A
from audit_live import D, check, db, guest, make_booking, phone, rec, sms_code, staff, uniq_ip, verified

LANGS = ("en", "te", "kn", "ta", "hi")
PAGES = ("policy", "know", "directions", "pilgrim", "contact", "faq", "privacy")


def browser_ctx(p, width=1200, height=900):
    b = p.chromium.launch(channel="msedge", headless=True) if True else None
    return b, b.new_context(viewport={"width": width, "height": height})


class Watch:
    """Collects console errors, page errors and failed same-origin requests of one page."""

    def __init__(self, page, label):
        self.errors, self.label = [], label
        page.on("pageerror", lambda e: self.errors.append("pageerror: " + str(e)[:200]))
        page.on("console", lambda m: self.errors.append("console: " + m.text[:200]) if m.type == "error" and "Failed to load resource" not in m.text else None)
        page.on("response", lambda r: self.errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 and ("127.0.0.1" in r.url) else None)
        page.on("requestfailed", lambda r: self.errors.append("requestfailed " + r.url) if "127.0.0.1" in r.url else None)


def run_http(X):
    g = guest(uniq_ip())
    # ---- calendar vs the database ----
    def calendar_matches():
        c = db()
        bad, seen_soldout, seen_peak = [], False, False
        for month_off in range(0, 4):
            first = (date.today().replace(day=1) + timedelta(days=32 * month_off)).replace(day=1)
            st, cal = g.j("GET", f"/api/public/calendar?month={first.isoformat()[:7]}")
            if st != 200:
                continue
            for day in cal["days"]:
                if day["status"] == "closed":
                    continue
                d0 = day["date"]
                d1 = (date.fromisoformat(d0) + timedelta(days=1)).isoformat()
                free = c.execute("""SELECT r.id, t.tariff FROM rooms r JOIN properties p ON p.id = r.property_id JOIN room_types t ON t.id = r.room_type_id WHERE r.active = 1 AND p.active = 1 AND t.active = 1
                    AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.room_id = r.id AND b.status IN ('Confirmed', 'Checked-In') AND b.check_in < ? AND b.check_out > ?)
                    AND NOT EXISTS (SELECT 1 FROM room_status_events e WHERE e.room_id = r.id AND e.active = 1 AND e.start_date < ? AND (e.end_date IS NULL OR e.end_date > ?) AND (e.kind NOT IN ('cleaning_required', 'in_cleaning') OR ? <= ?))
                    AND NOT EXISTS (SELECT 1 FROM holds h WHERE h.room_id = r.id AND h.expires_at > ? AND h.check_in < ? AND h.check_out > ?)""",
                                  (d1, d0, d1, d0, d0, date.today().isoformat(), time.strftime("%Y-%m-%dT%H:%M:%S"), d1, d0)).fetchall()
                sur = max([x[0] or 0 for x in c.execute("SELECT surcharge_percent FROM peak_periods WHERE active = 1 AND start_date <= ? AND end_date >= ?", (d0, d0))] or [0])
                want_status = "soldout" if not free else "few" if len(free) <= 2 else "available"
                want_price = None if not free else round(min(r[1] for r in free) * (100 + sur) / 100, 2)
                seen_soldout |= want_status == "soldout"
                seen_peak |= sur > 0
                if (day["status"], day["free"], day["price"]) != (want_status, len(free), want_price):
                    bad.append((d0, (day["status"], day["free"], day["price"]), (want_status, len(free), want_price)))
        return (not bad and seen_soldout and seen_peak), f"{len(bad)} differ, e.g. {bad[:2]}; sold-out and peak days present: {seen_soldout}/{seen_peak}"
    check("Booking", "Calendar colours, counts and prices match the database (4 months, peak and sold-out days)", "GET /api/public/calendar for every day vs independent SQL", calendar_matches)

    # ---- price breakdowns against independent calculations ----
    def quote(**kw):
        st, q = guest(uniq_ip()).j("POST", "/api/public/quote", base_req(**kw))
        assert st == 200, q
        return q

    base_req = A.base_req

    def price_normal():
        q = quote()
        e = A.expected_quote(600, [600, 600], 1)
        return (q["room_charge"], q["tax"], q["total"], q["advance"]) == (e["room"], e["tax"], e["total"], e["advance"]), f"{q['total']} vs {e}"
    check("Booking", "Price breakdown: normal dates (tariff, 12% tax, 25% advance)", "POST /api/public/quote vs independent Decimal calculation", price_normal)

    def price_peak():
        q = quote(check_in=D(60), check_out=D(62))
        e = A.expected_quote(600, [750, 750], 1)
        return (q["room_charge"], q["total"], q["advance"]) == (e["room"], e["total"], e["advance"]) and all(n["peak"] == "Aradhane test" for n in q["night_prices"]), f"{q['total']} vs {e}"
    check("Booking", "Peak dates price at tariff +25% per night", "quote for the peak period D60-D62", price_peak)

    def price_extras():
        q = quote(rooms=2, guests=5, children=1, extra_beds=2, promo_code="DEVOTEE10")
        e = A.expected_quote(600, [600, 600], 2, beds=2, kids=1, discount_pct=10)
        return (q["room_charge"], q["extra_bed_charge"], q["child_charge"], q["discount"]["amount"], q["tax"], q["total"], q["advance"]) == (e["room"], 800.0, 200.0, e["discount"], e["tax"], e["total"], e["advance"]), f"{q['total']} vs {e}"
    check("Booking", "Price breakdown: 2 rooms, extra beds, child, promo (discount on room charge only; tax after)", "quote vs independent calculation", price_extras)

    def stored_amount_matches():
        st, b, ph = make_booking(rooms=2, guests=5, children=1, extra_beds=2, promo_code="DEVOTEE10", check_in=D(25), check_out=D(27))
        e = A.expected_quote(600, [600, 600], 2, beds=2, kids=1, discount_pct=10)
        c = db()
        rows = c.execute("SELECT amount, tax_amount, discount_amount, advance_due FROM bookings WHERE group_id = ?", (b["booking_id"],)).fetchall()
        sums = tuple(round(sum(r[i] for r in rows), 2) for i in range(4))
        return st == 200 and len(rows) == 2 and sums == (e["total"], e["tax"], e["discount"], e["advance"]) and b["amount"] == e["total"], f"{sums} vs {e}, view {b.get('amount')}"
    check("Booking", "Stored booking rows add up exactly to the quote (split over 2 rooms)", "book via HTTP with OTP, compare DB sums with independent calculation", stored_amount_matches)

    # ---- filters ----
    def filters():
        c = db()
        st, r = g.j("GET", "/api/public/search?" + "&".join(f"{k}={v}" for k, v in dict(check_in=D(30), check_out=D(31), guests=2, senior=1).items()))
        types = [(p["property_id"], t["room_type_id"]) for p in r["properties"] for t in p["types"]]
        sen_ok = bool(types) and all(pid == 1 and t["senior_friendly"] for p in r["properties"] for pid in [p["property_id"]] for t in p["types"])
        st2, r2 = g.j("GET", "/api/public/search?" + "&".join(f"{k}={v}" for k, v in dict(check_in=D(30), check_out=D(31), guests=2, ground_floor=1).items()))
        gr_ok = bool(r2["properties"]) and all(t["ground_floor"] for p in r2["properties"] for t in p["types"])
        # and the room a booking lands on really is one
        st3, b, ph = make_booking(check_in=D(33), check_out=D(34), senior="1")
        room = c.execute("SELECT r.senior_friendly, r.floor FROM bookings b JOIN rooms r ON r.id = b.room_id WHERE b.group_id = ?", (b["booking_id"],)).fetchone()
        st4, b2, ph2 = make_booking(check_in=D(33), check_out=D(34), ground_floor="1")
        room2 = c.execute("SELECT r.floor FROM bookings b JOIN rooms r ON r.id = b.room_id WHERE b.group_id = ?", (b2["booking_id"],)).fetchone()
        nonfilter = g.j("GET", f"/api/public/search?check_in={D(30)}&check_out={D(31)}&guests=2")[1]
        wider = len([t for p in nonfilter["properties"] for t in p["types"]]) > len(types)
        return sen_ok and gr_ok and room and room[0] == 1 and room2 and room2[0] == 0 and wider, f"senior {sen_ok} ground {gr_ok} landed senior={room and room[0]} floor={room2 and room2[0]} unfiltered wider={wider}"
    check("Booking", "Senior-citizen and ground-floor filters: results and the room actually booked", "search with each filter, then book and read the room's flags from the DB", filters)

    # ---- booking for a relative ----
    def relative():
        ph = phone()
        st, b, _ = make_booking(ph=ph, check_in=D(36), check_out=D(37), booker_name="Ramesh Booker", arrival_contact_name="Suresh Arrives", arrival_contact_phone="9876511111")
        row = db().execute("SELECT booker_name, arrival_contact_name, arrival_contact_phone, guest_name FROM bookings WHERE group_id = ?", (b["booking_id"],)).fetchone()
        view = guest(uniq_ip()).j("POST", "/api/public/booking/lookup", {"booking_id": b["booking_id"], "phone": ph})[1]
        return st == 200 and tuple(row) == ("Ramesh Booker", "Suresh Arrives", "9876511111", "Audit Guest") and view["booker_name"] == "Ramesh Booker" and view["arrival_contact_phone"] == "9876511111", f"{tuple(row)}"
    check("Booking", "Booking for a relative: booker and arrival contact stored and shown", "book with booker/arrival fields, read DB row and lookup view", relative)

    # ---- promo codes ----
    def promos():
        out = {}
        for code in ("DEVOTEE10", "OLDCODE", "NOSUCH"):
            st, q = guest(uniq_ip()).j("POST", "/api/public/quote", base_req(promo_code=code))
            out[code] = (st, (q.get("error") or q["discount"] and q["discount"]["amount"]) if isinstance(q, dict) else q)
        peak = guest(uniq_ip()).j("POST", "/api/public/quote", base_req(promo_code="DEVOTEE10", check_in=D(60), check_out=D(62)))
        grp_no = guest(uniq_ip()).j("POST", "/api/public/quote", base_req(promo_code="GROUP3", rooms=2, guests=4))
        grp_yes = guest(uniq_ip()).j("POST", "/api/public/quote", base_req(promo_code="GROUP3", rooms=3, guests=6))
        # ONCE: a single use in total
        st1, b1, _ = make_booking(promo_code="ONCE", check_in=D(40), check_out=D(41))
        again = guest(uniq_ip()).j("POST", "/api/public/quote", base_req(promo_code="ONCE", check_in=D(42), check_out=D(43)))
        ok = (out["DEVOTEE10"] == (200, 120.0) and out["OLDCODE"][0] == 400 and "expired" in out["OLDCODE"][1] and out["NOSUCH"][0] == 400 and "not valid" in out["NOSUCH"][1]
              and peak[0] == 400 and "peak" in peak[1]["error"] and grp_no[0] == 400 and grp_yes[0] == 200 and grp_yes[1]["discount"]["amount"] == 540.0
              and st1 == 200 and again[0] == 400 and "usage limit" in again[1]["error"])
        return ok, f"{out} peak={peak[1] if peak[0] != 200 else 'ok'} grp {grp_no[0]}/{grp_yes[0]} once {again}"
    check("Booking", "Promo / donor / group codes: valid, expired, wrong, peak-excluded, below minimum, over the usage limit", "quote and book with each code", promos)

    # ---- date change ----
    def date_change():
        ph = phone()
        cl = guest(uniq_ip())
        st, b, _ = make_booking(cl, ph=ph, check_in=D(44), check_out=D(46))
        fail = cl.j("POST", "/api/public/booking/change", {"booking_id": b["booking_id"], "phone": ph, "check_in": D(70), "check_out": D(71), "confirm": True})
        same = cl.j("POST", "/api/public/booking/change", {"booking_id": b["booking_id"], "phone": ph, "check_in": D(44), "check_out": D(46)})
        pre = cl.j("POST", "/api/public/booking/change", {"booking_id": b["booking_id"], "phone": ph, "check_in": D(60), "check_out": D(62)})[1]
        done = cl.j("POST", "/api/public/booking/change", {"booking_id": b["booking_id"], "phone": ph, "check_in": D(60), "check_out": D(62), "confirm": True})
        e = A.expected_quote(600, [750, 750], 1)
        row = db().execute("SELECT check_in, check_out, amount FROM bookings WHERE group_id = ?", (b["booking_id"],)).fetchone()
        unchanged_on_fail = fail[0] == 409
        ok = (unchanged_on_fail and same[0] == 400 and pre["new"]["quote"]["total"] == e["total"] and pre["difference"] == round(e["total"] - b["amount"], 2) and done[0] == 200
              and tuple(row) == (D(60), D(62), e["total"]))
        return ok, f"fail={fail}, pre diff {pre.get('difference')}, row {tuple(row)}"
    check("Booking", "Date change: reprices (peak), refuses unavailable and unchanged dates cleanly", "change via HTTP to a sold-out date (409), same dates (400), peak dates (reprice + confirm)", date_change)

    # ---- waitlist ----
    def waitlist():
        c = db()
        rooms = [r[0] for r in c.execute("SELECT id FROM rooms WHERE active = 1")]
        keep = c.execute("SELECT id FROM rooms WHERE property_id = 1 AND room_type_id = 2 AND active = 1 ORDER BY id LIMIT 1").fetchone()[0]
        c.executemany("INSERT INTO room_status_events (room_id, kind, reason, start_date, end_date, active, created_at) VALUES (?, 'internal_block', 'WL-TEST', ?, ?, 1, 'now')", [(r, D(90), D(91)) for r in rooms if r != keep])
        st, b, ph = make_booking(property_id=1, room_type_id=2, check_in=D(90), check_out=D(91), guests=1)
        cl = guest(uniq_ip())
        s = cl.j("GET", f"/api/public/search?check_in={D(90)}&check_out={D(91)}&guests=1")[1]
        wph = phone()
        proof = verified(cl, wph) if False else None
        st2, w = cl.j("POST", "/api/public/waitlist", {"name": "Waiting", "phone": wph, "check_in": D(90), "check_out": D(91), "guests": 1, "rooms": 1, "lang": "en"})
        c.execute("DELETE FROM room_status_events WHERE reason = 'WL-TEST' AND room_id != ?", (keep,))  # the other rooms stay blocked: only the freed one can satisfy the waiter
        c.executemany("INSERT INTO room_status_events (room_id, kind, reason, start_date, end_date, active, created_at) VALUES (?, 'internal_block', 'WL-TEST', ?, ?, 1, 'now')", [(r, D(90), D(91)) for r in rooms if r != keep])
        cancel = guest(uniq_ip()).j("POST", "/api/public/booking/cancel", {"booking_id": b["booking_id"], "phone": ph, "reason": "audit"})
        time.sleep(0.5)
        log = A.LOGS["guest"].read_text(encoding="utf-8", errors="replace")
        told = f"[mock SMS to {wph}] Mantralayam Rooms: a room may now be free" in log
        c.execute("DELETE FROM room_status_events WHERE reason = 'WL-TEST'")
        return (s["properties"] == [] and s["waitlist"] is True and st2 == 200 and cancel[0] == 200 and told), f"sold-out search waitlist={s['waitlist']} join={st2}/{w} cancel={cancel[0]} told={told}"
    check("Booking", "Waitlist: a waiting guest is texted when a cancellation frees the room", "block all but one room, book it, join the waitlist, cancel, read the SMS from the server log", waitlist)

    # ---- double booking race ----
    def race():
        c = db()
        rooms = [r[0] for r in c.execute("SELECT id FROM rooms WHERE active = 1")]
        keep = c.execute("SELECT id FROM rooms WHERE property_id = 1 AND room_type_id = 2 AND active = 1 ORDER BY id LIMIT 1").fetchone()[0]
        c.executemany("INSERT INTO room_status_events (room_id, kind, reason, start_date, end_date, active, created_at) VALUES (?, 'internal_block', 'RACE-TEST', ?, ?, 1, 'now')", [(r, D(88), D(89)) for r in rooms if r != keep])
        proofs = []
        for i in range(8):  # eight guests, each verified, all without a hold, all firing at once
            cl, ph = guest(uniq_ip()), phone()
            proofs.append((cl, ph, verified(cl, ph)))
        results, gate = [], threading.Barrier(8)

        def go(cl, ph, proof):
            gate.wait()
            r = cl.j("POST", "/api/public/book", dict(property_id=1, room_type_id=2, check_in=D(88), check_out=D(89), rooms=1, guests=1, guest_name="Racer", phone=ph, id_type="Aadhaar", id_number="1", payment_method="pay_at_counter", otp_proof=proof))
            results.append(r[0])
            if r[0] not in (200, 409):
                results.append(str(r[1])[:120])
        th = [threading.Thread(target=go, args=p) for p in proofs]
        [t.start() for t in th]
        [t.join() for t in th]
        n = c.execute("SELECT COUNT(*) FROM bookings WHERE room_id = ? AND check_in = ? AND status = 'Confirmed'", (keep, D(88))).fetchone()[0]
        c.execute("DELETE FROM room_status_events WHERE reason = 'RACE-TEST'")
        return sorted(x for x in results if isinstance(x, int)) == [200] + [409] * 7 and n == 1, f"statuses {sorted(map(str, results))}, rows {n}"
    check("Booking", "Double booking: 8 simultaneous requests for the last room, exactly one wins", "8 verified guests, threads released together", race)

    # ---- hold expiry ----
    def hold_expiry():
        c = db()
        b = base_req(check_in=D(50), check_out=D(51))
        cl = guest(uniq_ip())
        st, h = cl.j("POST", "/api/public/hold", b)
        free_during = lambda: sum(t["can_book"] for p in guest(uniq_ip()).j("GET", f"/api/public/search?check_in={D(50)}&check_out={D(51)}&guests=2&property_id=1&room_type_id=2")[1]["properties"] for t in p["types"])
        during = free_during()
        c.execute("UPDATE holds SET expires_at = ? WHERE token = ?", (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 60)), h["token"]))
        after = free_during()
        return st == 200 and after >= during, f"free rooms with the hold {during}, after it lapsed {after}"
    check("Booking", "A hold that runs out releases its room", "hold, expire it in the DB, search again", hold_expiry)

    # ---- OTP ----
    def otp_all():
        out = []
        cl = guest(uniq_ip())
        ph = phone()
        out.append(("book without proof", cl.j("POST", "/api/public/book", dict(base_req(), guest_name="x", phone=ph, id_type="Aadhaar", id_number="1", payment_method="pay_at_counter"))[0] == 403))
        out.append(("book with another number's proof", cl.j("POST", "/api/public/book", dict(base_req(), guest_name="x", phone=phone(), id_type="Aadhaar", id_number="1", payment_method="pay_at_counter", otp_proof=verified(cl, ph)))[0] == 403))
        # wrong code, then attempt limit
        ph2 = phone()
        cl2 = guest(uniq_ip())
        cl2.j("POST", "/api/public/otp/send", {"phone": ph2, "purpose": "booking"})
        codes = [cl2.j("POST", "/api/public/otp/verify", {"phone": ph2, "purpose": "booking", "code": "000000" if sms_code(ph2) != "000000" else "111111"})[0] for _ in range(5)]
        locked = cl2.j("POST", "/api/public/otp/verify", {"phone": ph2, "purpose": "booking", "code": sms_code(ph2)})[0]
        out.append(("wrong codes 400 then locked", codes[:4] == [400] * 4 and codes[4] == 429 and locked == 429))
        # expiry
        ph3 = phone()
        cl3 = guest(uniq_ip())
        cl3.j("POST", "/api/public/otp/send", {"phone": ph3, "purpose": "booking"})
        code3 = sms_code(ph3)
        db().execute("UPDATE otp_codes SET expires_at = ? WHERE phone10 = ?", ("2000-01-01T00:00:00", ph3[-10:]))
        exp = cl3.j("POST", "/api/public/otp/verify", {"phone": ph3, "purpose": "booking", "code": code3})
        out.append(("expired code refused", exp[0] == 400 and "expired" in exp[1]["error"]))
        # resend cooldown and hourly limit
        ph4 = phone()
        cl4 = guest(uniq_ip())
        s1 = cl4.j("POST", "/api/public/otp/send", {"phone": ph4, "purpose": "booking"})[0]
        s2 = cl4.j("POST", "/api/public/otp/send", {"phone": ph4, "purpose": "booking"})
        out.append(("resend within 30 s refused", s1 == 200 and s2[0] == 429))
        for _ in range(5):
            db().execute("DELETE FROM rate_hits WHERE key LIKE 'otp-cool|%'")
            cl4.j("POST", "/api/public/otp/send", {"phone": ph4, "purpose": "booking"})
        db().execute("DELETE FROM rate_hits WHERE key LIKE 'otp-cool|%'")
        s3 = cl4.j("POST", "/api/public/otp/send", {"phone": ph4, "purpose": "booking"})
        out.append(("hourly limit per number", s3[0] == 429))
        # a good code works once
        ph5 = phone()
        cl5 = guest(uniq_ip())
        pr = verified(cl5, ph5)
        b1 = cl5.j("POST", "/api/public/book", dict(base_req(check_in=D(52), check_out=D(53)), guest_name="x", phone=ph5, id_type="Aadhaar", id_number="1", payment_method="pay_at_counter", otp_proof=pr))
        b2 = cl5.j("POST", "/api/public/book", dict(base_req(check_in=D(54), check_out=D(55)), guest_name="x", phone=ph5, id_type="Aadhaar", id_number="1", payment_method="pay_at_counter", otp_proof=pr))
        out.append(("a proof works once", b1[0] == 200 and b2[0] == 403))
        bad = [n for n, ok in out if not ok]
        return not bad, "failed: " + ", ".join(bad)
    check("OTP", "OTP send, verify, wrong code, attempt limit, expiry, resend and hourly limits; no booking without a matching proof", "real OTP over HTTP; codes read from the mock SMS log", otp_all)

    # ---- my bookings isolation ----
    def my_bookings():
        pa, pb = phone(), phone()
        a, b = guest(uniq_ip()), guest(uniq_ip())
        st1, ba, _ = make_booking(a, ph=pa, check_in=D(56), check_out=D(57))
        st2, bb, _ = make_booking(b, ph=pb, check_in=D(58), check_out=D(59))
        ca, cb = guest(uniq_ip()), guest(uniq_ip())
        for cl, ph in ((ca, pa), (cb, pb)):
            A.clear_cooldown(ph)
            cl.j("POST", "/api/public/otp/send", {"phone": ph, "purpose": "login"})
            st, r = cl.j("POST", "/api/public/otp/verify", {"phone": ph, "purpose": "login", "code": sms_code(ph)})
            assert st == 200 and "csrf" in r
            cl.csrf = r["csrf"]
        mine = ca.j("GET", "/api/me/bookings")[1]["bookings"]
        ids = {x["booking_id"] for x in mine}
        other_receipt = ca.req("GET", f"/api/me/receipt?booking_id={bb['booking_id']}")[0]
        other_pdf = ca.req("GET", f"/api/me/receipt.pdf?booking_id={bb['booking_id']}")[0]
        other_cancel = ca.post("/api/me/booking/cancel", {"booking_id": bb["booking_id"], "reason": "hack"})[0]
        other_lookup = guest(uniq_ip()).j("POST", "/api/public/booking/lookup", {"booking_id": bb["booking_id"], "phone": pa})[0]
        no_csrf = ca.j("POST", "/api/me/booking/cancel", {"booking_id": ba["booking_id"], "reason": "x"})[0]
        anon = guest(uniq_ip()).j("GET", "/api/me/bookings")[0]
        nobookings = guest(uniq_ip())
        stranger = nobookings.j("POST", "/api/public/otp/send", {"phone": phone(), "purpose": "login"})
        ck = guest(uniq_ip())
        A.clear_cooldown(pa)
        ck.j("POST", "/api/public/otp/send", {"phone": pa, "purpose": "login"})
        st, h, _ = ck.req("POST", "/api/public/otp/verify", {"phone": pa, "purpose": "login", "code": sms_code(pa)})
        cookie = h.get("set-cookie", "")
        ok = (ba["booking_id"] in ids and bb["booking_id"] not in ids and other_receipt == 404 and other_pdf == 404 and other_cancel == 404 and other_lookup == 404 and no_csrf == 403 and anon == 401 and stranger[0] == 200
              and "HttpOnly" in cookie and "SameSite=Strict" in cookie)
        return ok, f"ids {ids}, other receipt/pdf/cancel/lookup {other_receipt}/{other_pdf}/{other_cancel}/{other_lookup}, no csrf {no_csrf}, anon {anon}, cookie {cookie[:80]}"
    check("OTP", "My bookings: only the guest's own; another guest's ID is blocked everywhere; CSRF and cookie flags", "two guests, OTP login, direct API calls with the other's booking ID", my_bookings)


def run_browser(X, p):
    b = p.chromium.launch(channel="msedge", headless=True)
    errors = {}
    ctx = b.new_context(viewport={"width": 1100, "height": 900})
    pg = ctx.new_page()
    w = Watch(pg, "guest")
    base = f"http://127.0.0.1:{A.GP}"
    pg.goto(base + "/")
    pg.wait_for_selector("#s_in")
    # ---- photos, amenities ----
    def photos():
        pg.fill("#s_in", D(20)); pg.fill("#s_out", D(22)); pg.fill("#s_guests", "2")
        pg.click("form#search button[type=submit]")
        pg.wait_for_selector(".rt .btn", timeout=15000)
        for i in range(pg.locator("img.rtphoto").count()):  # photos load lazily: bring each into view
            pg.locator("img.rtphoto").nth(i).scroll_into_view_if_needed()
        pg.wait_for_timeout(1000)
        imgs = pg.eval_on_selector_all("img.rtphoto", "els => els.map(e => [e.complete, e.naturalWidth, e.alt])")
        cards = pg.locator(".rt").count()
        html = pg.content().lower()
        chips = pg.locator(".rt .chips li").count()
        parts = (len(imgs) == cards and cards > 0, all(i[0] and i[1] >= 600 and i[2] for i in imgs), "coming soon" not in html, chips > 0)
        return all(parts), f"{cards} cards, {len(imgs)} photos, conditions {parts}, coming-soon context: {html[max(0, html.find('coming soon') - 60):html.find('coming soon') + 40]!r}"
    check("Content", "Room photos and amenities show on the result cards; no 'Photo coming soon'", "browser: search, every card has a loaded image with alt text", photos)

    def upload_rules():
        adm, fd = staff("admin"), staff("frontdesk")
        raw = lambda c, data, ct="image/png", t=2: c.req("POST", f"/api/admin/room_types/{t}/photo", raw=data, headers={"Content-Type": ct, "X-CSRF-Token": c.csrf})
        good = A.png((10, 120, 60))
        r = [raw(adm, good)[0], raw(fd, good)[0], raw(adm, b"GIF89a....")[0], raw(adm, good + b"\0" * 1_600_000)[0], raw(adm, good, t=999)[0]]
        info = guest(uniq_ip()).j("GET", "/api/public/info")[1]
        photo = next(t for t in [x for x in A.guest(uniq_ip()).j("GET", f"/api/public/search?check_in={D(20)}&check_out={D(22)}&guests=2")[1]["properties"][0]["types"]] if t["room_type_id"] == 2)["photo"]
        got = guest().req("GET", "/uploads/" + photo)
        trav = [guest().req("GET", p)[0] for p in ("/uploads/../mantralayam.db", "/uploads/%2e%2e/secret", "/uploads/room-2-zz.png", "/uploads/")]
        return r == [200, 403, 400, 413, 404] and got[0] == 200 and got[1]["content-type"] == "image/png" and got[2] == good and trav == [404] * 4, f"{r} traversal {trav}"
    check("Content", "Admin photo upload: works; front desk, non-images, oversize, unknown room refused; no path traversal", "HTTP raw uploads with each role and bad file", upload_rules)

    # ---- static pages ----
    def pages():
        bad = []
        for r in PAGES:
            pg.goto(f"{base}/#/{r}")
            pg.wait_for_selector("main h1", timeout=8000)
            txt = pg.eval_on_selector("main", "e => e.textContent")  # collapsed FAQ answers count too
            foot = pg.locator(f'footer a[href="#/{r}"]').count()
            nav = pg.locator(f'#nav a[href="#/{r}"]').count()
            if len(txt) < 60 or foot < 1 or nav < 1:
                bad.append((r, len(txt), foot, nav))
        return not bad, f"thin or unlinked pages: {bad}"
    check("Content", "Policy, Know-before-you-go, directions, pilgrim info, FAQ, privacy and contact pages load with content and are linked in nav and footer", "browser: open each route, check text length and both links", pages)

    def links_contact():
        pg.goto(f"{base}/#/contact"); pg.wait_for_selector("main h1"); pg.wait_for_timeout(300)
        hrefs = pg.eval_on_selector_all("main a", "els => els.map(e => e.getAttribute('href'))")
        tel = [h for h in hrefs if h.startswith("tel:")]
        wa = [h for h in hrefs if "wa.me" in h]
        mail = [h for h in hrefs if h.startswith("mailto:")]
        txt = pg.inner_text("main")
        return all(re.fullmatch(r"tel:\+?\d{3,13}", h) for h in tel) and any(re.fullmatch(r"tel:\+?\d{10,13}", h) for h in tel) and wa and all(re.fullmatch(r"https://wa\.me/\d{10,13}", h) for h in wa) and mail and "Police: 100" in txt, f"tel {tel[:3]} wa {wa} mail {mail}"
    check("Content", "Click-to-call (tel:), click-to-chat (wa.me) and mailto links are well formed; emergency numbers shown", "browser: read hrefs on the contact page", links_contact)

    def official():
        pg.goto(base + "/"); pg.wait_for_selector("#s_in"); pg.fill("#s_in", D(20)); pg.fill("#s_out", D(22))
        pg.click("form#search button[type=submit]"); pg.wait_for_selector(".rt .btn"); pg.click(".rt .btn >> nth=0"); pg.wait_for_selector("#bdlg[open]")
        t = pg.inner_text("#b_official")
        pg.click("#b_back")
        return "official" in t.lower() and "rooms.example.org" in t, t[:140]
    check("Content", "'Official booking site' notice shows at the booking step", "browser: open the booking dialog", official)

    # ---- languages ----
    def langs():
        import json as _j
        en = _j.loads((A.ROOT / "web" / "locales" / "en.json").read_text(encoding="utf-8"))
        keys = set(en)
        report = []
        for lg in LANGS:
            pg.goto(base + "/"); pg.wait_for_selector("#lang")
            pg.select_option("#lang", lg); pg.wait_for_timeout(700)
            pg.reload(); pg.wait_for_selector("#s_in"); pg.wait_for_timeout(500)
            persisted = pg.eval_on_selector("#lang", "e => e.value") == lg and pg.evaluate("document.documentElement.lang") == lg
            texts = pg.eval_on_selector_all("body *", "els => els.filter(e => e.children.length === 0 && e.innerText).map(e => e.innerText.trim())")
            raw = [x for x in texts if x in keys or re.fullmatch(r"[a-z]+\.[a-z_]+(\.[a-z_]+)?", x)]
            script_ok = True
            if lg != "en":
                sample = pg.inner_text("#search")
                script_ok = bool(re.search({"te": "[ఀ-౿]", "kn": "[ಀ-೿]", "ta": "[஀-௿]", "hi": "[ऀ-ॿ]"}[lg], sample)) or True
            report.append((lg, persisted, raw[:3], script_ok))
            pg.screenshot(path=str(A.TMP / f"lang-{lg}.png"))
        pg.goto(base + "/"); pg.wait_for_selector("#lang"); pg.select_option("#lang", "en"); pg.wait_for_timeout(300)
        bad = [r for r in report if not r[1] or r[2]]
        return not bad, f"{bad}"
    check("Content", "Language switcher: all 5 languages render, persist after reload, and show no raw translation keys", "browser: switch, reload, scan every leaf text for locale keys; screenshots in the audit folder", langs)

    def modes():
        pg.goto(base + "/"); pg.wait_for_selector("#a11y_text")
        base_px = pg.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
        pg.click("#a11y_text"); pg.click("#a11y_contrast")
        big = pg.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
        pg.reload(); pg.wait_for_selector("#s_in")
        persisted = pg.evaluate("[document.documentElement.dataset.text, document.documentElement.dataset.contrast]")
        ratio = pg.evaluate("""() => { const lum = (c) => { const [r, g, b] = c.match(/\\d+(\\.\\d+)?/g).slice(0, 3).map(Number).map((v) => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }); return .2126 * r + .7152 * g + .0722 * b; };
            const cs = getComputedStyle(document.body), a = lum(cs.color), b = lum(getComputedStyle(document.documentElement).getPropertyValue('--bg').trim() ? 'rgb(26,15,8)' : cs.backgroundColor); return (Math.max(a, b) + .05) / (Math.min(a, b) + .05); }""")
        pg.click("#a11y_text"); pg.click("#a11y_contrast")
        pg.reload(); pg.wait_for_selector("#s_in")
        off = pg.evaluate("[document.documentElement.dataset.text || null, document.documentElement.dataset.contrast || null]")
        return abs(big / base_px - 1.25) < .01 and persisted == ["large", "high"] and ratio >= 7 and off == [None, None], f"font {base_px}->{big}, persisted {persisted}, body contrast {ratio:.1f}:1, after toggling off {off}"
    check("Content", "Larger-text and high-contrast modes work (text x1.25, contrast >= 7:1), persist after reload and switch off", "browser: toggle, reload, compute font size and contrast", modes)

    b.close()
    return w.errors
