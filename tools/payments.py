"""Online payments (Razorpay, UPI / cards / net banking through Razorpay Checkout) behind providers.payments(), and the payment records that reconcile against bookings.

Flow. The guest picks full payment, the advance, or pay at the counter. For an online option, start() checks the stay, extends the room hold to PAY_MINUTES, creates a provider
order and stores a `payments` row ("created") holding the booking details, sealed with idvault. The booking itself is only created when the payment is confirmed:
  * the page's checkout success handler calls confirm() with the provider's signature, and/or
  * the provider's webhook calls webhook() (signature checked over the raw body; each event id is processed once).
Both end in captured(), which is idempotent: a payment is applied once however many times it is reported. If the rooms cannot be given any more (the hold lapsed and someone
else took them) the payment is refunded in full automatically and recorded as "unfulfilled".
An unpaid or abandoned payment simply lets the hold lapse (the rooms are released) and tick() marks the row expired. Card details never reach this server (Razorpay Checkout
collects them); only provider ids, the method name and amounts are stored.
Amounts: rupees in the API, integer paise in the tables.
"""
import json
import secrets
import sqlite3
import sys
import traceback
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

import idvault
import providers

g = None  # the guest module; mantralayam_rooms binds itself here at import (importing it from here would load it twice when it runs as __main__)
PAY_MINUTES = 15  # how long the rooms stay held for an online payment
PAYLOAD_KEEP_HOURS = 24  # an unpaid row keeps its sealed booking details this long, in case the provider reports a late capture
MIN_PAISE = 100  # Razorpay does not accept orders under Rs 1
REFUND_ATTEMPTS = 5
OPTIONS = ("full", "advance", "counter")


# ---------- pure calculations (tested) ----------
def to_paise(rupees):
    return int((Decimal(str(rupees)) * 100).quantize(Decimal(1), ROUND_HALF_UP))


def rupees(paise):
    return float(Decimal(paise) / 100)


def pay_now(total, advance, kind):
    """Paise to collect online now. `full` is the whole total; `advance` the advance from the quote (advance_percent of the total, already rounded to the paisa).
    Raises when the option cannot be used: unknown kind, no advance configured, or an amount below the provider's minimum."""
    t = to_paise(total)
    if kind == "full":
        amount = t
    elif kind == "advance":
        amount = to_paise(advance)
        if amount <= 0:
            raise g.UserError("An advance payment is not available for this booking.")
    else:
        raise g.UserError("Please choose a valid payment option.")
    if amount < MIN_PAISE or amount > t:
        raise g.UserError("This booking is too small to pay online. Please choose pay at the counter.")
    return amount


def refund_paise(paid_paise, percent):
    """Paise to refund of `paid_paise` under a policy of `percent` (0 to 100), half up to the paisa. No policy (None), nothing paid or a negative amount: 0."""
    if percent is None or not paid_paise or paid_paise <= 0:
        return 0
    return int((Decimal(paid_paise) * Decimal(percent) / 100).quantize(Decimal(1), ROUND_HALF_UP))


def cancel_share(net_paise, cancelled, live_total):
    """The part of the net money received that belongs to the rooms being cancelled: everything when all live rooms go, else in proportion to their amounts (rounded down)."""
    if live_total <= 0 or cancelled >= live_total:
        return max(net_paise, 0)
    return max(net_paise, 0) * int(Decimal(str(cancelled)) * 100) // int(Decimal(str(live_total)) * 100)


# ---------- configuration ----------
def active_provider():
    """The configured payment provider, or None when none is set or it is misconfigured (the problem is printed, the site then offers pay at the counter only)."""
    try:
        return providers.payments()
    except providers.ProviderError as e:
        print(f"Payment provider problem: {e}", file=sys.stderr)
        return None


def options(s):
    """The payment options a guest may choose now: online ones only when a provider is configured, `advance` only when advance_percent is set; pay at the counter is always
    possible when no online option is on (so there is always a way to book)."""
    on = set(s["pay_options"])
    out = []
    if active_provider():
        if "full" in on:
            out.append("full")
        if "advance" in on and s["advance_percent"]:
            out.append("advance")
    if "counter" in on or not out:
        out.append("counter")
    return out


def enabled(s):
    return any(o != "counter" for o in options(s))


