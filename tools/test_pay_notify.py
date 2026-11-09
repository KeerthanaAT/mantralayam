"""Self-check for notifications (SMS / WhatsApp / email with QR and PDF, the log, reminders), the PDF and QR writers, the ID vault, and online payments
(Razorpay behind the provider interface, mock mode): pricing, advance and refund calculations, webhooks, idempotency, expiry and refunds.
Runs against a COPY of the real database. Run: python tools/test_pay_notify.py   (or pytest)
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import zlib
from datetime import timedelta
from pathlib import Path

import idvault
import mantralayam_rooms as m
import notifications
import payments as pay
import pdfdoc
import providers
import qr

SRC = Path(__file__).resolve().parent.parent / ".tmp" / "mantralayam.db"
TMP = Path(tempfile.mkdtemp())
m.DB = TMP / "copy.db"
shutil.copy(SRC, m.DB)
m.BACKUPS = TMP / "backups"
D = lambda n: (m.today() + timedelta(days=n)).isoformat()
N = [0]
IP = "127.0.0.1"
ENV = ("PAYMENT_PROVIDER", "WHATSAPP_PROVIDER", "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET", "ID_ENCRYPTION_KEY")


def db():
    c = sqlite3.connect(m.DB, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def call(method, path, data=None, ip=IP):
    m.reset_throttle()
    return m.dispatch(method, path, data if method == "GET" else None, None if method == "GET" else data, ip)


def phone():
    N[0] += 1
    return f"96543{N[0]:05d}"


def setting(**kw):
    c = db()
    for k, v in kw.items():
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
    c.close()


def clear_setting(*keys):
    c = db()
    c.executemany("DELETE FROM settings WHERE key = ?", [(k,) for k in keys])
    c.close()


def req(**kw):
    c = db()
    pid = c.execute("SELECT property_id FROM rooms WHERE room_type_id = 1 AND active = 1 GROUP BY property_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    c.close()
    return dict(property_id=pid, room_type_id=1, check_in=D(40), check_out=D(42), rooms=1, guests=2) | kw


def guest(**kw):
    return dict(guest_name="Test Guest", phone=phone(), id_type="Aadhaar", id_number="1234-5678-9012", email="guest@example.org", lang="en") | kw


def hold(**kw):
    st, h = call("POST", "/api/public/hold", req(**kw))
    assert st == 200, h
    return h["token"]


def book(method="pay_at_counter", token=None, **kw):
    body = req(**{k: kw.pop(k) for k in ("check_in", "check_out", "rooms", "guests") if k in kw}) | guest(**kw) | dict(payment_method=method)
    if token:
        body["hold_token"] = token
    return call("POST", "/api/public/book", body)


def outbox(kind):
    return [x for x in providers.OUTBOX if x["kind"] == kind]


def reset():
    m.reset_throttle()
    providers.OUTBOX.clear()


def cleanup():
    c = db()
    for t in ("bookings", "holds", "payments", "refunds", "webhook_events", "notification_log", "alerts", "waitlist"):
        if t == "bookings":
            c.execute("DELETE FROM bookings WHERE source = 'online'")
        else:
            c.execute(f"DELETE FROM {t}")
    c.execute("DELETE FROM settings WHERE key IN ('pay_options', 'advance_percent', 'refund_percent', 'refund_days', 'reminder_days', 'reminder_hour', 'tax_percent')")
    c.close()
    for k in ENV:
        os.environ.pop(k, None)
    reset()


def online(options="full,advance,counter", advance=25, refund=None):
    os.environ["PAYMENT_PROVIDER"] = "mock"
    setting(pay_options=options, advance_percent=advance)
    if refund is not None:
        setting(refund_percent=refund)


def start(method="pay_full", **kw):
    """Takes a hold and starts an online payment. Returns (payment dict, request fields used)."""
    tok = hold(**{k: kw[k] for k in ("check_in", "check_out", "rooms") if k in kw})
    st, out = book(method, token=tok, **kw)
    assert st == 200 and out["payment_required"], (st, out)
    return out["payment"], tok


def mock_pay(payment, outcome="success", method="upi"):
    return call("POST", "/api/public/payment/mock", {"order_id": payment["order_id"], "outcome": outcome, "method": method})


def settle(payment):
    """The checkout success handler: mock window, then verify. Returns (status, body)."""
    st, r = mock_pay(payment)
    assert st == 200, r
    return call("POST", "/api/public/payment/verify", r)  # {order_id, payment_id, signature, method}: what the page passes on


def hook(event, order_id=None, pay_id="pay_test1", amount=None, method="upi", eid=None, refund_id=None):
    ent = {"payment": {"entity": {"id": pay_id, "order_id": order_id, "amount": amount, "method": method}}}
    if refund_id:
        ent["refund"] = {"entity": {"id": refund_id}}
    raw = json.dumps({"event": event, "payload": ent}).encode()
    return m.payment_webhook(raw, providers.payments().webhook_signature(raw), eid)


def bookings_of(gid):
    c = db()
    try:
        return c.execute("SELECT * FROM bookings WHERE group_id = ?", (gid,)).fetchall()
    finally:
        c.close()


def paymentrow(order_id):
    c = db()
    try:
        return c.execute("SELECT * FROM payments WHERE order_id = ?", (order_id,)).fetchone()
    finally:
        c.close()


# ---------- pure pieces ----------
def test_amounts():
    pay.g = m
    assert pay.to_paise(1234.5) == 123450 and pay.to_paise("0.005") == 1 and pay.to_paise(0.1 + 0.2) == 30
    assert pay.pay_now(1200.0, 300.0, "full") == 120000 and pay.pay_now(1200.0, 300.0, "advance") == 30000
    for args in ((1200, 0, "advance"), (1200, 1300, "advance"), (0.5, 0, "full"), (1200, 300, "bogus")):
        try:
            pay.pay_now(*args)
        except m.UserError:
            continue
        raise AssertionError(args)
    assert pay.pay_now(1200, 1200, "advance") == 120000  # a 100% advance is allowed and equals the full amount
    # the advance in the quote: advance_percent of the total, tax included, half up to the paisa
    setting(advance_percent="33.33", tax_percent="12")
    c = m.connect()
    try:
        s, ci, co = None, m.today() + timedelta(days=40), m.today() + timedelta(days=42)
        _, _, _, s = m.parse_stay(c, ci.isoformat(), co.isoformat())
        q = m.quote(c, s, ci, co, pid=req()["property_id"], tid=1, tariff=600, rooms=1, guests=2)
        assert q["total"] == 1344.0 and q["advance"] == 447.96 and q["balance"] == 896.04, q  # 1200 + 12% = 1344; 33.33% = 447.9552
        assert pay.pay_now(q["total"], q["advance"], "advance") == 44796
    finally:
        c.close()
        clear_setting("advance_percent", "tax_percent")


def test_refund_math():
    assert pay.refund_paise(100000, 50) == 50000 and pay.refund_paise(100001, 50) == 50001  # 50000.5 rounds half up
    assert pay.refund_paise(100000, 0) == 0 and pay.refund_paise(100000, 100) == 100000
    assert pay.refund_paise(100000, None) == 0 and pay.refund_paise(0, 50) == 0 and pay.refund_paise(-5, 50) == 0
    assert pay.refund_paise(33333, 33) == 11000  # 10999.89
    assert pay.cancel_share(90000, 400.0, 400.0) == 90000  # everything when all live rooms go
    assert pay.cancel_share(90000, 100.0, 400.0) == 22500  # one of four equal rooms
    assert pay.cancel_share(90001, 100.0, 300.0) == 30000  # rounded down: 30000.33
    assert pay.cancel_share(-10, 100.0, 400.0) == 0 and pay.cancel_share(100, 0, 0) == 100


def test_vault():
    k = "a-long-enough-master-key"
    blob = idvault.seal(k, b"1234-5678-9012", b"booking:7")
    assert b"1234" not in blob and idvault.open_(k, blob, b"booking:7") == b"1234-5678-9012"
    assert idvault.seal(k, b"x") != idvault.seal(k, b"x"), "a fresh nonce every time"
    for bad in ((k, blob, b"booking:8"), ("another-long-master-key!", blob, b"booking:7"), (k, blob[:-1] + bytes([blob[-1] ^ 1]), b"booking:7"), (k, blob[:20], b"booking:7"), (k, b"", b"")):
        try:
            idvault.open_(*bad)
        except idvault.VaultError:
            continue
        raise AssertionError("opened a blob it should reject")
    try:
        idvault.seal("short", b"x")
    except idvault.VaultError:
        pass
    else:
        raise AssertionError("short key accepted")
    big = os.urandom(5000)
    assert idvault.open_(k, idvault.seal(k, big)) == big


def test_qr_and_pdf():
    mat = qr.qr_matrix("MR1.42.0123456789ab")
    n = len(mat)
    assert n == 25 and all(len(r) == n for r in mat)  # version 2
    for x0, y0 in ((0, 0), (n - 7, 0), (0, n - 7)):  # three finder patterns
        assert all(mat[y0][x0 + i] and mat[y0 + 6][x0 + i] and mat[y0 + i][x0] and mat[y0 + i][x0 + 6] for i in range(7)) and mat[y0 + 3][x0 + 3] and not mat[y0 + 1][x0 + 1]
    assert qr.qr_matrix("x" * 213) and len(qr.qr_matrix("x" * 213)) == 57
    try:
        qr.qr_matrix("x" * 214)
    except ValueError:
        pass
    else:
        raise AssertionError("too long accepted")
    png = qr.qr_png(mat, 4, 2)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and png[12:16] == b"IHDR" and int.from_bytes(png[16:20], "big") == (n + 4) * 4
    zlib.decompress(png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8])  # the pixel data inflates
    try:  # when OpenCV happens to be installed, a real decoder must read the codes
        import cv2
        import numpy
        for text in ("MR1.1.abcdef012345", "MR1.123456.0123456789abcdef" * 3):
            img = cv2.imdecode(numpy.frombuffer(qr.qr_png(qr.qr_matrix(text), 8, 4), numpy.uint8), cv2.IMREAD_GRAYSCALE)
            assert cv2.QRCodeDetector().detectAndDecode(img)[0] == text
    except ImportError:
        pass
    p = pdfdoc.Pdf("T")
    p.text(50, 50, "Hello (world) \\ Rs 1,200.00 ₹")
    p.squares(mat, 400, 50, 100)
    p.new_page()
    p.text(50, 50, "page two")
    data = p.render()
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF") and b"/Count 2" in data
    start = int(re.search(rb"startxref\n(\d+)", data)[1])
    assert data[start:start + 4] == b"xref"  # every object offset in the table points at its "N 0 obj"
    for i, off in enumerate(re.findall(rb"(\d{10}) 00000 n", data), 1):
        assert data[int(off):].startswith(f"{i} 0 obj".encode()), i


# ---------- notifications ----------
def test_confirmation_messages():
    reset()
    os.environ["WHATSAPP_PROVIDER"] = "mock"
    st, b = book(email="guest@example.org", phone="9654300001")
    assert st == 200
    sms, wa, mail = outbox("sms"), outbox("whatsapp"), outbox("email")
    assert len(sms) == len(wa) == len(mail) == 1 and sms[0]["to"] == wa[0]["to"] == "9654300001" and mail[0]["to"] == "guest@example.org"
    assert f"#{b['booking_id']}" in sms[0]["text"] and "confirmed" in sms[0]["text"] and "1234-5678" not in sms[0]["text"]
    e = mail[0]
    assert "confirmed" in e["subject"] and "cid:qr" in e["html"] and e["inline"] == ["qr"] and b["checkin_code"] in e["html"] and b["checkin_code"] in e["text"]
    (name, mime, pdf), = e["attachments"]
    assert name == f"booking-{b['booking_id']}.pdf" and mime == "application/pdf" and pdf.startswith(b"%PDF") and b"MR1" not in pdf[:0] and len(pdf) > 1500
    assert b["checkin_qr"].startswith("data:image/png;base64,")
    # the log: one row per channel, delivery status, masked recipient, no message text and no ID number
    c = db()
    rows = c.execute("SELECT * FROM notification_log WHERE group_id = ? ORDER BY id", (b["booking_id"],)).fetchall()
    assert [(r["event"], r["channel"], r["status"]) for r in rows] == [("confirmed", "sms", "sent"), ("confirmed", "whatsapp", "sent"), ("confirmed", "email", "sent")]
    dump = json.dumps([dict(r) for r in rows])
    assert "9654300001" not in dump and "guest@example.org" not in dump and "1234-5678" not in dump and "confirmed at" not in dump and "MR1" not in dump
    assert rows[0]["to_hint"] == "•••0001" and rows[2]["to_hint"] == "g•••@example.org"
    # WhatsApp is off when no provider is configured; no email address is logged as skipped, not failed
    reset()
    os.environ.pop("WHATSAPP_PROVIDER")
    st, b2 = book(email="")
    assert not outbox("whatsapp") and not outbox("email")
    rows = c.execute("SELECT channel, status, detail FROM notification_log WHERE group_id = ? ORDER BY id", (b2["booking_id"],)).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("sms", "sent"), ("email", "skipped")]
    c.close()


def test_change_and_cancel_messages():
    reset()
    st, b = book(email="g@example.org")
    reset()
    st, ch = call("POST", "/api/public/booking/change", dict(booking_id=b["booking_id"], phone=b["mobile"], check_in=D(50), check_out=D(52), confirm=True))
    assert st == 200 and ch["changed"], ch
    assert "updated" in outbox("sms")[0]["text"] and D(50) in outbox("sms")[0]["text"] and "updated" in outbox("email")[0]["subject"] and outbox("email")[0]["attachments"]
    reset()
    st, c = call("POST", "/api/public/booking/cancel", dict(booking_id=b["booking_id"], phone=b["mobile"], reason="plans changed"))
    assert st == 200 and c["status"] == "Cancelled"
    assert "cancelled" in outbox("sms")[0]["text"] and "cancelled" in outbox("email")[0]["subject"]
    assert not outbox("email")[0]["attachments"] and "cid:qr" not in outbox("email")[0]["html"], "a cancelled booking has no check-in code"
    events = [r[0] for r in db().execute("SELECT DISTINCT event FROM notification_log WHERE group_id = ? ORDER BY id", (b["booking_id"],))]
    assert events == ["confirmed", "changed", "cancelled"]


def test_failures_are_logged_and_retried():
    reset()

    class Broken:
        def send(self, *a, **k):
            raise providers.ProviderError("gateway failed: ConnectionError")
    real = providers.sms
    providers.sms = lambda: Broken()
    try:
        st, b = book(email="g@example.org")
    finally:
        providers.sms = real
    assert st == 200, "a broken SMS gateway never fails the booking"
    c = db()
    row = c.execute("SELECT * FROM notification_log WHERE group_id = ? AND channel = 'sms'", (b["booking_id"],)).fetchone()
    assert row["status"] == "failed" and row["attempts"] == 1 and "ConnectionError" in row["detail"]
    assert c.execute("SELECT status FROM notification_log WHERE group_id = ? AND channel = 'email'", (b["booking_id"],)).fetchone()[0] == "sent"
    cn = m.connect()
    reset()
    assert notifications.retry_failed(cn) == 1
    row = c.execute("SELECT * FROM notification_log WHERE id = ?", (row["id"],)).fetchone()
    assert row["status"] == "sent" and row["attempts"] == 2 and len(outbox("sms")) == 1 and not outbox("email"), "only the failed channel is retried"
    assert notifications.retry_failed(cn) == 0
    # attempts stop at the limit
    c.execute("UPDATE notification_log SET status = 'failed', attempts = ? WHERE id = ?", (notifications.MAX_ATTEMPTS, row["id"]))
    assert notifications.retry_failed(cn) == 0
    cn.close()
    c.close()


def test_reminders():
    reset()
    setting(reminder_days=1, reminder_hour=0)
    st, soon = book(check_in=D(1), check_out=D(2), email="g@example.org")
    st, later = book(check_in=D(5), check_out=D(6))
    st, gone = book(check_in=D(1), check_out=D(2), rooms=1)
    c = db()
    c.execute("UPDATE bookings SET status = 'Cancelled' WHERE group_id = ?", (gone["booking_id"],))  # (online cancelling closes 24 hours before check-in)
    c.close()
    reset()
    cn = m.connect()
    assert notifications.reminders(cn) == 1, "only the booking arriving tomorrow"
    assert "reminder" in outbox("email")[0]["subject"].lower() or "soon" in outbox("email")[0]["subject"] and f"#{soon['booking_id']}" in outbox("sms")[0]["text"]
    assert outbox("email")[0]["inline"] == ["qr"], "the reminder carries the QR code too"
    reset()
    assert notifications.reminders(cn) == 0, "sent once"
    # off when no days are set; not before the hour
    setting(reminder_days="")
    st, other = book(check_in=D(1), check_out=D(2))
    reset()
    assert notifications.reminders(cn) == 0
    setting(reminder_days=1, reminder_hour=m.now().hour + 1 if m.now().hour < 23 else 24)
    assert notifications.reminders(cn) == 0
    cn.close()
    out = None
    setting(reminder_hour=0)
    out = m.tick()
    assert out["reminders"] == 1 and out["payments"]["expired"] == 0, out  # the periodic job runs reminders (the booking made while they were off)


def test_receipt_pdf_download():
    reset()
    st, b = book(email="g@example.org")
    st, r = call("POST", "/api/public/booking/receipt", dict(booking_id=b["booking_id"], phone=b["mobile"]))
    assert st == 200 and r["_ctype"] == "application/pdf" and r["_bytes"].startswith(b"%PDF") and r["_filename"] == f"booking-{b['booking_id']}.pdf"
    assert call("POST", "/api/public/booking/receipt", dict(booking_id=b["booking_id"], phone="9000000000"))[0] == 404
    c = db()
    cn = m.connect()
    assert notifications.parse_code(cn, b["checkin_code"]) == b["booking_id"]
    for bad in (b["checkin_code"][:-1] + ("0" if b["checkin_code"][-1] != "0" else "1"), "MR1.999999.000000000000", "nonsense", "", None, b["checkin_code"].replace("MR1", "MR2")):
        assert notifications.parse_code(cn, bad) is None, bad
    cn.close()
    c.close()
    html = m.receipt_html(m.booking_view(m.connect(), m.find_booking(m.connect(), b["booking_id"], b["mobile"])), "en", b["checkin_code"])
    assert "data:image/png;base64," in html and b["checkin_code"] in html


# ---------- payments ----------
def test_options_and_badge():
    reset()
    st, i = call("GET", "/api/public/info", {})
    assert i["payments"] == {"enabled": False, "options": ["counter"], "secure_badge": False}, "no provider: no online option and no secure-payment badge"
    setting(pay_options="full,advance,counter")
    assert call("GET", "/api/public/info", {})[1]["payments"]["secure_badge"] is False, "choosing online options in settings is not enough without a provider"
    st, e = book("pay_full")
    assert st == 400 and "valid payment option" in e["error"]
    online("full,advance,counter", advance=25)
    p = call("GET", "/api/public/info", {})[1]["payments"]
    assert p["enabled"] and p["secure_badge"] and p["options"] == ["full", "advance", "counter"] and p["advance_percent"] == 25 and p["test_mode"] and p["mock"]
    online("counter")  # provider on, but only the counter chosen: still no badge
    assert call("GET", "/api/public/info", {})[1]["payments"]["secure_badge"] is False
    online("advance,counter", advance="")  # advance chosen but no percentage set: not offered
    assert call("GET", "/api/public/info", {})[1]["payments"]["options"] == ["counter"]
    online("full", advance=25)  # a provider with only "full": the counter stays available so a guest can always book
    assert call("GET", "/api/public/info", {})[1]["payments"]["options"] == ["full", "counter"] or call("GET", "/api/public/info", {})[1]["payments"]["options"] == ["full"]
    os.environ["PAYMENT_PROVIDER"] = "razorpay"  # keys missing: treated as off, with the page falling back to the counter
    assert call("GET", "/api/public/info", {})[1]["payments"]["enabled"] is False
    os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"] = "rzp_test_abc123", "secret-value"
    p = call("GET", "/api/public/info", {})[1]["payments"]
    assert p["provider"] == "razorpay" and p["key_id"] == "rzp_test_abc123" and p["test_mode"] and not p["mock"] and "secret" not in json.dumps(p)
    assert "https://checkout.razorpay.com" in m.csp() and "https://api.razorpay.com" in m.csp()
    os.environ.pop("PAYMENT_PROVIDER")
    assert "razorpay" not in m.csp(), "the page may only load the provider's script when the provider is configured"
    assert any("RAZORPAY_WEBHOOK_SECRET" in x for x in (os.environ.__setitem__("PAYMENT_PROVIDER", "razorpay"), m.serve_checks())[1])


def test_full_payment_books_after_confirmation():
    online()
    reset()
    p, tok = start("pay_full")
    assert p["amount"] == 1200.0 and p["kind"] == "full" and p["balance"] == 0 and p["mock"] and p["order_id"].startswith("order_mock_")
    c = db()
    assert c.execute("SELECT COUNT(*) FROM bookings WHERE source = 'online'").fetchone()[0] == 0, "nothing is booked until the payment is confirmed"
    row = paymentrow(p["order_id"])
    assert row["status"] == "created" and row["amount_paise"] == 120000 and row["payload"] and b"1234-5678" not in bytes(row["payload"]) and b"Test Guest" not in bytes(row["payload"]), "booking details are sealed"
    hold_until = c.execute("SELECT MIN(expires_at) FROM holds WHERE token = ?", (p["hold_token"],)).fetchone()[0]
    assert hold_until > (m.now() + timedelta(minutes=pay.PAY_MINUTES - 1)).isoformat(timespec="seconds"), "the rooms stay held while the guest pays"
    # another guest cannot take the held rooms
    free_ids = {r["id"] for r in m.free_rooms(m.connect(), m.today() + timedelta(days=40), m.today() + timedelta(days=42), req()["property_id"], 1)}
    held = {r[0] for r in c.execute("SELECT room_id FROM holds WHERE token = ?", (p["hold_token"],))}
    assert held and not held & free_ids
    st, b = settle(p)
    assert st == 200 and b["status"] == "Confirmed" and b["paid_now"], b
    assert b["payment"] == {"method": "Online payment", "status": "paid", "channel": "upi", "kind": "full", "paid": 1200.0, "refunded": 0.0, "refund_in_progress": 0.0, "refund_status": "none", "balance": 0.0}, b["payment"]
    rows = bookings_of(b["booking_id"])
    assert len(rows) == 1 and rows[0]["payment_status"] == "paid" and rows[0]["payment_method"] == "online" and rows[0]["lang"] == "en" and rows[0]["amount"] == 1200.0
    row = paymentrow(p["order_id"])
    assert row["status"] == "paid" and row["group_id"] == b["booking_id"] and row["payload"] is None and row["payment_id"].startswith("pay_mock_") and row["method"] == "upi"
    assert c.execute("SELECT COUNT(*) FROM holds WHERE token = ?", (p["hold_token"],)).fetchone()[0] == 0
    assert "Paid Rs 1,200.00 online" in outbox("sms")[-1]["text"] and len(outbox("email")) == 1, "the confirmation goes out once, mentioning the payment"
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'payment.captured'").fetchone()[0] == 1
    cols = {r[1] for r in c.execute("PRAGMA table_info(payments)")}
    assert not cols & {"card", "card_number", "cvv", "pan", "expiry"}, "no card data is ever stored"
    c.close()


def test_advance_payment():
    online(advance=25)
    reset()
    p, _ = start("pay_advance")
    assert p["amount"] == 300.0 and p["balance"] == 900.0 and p["kind"] == "advance"
    st, b = settle(p)
    assert st == 200 and b["payment"]["status"] == "partial" and b["payment"]["paid"] == 300.0 and b["payment"]["balance"] == 900.0 and b["amount"] == 1200.0
    assert "Paid Rs 300.00 online; balance Rs 900.00 at the counter" in outbox("sms")[-1]["text"]
    pdf = outbox("email")[-1]["attachments"][0][2]
    assert pdf.startswith(b"%PDF")
    assert b["can_change"] is False, "a booking with money received is changed by the property"
    online(advance="")
    assert book("pay_advance")[0] == 400
    online("full,advance,counter", advance=25)
    assert book("pay_bogus")[0] == 400


def test_signatures_and_idempotency():
    online()
    reset()
    p, _ = start("pay_full")
    st, r = mock_pay(p)
    ok = {"order_id": r["order_id"], "payment_id": r["payment_id"], "signature": r["signature"]}
    for bad in (ok | {"signature": "0" * 64}, ok | {"payment_id": "pay_other"}, ok | {"order_id": "order_mock_nope"}, ok | {"signature": ""}):
        assert call("POST", "/api/public/payment/verify", bad)[0] == 400
    assert paymentrow(p["order_id"])["status"] == "created" and not db().execute("SELECT 1 FROM bookings WHERE source = 'online'").fetchone(), "a bad signature changes nothing"
    st1, b1 = call("POST", "/api/public/payment/verify", ok)
    st2, b2 = call("POST", "/api/public/payment/verify", ok)
    assert st1 == st2 == 200 and b1["booking_id"] == b2["booking_id"]
    # the provider's webhook for the same payment, twice more: still one booking, one payment, one confirmation
    for eid in ("evt1", "evt2"):
        assert hook("payment.captured", p["order_id"], r["payment_id"], 120000, eid=eid)[0] == 200
    assert hook("order.paid", p["order_id"], r["payment_id"], 120000, eid="evt3")[0] == 200
    c = db()
    assert c.execute("SELECT COUNT(*) FROM bookings WHERE source = 'online'").fetchone()[0] == 1 and c.execute("SELECT COUNT(*) FROM payments WHERE status = 'paid'").fetchone()[0] == 1
    assert len(outbox("email")) == 1 and len(outbox("sms")) == 1
    assert hook("payment.captured", p["order_id"], r["payment_id"], 120000, eid="evt1") == (200, {"ok": True, "duplicate": True}), "a repeated event id is recognised"
    assert c.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0] == 3
    c.close()


def test_webhook_alone_books_and_rejects_forgeries():
    online()
    reset()
    p, _ = start("pay_full")
    body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_w1", "order_id": p["order_id"], "amount": 120000, "method": "card"}}}}).encode()
    assert m.payment_webhook(body, "0" * 64)[0] == 400 and m.payment_webhook(body, "")[0] == 400 and m.payment_webhook(body, None)[0] == 400
    assert m.payment_webhook(body + b" ", providers.payments().webhook_signature(body))[0] == 400, "the signature covers the exact body"
    assert paymentrow(p["order_id"])["status"] == "created"
    assert m.payment_webhook(b"not json", providers.payments().webhook_signature(b"not json"))[0] == 400
    assert m.payment_webhook(body, providers.payments().webhook_signature(body), "e1")[0] == 200
    row = paymentrow(p["order_id"])
    assert row["status"] == "paid" and row["method"] == "card" and bookings_of(row["group_id"])[0]["payment_status"] == "paid", "the webhook alone completes the booking (the guest closed the tab)"
    assert hook("payment.captured", "order_unknown", "pay_x", 100, eid="e2")[0] == 200, "an event for an unknown order is acknowledged, not retried forever"
    assert hook("payment.failed", p["order_id"], "pay_f", 0, eid="e3")[0] == 200
    os.environ["PAYMENT_PROVIDER"] = "razorpay"  # real provider without a webhook secret: nothing verifies
    os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"] = "rzp_test_k", "s"
    assert m.payment_webhook(body, "anything")[0] == 400
    import hmac as _h
    os.environ["RAZORPAY_WEBHOOK_SECRET"] = "whsec_123456"
    good = _h.new(b"whsec_123456", body, "sha256").hexdigest()
    assert m.payment_webhook(body, good, "e9")[0] == 200 and m.payment_webhook(body, "x" + good[1:])[0] == 400


def test_failed_payment_can_retry_and_amount_mismatch_refunds():
    online(refund=100)
    reset()
    p, _ = start("pay_full")
    st, e = mock_pay(p, "fail")
    assert st == 402 and "not completed" in e["error"]
    assert hook("payment.failed", p["order_id"], "pay_f1", 120000, eid="f1")[0] == 200
    row = paymentrow(p["order_id"])
    assert row["status"] == "created", "a failed attempt leaves the order open for a retry within the window"
    assert settle(p)[0] == 200, "the retry succeeds"
    # a captured amount that does not match the order is never booked: it is refunded
    p2, _ = start("pay_full", check_in=D(60), check_out=D(61))
    assert hook("payment.captured", p2["order_id"], "pay_short", 5000, eid="m1")[0] == 200
    row = paymentrow(p2["order_id"])
    assert row["status"] == "unfulfilled" and row["failure_reason"] == "amount mismatch"
    c = db()
    rf = c.execute("SELECT * FROM refunds WHERE payment_row = ?", (row["id"],)).fetchone()
    assert rf["status"] == "processed" and rf["amount_paise"] == 5000 - 5000 + row["amount_paise"], "refunded through the provider in full"
    assert c.execute("SELECT COUNT(*) FROM alerts WHERE kind = 'payment' AND resolved_at IS NULL").fetchone()[0] == 1
    c.close()


def test_abandoned_and_unpaid_holds_release_rooms():
    online()
    reset()
    cn = m.connect()
    ci, co = m.today() + timedelta(days=40), m.today() + timedelta(days=42)
    free = lambda: len(m.free_rooms(cn, ci, co, req()["property_id"], 1))
    before = free()
    p, _ = start("pay_full")
    assert free() == before - 1
    # the guest closes the payment window: the room is released at once
    assert call("POST", "/api/public/payment/cancel", {"order_id": p["order_id"]})[0] == 200
    assert free() == before and paymentrow(p["order_id"])["status"] == "abandoned"
    assert call("GET", "/api/public/payment/status", {"order_id": p["order_id"]})[1]["status"] == "abandoned"
    # a late capture after the guest left still books, while the rooms are free
    assert hook("payment.captured", p["order_id"], "pay_late", 120000, eid="l1")[0] == 200
    row = paymentrow(p["order_id"])
    assert row["status"] == "paid" and row["group_id"] and free() == before - 1
    # an unpaid order whose time ran out: the hold lapses by itself and tick() closes the record
    p2, _ = start("pay_full", check_in=D(70), check_out=D(71))
    c = db()
    c.execute("UPDATE payments SET expires_at = ? WHERE order_id = ?", ((m.now() - timedelta(minutes=1)).isoformat(timespec="seconds"), p2["order_id"]))
    c.execute("UPDATE holds SET expires_at = ? WHERE token = ?", ((m.now() - timedelta(minutes=1)).isoformat(timespec="seconds"), p2["hold_token"]))
    assert len(m.free_rooms(cn, m.today() + timedelta(days=70), m.today() + timedelta(days=71), req()["property_id"], 1)) == before, "an expired hold no longer blocks the room"
    assert pay.tick(cn)["expired"] == 1 and paymentrow(p2["order_id"])["status"] == "expired"
    assert pay.tick(cn)["expired"] == 0
    # a capture that arrives after the rooms went to someone else: refunded in full, nothing double-booked
    p3, _ = start("pay_full", check_in=D(80), check_out=D(81))
    call("POST", "/api/public/payment/cancel", {"order_id": p3["order_id"]})
    others = [r["id"] for r in m.free_rooms(cn, m.today() + timedelta(days=80), m.today() + timedelta(days=81), req()["property_id"], 1)]
    c.executemany("INSERT INTO room_status_events (room_id, kind, reason, start_date, end_date, active, created_at) VALUES (?, 'internal_block', 'taken', ?, ?, 1, 'now')", [(r, D(80), D(81)) for r in others])
    assert hook("payment.captured", p3["order_id"], "pay_gone", p3["amount_paise"], eid="g1")[0] == 200
    row = paymentrow(p3["order_id"])
    assert row["status"] == "unfulfilled" and not row["group_id"]
    rf = c.execute("SELECT status, amount_paise FROM refunds WHERE payment_row = ?", (row["id"],)).fetchone(); assert rf and tuple(rf) == ("processed", p3["amount_paise"]), rf and tuple(rf)
    c.execute("DELETE FROM room_status_events WHERE reason = 'taken'")
    c.close()
    cn.close()


def test_sealed_details_are_discarded():
    online()
    reset()
    p, _ = start("pay_full")
    c = db()
    c.execute("UPDATE payments SET status = 'expired', created_at = ? WHERE order_id = ?", ((m.now() - timedelta(hours=pay.PAYLOAD_KEEP_HOURS + 1)).isoformat(timespec="seconds"), p["order_id"]))
    cn = m.connect()
    pay.tick(cn)
    assert paymentrow(p["order_id"])["payload"] is None, "unpaid booking details (with the ID number) do not linger"
    assert hook("payment.captured", p["order_id"], "pay_verylate", 120000, eid="v1")[0] == 200
    assert paymentrow(p["order_id"])["status"] == "unfulfilled", "no details left to book from: refund"
    cn.close()
    c.close()


def test_cancel_refund_follows_policy():
    online(refund=50)
    reset()
    p, _ = start("pay_full")
    st, b = settle(p)
    st, c = call("POST", "/api/public/booking/cancel", dict(booking_id=b["booking_id"], phone=b["mobile"], reason="plans changed"))
    assert st == 200 and c["status"] == "Cancelled"
    assert c["payment"]["refunded"] == 600.0 and c["payment"]["refund_status"] == "processed" and c["refund"]["amount"] == 600.0 and c["payment"]["status"] == "partially_refunded", c["payment"]
    cn = db()
    rf = cn.execute("SELECT * FROM refunds").fetchall()
    assert len(rf) == 1 and rf[0]["amount_paise"] == 60000 and rf[0]["status"] == "processed" and rf[0]["provider_refund_id"].startswith("rfnd_mock_")
    assert "refund of Rs 600.00" in outbox("sms")[-1]["text"].lower() or "600.00" in outbox("sms")[-1]["text"]
    assert call("POST", "/api/public/booking/cancel", dict(booking_id=b["booking_id"], phone=b["mobile"], reason="again"))[0] == 409
    assert cn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0] == 1, "cancelling twice never refunds twice"
    # 100% policy, advance only paid: refund of what was paid
    cn.execute("DELETE FROM refunds")
    setting(refund_percent=100)
    p2, _ = start("pay_advance", check_in=D(66), check_out=D(68))
    st, b2 = settle(p2)
    st, c2 = call("POST", "/api/public/booking/cancel", dict(booking_id=b2["booking_id"], phone=b2["mobile"], reason="x"))
    assert c2["payment"]["refunded"] == 300.0 and c2["payment"]["status"] == "refunded"
    # 0% policy: nothing to refund, nothing recorded
    setting(refund_percent=0)
    p3, _ = start("pay_full", check_in=D(75), check_out=D(76))
    b3 = settle(p3)[1]
    c3 = call("POST", "/api/public/booking/cancel", dict(booking_id=b3["booking_id"], phone=b3["mobile"], reason="x"))[1]
    assert c3["payment"]["refund_status"] == "none" and c3["refund"]["amount"] == 0
    cn.close()


def test_refund_without_policy_waits_for_a_person():
    online()  # refund_percent not set: no number is assumed
    reset()
    p, _ = start("pay_full")
    b = settle(p)[1]
    c = call("POST", "/api/public/booking/cancel", dict(booking_id=b["booking_id"], phone=b["mobile"], reason="x"))[1]
    assert c["payment"]["refund_status"] == "manual" and c["payment"]["refunded"] == 0.0 and c["payment"]["refund_in_progress"] == 1200.0
    cn = m.connect()
    rf = cn.execute("SELECT * FROM refunds").fetchone()
    assert rf["status"] == "manual" and rf["amount_paise"] == 120000 and "policy not set" in rf["reason"]
    alert = [a for a in m.alerts_open(cn) if a["kind"] == "refund"]
    assert len(alert) == 1 and "need a person" in alert[0]["message"]
    pay.refund_manual(cn, None, rf["id"], True, "paid back by bank transfer")
    assert cn.execute("SELECT status FROM refunds").fetchone()[0] == "processed" and not [a for a in m.alerts_open(cn) if a["kind"] == "refund"]
    assert call("POST", "/api/public/booking/lookup", dict(booking_id=b["booking_id"], phone=b["mobile"]))[1]["payment"]["refund_status"] == "processed"
    try:
        pay.refund_manual(cn, None, rf["id"], True, "")
    except m.UserError as e:
        assert e.status == 404
    else:
        raise AssertionError("a finished refund cannot be actioned again")
    cn.close()


def test_refund_retries_once_and_gives_up():
    online(refund=100)
    reset()
    calls = []
    real = providers.MockPayments.refund

    def flaky(self, payment_id, amount, receipt):
        calls.append(receipt)
        raise providers.ProviderError("Razorpay request failed: URLError")
    providers.MockPayments.refund = flaky
    try:
        p, _ = start("pay_full")
        b = settle(p)[1]
        call("POST", "/api/public/booking/cancel", dict(booking_id=b["booking_id"], phone=b["mobile"], reason="x"))
        cn = m.connect()
        rf = cn.execute("SELECT * FROM refunds").fetchone()
        assert rf["status"] == "requested" and rf["attempts"] == 1 and "URLError" in rf["error"], dict(rf)
        for _ in range(pay.REFUND_ATTEMPTS - 1):
            pay.process_refunds(cn)
        rf = cn.execute("SELECT * FROM refunds").fetchone()
        assert rf["status"] == "failed" and rf["attempts"] == pay.REFUND_ATTEMPTS and len(calls) == pay.REFUND_ATTEMPTS
        assert pay.process_refunds(cn) == 0 and len(calls) == pay.REFUND_ATTEMPTS, "a failed refund is not retried by itself"
        assert [a for a in m.alerts_open(cn) if a["kind"] == "refund"]
    finally:
        providers.MockPayments.refund = real
    pay.refund_manual(cn, None, rf["id"], False, "")  # a manager sends it again
    rf = cn.execute("SELECT * FROM refunds").fetchone()
    assert rf["status"] == "processed" and not [a for a in m.alerts_open(cn) if a["kind"] == "refund"]
    # every refund goes to the provider exactly once, with its own receipt as the idempotency key
    n = []
    providers.MockPayments.refund = lambda self, pid, amt, rcpt: (n.append(rcpt), {"id": "rfnd_x" + str(len(n)), "status": "pending"})[1]
    try:
        p2, _ = start("pay_full", check_in=D(66), check_out=D(67))
        b2 = settle(p2)[1]
        call("POST", "/api/public/booking/cancel", dict(booking_id=b2["booking_id"], phone=b2["mobile"], reason="x"))
        pay.process_refunds(cn)
        pay.process_refunds(cn)
        assert len(n) == 1
        r2 = cn.execute("SELECT * FROM refunds WHERE status = 'pending'").fetchone()
        assert r2["provider_refund_id"] == "rfnd_x1"
        assert call("POST", "/api/public/booking/lookup", dict(booking_id=b2["booking_id"], phone=b2["mobile"]))[1]["payment"]["refund_status"] == "pending"
        assert hook("refund.processed", None, eid="r1", refund_id="rfnd_x1")[0] == 200  # the provider's webhook completes the tracking
        assert cn.execute("SELECT status FROM refunds WHERE id = ?", (r2["id"],)).fetchone()[0] == "processed"
        assert hook("refund.failed", None, eid="r2", refund_id="rfnd_x1")[0] == 200
    finally:
        providers.MockPayments.refund = real
    cn.close()


def test_reconciliation():
    online(refund=50)
    reset()
    b1 = settle(start("pay_full")[0])[1]  # paid in full
    b2 = settle(start("pay_advance", check_in=D(60), check_out=D(62))[0])[1]  # advance only: balance due
    b3 = book(check_in=D(70), check_out=D(72))[1]  # counter: nothing paid
    b4 = settle(start("pay_full", check_in=D(80), check_out=D(82))[0])[1]
    call("POST", "/api/public/booking/cancel", dict(booking_id=b4["booking_id"], phone=b4["mobile"], reason="x"))  # half refunded
    cn = m.connect()
    props = [r[0] for r in cn.execute("SELECT id FROM properties")]
    rep = pay.reconcile(cn, props, D(0), D(120))
    by = {r["booking_id"]: r for r in rep["rows"]}
    assert by[b1["booking_id"]]["flags"] == [] and by[b1["booking_id"]]["net"] == 1200.0 and by[b1["booking_id"]]["balance"] == 0
    assert by[b2["booking_id"]]["flags"] == ["unpaid"] and by[b2["booking_id"]]["balance"] == 900.0
    assert b3["booking_id"] not in by or by[b3["booking_id"]]["flags"] == ["unpaid"]
    assert by[b4["booking_id"]]["paid"] == 1200.0 and by[b4["booking_id"]]["refunded"] == 600.0 and by[b4["booking_id"]]["flags"] == ["mismatch"], by[b4["booking_id"]]  # 600 kept after cancelling: policy keeps half
    t = rep["totals"]
    assert t["online"] == 1200 + 300 + 1200 and t["refunded"] == 600 and t["counter"] == 0
    # money taken at the desk reconciles too
    g = b2["booking_id"]
    m.write(cn, lambda: pay.record_counter(cn, g, 900, "cash", None))
    rep = pay.reconcile(cn, props, D(0), D(120))
    row = {r["booking_id"]: r for r in rep["rows"]}[g]
    assert row["flags"] == [] and row["net"] == 1200.0 and rep["totals"]["counter"] == 900
    assert {r["payment_status"] for r in bookings_of(g)} == {"paid"}
    try:
        m.write(cn, lambda: pay.record_counter(cn, g, 0, "cash", None))
    except m.UserError:
        pass
    else:
        raise AssertionError("a zero payment was accepted")
    cn.close()


def test_payment_needs_verified_phone_and_throttles():
    online()
    reset()
    m.OTP_REQUIRED = True
    try:
        tok = hold()
        st, e = book("pay_full", token=tok)
        assert st == 403 and "verify your mobile" in e["error"], "no online payment without the mobile-number code either"
        assert not db().execute("SELECT 1 FROM payments").fetchone()
    finally:
        m.OTP_REQUIRED = False
    # starting payments is limited per device (each needs its own hold, so the holds come from different addresses here)
    m.reset_throttle()
    results = []
    for i in range(7):
        ci, co = D(50 + 2 * i), D(51 + 2 * i)
        tok = m.dispatch("POST", "/api/public/hold", None, req(check_in=ci, check_out=co), f"10.1.0.{i}")[1]["token"]
        results.append(m.dispatch("POST", "/api/public/book", None, req(check_in=ci, check_out=co) | guest() | dict(payment_method="pay_full", hold_token=tok), IP)[0])
    assert results == [200] * 6 + [429], results


def test_mock_checkout_is_local_only():
    online()
    reset()
    p, _ = start("pay_full")
    m.HTTPS_ONLY = True
    try:
        assert mock_pay(p)[0] == 404, "the test checkout does not exist on a public (HTTPS) deployment"
    finally:
        m.HTTPS_ONLY = False
    os.environ.pop("PAYMENT_PROVIDER")
    assert mock_pay(p)[0] == 404
    assert call("POST", "/api/public/payment/verify", {"order_id": p["order_id"], "payment_id": "x", "signature": "y"})[0] == 503


def test_tick_runs_everything():
    online(refund=100)
    reset()
    out = m.tick()
    assert set(out) >= {"alerts", "waitlist_notified", "payments", "reminders", "messages_retried", "id_purged", "heartbeat"}, out
    assert out["payments"] == {"expired": 0, "refunds_sent": 0}


def test_id_retention():
    cn = m.connect()
    assert m.purge_ids(cn) == 0, "nothing is erased while the retention setting is empty"
    st, b = book(guests=2)
    c = db()
    gid = b["booking_id"]
    c.execute("UPDATE bookings SET status = 'Checked-Out', check_in = ?, check_out = ? WHERE group_id = ?", (D(-40), D(-38), gid))
    c.execute("INSERT INTO id_records (booking_id, id_type, last4, number_enc, image_enc, image_mime, captured_at) VALUES (?, 'Aadhaar', '9012', x'01', x'02', 'image/jpeg', ?)", (gid, m.now_iso()))
    assert m.purge_ids(cn) == 0
    setting(id_retention_days=90)
    assert m.purge_ids(cn) == 0, "inside the retention period"
    setting(id_retention_days=30)
    assert m.purge_ids(cn) == 2
    rec = c.execute("SELECT * FROM id_records WHERE booking_id = ?", (gid,)).fetchone()
    assert rec["number_enc"] is None and rec["image_enc"] is None and rec["last4"] is None and rec["purged_at"]
    assert c.execute("SELECT id_number FROM bookings WHERE group_id = ?", (gid,)).fetchone()[0] == ""
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'id.purge'").fetchone()[0] == 1
    assert m.purge_ids(cn) == 0
    c.execute("DELETE FROM id_records")
    c.execute("DELETE FROM settings WHERE key = 'id_retention_days'")
    cn.close()
    c.close()


def test_overlap_guard_in_the_database():
    c = db()
    cn = m.connect()
    st, b = book()
    row = c.execute("SELECT * FROM bookings WHERE group_id = ?", (b["booking_id"],)).fetchone()
    for ins in (("Confirmed", row["check_in"], row["check_out"]), ("Checked-In", row["check_in"], D(41))):
        try:
            c.execute("INSERT INTO bookings (property_id, room_id, guest_name, phone, id_type, id_number, guests, check_in, check_out, status, created_at, status_at) VALUES (?,?, 'X', '9000000000', 'Other', 'x', 1, ?, ?, ?, 'n', 'n')",
                      (row["property_id"], row["room_id"], ins[1], ins[2], ins[0]))
        except sqlite3.IntegrityError as e:
            assert "already booked" in str(e)
        else:
            raise AssertionError("an overlapping booking was written")
    c.execute("INSERT INTO bookings (property_id, room_id, guest_name, phone, id_type, id_number, guests, check_in, check_out, status, created_at, status_at) VALUES (?,?, 'X', '9000000000', 'Other', 'x', 1, ?, ?, 'Confirmed', 'n', 'n')",
              (row["property_id"], row["room_id"], row["check_out"], D(44)))  # back to back is fine
    other = c.execute("SELECT id FROM bookings WHERE guest_name = 'X'").fetchone()[0]
    try:
        c.execute("UPDATE bookings SET check_in = ? WHERE id = ?", (D(41), other))
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("an update that overlaps was allowed")
    c.execute("UPDATE bookings SET status = 'Cancelled' WHERE group_id = ?", (b["booking_id"],))
    c.execute("UPDATE bookings SET check_in = ? WHERE id = ?", (D(41), other))  # fine once the other stay is cancelled
    c.execute("DELETE FROM bookings WHERE guest_name = 'X'")
    cn.close()
    c.close()


if __name__ == "__main__":
    os.environ.pop("PAYMENT_PROVIDER", None)
    try:
        m.OTP_REQUIRED = False
        cn = m.connect()  # migrates the copy
        cn.execute("UPDATE room_types SET tariff = 600 WHERE id = 1")  # known prices for the money tests: 600 a night, no peak surcharge, no promo in the way
        cn.execute("UPDATE peak_periods SET active = 0")
        cn.execute("UPDATE promo_codes SET active = 0")
        cn.close()
        for name, fn in list(globals().items()):
            if name.startswith("test_") and callable(fn):
                try:
                    cleanup()
                    fn()
                except BaseException:
                    print("FAILED:", name, file=sys.stderr)
                    raise
        print("ok")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
