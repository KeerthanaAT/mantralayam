"""Audit phase 2: notifications (content, QR, PDF, retries, reminders) and payments (full, advance, counter, webhooks, failures, refunds, reconciliation)."""
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from decimal import ROUND_HALF_UP, Decimal

import audit_live as A
from audit_live import D, check, db, guest, make_booking, phone, rec, sms_code, staff, uniq_ip, verified


def half_up(x):
    return Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def tick():
    out = subprocess.run([sys.executable, str(A.HERE / "mantralayam_rooms.py"), "tick"], env=A.ENV, capture_output=True, text=True, cwd=A.ROOT, timeout=120)
    return out


def log_text():
    return A.LOGS["guest"].read_text(encoding="utf-8", errors="replace")


def row_id(gid):
    return db().execute("SELECT id FROM bookings WHERE group_id = ? ORDER BY id LIMIT 1", (gid,)).fetchone()[0]


def mock_sig(raw):
    return hmac.new(b"mock-payment-secret", raw, hashlib.sha256).hexdigest()


def webhook(raw, sig, event_id="evt-x", origin=False):
    c = guest(uniq_ip())
    h = {"Content-Type": "application/json", "X-Razorpay-Event-Id": event_id}
    if sig is not None:
        h["X-Razorpay-Signature"] = sig
    st, hd, out = c.req("POST", "/api/public/payment/webhook", raw=raw, headers=h, origin=False)
    return st, out


def start_payment(method, **kw):
    """hold -> OTP -> book with an online option. Returns (client, payment dict, phone, body fields)."""
    cl, ph = guest(uniq_ip()), phone()
    b = A.base_req(**kw)
    st, hold = cl.j("POST", "/api/public/hold", b)
    assert st == 200, hold
    proof = verified(cl, ph)
    st, out = cl.j("POST", "/api/public/book", b | dict(guest_name="Pay Guest", phone=ph, id_type="Aadhaar", id_number="1234-5678-9012", email="pay@example.org", lang="en", payment_method=method, hold_token=hold["token"], otp_proof=proof))
    assert st == 200 and out.get("payment_required"), out
    return cl, out["payment"], ph