def public_config(s):
    """What the guest page needs. `secure_badge` is true only when an online option is on, so the badge never appears otherwise."""
    opts = options(s)
    online = enabled(s)
    if not online:
        return {"enabled": False, "options": ["counter"], "secure_badge": False}
    prov = active_provider()
    return {"enabled": True, "options": opts, "advance_percent": float(s["advance_percent"]) if s["advance_percent"] else None, "provider": prov.name, "key_id": prov.key_id,
            "test_mode": prov.name == "mock" or prov.key_id.startswith("rzp_test_"), "mock": prov.name == "mock", "secure_badge": True, "hold_minutes": PAY_MINUTES}


def provider():
    p = active_provider()
    if not p:
        raise g.UserError("Online payment is not available right now. Please choose pay at the counter.", 503)
    return p


# ---------- helpers ----------
def seal_payload(conn, order_id, data):
    return idvault.seal(g.vault_key(conn), json.dumps(data).encode(), order_id.encode())


def open_payload(conn, row):
    if not row["payload"]:
        return None
    return json.loads(idvault.open_(g.vault_key(conn), bytes(row["payload"]), row["order_id"].encode()))


def gid_of(r):
    return r["group_id"] or r["id"]


def group_rows(conn, gid):
    return conn.execute("SELECT * FROM bookings WHERE group_id = ? OR (group_id IS NULL AND id = ?) ORDER BY id", (gid, gid)).fetchall()


def open_refund_sql(alias=""):
    return f"{alias}status IN ('requested', 'sending', 'pending', 'processed', 'manual')"


def money_state(conn, gid):
    """Paise paid, refunded (processed), refund in progress (requested/sending/pending), refund waiting for a person (manual) and failed, for one booking group."""
    paid, any_row = conn.execute("SELECT COALESCE(SUM(CASE WHEN status = 'paid' THEN amount_paise END), 0), COUNT(*) FROM payments WHERE group_id = ?", (gid,)).fetchone()
    if not any_row:  # a booking marked paid before payment records existed: its own amounts count as received
        paid = to_paise(conn.execute("SELECT COALESCE(SUM(amount), 0) FROM bookings WHERE (group_id = ? OR (group_id IS NULL AND id = ?)) AND payment_status = 'paid'", (gid, gid)).fetchone()[0])
    by = {r[0]: r[1] for r in conn.execute("SELECT r.status, SUM(r.amount_paise) FROM refunds r WHERE r.group_id = ? GROUP BY r.status", (gid,))}
    done, open_, manual, failed = by.get("processed", 0), sum(by.get(k, 0) for k in ("requested", "sending", "pending")), by.get("manual", 0), by.get("failed", 0)
    return {"paid": paid, "refunded": done, "refund_open": open_, "refund_manual": manual, "refund_failed": failed, "net": paid - done - open_ - manual}


def apply_status(conn, gid):
    """Sets payment_status (and payment_method for online payments) on every row of the booking group from the money recorded. Call inside the writing transaction."""
    rows = group_rows(conn, gid)
    if not rows:
        return
    m = money_state(conn, gid)
    live = [r for r in rows if r["status"] != "Cancelled"]
    total = to_paise(sum(r["amount"] or 0 for r in live))
    if not live:
        status = "due" if m["paid"] <= 0 else "refunded" if m["net"] <= 0 else "partially_refunded"
    else:
        status = "paid" if m["net"] > 0 and m["net"] >= total else "partial" if m["net"] > 0 else "due"
    online = conn.execute("SELECT 1 FROM payments WHERE group_id = ? AND status = 'paid' AND provider != 'counter' LIMIT 1", (gid,)).fetchone()
    conn.execute("UPDATE bookings SET payment_status = ?, payment_method = CASE WHEN ? THEN 'online' ELSE payment_method END WHERE id IN (%s)" % ",".join("?" * len(rows)),
                 (status, bool(online), *[r["id"] for r in rows]))


