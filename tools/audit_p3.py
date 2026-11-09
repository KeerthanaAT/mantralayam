"""Audit phase 3: staff side (roles, check-in with ID, housekeeping, blocks, call-backs, audit log, reports and exports) and growth features (sevas, festivals, notices, feedback,
broadcasts, analytics, PWA)."""
import json
import re
import struct
import subprocess
import sys
import time
from datetime import date, timedelta

import audit_live as A
from audit_live import D, check, db, guest, make_booking, phone, rec, sms_code, staff, uniq_ip, verified
from audit_p2 import log_text, row_id, tick

MARKER = b"\xff\xd8\xff\xe0AUDIT-ID-PHOTO-MARKER-7f3a" + bytes(range(200))


def run_http(X):
    adm, mgr, fd, hk, vw = staff("admin"), staff("manager"), staff("frontdesk"), staff("housekeeping"), staff("viewer")

    # ============ roles ============
    def staff_anon(m_, p):
        c = A.Client(A.SP, uniq_ip())
        return c.j(m_, p, {} if m_ == "POST" else None)[0]

    def roles():
        mgr_only = [("GET", "/api/reports/data?report=arrivals"), ("GET", "/api/reports/csv?report=arrivals"), ("GET", "/api/payments/reconcile"), ("GET", "/api/payments/refunds"), ("GET", "/api/notification_log"), ("GET", "/api/audit"),
                    ("GET", "/api/feedback"), ("GET", "/api/broadcasts"), ("POST", "/api/broadcasts"), ("POST", "/api/payments/refund"), ("POST", "/api/refunds/1/done"), ("GET", "/api/reports"), ("GET", "/api/room_types"),
                    ("GET", "/api/settings")]
        super_only = [("GET", "/api/admin/data"), ("POST", "/api/admin/settings"), ("POST", "/api/admin/users"), ("POST", "/api/admin/rooms"), ("POST", "/api/admin/sevas"), ("POST", "/api/admin/festivals"), ("POST", "/api/admin/promos")]
        desk_only = [("GET", "/api/checkin/lookup?code=1"), ("POST", "/api/bookings/1/id"), ("POST", "/api/bookings/1/payment"), ("POST", "/api/bookings/1/checkin"), ("POST", "/api/bookings"), ("POST", "/api/room_events")]
        bad = []
        for who, cl in (("frontdesk", fd), ("housekeeping", hk), ("viewer", vw)):
            for m_, path in mgr_only + super_only:
                st = cl.j(m_, path, {} if m_ == "POST" else None)[0] if m_ == "GET" else cl.post(path, {})[0]
                if st != 403:
                    bad.append((who, path, st))
        for who, cl in (("housekeeping", hk), ("viewer", vw)):
            for m_, path in desk_only:
                st = cl.j(m_, path)[0] if m_ == "GET" else cl.post(path, {})[0]
                if st != 403:
                    bad.append((who, path, st))
        for m_, path in super_only:
            st = mgr.post(path, {})[0] if m_ == "POST" else mgr.j(m_, path)[0]
            if st != 403:
                bad.append(("manager", path, st))
        anon = [staff_anon(m_, p) for m_, p in mgr_only[:4]]
        # a signed-in POST without the CSRF token, and with another session's token
        no_csrf = fd.j("POST", "/api/callbacks/1/note", {"note": "x"})[0]
        wrong_csrf = fd.j("POST", "/api/callbacks/1/note", {"note": "x"}, headers={"X-CSRF-Token": mgr.csrf})[0]
        return not bad and anon == [401] * 4 and no_csrf == 403 and wrong_csrf == 403, f"unexpected: {bad[:5]} anon {anon} csrf {no_csrf}/{wrong_csrf}"
    check("Staff", "Roles: front desk, housekeeping and viewer get 403 on every manager/admin API, manager gets 403 on admin APIs; anonymous 401; CSRF enforced", "direct API calls with each role's session", roles)

    # ============ check-in, ID, QR, housekeeping ============
    def checkin_flow():
        ph = phone()
        st, b, _ = make_booking(ph=ph, check_in=D(0), check_out=D(1), guests=2)
        code = b["checkin_code"]
        lk = fd.j("GET", f"/api/checkin/lookup?code={code}")[1]
        bid = lk["rows"][0]["id"]
        room_id = lk["rows"][0]["room_id"]
        refused = fd.post(f"/api/bookings/{bid}/checkin")
        cap = fd.post(f"/api/bookings/{bid}/id", {"id_type": "Passport", "id_number": "P7654321"})
        img = fd.req("POST", f"/api/bookings/{bid}/id_image", raw=MARKER, headers={"Content-Type": "image/jpeg", "X-CSRF-Token": fd.csrf})
        cin = fd.post(f"/api/bookings/{bid}/checkin")
        board = hk.j("GET", "/api/housekeeping")[1]
        occ = next(r for r in board["rooms"] if r["id"] == room_id)["state"]
        cout = fd.post(f"/api/bookings/{bid}/checkout")
        dirty = next(r for r in hk.j("GET", "/api/housekeeping")[1]["rooms"] if r["id"] == room_id)
        s1 = hk.post(f"/api/room_events/{dirty['event_id']}/start_cleaning")[0]
        clean = next(r for r in hk.j("GET", "/api/housekeeping")[1]["rooms"] if r["id"] == room_id)["state"]
        s2 = hk.post(f"/api/room_events/{dirty['event_id']}/release")[0]
        ready = next(r for r in hk.j("GET", "/api/housekeeping")[1]["rooms"] if r["id"] == room_id)["state"]
        X.checked = (b, ph, bid)
        ok = (refused[0] == 409 and cap[0] == 200 and img[0] == 200 and cin[0] == 200 and cin[1]["status"] == "Checked-In" and occ == "occupied" and cout[0] == 200 and dirty["state"] == "to_clean"
              and s1 == 200 and clean == "cleaning" and s2 == 200 and ready == "ready")
        return ok, f"refused-without-ID {refused[0]}, states {occ}/{dirty['state']}/{clean}/{ready}"
    check("Staff", "Check-in by QR lookup: refused without ID; ID + photo capture; check-in, check-out; housekeeping board to_clean -> cleaning -> ready", "live: booking, QR lookup, ID capture, raw photo upload, check-in/out, board", checkin_flow)

    def id_private():
        b, ph, bid = X.checked
        raw_db = A.DB.read_bytes()
        files = [p for p in (A.TMP / "uploads").glob("*")] if (A.TMP / "uploads").exists() else []
        on_disk = any(b"AUDIT-ID-PHOTO-MARKER" in p.read_bytes() for p in files)
        public = [guest(uniq_ip()).req("GET", p)[0] for p in (f"/uploads/id-{bid}.jpg", f"/uploads/{bid}", f"/api/bookings/{bid}/id_image", f"/images/id-{bid}.jpg")]
        anon = A.Client(A.SP, uniq_ip()).req("GET", f"/api/bookings/{bid}/id_image?reason=audit")[0]
        fdv = fd.req("GET", f"/api/bookings/{bid}/id_image?reason=audit")[0]
        no_reason = mgr.req("GET", f"/api/bookings/{bid}/id_image")[0]
        got = mgr.req("GET", f"/api/bookings/{bid}/id_image?reason=audit+check")
        num = mgr.post(f"/api/bookings/{bid}/id_reveal", {"reason": "audit check"})[1]
        fd_reveal = fd.post(f"/api/bookings/{bid}/id_reveal", {"reason": "peek"})[0]
        row = fd.j("GET", f"/api/bookings?q={ph}")[1]
        shown = json.dumps(row, ensure_ascii=False)
        ok = (b"AUDIT-ID-PHOTO-MARKER" not in raw_db and b"P7654321" not in raw_db and not on_disk and public == [404, 404, 404, 404] and anon == 401 and fdv == 403 and no_reason == 400
              and got[0] == 200 and got[2] == MARKER and num.get("id_number") == "P7654321" and fd_reveal == 403 and "P7654321" not in shown and "…4321" in shown)
        return ok, f"db leak {b'AUDIT-ID-PHOTO-MARKER' in raw_db}/{b'P7654321' in raw_db}, disk {on_disk}, public {public}, anon {anon}, fd {fdv}, no-reason {no_reason}, mgr roundtrip {got[0]}, fd list shows {'P7654321' in shown}, list: {shown[:160]}"
    check("Staff", "ID photo and number are encrypted in the database, absent from disk and public paths; only a manager with a reason can view them; staff lists show only the last 4", "search the raw DB file and upload folder for the photo marker / number; request every public path", id_private)

    def blocked_rooms():
        c = db()
        room = c.execute("SELECT r.id FROM rooms r WHERE r.property_id = 1 AND r.room_type_id = 2 AND r.active = 1 ORDER BY r.id LIMIT 1").fetchone()[0]
        count = lambda: next(d for d in guest(uniq_ip()).j("GET", f"/api/public/calendar?month={D(86)[:7]}&property_id=1&room_type_id=2")[1]["days"] if d["date"] == D(86))["free"]
        before = count()
        ev = fd.post("/api/room_events", {"room_id": room, "kind": "maintenance", "issue_category": "Plumbing", "reason": "Leak", "start_date": D(85), "end_date": D(88)})
        after = count()
        walk = fd.post("/api/bookings", {"property_id": 1, "room_id": room, "guest_name": "Walk", "phone": phone(), "id_type": "Aadhaar", "id_number": "1", "guests": 1, "check_in": D(86), "check_out": D(87)})
        cal = fd.j("GET", f"/api/calendar?property_id=1&start={D(85)}&days=4")[1]
        cell = next(r for r in cal["rooms"] if r["id"] == room)["cells"][1]
        hk_try = hk.post("/api/room_events", {"room_id": room, "kind": "internal_block", "reason": "x", "end_date": D(90)})[0]
        rel = fd.post(f"/api/room_events/{ev[1]['id']}/release")[0]
        back = count()
        return ev[0] == 200 and after == before - 1 and walk[0] == 409 and "maintenance" in walk[1]["error"] and cell and cell["status"] == "maintenance" and hk_try == 403 and rel == 200 and back == before, f"free {before}->{after}->{back}, walk-in {walk[0]} {walk[1]}"
    check("Staff", "A blocked room: leaves the guest search, cannot be booked at the desk (clear message), shows in the calendar; only front desk+ can block; clearing restores it", "create a maintenance block, search, walk-in, calendar, release", blocked_rooms)

    def callbacks():
        g = guest(uniq_ip())
        st, r = g.j("POST", "/api/public/callback", {"name": "Audit Caller", "phone": phone(), "call_time": "morning", "lang": "ta", "property_id": 1})
        rows = fd.j("GET", "/api/callbacks")[1]
        cid = next(x for x in rows if x["name"] == "Audit Caller")["id"]
        t0 = time.time()
        s1 = fd.post(f"/api/callbacks/{cid}/status", {"status": "contacted", "note": "Rang, will confirm"})
        s2 = mgr.post(f"/api/callbacks/{cid}/note", {"note": "Wants ground floor"})
        s3 = fd.post(f"/api/callbacks/{cid}/status", {"status": "follow_up", "follow_up_on": D(2), "note": "Call after lunch"})
        bad_close = fd.post(f"/api/callbacks/{cid}/status", {"status": "closed"})[0]
        s4 = mgr.post(f"/api/callbacks/{cid}/status", {"status": "closed", "note": "Booked 2 rooms"})
        notes = db().execute("SELECT user_name, note, status_to, created_at FROM callback_notes WHERE callback_id = ? ORDER BY id", (cid,)).fetchall()
        who = {u[0]: u[1] for u in db().execute("SELECT username, full_name FROM users")}
        names = [n["user_name"] for n in notes]
        fresh = all(abs(time.mktime(time.strptime(n["created_at"], "%Y-%m-%dT%H:%M:%S")) - t0) < 120 for n in notes)
        ok = (st == 200 and s1[0] == s2[0] == s3[0] == s4[0] == 200 and bad_close == 400 and names == [who["frontdesk"], who["manager"], who["frontdesk"], who["manager"]] and [n["status_to"] for n in notes] == ["contacted", None, "follow_up", "closed"] and fresh)
        return ok, f"statuses {[s1[0], s2[0], s3[0], s4[0]]}, close-without-note {bad_close}, names {names}, timestamps fresh {fresh}"
    check("Staff", "Call-back queue: status workflow, notes saved with timestamp and the acting staff member's name; closing needs a note", "guest call-back, then staff actions as two users; read callback_notes", callbacks)

    def audit_log():
        rows = mgr.j("GET", "/api/audit?sensitive=1")[1]
        acts = {r["action"] for r in rows} | {r["action"] for r in adm.j("GET", "/api/audit?sensitive=1")[1]}  # admin changes carry no property: only the super admin sees them
        want = {"id.capture", "id.photo", "id.reveal", "id.view_image", "room.block", "callback.closed", "admin.room_photo"}
        everything = json.dumps(rows)
        return (want <= acts) and "P7654321" not in everything and "1234-5678-9012" not in everything, f"missing {sorted(want - acts)}; present sample {sorted(acts)[:12]}"
    check("Staff", "Audit log records sensitive actions (ID capture/reveal/photo, blocks, call-backs, uploads, cancellations) and never the ID number", "read /api/audit?sensitive=1 after the actions above", audit_log)

    # ============ reports ============
    def reports():
        c = db()
        today = D(0)
        arr_db = c.execute("SELECT COUNT(*) FROM bookings WHERE check_in = ? AND status IN ('Confirmed', 'Checked-In', 'Checked-Out')", (today,)).fetchone()[0]
        dep_db = c.execute("SELECT COUNT(*) FROM bookings WHERE check_out = ? AND status IN ('Confirmed', 'Checked-In', 'Checked-Out')", (today,)).fetchone()[0]
        arr = mgr.j("GET", f"/api/reports/data?report=arrivals&date={today}")[1]
        dep = mgr.j("GET", f"/api/reports/data?report=departures&date={today}")[1]
        occ = mgr.j("GET", f"/api/reports/data?report=occupancy&from={D(20)}&to={D(20)}")[1]["rows"]
        occ_db = {r[0]: r[1] for r in c.execute("SELECT p.name, COUNT(DISTINCT b.room_id) FROM properties p LEFT JOIN bookings b ON b.property_id = p.id AND b.status IN ('Confirmed', 'Checked-In', 'Checked-Out') AND b.check_in <= ? AND b.check_out > ? GROUP BY p.id", (D(20), D(20)))}
        occ_ok = all(r[4] == occ_db[r[1]] for r in occ)
        csv_ = mgr.req("GET", f"/api/reports/csv?report=arrivals&date={today}")
        lines = csv_[2].decode("utf-8-sig").strip().splitlines()
        sheets = mgr.post("/api/reports/export", {"report": "arrivals", "date": today})
        return (len(arr["rows"]) == arr_db and len(dep["rows"]) == dep_db and occ_ok and csv_[0] == 200 and csv_[1]["content-type"].startswith("text/csv") and "attachment" in csv_[1]["content-disposition"]
                and len(lines) == arr_db + 1 and sheets[1].get("exported") == "sheets"), f"arrivals {len(arr['rows'])}/{arr_db}, departures {len(dep['rows'])}/{dep_db}, occupancy matches {occ_ok}, csv lines {len(lines)}, sheets {sheets[1]}"
    check("Reports", "Arrivals, departures, occupancy match the database; CSV download has header + one line per row; Google Sheets (mock) export works", "reports API vs SQL on the seeded data; CSV; export with SHEETS_PROVIDER=mock", reports)

    def sheets_fallback():
        A.start("staff2", A.XP, {"SHEETS_PROVIDER": "", "GOOGLE_SERVICE_ACCOUNT_JSON": "", "GOOGLE_SHEETS_ID": ""}, script="mantralayam_staff.py")
        try:
            c = A.Client(A.XP, uniq_ip())
            st, out = c.j("POST", "/api/login", {"username": "manager", "password": A.PW})
            c.csrf = out["csrf"]
            r = c.post("/api/reports/export", {"report": "arrivals", "date": D(0)})
            bad = A.Client(A.XP, uniq_ip())
            return r[0] == 200 and r[1]["exported"] == "csv" and "not configured" in r[1]["message"], str(r)
        finally:
            A.stop("staff2")
    check("Reports", "With Google Sheets unconfigured the export falls back to CSV with a clear message", "second staff server with the Sheets env removed", sheets_fallback)

    # ============ growth ============
    def sevas():
        st, b, ph = make_booking(property_id=1, room_type_id=2, check_in=D(10), check_out=D(12), guests=2, sevas=[{"seva_id": X.sev, "date": D(11), "qty": 2}, {"seva_id": X.sev2, "date": D(10), "qty": 1}])
        e = A.expected_quote(600, [600, 600], 1, seva=500 * 2 + 100)
        # package: 10% off the seva, one place per guest, on day 0; capacity 4 a day
        st2, q2 = guest(uniq_ip()).j("POST", "/api/public/quote", A.base_req(check_in=D(13), check_out=D(15), guests=3, package_id=X.pkg))
        e2 = A.expected_quote(600, [600, 600], 1, seva=450 * 3)
        full = guest(uniq_ip()).j("POST", "/api/public/quote", A.base_req(check_in=D(13), check_out=D(15), guests=5, package_id=X.pkg))
        stp, bp, php = make_booking(check_in=D(13), check_out=D(15), guests=3, package_id=X.pkg)
        over = guest(uniq_ip()).j("POST", "/api/public/quote", A.base_req(check_in=D(13), check_out=D(15), guests=2, package_id=X.pkg, sevas=[{"seva_id": X.sev, "date": D(13), "qty": 2}]))
        sv = db().execute("SELECT COUNT(*), SUM(amount) FROM seva_bookings WHERE group_id = ? AND status = 'Confirmed'", (b["booking_id"],)).fetchone()
        guest(uniq_ip()).j("POST", "/api/public/booking/cancel", {"booking_id": bp["booking_id"], "phone": php, "reason": "audit"})
        freed = guest(uniq_ip()).j("POST", "/api/public/quote", A.base_req(check_in=D(13), check_out=D(15), guests=2, package_id=X.pkg, sevas=[{"seva_id": X.sev, "date": D(13), "qty": 2}]))[0]
        ok = (st == 200 and b["amount"] == e["total"] and tuple(sv) == (2, 1100.0) and st2 == 200 and q2["total"] == e2["total"] and q2["seva_charge"] == 1350.0 and full[0] == 409 and stp == 200 and bp["amount"] == e2["total"]
              and over[0] == 409 and "full" in over[1]["error"] and freed == 200)
        return ok, f"booking {b['amount']} vs {e['total']}, package {q2['total']} vs {e2['total']}, 5 guests {full[0]}, over capacity {over[0]}, after cancel {freed}"
    check("Growth", "Sevas and room + seva packages: price, discount, per-day capacity, booked amounts, capacity freed on cancel", "quote/book with sevas and the package; independent totals", sevas)

    def festival_peak():
        base = guest(uniq_ip()).j("POST", "/api/public/quote", A.base_req(check_in=D(80), check_out=D(82)))[1]
        cal = guest(uniq_ip()).j("GET", f"/api/public/calendar?month={D(80)[:7]}")[1]
        day = next(d for d in cal["days"] if d["date"] == D(80))
        fid = db().execute("SELECT id FROM festivals WHERE name = 'Aradhane'").fetchone()[0]
        off = adm.post(f"/api/admin/extras/festivals/{fid}/active", {"active": False})
        plain = guest(uniq_ip()).j("POST", "/api/public/quote", A.base_req(check_in=D(80), check_out=D(82)))[1]
        adm.post(f"/api/admin/extras/festivals/{fid}/active", {"active": True})
        pub = guest(uniq_ip()).j("GET", "/api/public/festivals")[1]
        e = A.expected_quote(600, [780, 780], 1)
        e0 = A.expected_quote(600, [600, 600], 1)
        return base["total"] == e["total"] and plain["total"] == e0["total"] and day["peak"] == "Aradhane" and any(f["name"] == "Aradhane" and f["peak"] for f in pub["festivals"]) and any(p["tithi"] == "Shukla Dashami" for p in pub["panchanga"]), f"peak {base['total']} vs {e['total']}, off {plain['total']} vs {e0['total']}, calendar peak {day['peak']}"
    check("Growth", "Festival calendar drives peak pricing (+30%) while active, price returns to normal when switched off; panchanga entries are published", "quote and calendar with the festival on and off", festival_peak)

    def notices():
        w = mgr.post("/api/notices", {"kind": "river", "level": "warning", "text": "Tungabhadra is in spate: no bathing at the ghat.", "starts_on": D(0), "ends_on": D(60)})
        site = guest(uniq_ip()).j("GET", "/api/public/info")[1]["notices"]
        before = len(log_text())
        st, b, ph = make_booking(check_in=D(16), check_out=D(17))
        time.sleep(0.3)
        sms = log_text()[before:]
        view = b["notices"]
        import mantralayam_rooms as m
        import providers
        m.OTP_REQUIRED = False
        providers.OUTBOX.clear()
        m.reset_throttle()
        ip = uniq_ip()
        hold = m.dispatch("POST", "/api/public/hold", None, A.base_req(check_in=D(18), check_out=D(19)), ip)[1]
        m.reset_throttle()
        m.dispatch("POST", "/api/public/book", None, A.base_req(check_in=D(18), check_out=D(19)) | dict(guest_name="N", phone=phone(), id_type="Aadhaar", id_number="1", email="n@example.org", payment_method="pay_at_counter", hold_token=hold["token"]), ip)
        mail = next(x for x in providers.OUTBOX if x["kind"] == "email")
        ok = w[0] == 200 and any("Tungabhadra" in n["text"] for n in site) and "Notice: Tungabhadra" in sms and any("Tungabhadra" in n["text"] for n in view) and "Tungabhadra" in mail["text"] and "Tungabhadra" in mail["html"]
        mgr.post("/api/notices", {"id": w[1]["id"], "kind": "river", "level": "warning", "text": "Tungabhadra is in spate", "starts_on": D(0), "active": False})
        gone = not any("Tungabhadra" in n["text"] for n in guest(uniq_ip()).j("GET", "/api/public/info")[1]["notices"])
        return ok and gone, f"site {len(site)}, sms has notice {'Notice: Tungabhadra' in sms}, view {len(view)}, email {'Tungabhadra' in mail['text']}, gone after switch-off {gone}"
    check("Growth", "Weather/river notice set by staff shows on the site, in the booking view, the SMS (warning) and the confirmation email; switching it off removes it", "post as manager, read info, book, read SMS log and captured email", notices)

    def feedback():
        b, ph, bid = X.checked
        gid = b["booking_id"]
        db().execute("UPDATE bookings SET check_in = ?, check_out = ? WHERE group_id = ?", (D(-3), D(-1), gid))  # it checked out yesterday
        out = tick()
        m_ = re.search(rf"#/feedback\?c=(FB1\.{gid}\.[0-9a-f]{{12}})", out.stdout)
        code = m_[1] if m_ else None
        g = guest(uniq_ip())
        info = g.j("GET", f"/api/public/feedback/info?code={code}")
        bad = [g.j("POST", "/api/public/feedback", {"code": code, "rating": r})[0] for r in (0, 6)]
        sub = g.j("POST", "/api/public/feedback", {"code": code, "rating": 2, "cleanliness": 3, "service": 1, "comment": "Room was noisy"})
        dup = g.j("POST", "/api/public/feedback", {"code": code, "rating": 5})[0]
        fake = g.j("POST", "/api/public/feedback", {"code": "FB1.1.000000000000", "rating": 5})[0]
        adminv = mgr.j("GET", "/api/feedback")[1]
        alert = [a for a in mgr.j("GET", "/api/dashboard")[1].get("alerts", []) if a["kind"] == "feedback"]
        parts = (bool(code), info[0] == 200, bad == [400, 400], sub[0] == 200, dup == 409, fake == 404, any(r["group_id"] == gid and r["rating"] == 2 and r["comment"] == "Room was noisy" for r in adminv["rows"]), adminv["summary"]["count"] >= 1, bool(alert))
        ok = all(parts)
        return ok, f"parts {parts}, rows {[(r['group_id'], r['rating']) for r in adminv['rows']]}, gid {gid}, link {code}, info {info[0]}, invalid {bad}, submit {sub[0]}, duplicate {dup}, forged {fake}, admin sees it {adminv['summary']['count']}, alert {bool(alert)}"
    check("Growth", "Feedback: link sent after check-out by the job; rating validated; one per stay; admin list, averages and low-rating alert", "tick CLI, public feedback API, manager API", feedback)

    def broadcast():
        import mantralayam_rooms as m
        cl, ph, ph2 = guest(uniq_ip()), phone(), phone()
        subs = []
        for who in (ph, ph2):
            proof = verified(cl, who, "subscribe")
            st, r = cl.j("POST", "/api/public/subscribe", {"name": "Sub", "phone": who, "sms": True, "whatsapp": True, "consent": True, "lang": "en", "otp_proof": proof})
            subs.append(st)
        no_consent = cl.j("POST", "/api/public/subscribe", {"phone": ph, "sms": True, "consent": False})[0]
        no_proof = cl.j("POST", "/api/public/subscribe", {"phone": phone(), "sms": True, "consent": True})[0]
        tok = db().execute("SELECT token FROM subscribers WHERE phone10 = ?", (ph2[-10:],)).fetchone()[0]
        unsub = [cl.j("POST", "/api/public/unsubscribe", {"token": tok})[0] for _ in range(2)]
        slog = lambda: A.LOGS["staff"].read_text(encoding="utf-8", errors="replace")  # broadcasts are sent by the staff server
        before = len(slog())
        st, out = mgr.post("/api/broadcasts", {"title": "Aradhane", "texts": {"en": "Rooms fill early for Aradhane. Book now."}, "channels": ["sms", "whatsapp"], "confirm": True})
        time.sleep(0.3)
        sent = slog()[before:]
        ok = (subs == [200, 200] and no_consent == 400 and no_proof == 403 and unsub == [200, 200] and st == 200 and f"[mock SMS to {ph}]" in sent and f"[mock WhatsApp to {ph}]" in sent and ph2 not in sent and "#/unsubscribe?t=" in sent)
        return ok, f"signups {subs}, no consent {no_consent}, no proof {no_proof}, broadcast {st} {out}, unsubscribed number got message: {ph2 in sent}"
    check("Growth", "Broadcast: opt-in needs consent and a verified number; unsubscribed people get nothing; every message has an unsubscribe link", "live sign-ups with OTP, unsubscribe, broadcast; read the mock SMS/WhatsApp log", broadcast)

    def analytics_privacy():
        guest(uniq_ip()).j("GET", f"/api/public/search?check_in={D(20)}&check_out={D(21)}&guests=2")
        c = db()
        cols = [r[1] for r in c.execute("PRAGMA table_info(analytics_daily)")]
        rows = c.execute("SELECT * FROM analytics_daily").fetchall()
        dump = json.dumps([tuple(r) for r in rows])
        kinds = {r["kind"] for r in rows}
        pii = re.findall(r"98\d{8}|10\.\d+\.\d+\.\d+|@|Audit Guest|Pay Guest", dump)
        return cols == ["day", "kind", "key", "n"] and {"search", "hold", "submit", "booked"} <= kinds and not pii, f"columns {cols}, kinds {sorted(kinds)}, personal data found: {pii[:3]}"
    check("Growth", "Analytics events are recorded (search, hold, submit, booked, sold-out nights) with no personal data (no IP, phone, name, email)", "inspect analytics_daily after all the traffic of this audit", analytics_privacy)

    def pwa_files():
        g = guest(uniq_ip())
        mf = g.req("GET", "/manifest.webmanifest")
        man = json.loads(mf[2])
        icons = []
        for i in man["icons"]:
            st, h, data = g.req("GET", i["src"])
            w, hgt = struct.unpack(">II", data[16:24]) if data[:8] == b"\x89PNG\r\n\x1a\n" else (0, 0)
            icons.append((st, f"{w}x{hgt}" == i["sizes"]))
        sw = g.req("GET", "/sw.js")
        ok = (mf[0] == 200 and "manifest+json" in mf[1]["content-type"] and man["display"] == "standalone" and man["start_url"] and all(i == (200, True) for i in icons) and sw[0] == 200 and "javascript" in sw[1]["content-type"]
              and b"addEventListener(\"fetch\"" in sw[2] and b"/api/" in sw[2])
        return ok, f"icons {icons}, sw {sw[0]} {sw[1].get('content-type')}"
    check("Growth", "PWA: manifest valid, icons exist with the declared sizes, service worker served as JavaScript with a fetch handler that skips the API", "HTTP fetches and PNG header sizes", pwa_files)