def run_http(X):
    import mantralayam_rooms as m
    import providers
    os.environ["WHATSAPP_PROVIDER"] = "mock"
    m.OTP_REQUIRED = False  # in-process bookings below skip the SMS step (the live HTTP flows above and below use real OTP)
    mgr, fd = staff("manager"), staff("frontdesk")

    # ============ notifications ============
    def in_process_booking(**kw):
        m.reset_throttle()
        providers.OUTBOX.clear()
        ph = phone()
        base = A.base_req(**{k: kw.pop(k) for k in list(kw) if k in ("check_in", "check_out", "rooms", "guests")})
        ip = uniq_ip()
        st, h = m.dispatch("POST", "/api/public/hold", None, base, ip)
        assert st == 200, h
        body = base | dict(guest_name="Notify Guest", phone=ph, id_type="Aadhaar", id_number="1234-5678-9012", email="notify@example.org", lang="en", payment_method="pay_at_counter", hold_token=h["token"]) | kw
        m.reset_throttle()
        st, b = m.dispatch("POST", "/api/public/book", None, body, ip)
        assert st == 200, b
        return b, ph

    holder = {}

    def confirmation_content():
        b, ph = in_process_booking(check_in=D(23), check_out=D(25))
        holder["b"], holder["ph"] = b, ph
        sms = [x for x in providers.OUTBOX if x["kind"] == "sms"]
        wa = [x for x in providers.OUTBOX if x["kind"] == "whatsapp"]
        em = [x for x in providers.OUTBOX if x["kind"] == "email"]
        holder["emails"] = em
        e = A.expected_quote(600, [600, 600], 1)
        txt = sms[0]["text"]
        ok = (len(sms) == len(wa) == len(em) == 1 and sms[0]["to"] == wa[0]["to"] == ph and f"#{b['booking_id']}" in txt and "confirmed" in txt and D(23) in txt and D(25) in txt
              and f"{e['total']:,.2f}" in txt and "DHEERENDRA" in txt.upper() and em[0]["to"] == "notify@example.org" and "cid:qr" in em[0]["html"] and em[0]["inline"] == ["qr"]
              and em[0]["attachments"][0][1] == "application/pdf" and f"{e['total']:,.2f}" in em[0]["text"] and f"#{b['booking_id']}" in em[0]["text"])
        return ok, f"sms: {txt[:150]!r}"
    check("Notifications", "Confirmation: SMS, WhatsApp and email with correct booking, dates, amount; email has printable HTML, inline QR, PDF attachment", "in-process booking with the mock providers; compare each message with the independent price", confirmation_content)

    def qr_resolves():
        if not A.HAVE_CV:
            return "blocked", "opencv not installed"
        b, em = holder["b"], holder["emails"][0]
        code_email = A.decode_qr(em["inline_data"][0][2])
        code_view = A.decode_qr(__import__("base64").b64decode(b["checkin_qr"].split(",")[1]))
        st, lk = fd.j("GET", f"/api/checkin/lookup?code={code_email}")
        ok = code_email == code_view == b["checkin_code"] and st == 200 and lk["booking_id"] == b["booking_id"] and lk["via"] == "qr" and lk["rows"][0]["guest_name"] == "Notify Guest"
        flip = "1" if code_email[-1] == "0" else "0"
        bad = fd.j("GET", f"/api/checkin/lookup?code={code_email[:-1]}{flip}")[0]
        return ok and bad == 400, f"decoded {code_email!r} -> staff lookup {st}, tampered code {bad}"
    check("Notifications", "Decode the QR from the email: it resolves to the right booking at the staff desk; a tampered code is refused", "opencv decodes the emailed PNG; staff GET /api/checkin/lookup", qr_resolves)

    def pdf_ok():
        if not A.HAVE_PDF:
            return "blocked", "pymupdf not installed"
        b, ph, em = holder["b"], holder["ph"], holder["emails"][0]
        st, h, pdf = guest(uniq_ip()).req("POST", "/api/public/booking/receipt", {"booking_id": b["booking_id"], "phone": ph})
        e = A.expected_quote(600, [600, 600], 1)
        txt = A.pdf_text(pdf)
        att_txt = A.pdf_text(em["attachments"][0][2])
        code_in_pdf = None
        if A.HAVE_CV:
            import fitz
            pix = fitz.open(stream=pdf, filetype="pdf")[0].get_pixmap(dpi=120)
            code_in_pdf = A.decode_qr(pix.tobytes("png"))
        need = [f"#{b['booking_id']}", "Notify Guest", f"{e['room']:,.2f}", f"{e['total']:,.2f}", D(23), "Confirmed"]
        ok = st == 200 and pdf[:5] == b"%PDF-" and all(x in txt for x in need) and txt == att_txt and code_in_pdf in (None, b["checkin_code"])
        return ok, f"missing {[x for x in need if x not in txt]}, same as emailed {txt == att_txt}, QR in PDF {code_in_pdf}"
    check("Notifications", "PDF receipt downloads, opens, shows the right amounts and carries the same QR", "HTTP download parsed with pymupdf; compared with the emailed attachment and the independent price", pdf_ok)

    def change_cancel_reminder():
        cl, ph = guest(uniq_ip()), phone()
        st, b, _ = make_booking(cl, ph=ph, check_in=D(26), check_out=D(28))
        before = len(log_text())
        cl.j("POST", "/api/public/booking/change", {"booking_id": b["booking_id"], "phone": ph, "check_in": D(27), "check_out": D(29), "confirm": True})
        time.sleep(0.3)
        chg = log_text()[before:]
        before = len(log_text())
        st, c = guest(uniq_ip()).j("POST", "/api/public/booking/cancel", {"booking_id": b["booking_id"], "phone": ph, "reason": "audit"})
        time.sleep(0.3)
        can = log_text()[before:]
        # reminder: a booking for tomorrow, then the periodic job
        ph2 = phone()
        st2, b2, _ = make_booking(ph=ph2, check_in=D(1), check_out=D(2))
        before = len(log_text())
        out = tick()
        rem = out.stdout  # the job prints the mock messages in its own process
        ok = (f"[mock SMS to {ph}]" in chg and "updated" in chg and D(27) in chg and D(29) in chg and "cancelled" in can and f"#{b['booking_id']}" in can and st == 200
              and f"[mock SMS to {ph2}]" in rem and "starts on" in rem and D(1) in rem and f"[mock WhatsApp to {ph2}]" in rem)
        again = tick()
        twice = (out.stdout + again.stdout).count(f"[mock SMS to {ph2}] Mantralayam Rooms: your stay")
        return ok and twice == 1, f"change ok={('updated' in chg)} cancel ok={('cancelled' in can)} reminder ok={('starts on' in rem)} sent {twice}x; tick rc {out.returncode}"
    check("Notifications", "Change, cancellation and arrival-reminder messages (reminder sent once, by the scheduled job)", "live HTTP actions; the real `tick` command; messages read from the server log", change_cancel_reminder)

    def failures():
        real = providers.sms

        class Broken:
            def send(self, *a, **k):
                raise providers.ProviderError("gateway failed: ConnectionError")
        providers.sms = lambda: Broken()
        try:
            b, ph = in_process_booking(check_in=D(30), check_out=D(31))
        finally:
            providers.sms = real
        c = db()
        row = c.execute("SELECT * FROM notification_log WHERE group_id = ? AND channel = 'sms'", (b["booking_id"],)).fetchone()
        mail_ok = c.execute("SELECT status FROM notification_log WHERE group_id = ? AND channel = 'email'", (b["booking_id"],)).fetchone()[0] == "sent"
        import notifications
        cn = m.connect()
        providers.OUTBOX.clear()
        n = notifications.retry_failed(cn)
        row2 = c.execute("SELECT status, attempts FROM notification_log WHERE id = ?", (row["id"],)).fetchone()
        dump = json.dumps([dict(r) for r in c.execute("SELECT * FROM notification_log")])
        cn.close()
        clean = ph not in dump and "1234-5678" not in dump and "Notify Guest" not in dump
        return row["status"] == "failed" and mail_ok and n >= 1 and tuple(row2) == ("sent", 2) and clean, f"first {row['status']}, email {mail_ok}, retried {n}, now {tuple(row2)}, log free of phone/ID/name {clean}"
    check("Notifications", "A broken SMS gateway is logged as failed, the booking still succeeds, the retry job delivers it; the log holds no personal data", "break providers.sms, book, retry_failed, inspect notification_log", failures)

    # ============ payments ============
    def pay_full():
        cl, pay, ph = start_payment("pay_full", check_in=D(31), check_out=D(33))
        e = A.expected_quote(600, [600, 600], 1)
        no_booking = db().execute("SELECT COUNT(*) FROM bookings WHERE phone = ?", (ph,)).fetchone()[0] == 0
        st, r = cl.j("POST", "/api/public/payment/mock", {"order_id": pay["order_id"], "outcome": "success", "method": "card"})
        st, b = cl.j("POST", "/api/public/payment/verify", r)
        c = db()
        p = c.execute("SELECT * FROM payments WHERE order_id = ?", (pay["order_id"],)).fetchone()
        rows = c.execute("SELECT amount, payment_status, payment_method FROM bookings WHERE group_id = ?", (b["booking_id"],)).fetchall()
        rec_ = mgr.j("GET", f"/api/payments/reconcile?start={D(30)}&end={D(34)}")[1]
        line = next(x for x in rec_["rows"] if x["booking_id"] == b["booking_id"])
        ok = (no_booking and pay["amount"] == e["total"] and st == 200 and b["status"] == "Confirmed" and p["status"] == "paid" and p["amount_paise"] == int(e["total"] * 100) and p["method"] == "card"
              and sum(r[0] for r in rows) == e["total"] and {r[1] for r in rows} == {"paid"} and line["paid"] == e["total"] and line["balance"] == 0 and line["flags"] == [])
        return ok, f"pay {pay['amount']} vs {e['total']}; booking before payment: {not no_booking}; line {line}"
    check("Payments", "Full online payment (Razorpay mock): no booking until paid; payment, booking rows and reconciliation agree to the paisa", "hold, OTP, book pay_full, mock checkout, verify; DB and /api/payments/reconcile", pay_full)

    def pay_advance_counter():
        cl, pay, ph = start_payment("pay_advance", check_in=D(34), check_out=D(36))
        e = A.expected_quote(600, [600, 600], 1)
        st, r = cl.j("POST", "/api/public/payment/mock", {"order_id": pay["order_id"], "outcome": "success", "method": "upi"})
        st, b = cl.j("POST", "/api/public/payment/verify", r)
        status = db().execute("SELECT payment_status FROM bookings WHERE group_id = ?", (b["booking_id"],)).fetchone()[0]
        line = lambda: next(x for x in mgr.j("GET", f"/api/payments/reconcile?start={D(33)}&end={D(37)}")[1]["rows"] if x["booking_id"] == b["booking_id"])
        l1 = line()
        fd.post(f"/api/bookings/{row_id(b['booking_id'])}/payment", {"amount": e["total"] - e["advance"], "mode": "cash"})
        l2 = line()
        # pay at the counter: nothing taken online
        st3, b3, _ = make_booking(check_in=D(37), check_out=D(38))
        s3 = db().execute("SELECT payment_status, payment_method FROM bookings WHERE group_id = ?", (b3["booking_id"],)).fetchone()
        ok = (pay["amount"] == e["advance"] and status == "partial" and l1["paid"] == e["advance"] and l1["flags"] == ["unpaid"] and l1["balance"] == round(e["total"] - e["advance"], 2)
              and l2["flags"] == [] and l2["paid"] == e["total"] and tuple(s3) == ("due", "pay_at_counter"))
        return ok, f"advance {pay['amount']} vs {e['advance']}; {l1['flags']} -> {l2['flags']}; counter {tuple(s3)}"
    check("Payments", "Advance (25%) then balance at the counter reconciles; pay-at-counter books with nothing paid", "book pay_advance, record the balance at the desk, read reconciliation; book pay_at_counter", pay_advance_counter)

    def webhooks():
        cl, pay, ph = start_payment("pay_full", check_in=D(38), check_out=D(39))
        ent = {"payment": {"entity": {"id": "pay_audit1", "order_id": pay["order_id"], "amount": pay["amount_paise"], "method": "upi"}}}
        raw = json.dumps({"event": "payment.captured", "payload": ent}).encode()
        sig = mock_sig(raw)
        bad = [webhook(raw, "0" * 64)[0], webhook(raw, None)[0], webhook(raw, "")[0], webhook(raw + b" ", sig)[0], webhook(b"not json", mock_sig(b"not json"))[0]]
        still_created = db().execute("SELECT status FROM payments WHERE order_id = ?", (pay["order_id"],)).fetchone()[0] == "created"
        good = webhook(raw, sig, "evt-audit-1")
        replay = webhook(raw, sig, "evt-audit-1")
        again_new_id = webhook(raw, sig, "evt-audit-2")
        verify = cl.j("POST", "/api/public/payment/verify", {"order_id": pay["order_id"], "payment_id": "pay_audit1", "signature": hmac.new(b"mock-payment-secret", f"{pay['order_id']}|pay_audit1".encode(), hashlib.sha256).hexdigest()})
        c = db()
        n_book = c.execute("SELECT COUNT(DISTINCT COALESCE(group_id, id)) FROM bookings WHERE phone = ?", (ph,)).fetchone()[0]
        n_pay = c.execute("SELECT COUNT(*) FROM payments WHERE order_id = ? AND status = 'paid'", (pay["order_id"],)).fetchone()[0]
        n_mail = log_text().count(f"[mock SMS to {ph}] Mantralayam Rooms: booking")
        tampered_checkout = cl.j("POST", "/api/public/payment/verify", {"order_id": pay["order_id"], "payment_id": "pay_other", "signature": "0" * 64})[0]
        ok = bad == [400] * 5 and still_created and good[0] == 200 and b"duplicate" in replay[1] and again_new_id[0] == 200 and verify[0] == 200 and n_book == 1 and n_pay == 1 and n_mail == 1 and tampered_checkout == 400
        return ok, f"bad {bad}, created-after-bad {still_created}, good {good}, replay {replay}, bookings {n_book}, paid rows {n_pay}, confirmations {n_mail}, forged checkout {tampered_checkout}"
    check("Payments", "Webhook: valid accepted; tampered, unsigned, wrong-signature and malformed refused; replay and duplicate events idempotent (one booking, one confirmation)", "raw signed POSTs without Origin; then the page's own verify call; DB counts", webhooks)

    def failed_abandoned():
        out = {}
        cl, pay, ph = start_payment("pay_full", check_in=D(39), check_out=D(40))
        st, e = cl.j("POST", "/api/public/payment/mock", {"order_id": pay["order_id"], "outcome": "fail"})
        out["declined"] = (st, db().execute("SELECT status FROM payments WHERE order_id = ?", (pay["order_id"],)).fetchone()[0], db().execute("SELECT COUNT(*) FROM bookings WHERE phone = ?", (ph,)).fetchone()[0])
        free = lambda: sum(t["can_book"] for p in guest(uniq_ip()).j("GET", f"/api/public/search?check_in={D(39)}&check_out={D(40)}&guests=2&property_id=1&room_type_id=2")[1]["properties"] for t in p["types"])
        held = free()
        cl.j("POST", "/api/public/payment/cancel", {"order_id": pay["order_id"]})
        out["abandoned"] = (db().execute("SELECT status FROM payments WHERE order_id = ?", (pay["order_id"],)).fetchone()[0], free() >= held)
        cl2, pay2, ph2 = start_payment("pay_full", check_in=D(41), check_out=D(42))
        c = db()
        past = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 120))
        c.execute("UPDATE payments SET expires_at = ? WHERE order_id = ?", (past, pay2["order_id"]))
        c.execute("UPDATE holds SET expires_at = ? WHERE token = ?", (past, pay2["hold_token"]))
        r = tick()
        out["expired"] = (c.execute("SELECT status FROM payments WHERE order_id = ?", (pay2["order_id"],)).fetchone()[0], c.execute("SELECT COUNT(*) FROM bookings WHERE phone = ?", (ph2,)).fetchone()[0], c.execute("SELECT COUNT(*) FROM holds WHERE token = ?", (pay2["hold_token"],)).fetchone()[0])
        # a capture arriving after the room went elsewhere is refunded in full and not booked
        cl3, pay3, ph3 = start_payment("pay_full", check_in=D(43), check_out=D(44))
        cl3.j("POST", "/api/public/payment/cancel", {"order_id": pay3["order_id"]})
        rooms = [x[0] for x in c.execute("SELECT id FROM rooms WHERE active = 1")]
        c.executemany("INSERT INTO room_status_events (room_id, kind, reason, start_date, end_date, active, created_at) VALUES (?, 'internal_block', 'LATE-TEST', ?, ?, 1, 'now')", [(x, D(43), D(44)) for x in rooms])
        raw = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_late", "order_id": pay3["order_id"], "amount": pay3["amount_paise"], "method": "upi"}}}}).encode()
        webhook(raw, mock_sig(raw), "evt-late")
        rf = c.execute("SELECT r.status, r.amount_paise FROM refunds r JOIN payments p ON p.id = r.payment_row WHERE p.order_id = ?", (pay3["order_id"],)).fetchone()
        out["late"] = (c.execute("SELECT status FROM payments WHERE order_id = ?", (pay3["order_id"],)).fetchone()[0], tuple(rf) if rf else None, c.execute("SELECT COUNT(*) FROM bookings WHERE phone = ?", (ph3,)).fetchone()[0])
        c.execute("DELETE FROM room_status_events WHERE reason = 'LATE-TEST'")
        ok = (out["declined"] == (402, "created", 0) and out["abandoned"] == ("abandoned", True) and out["expired"] == ("expired", 0, 0) and out["late"] == ("unfulfilled", ("processed", pay3["amount_paise"]), 0))
        return ok, str(out)
    check("Payments", "Declined, abandoned and expired payments leave no booking and release the room; a capture after the room is gone is refunded in full", "mock decline, cancel call, forced expiry + real `tick`, late webhook with the room blocked", failed_abandoned)

    def refunds():
        out = {}
        # (a) full cancel before the cutoff: 50% of what was paid, exactly (an amount that rounds)
        cl, pay, ph = start_payment("pay_full", check_in=D(45), check_out=D(48), guests=3, children=1, extra_beds=1)
        r = cl.j("POST", "/api/public/payment/mock", {"order_id": pay["order_id"], "outcome": "success"})[1]
        b = cl.j("POST", "/api/public/payment/verify", r)[1]
        paid = half_up(b["payment"]["paid"])
        c = guest(uniq_ip()).j("POST", "/api/public/booking/cancel", {"booking_id": b["booking_id"], "phone": ph, "reason": "audit"})[1]
        want = half_up(paid * 50 / 100)
        out["before"] = (c["payment"]["refunded"], float(want), c["payment"]["refund_status"], c["payment"]["status"])
        # (b) after the cutoff: online cancel refused, nothing refunded; the manager's cancel still follows the policy
        cl2, pay2, ph2 = start_payment("pay_full", check_in=D(0), check_out=D(1))
        r2 = cl2.j("POST", "/api/public/payment/mock", {"order_id": pay2["order_id"], "outcome": "success"})[1]
        b2 = cl2.j("POST", "/api/public/payment/verify", r2)[1]
        late = guest(uniq_ip()).j("POST", "/api/public/booking/cancel", {"booking_id": b2["booking_id"], "phone": ph2, "reason": "late"})
        none_yet = db().execute("SELECT COUNT(*) FROM refunds WHERE group_id = ?", (b2["booking_id"],)).fetchone()[0]
        fd_try = fd.post(f"/api/bookings/{row_id(b2['booking_id'])}/cancel", {"reason": "x"})[0]
        mg = mgr.post(f"/api/bookings/{row_id(b2['booking_id'])}/cancel", {"reason": "guest rang"})
        rf2 = db().execute("SELECT amount_paise, status FROM refunds WHERE group_id = ?", (b2["booking_id"],)).fetchone()
        out["after"] = (late[0], none_yet, fd_try, mg[0], tuple(rf2) if rf2 else None, int(half_up(b2["payment"]["paid"] * 50 / 100) * 100))
        # (c) partway: one of two rooms cancelled by the manager: that room's share only
        cl3, pay3, ph3 = start_payment("pay_full", check_in=D(49), check_out=D(51), rooms=2, guests=4)
        r3 = cl3.j("POST", "/api/public/payment/mock", {"order_id": pay3["order_id"], "outcome": "success"})[1]
        b3 = cl3.j("POST", "/api/public/payment/verify", r3)[1]
        rows = db().execute("SELECT id, amount FROM bookings WHERE group_id = ? ORDER BY id", (b3["booking_id"],)).fetchall()
        paid3 = half_up(b3["payment"]["paid"])
        mgr.post(f"/api/bookings/{rows[0]['id']}/cancel", {"reason": "one room fewer"})
        share = half_up(paid3 * Decimal(str(rows[0]["amount"])) / Decimal(str(sum(r["amount"] for r in rows))))
        want3 = int(half_up(share * 50 / 100) * 100)
        got3 = db().execute("SELECT SUM(amount_paise) FROM refunds WHERE group_id = ?", (b3["booking_id"],)).fetchone()[0]
        out["partway"] = (got3, want3)
        ok = (out["before"][:3] == (float(want), float(want), "processed") and out["before"][3] == "partially_refunded" and out["after"][0] == 409 and out["after"][1] == 0 and out["after"][2] == 403 and out["after"][3] == 200
              and out["after"][4] == (out["after"][5], "processed") and abs(out["partway"][0] - out["partway"][1]) <= 1)
        return ok, str(out)
    check("Payments", "Refund = exactly the policy % (50%): cancel before the cutoff, after the cutoff (guest refused, manager follows policy), and one room of two; status updates", "paid online, cancelled each way; amounts recomputed independently with half-up rounding", refunds)

    def reconcile_all():
        c = db()
        tot_pay = c.execute("SELECT COALESCE(SUM(amount_paise), 0) FROM payments WHERE status IN ('paid', 'unfulfilled') AND paid_at IS NOT NULL AND substr(paid_at, 1, 10) = ?", (D(0),)).fetchone()[0]
        tot_ref = c.execute("SELECT COALESCE(SUM(r.amount_paise), 0) FROM refunds r WHERE r.status IN ('processed', 'pending', 'requested', 'sending') AND substr(r.updated_at, 1, 10) = ?", (D(0),)).fetchone()[0]
        d = mgr.j("GET", f"/api/reports/data?report=revenue_by_day&from={D(0)}&to={D(0)}")[1]["rows"]
        total = d[-1] if d else None
        by = {r[0]: r for r in mgr.j("GET", f"/api/reports/data?report=revenue_by_method&from={D(0)}&to={D(0)}")[1]["rows"]}
        method_sum = round(sum(r[2] for k, r in by.items() if k != "Refunds"), 2)
        # every booking's paid amount in the guest view equals the payments table; net = paid - refunded
        bad = []
        for (gid,) in c.execute("SELECT DISTINCT group_id FROM payments WHERE group_id IS NOT NULL AND status = 'paid' LIMIT 40").fetchall():
            paid = c.execute("SELECT SUM(amount_paise) FROM payments WHERE group_id = ? AND status = 'paid'", (gid,)).fetchone()[0]
            refunded = c.execute("SELECT COALESCE(SUM(amount_paise), 0) FROM refunds WHERE group_id = ? AND status IN ('processed', 'pending', 'requested', 'sending', 'manual')", (gid,)).fetchone()[0]
            ph = c.execute("SELECT phone FROM bookings WHERE group_id = ? OR id = ? LIMIT 1", (gid, gid)).fetchone()
            v = guest(uniq_ip()).j("POST", "/api/public/booking/lookup", {"booking_id": gid, "phone": ph[0]})[1] if ph else None
            if v and "payment" not in v:
                bad.append((gid, str(v)[:80]))
            elif v and (round(v["payment"]["paid"] * 100) != paid or round((v["payment"]["refunded"] + v["payment"]["refund_in_progress"]) * 100) != refunded):
                bad.append(gid)
        ok = total and round(total[3] + total[4], 2) == round(tot_pay / 100, 2) and round(total[5], 2) == round(tot_ref / 100, 2) and method_sum == round(tot_pay / 100, 2) and not bad
        return ok, f"payments {tot_pay / 100}, refunds {tot_ref / 100}, report {total}, by-method {method_sum}, guest view mismatches {bad}"
    check("Payments", "Payment records, booking views and reports reconcile (money in = revenue report = method report; refunds match)", "sum the payments/refunds tables and compare with the revenue-by-day, by-method reports and each guest's booking view", reconcile_all)