def summary(conn, gid, total):
    """The guest- and staff-facing money summary of a booking group, in rupees. `total` is the booking's total in rupees."""
    m = money_state(conn, gid)
    r = rupees
    online = conn.execute("SELECT kind, method, provider FROM payments WHERE group_id = ? AND status = 'paid' ORDER BY id LIMIT 1", (gid,)).fetchone()
    refunds = [dict(x) for x in conn.execute("SELECT id, amount_paise, status, reason, created_at, updated_at FROM refunds WHERE group_id = ? ORDER BY id", (gid,))]
    states = {x["status"] for x in refunds}
    rstatus = "none" if not refunds else "failed" if "failed" in states else "manual" if "manual" in states else "pending" if states & {"requested", "sending", "pending"} else "processed"
    return {"paid": r(m["paid"]), "refunded": r(m["refunded"]), "refund_in_progress": r(m["refund_open"] + m["refund_manual"]), "net": r(m["net"]), "balance": max(0.0, round(total - r(m["net"]), 2)),
            "refund_status": rstatus, "method": online["method"] if online else None, "provider": online["provider"] if online else None, "kind": online["kind"] if online else None}


# ---------- starting a payment ----------
def start(conn, p, kind, ip):
    """Called by book() for an online option. Returns the checkout details for the page; nothing is booked yet."""
    s = g.settings(conn)
    if kind not in options(s) or kind == "counter":
        raise g.UserError("Please choose a valid payment option.")
    g.throttle(("pay-start", ip), 6, 600, "Too many payment attempts from this device.")
    prov = provider()
    plan = g.plan_booking(conn, p)  # raises when the rooms are gone or the party does not fit
    q = plan["q"]
    amount = pay_now(q["total"], q["advance"], kind)

    def extend():
        t = g.now()
        exp = (t + timedelta(minutes=PAY_MINUTES)).isoformat(timespec="seconds")
        tok = p["token"]
        have = conn.execute("SELECT COUNT(*) FROM holds WHERE token = ? AND expires_at > ?", (tok, g.now_iso())).fetchone()[0] if tok else 0
        if have >= p["n"]:
            conn.execute("UPDATE holds SET expires_at = ? WHERE token = ?", (exp, tok))
            return tok, exp
        tok = secrets.token_urlsafe(16)  # no live hold of the guest's own (it lapsed): take one now, if the rooms are still free
        free = g.free_rooms(conn, plan["ci"], plan["co"], p["pid"], p["tid"], None, **p["flt"])
        if len(free) < p["n"]:
            raise g.UserError("Sorry, those rooms are no longer available for your dates. Please search again.", 409)
        for r in g.pick_rooms(free, p["n"]):
            conn.execute("INSERT INTO holds (token, room_id, check_in, check_out, expires_at, ip) VALUES (?,?,?,?,?,?)", (tok, r["id"], p["ci"], p["co"], exp, ip))
        return tok, exp
    token, expires = g.write(conn, extend)
    p = dict(p, token=token)
    try:
        order = prov.create_order(amount, f"mr-{secrets.token_hex(6)}", {"purpose": kind, "phone_last4": p["phone"][-4:]})
    except providers.ProviderError as e:
        print(f"Payment order failed: {e}", file=sys.stderr)
        g.write(conn, lambda: conn.execute("DELETE FROM holds WHERE token = ?", (token,)))
        raise g.UserError("We could not start the payment. Please try again, or choose pay at the counter.", 503)
    oid = order["id"]
    proof = p.pop("proof", "")

    def store():
        if proof:
            p["verified"] = g.use_proof(conn, proof, p["phone"], g.OTP_REQUIRED)
        elif g.OTP_REQUIRED:
            raise g.UserError("Please verify your mobile number with the code we send.", 403)
        conn.execute("INSERT INTO payments (order_id, provider, kind, amount_paise, total_paise, status, hold_token, payload, phone10, created_at, expires_at, ip, property_id) VALUES (?,?,?,?,?, 'created', ?,?,?,?,?,?,?)",
                     (oid, prov.name, kind, amount, to_paise(q["total"]), token, seal_payload(conn, oid, {"p": p, "q": q}), g.p10(p["phone"]), g.now_iso(), expires, ip, p["pid"]))
    g.write(conn, store)
    prop = conn.execute("SELECT name FROM properties WHERE id = ?", (p["pid"],)).fetchone()[0]
    return {"payment_required": True, "payment": {"order_id": oid, "amount": rupees(amount), "amount_paise": amount, "currency": "INR", "kind": kind, "total": q["total"], "balance": round(q["total"] - rupees(amount), 2),
            "key_id": prov.key_id, "provider": prov.name, "mock": prov.name == "mock", "expires_at": expires, "seconds": PAY_MINUTES * 60, "name": "Mantralayam Rooms", "description": prop,
            "prefill": {"name": p["name"], "contact": p["phone"], "email": p["email"]}, "hold_token": token}}