def run_browser(X, p):
    base = f"http://127.0.0.1:{A.GP}"
    sbase = f"http://127.0.0.1:{A.SP}"
    b = p.chromium.launch(channel="msedge", headless=True)
    errs = []

    def watch(pg, label):
        pg.on("pageerror", lambda e: errs.append(f"{label} pageerror {str(e)[:150]}"))
        pg.on("response", lambda r: errs.append(f"{label} HTTP {r.status} {r.url}") if r.status >= 400 and "127.0.0.1" in r.url and "/api/" not in r.url else None)

    # ---- PWA: install criteria, service worker, offline shell ----
    ctx = b.new_context(viewport={"width": 420, "height": 800})
    pg = ctx.new_page()
    watch(pg, "guest")
    pg.goto(base + "/")
    pg.wait_for_selector("#s_in")
    pg.wait_for_function("navigator.serviceWorker && navigator.serviceWorker.controller || true")
    pg.evaluate("navigator.serviceWorker.ready.then(() => true)")
    pg.reload(); pg.wait_for_selector("#s_in")
    controlled = pg.evaluate("!!navigator.serviceWorker.controller")

    def installable():
        cdp = ctx.new_cdp_session(pg)
        cdp.send("Page.enable")
        errors = cdp.send("Page.getInstallabilityErrors")["installabilityErrors"]
        real = [e for e in errors if e["errorId"] != "in-incognito"]  # Playwright's browser contexts are always incognito, which blocks installing: not a defect of the page
        return controlled and not real, f"controlled by the service worker: {controlled}; installability errors: {errors}"
    check("Growth", "PWA is installable (Chromium installability check passes) and the service worker controls the page", "CDP Page.getInstallabilityErrors after the worker registered", installable)

    def offline():
        ctx.set_offline(True)
        try:
            pg.reload()
            try:
                pg.wait_for_selector("a[href^='tel:']", timeout=8000)
            except Exception:
                return False, f"url {pg.url!r} title {pg.title()!r} body {pg.evaluate('document.body ? document.body.innerText.slice(0, 160) : null')!r} sw {pg.evaluate('!!navigator.serviceWorker.controller')}"
            pg.wait_for_timeout(500)
            banner = pg.inner_text("#view")
            shell = pg.locator(".brand").count() > 0 and pg.locator("#nav").count() > 0
        finally:
            ctx.set_offline(False)
        return shell and "offline" in banner.lower() and pg.locator("#view a[href^='tel:']").count() > 0, f"offline page text: {banner[:200]!r}"
    check("Growth", "Offline: the shell still loads and shows the properties' phone numbers", "browser offline + reload", offline)

    def rebook():
        pg.goto(base + "/"); pg.wait_for_selector("#s_in")
        pg.fill("#s_in", D(20)); pg.fill("#s_out", D(22)); pg.fill("#s_guests", "3")
        pg.evaluate("localStorage.setItem('rebook', JSON.stringify({property_id: 1, room_type_id: 2, guests: 3, rooms: 1, nights: 2}))")
        pg.goto(base + "/#/rebook"); pg.wait_for_selector("#s_in")
        pg.wait_for_timeout(300)
        g_ = pg.input_value("#s_guests"); nights = (date.fromisoformat(pg.input_value("#s_out")) - date.fromisoformat(pg.input_value("#s_in"))).days
        stored = pg.evaluate("localStorage.getItem('rebook')")
        return g_ == "3" and nights == 2 and "phone" not in stored and "name" not in stored, f"guests {g_}, nights {nights}, stored {stored}"
    check("Growth", "Quick re-book shortcut prefills guests and nights and stores no personal data", "browser: #/rebook", rebook)

    # ---- roles in the staff UI ----
    def staff_ui():
        s = b.new_context(viewport={"width": 1300, "height": 900}).new_page()
        watch(s, "staff")
        s.goto(sbase + "/staff"); s.fill("#u", "frontdesk"); s.fill("#p", A.PW); s.click("button[type=submit]"); s.wait_for_selector("#navlist a")
        nav = s.locator("#navlist a").all_inner_texts()
        hidden = [n for n in ("Payments", "Messages", "Feedback", "Broadcasts", "Reports", "Audit log", "Admin") if n in nav]
        landed = []
        for route in ("payments", "reports", "audit", "admin", "broadcasts"):
            s.goto(f"{sbase}/staff#/{route}"); s.wait_for_selector("main h1"); s.wait_for_timeout(300)
            landed.append(s.inner_text("main h1"))
        return not hidden and all(h in ("Today", "Dashboard") or h.lower().startswith("today") for h in landed), f"nav {nav}, hidden-but-visible {hidden}, landing pages {landed}"
    check("Staff", "Front desk UI: manager-only screens are absent from the menu and their URLs fall back to Today", "browser as front desk, visit each manager URL directly", staff_ui)

    def feedback_page():
        pg.goto(base + "/#/feedback?c=FB1.1.000000000000"); pg.wait_for_selector("main h1"); pg.wait_for_timeout(600)
        t = pg.inner_text("main")
        return "not valid" in t.lower() or "feedback link" in t.lower(), t[:100]
    check("Growth", "Feedback page shows a clear message for a bad link", "browser", feedback_page)
    b.close()
    return errs