# ---------- confirming a payment ----------
def confirm(conn, b, ip):
    """POST /api/public/payment/verify {order_id, payment_id, signature}: the checkout success handler. The signature proves the provider reported this payment."""
    g.throttle(("pay-verify", ip), 30, 600, "Too many attempts.")
    oid, pid, sig = (str(b.get(k) or "")[:100] for k in ("order_id", "payment_id", "signature"))
    prov = provider()
    if not (oid and pid and prov.verify_checkout(oid, pid, sig)):
        raise g.UserError("The payment could not be verified. If money was taken from your account it will be refunded automatically.", 400)
    method = b.get("method") if prov.name == "mock" and b.get("method") in ("upi", "card", "netbanking") else None  # a real provider reports the method itself, in its webhook
    out = captured(conn, oid, pid, None, method)
    return out_view(conn, out)


def out_view(conn, out):
    """The reply to the page after a payment was applied: the booking, or the explanation when the rooms could not be given."""
    if out.get("group_id"):
        rows = group_rows(conn, out["group_id"])
        return g.booking_view(conn, g.find_booking(conn, out["group_id"], rows[0]["phone"])) | {"paid_now": True}
    raise g.UserError("Your payment was received but the rooms are no longer available, so it is being refunded in full. Please search again.", 409)


def captured(conn, order_id, payment_id, amount_paise, method):
    """Applies a captured payment to its order, once. Safe to call any number of times, from the checkout handler, the webhook and tick().
    Returns {"status", "group_id"}; status is paid, unfulfilled (refund requested), or ignored (unknown order)."""
    def work():
        row = conn.execute("SELECT * FROM payments WHERE order_id = ?", (order_id,)).fetchone()
        if not row or row["provider"] == "counter":
            return {"status": "ignored", "group_id": None, "notify": False}
        if row["status"] == "paid" or row["status"] == "unfulfilled":
            if method and not row["method"]:  # the webhook usually knows how it was paid; the checkout handler does not
                conn.execute("UPDATE payments SET method = ? WHERE id = ?", (method, row["id"]))
            return {"status": row["status"], "group_id": row["group_id"], "notify": False}
        if amount_paise is not None and amount_paise != row["amount_paise"]:
            conn.execute("UPDATE payments SET status = 'unfulfilled', payment_id = ?, method = ?, paid_at = ?, failure_reason = 'amount mismatch' WHERE id = ?", (payment_id, method, g.now_iso(), row["id"]))
            conn.execute("INSERT OR IGNORE INTO refunds (payment_row, group_id, amount_paise, status, reason, dedupe_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                         (row["id"], None, row["amount_paise"], "requested", "Amount did not match the order", f"unfulfilled:{row['id']}", g.now_iso(), g.now_iso()))
            return {"status": "unfulfilled", "group_id": None, "notify": False, "alert": f"Payment {payment_id} did not match its order amount and is being refunded."}
        conn.execute("SAVEPOINT fulfil")
        try:
            data = open_payload(conn, row)
            if data is None:
                raise g.UserError("The booking details have expired.")
            gid = g.place(conn, data["p"], data["q"], method="online", use_otp=False)
        except (g.UserError, idvault.VaultError, sqlite3.IntegrityError) as e:  # the rooms are gone, or the stay no longer passes the rules: money back, in full
            conn.execute("ROLLBACK TO fulfil")
            conn.execute("RELEASE fulfil")
            conn.execute("UPDATE payments SET status = 'unfulfilled', payment_id = ?, method = ?, paid_at = ?, failure_reason = ? WHERE id = ?",
                         (payment_id, method, g.now_iso(), str(e)[:200], row["id"]))
            conn.execute("INSERT OR IGNORE INTO refunds (payment_row, group_id, amount_paise, status, reason, dedupe_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                         (row["id"], None, row["amount_paise"], "requested", "Rooms no longer available", f"unfulfilled:{row['id']}", g.now_iso(), g.now_iso()))
            return {"status": "unfulfilled", "group_id": None, "notify": False, "alert": f"Payment {payment_id} arrived after the rooms were released and is being refunded in full."}
        conn.execute("RELEASE fulfil")
        conn.execute("UPDATE payments SET status = 'paid', payment_id = ?, method = ?, paid_at = ?, group_id = ?, payload = NULL, failure_reason = NULL WHERE id = ?",
                     (payment_id, method, g.now_iso(), gid, row["id"]))
        apply_status(conn, gid)
        g.audit(conn, "payment.captured", data["p"]["pid"], None, f"#{gid}: Rs {rupees(row['amount_paise']):.2f} ({row['kind']}, {method or 'online'}), payment {payment_id}", "Online payment")
        return {"status": "paid", "group_id": gid, "notify": True}
    out = g.write(conn, work)
    if out.get("alert"):
        g.alert_raise(conn, "payment", out["alert"][:60], out["alert"])
    if out["status"] == "unfulfilled":
        process_refunds(conn)
    if out["notify"]:
        g.notify_safe(conn, "confirmed", out["group_id"])
    return out


def abandon(conn, b, ip):
    """POST /api/public/payment/cancel {order_id}: the guest closed the checkout. The rooms are released at once; a late capture is still handled (tick keeps the details a day)."""
    g.throttle(("pay-verify", ip), 30, 600, "Too many attempts.")
    oid = str(b.get("order_id") or "")[:100]

    def work():
        row = conn.execute("SELECT * FROM payments WHERE order_id = ? AND status = 'created'", (oid,)).fetchone()
        if row:
            conn.execute("UPDATE payments SET status = 'abandoned' WHERE id = ?", (row["id"],))
            conn.execute("DELETE FROM holds WHERE token = ?", (row["hold_token"],))
    g.write(conn, work)
    return {"ok": True}


def status(conn, q, ip):
    """GET /api/public/payment/status?order_id=: lets the page poll after checkout (the webhook may have booked it before the success handler ran)."""
    g.throttle(("pay-verify", ip), 60, 600, "Too many attempts.")
    row = conn.execute("SELECT status, group_id FROM payments WHERE order_id = ?", (str(q.get("order_id") or "")[:100],)).fetchone()
    if not row:
        raise g.UserError("We could not find that payment.", 404)
    return {"status": row["status"], "booking_id": row["group_id"]}


def mock_checkout(conn, b, ip):
    """POST /api/public/payment/mock {order_id, outcome: success|fail}: stands in for the provider's checkout window on a local machine (mock provider only, never over HTTPS)."""
    prov = providers.payments()
    if not prov or prov.name != "mock" or g.HTTPS_ONLY:
        raise g.UserError("Not found.", 404)
    g.throttle(("pay-verify", ip), 30, 600, "Too many attempts.")
    oid = str(b.get("order_id") or "")[:100]
    row = conn.execute("SELECT * FROM payments WHERE order_id = ? AND provider = 'mock'", (oid,)).fetchone()
    if not row:
        raise g.UserError("We could not find that payment.", 404)
    method = b.get("method") if b.get("method") in ("upi", "card", "netbanking") else "upi"
    if b.get("outcome") == "fail":
        conn.execute("UPDATE payments SET failure_reason = 'The mock payment was declined' WHERE id = ? AND status = 'created'", (row["id"],))
        raise g.UserError("The payment was not completed. You can try again, or choose pay at the counter.", 402)
    pay_id = "pay_mock_" + secrets.token_hex(7)
    return {"order_id": oid, "payment_id": pay_id, "signature": prov.sign(oid, pay_id), "method": method}


# ---------- webhook ----------
def webhook(conn, raw, signature, event_id=None):
    """Handles one provider webhook. `raw` is the exact request body (the signature covers it). Returns (http status, json).
    A bad signature is refused with 400 and does nothing. A verified event is answered 200 even when it needs no action, so the provider stops retrying."""
    try:
        prov = providers.payments()
    except providers.ProviderError:
        prov = None
    if not prov or not prov.verify_webhook(raw, signature):
        return 400, {"error": "Invalid signature."}
    try:
        event = json.loads(raw)
        kind = str(event["event"])
        ent = event.get("payload", {})
    except (ValueError, KeyError, TypeError):
        return 400, {"error": "Invalid event."}
    eid = (event_id or "")[:100] or "sha:" + g.sha(raw.decode("utf-8", "replace"))
    if conn.execute("SELECT 1 FROM webhook_events WHERE event_id = ?", (eid,)).fetchone():
        return 200, {"ok": True, "duplicate": True}
    pay = (ent.get("payment") or {}).get("entity") or {}
    ref = (ent.get("refund") or {}).get("entity") or {}
    if kind in ("payment.captured", "order.paid"):
        if kind == "order.paid":
            pay = pay or {}
        captured(conn, pay.get("order_id") or ((ent.get("order") or {}).get("entity") or {}).get("id"), pay.get("id"), pay.get("amount"), pay.get("method"))
    elif kind == "payment.failed":
        g.write(conn, lambda: conn.execute("UPDATE payments SET failure_reason = ? WHERE order_id = ? AND status IN ('created', 'abandoned')", (str(pay.get("error_description") or "failed")[:200], pay.get("order_id"))))
    elif kind in ("refund.processed", "refund.failed"):
        refund_update(conn, ref.get("id"), "processed" if kind == "refund.processed" else "failed")
    g.write(conn, lambda: conn.execute("INSERT OR IGNORE INTO webhook_events (event_id, kind, received_at) VALUES (?,?,?)", (eid, kind[:60], g.now_iso())))
    return 200, {"ok": True}


# ---------- refunds ----------
def request_cancel_refund(conn, gid, cancelled_ids, cancelled, live_total):
    """Inside the cancelling transaction: works out the refund the policy gives for the rooms just cancelled and records it. `cancelled` and `live_total` are rupee amounts
    (the cancelled rooms, and all live rooms just before). Returns the refund row ids. With no refund_percent set, or for money taken at the counter, the row is `manual`:
    a person must decide and pay it. Nothing is sent to the provider here: process_refunds() does that after the transaction commits."""
    pays = conn.execute("SELECT * FROM payments WHERE group_id = ? AND status = 'paid' ORDER BY id DESC", (gid,)).fetchall()
    if not pays:
        return []
    pct = g.settings(conn)["refund_percent"]
    share = cancel_share(money_state(conn, gid)["net"], cancelled, live_total)
    due = refund_paise(share, pct) if pct is not None else share
    ids, t = [], g.now_iso()
    event = "-".join(map(str, sorted(cancelled_ids)))
    for pay in pays:
        left = pay["amount_paise"] - conn.execute("SELECT COALESCE(SUM(amount_paise), 0) FROM refunds WHERE payment_row = ? AND status != 'failed'", (pay["id"],)).fetchone()[0]
        part = min(due, max(left, 0))
        if part <= 0:
            continue
        manual = pct is None or pay["provider"] == "counter"
        reason = "Cancelled: refund policy not set, decide and refund by hand" if pct is None else "Cancelled: money was taken at the counter, refund by hand" if manual else f"Cancelled: {pct}% refund policy"
        cur = conn.execute("INSERT OR IGNORE INTO refunds (payment_row, group_id, amount_paise, status, reason, dedupe_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                           (pay["id"], gid, part, "manual" if manual else "requested", reason, f"cancel:{gid}:{pay['id']}:{event}", t, t))
        if cur.rowcount:
            ids.append(cur.lastrowid)
        due -= part
    apply_status(conn, gid)
    return ids


def process_refunds(conn):
    """Sends every `requested` refund to the provider (once each: a refund is claimed before the call, and the provider is given the refund's receipt as an idempotency key).
    A network or provider error leaves it to be retried by the next tick, up to REFUND_ATTEMPTS times, then it is marked failed for a person to handle. Returns how many were sent."""
    sent = 0
    try:
        prov = providers.payments()
    except providers.ProviderError:
        prov = None
    g.write(conn, lambda: conn.execute("UPDATE refunds SET status = 'requested' WHERE status = 'sending' AND updated_at < ?", ((g.now() - timedelta(minutes=10)).isoformat(timespec="seconds"),)))
    for r in conn.execute("SELECT r.*, p.payment_id, p.provider FROM refunds r JOIN payments p ON p.id = r.payment_row WHERE r.status = 'requested' ORDER BY r.id").fetchall():
        claimed = g.write(conn, lambda: conn.execute("UPDATE refunds SET status = 'sending', attempts = attempts + 1, updated_at = ? WHERE id = ? AND status = 'requested'", (g.now_iso(), r["id"])).rowcount)
        if not claimed:
            continue
        try:
            if not prov or prov.name != r["provider"]:
                raise providers.ProviderError("payment provider not configured")
            res = prov.refund(r["payment_id"], r["amount_paise"], f"rf{r['id']}")
            new, rid, err = res["status"], res["id"], None
        except providers.ProviderError as e:
            new, rid, err = ("failed" if r["attempts"] + 1 >= REFUND_ATTEMPTS else "requested"), None, str(e)[:200]
        except Exception as e:  # never lose track of a refund because of an unexpected error
            traceback.print_exc()
            new, rid, err = "requested", None, type(e).__name__

        def save():
            conn.execute("UPDATE refunds SET status = ?, provider_refund_id = COALESCE(?, provider_refund_id), error = ?, updated_at = ? WHERE id = ?", (new, rid, err, g.now_iso(), r["id"]))
            if r["group_id"]:
                apply_status(conn, r["group_id"])
        g.write(conn, save)
        sent += new in ("pending", "processed")
    refund_alerts(conn)
    return sent


def refund_alerts(conn):
    """One staff alert while any refund needs a person (policy not set, taken at the counter, or the provider refused it); it clears when they are all dealt with."""
    n = conn.execute("SELECT COUNT(*) FROM refunds WHERE status IN ('manual', 'failed')").fetchone()[0]
    if n:
        g.alert_raise(conn, "refund", "waiting", f"{n} refund(s) need a person: open Payments in the staff app, pay or retry each one and mark it done.")
    else:
        g.alert_resolve(conn, "refund")


def refund_update(conn, provider_refund_id, new):
    """A provider's refund.processed / refund.failed event: updates the tracked refund."""
    def work():
        r = conn.execute("SELECT id, group_id, status FROM refunds WHERE provider_refund_id = ?", (provider_refund_id,)).fetchone()
        if r and r["status"] not in ("processed",):
            conn.execute("UPDATE refunds SET status = ?, updated_at = ? WHERE id = ?", (new, g.now_iso(), r["id"]))
            if r["group_id"]:
                apply_status(conn, r["group_id"])
        return r
    g.write(conn, work)
    refund_alerts(conn)


def refund_manual(conn, user, refund_id, done, note):
    """Staff (manager) action on a `manual` or `failed` refund: mark it paid by hand (`done`), or send it to the provider again."""
    def work():
        r = conn.execute("SELECT * FROM refunds WHERE id = ? AND status IN ('manual', 'failed')", (refund_id,)).fetchone()
        if not r:
            raise g.UserError("That refund is not waiting for action.", 404)
        conn.execute("UPDATE refunds SET status = ?, attempts = 0, error = ?, updated_at = ? WHERE id = ?", ("processed" if done else "requested", (note or "")[:200] or None, g.now_iso(), r["id"]))
        if r["group_id"]:
            apply_status(conn, r["group_id"])
        return r
    r = g.write(conn, work)
    if not done:
        process_refunds(conn)
    refund_alerts(conn)
    return r


# ---------- counter payments (so cash taken at the desk reconciles too) ----------
def record_counter(conn, gid, amount, mode, user_id):
    """Inside a transaction: a payment taken at the counter, as a paid `payments` row. Returns its id."""
    paise = to_paise(amount)
    if paise <= 0:
        raise g.UserError("Please enter an amount greater than zero.")
    rows = group_rows(conn, gid)
    if not rows:
        raise g.UserError("Booking not found.", 404)
    ref = "counter-" + secrets.token_hex(6)
    cur = conn.execute("INSERT INTO payments (order_id, payment_id, provider, kind, amount_paise, total_paise, status, method, group_id, created_at, paid_at, expires_at, property_id) VALUES (?,?, 'counter', 'counter', ?,?, 'paid', ?,?,?,?,?,?)",
                       (ref, ref, paise, to_paise(sum(r["amount"] or 0 for r in rows if r["status"] != "Cancelled")), mode, gid, g.now_iso(), g.now_iso(), g.now_iso(), rows[0]["property_id"]))
    apply_status(conn, gid)
    return cur.lastrowid


# ---------- the periodic job ----------
def tick(conn):
    """Every 5 minutes: payments past their window become expired (after asking a real provider whether the money did arrive), old sealed booking details are discarded,
    and refunds that could not be sent are retried. Returns a small report."""
    t = g.now_iso()
    expired = 0
    for row in conn.execute("SELECT * FROM payments WHERE status IN ('created', 'abandoned') AND expires_at <= ?", (t,)).fetchall():
        try:
            prov = providers.payments()
            late = [x for x in (prov.order_payments(row["order_id"]) if prov and prov.name == row["provider"] else []) if x["status"] == "captured"]
        except providers.ProviderError:
            late = []
        if late:
            captured(conn, row["order_id"], late[0]["id"], late[0]["amount"], late[0].get("method"))
            continue
        expired += g.write(conn, lambda: conn.execute("UPDATE payments SET status = 'expired' WHERE id = ? AND status IN ('created', 'abandoned')", (row["id"],)).rowcount)
        g.write(conn, lambda: conn.execute("DELETE FROM holds WHERE token = ?", (row["hold_token"],)))
    cut = (g.now() - timedelta(hours=PAYLOAD_KEEP_HOURS)).isoformat(timespec="seconds")
    g.write(conn, lambda: conn.execute("UPDATE payments SET payload = NULL WHERE payload IS NOT NULL AND status != 'created' AND created_at < ?", (cut,)))
    return {"expired": expired, "refunds_sent": process_refunds(conn)}


# ---------- reconciliation (staff) ----------
def reconcile(conn, property_ids, start, end):
    """One row per booking group with a payment or an amount due, for bookings checking in between start and end (ISO dates), against the money recorded.
    `flags`: unpaid (live booking with a balance), overpaid (more received than the booking total), refund_open (a refund is not finished), refund_manual (needs a person),
    mismatch (a cancelled booking still holds money no refund covers). Totals count each payment once."""
    marks = ",".join("?" * len(property_ids)) or "NULL"
    out, tot = [], {"online": 0, "counter": 0, "refunded": 0, "refund_open": 0, "balance": 0}
    groups = conn.execute(f"SELECT COALESCE(group_id, id) AS gid, MIN(property_id) AS pid, MIN(guest_name) AS guest, MIN(phone) AS phone, MIN(check_in) AS check_in, MAX(check_out) AS check_out, "
                          f"MAX(CASE WHEN status != 'Cancelled' THEN 1 ELSE 0 END) AS live, SUM(CASE WHEN status != 'Cancelled' THEN COALESCE(amount, 0) ELSE 0 END) AS total, "
                          f"MIN(CASE WHEN status != 'Cancelled' THEN status END) AS status FROM bookings WHERE property_id IN ({marks}) AND check_in <= ? AND check_out >= ? "
                          "GROUP BY COALESCE(group_id, id) ORDER BY MIN(check_in), 1", (*property_ids, end, start)).fetchall()
    for gr in groups:
        m = money_state(conn, gr["gid"])
        by = {r[0]: r[1] for r in conn.execute("SELECT provider = 'counter', SUM(amount_paise) FROM payments WHERE group_id = ? AND status = 'paid' GROUP BY 1", (gr["gid"],))}
        total = to_paise(gr["total"])
        flags = []
        if gr["live"] and m["net"] < total and gr["status"] in ("Confirmed", "Checked-In", "Checked-Out"):
            flags.append("unpaid")
        if gr["live"] and m["net"] > total:
            flags.append("overpaid")
        if m["refund_open"]:
            flags.append("refund_open")
        if m["refund_manual"] or m["refund_failed"]:
            flags.append("refund_manual")
        if not gr["live"] and m["net"] > 0:
            flags.append("mismatch")
        if not m["paid"] and not gr["live"]:
            continue
        tot["online"] += by.get(0, 0)
        tot["counter"] += by.get(1, 0)
        tot["refunded"] += m["refunded"]
        tot["refund_open"] += m["refund_open"] + m["refund_manual"]
        tot["balance"] += max(0, total - m["net"]) if gr["live"] else 0
        out.append({"booking_id": gr["gid"], "property_id": gr["pid"], "guest": gr["guest"], "phone": gr["phone"], "check_in": gr["check_in"], "check_out": gr["check_out"],
                    "status": gr["status"] or "Cancelled", "total": rupees(total) if gr["live"] else 0.0, "paid": rupees(m["paid"]), "refunded": rupees(m["refunded"]),
                    "refund_in_progress": rupees(m["refund_open"] + m["refund_manual"]), "net": rupees(m["net"]), "balance": rupees(max(0, total - m["net"])) if gr["live"] else 0.0, "flags": flags})
    return {"rows": out, "totals": {k: rupees(v) for k, v in tot.items()}}
