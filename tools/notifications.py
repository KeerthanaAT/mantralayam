"""Booking notifications: SMS and WhatsApp text plus an email with a printable confirmation (HTML with a QR code, and the same as a PDF attachment).

Events: confirmed, changed, cancelled, reminder. The texts come from web/locales (sms.* and email.*), in the language the guest used when booking (English where a text is
still TODO). Providers are the interfaces in providers.py (mock by default, WhatsApp off unless WHATSAPP_PROVIDER is set).
Every attempt is written to `notification_log` with its delivery status, the channel, a masked recipient and the error class: never the message text, an ID number or a
check-in code. A failed delivery is retried by the periodic job (retry_failed), at most 3 attempts in 24 hours. Sending never raises into a booking (see notify_safe).
The check-in code in the QR is "MR1.<booking id>.<signature>"; the signature is an HMAC with a server secret, so a code cannot be made up (the staff screen checks it).
"""
import hashlib
import hmac
import html
import re
from datetime import timedelta

import pdfdoc
import providers
import qr

g = None  # the guest module, bound by mantralayam_rooms at import
EVENTS = ("confirmed", "changed", "cancelled", "reminder", "feedback")
MAX_ATTEMPTS = 3
CODE_RE = re.compile(r"MR1\.(\d{1,12})\.([0-9a-f]{12})", re.IGNORECASE)


# ---------- the check-in code ----------
def checkin_code(conn, gid):
    sig = hmac.new(g.secret(conn, "checkin").encode(), f"checkin|{gid}".encode(), hashlib.sha256).hexdigest()[:12]
    return f"MR1.{gid}.{sig}"


def parse_code(conn, code):
    """The booking id inside a valid check-in code, or None (malformed or wrong signature)."""
    m = CODE_RE.fullmatch(str(code or "").strip())  # a scanner or a person may change the case
    if not m:
        return None
    return int(m[1]) if hmac.compare_digest(checkin_code(conn, int(m[1])), f"MR1.{int(m[1])}.{m[2].lower()}") else None


# ---------- message text ----------
def inr(v):
    return f"{v:,.2f}"


def payment_phrase(v, lang):
    p = v["payment"]
    if p["paid"] and p["balance"] <= 0:
        return g.tr(lang, "sms.pay_full", paid=inr(p["paid"]))
    if p["paid"]:
        return g.tr(lang, "sms.pay_part", paid=inr(p["paid"]), balance=inr(p["balance"]))
    return g.tr(lang, "sms.pay_counter", amount=inr(v["amount"]))


def compose(event, v, lang, link=""):
    """-> {"sms": text, "subject": ..., "intro": ...} for one event."""
    kw = dict(id=v["booking_id"], property=v["property"]["name"], check_in=v["check_in"], check_out=v["check_out"], amount=inr(v["amount"]))
    if event == "confirmed":
        sms = g.tr(lang, "sms.confirmed", **kw, payment=payment_phrase(v, lang))
    elif event == "changed":
        sms = g.tr(lang, "sms.changed", **kw)
    elif event == "cancelled":
        due = v["payment"]["refunded"] + v["payment"]["refund_in_progress"]
        sms = g.tr(lang, "sms.cancelled", **kw, refund=g.tr(lang, "sms.refund_due", amount=inr(due)) if due else "")
    elif event == "feedback":
        sms = g.tr(lang, "sms.feedback", **kw, link=link)
    else:
        sms = g.tr(lang, "sms.reminder", **kw, time=v["check_in_time"])
    warn = [n["text"] for n in v.get("notices") or [] if n["level"] == "warning"]
    if warn and event in ("confirmed", "reminder"):
        sms += " " + g.tr(lang, "sms.notice", text=warn[0][:100])
    return {"sms": sms.strip(), "subject": g.tr(lang, f"email.{event}_subject", id=v["booking_id"]), "intro": g.tr(lang, f"email.{event}_intro")}


def email_bodies(v, lang, intro, code, with_qr):
    """(plain text, HTML) of the email. The HTML is the printable confirmation: the booking table, then the QR code (an inline image, cid:qr) with the code in words."""
    T = lambda k, **kw: g.tr(lang, k, **kw)
    text = "\n".join([intro, "", *(f"{k}: {x}" for k, x in g.receipt_rows(v, lang)), "", *(f"{k}: {inr(x)}" for k, x in g.receipt_lines(v, lang)), "",
                      T("receipt.qr_note"), code if with_qr else "", "", g.receipt_note(v, lang),
                      *(["", T("notice.heading") + ":", *(f"- {T('notice.' + n['kind'])}: {n['text']}" for n in v["notices"])] if v.get("notices") else [])]).strip()
    e = html.escape
    qr_html = (f"<p style=\"text-align:center\"><img src=\"cid:qr\" width=\"180\" height=\"180\" alt=\"{e(T('receipt.qr_alt'))}\"><br><code>{e(code)}</code></p><p>{e(T('receipt.qr_note'))}</p>") if with_qr else ""
    body = (f"<!doctype html><html lang=\"{e(lang)}\"><body style=\"font:16px/1.5 Georgia,serif;max-width:560px;margin:auto;padding:16px;color:#222\"><h1 style=\"font-size:1.3rem\">Mantralayam Rooms</h1>"
            f"<p>{e(intro)}</p>{g.receipt_table(v, lang)}{g.notice_html(v, lang)}{qr_html}<p style=\"color:#555\">{e(g.receipt_note(v, lang))}</p></body></html>")
    return text, body


# ---------- PDF ----------
def wrap(s, n):
    words, lines, cur = str(s).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > n:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return lines + [cur] if cur else lines or [""]


def receipt_pdf(conn, v, code):
    """The booking receipt as a PDF (English: the standard PDF fonts cannot show Indian scripts). Has the QR code to show at check-in."""
    T = lambda k, **kw: g.tr("en", k, **kw)
    pdf = pdfdoc.Pdf(f"Booking {v['booking_id']}")
    pdf.text(50, 62, "Mantralayam Rooms", 20, bold=True)
    pdf.text(50, 84, T("receipt.title"), 13, grey=0.3)
    pdf.rule(50, 545, 98)
    y = 124
    for k, x in g.receipt_rows(v, "en"):
        x = str(x).replace("→", "to")  # the standard PDF fonts have no arrow
        pdf.text(50, y, k, 10, grey=0.4)
        for line in wrap(x, 38):
            pdf.text(170, y, line, 11)
            y += 15
        y += 4
    pdf.squares(qr.qr_matrix(code), 410, 112, 135)
    for i, line in enumerate(wrap(T("receipt.qr_note"), 30)):
        pdf.text(412, 258 + i * 10, line, 8, grey=0.4)
    pdf.text(412, 258 + 10 * len(wrap(T("receipt.qr_note"), 30)) + 4, code, 7, grey=0.5)
    y = max(y, 290) + 10
    pdf.rule(50, 545, y - 14)
    for k, x in g.receipt_lines(v, "en"):
        bold = k == T("receipt.total")
        pdf.text(50, y, k, 11, bold=bold)
        pdf.text(545, y, "Rs " + inr(x), 11, bold=bold, right=True)
        y += 18
    y += 14
    for line in wrap(g.receipt_note(v, "en"), 90):
        pdf.text(50, y, line, 9, grey=0.4)
        y += 12
    return pdf.render()


# ---------- sending and the log ----------
def mask_phone(p):
    d = re.sub(r"\D", "", str(p or ""))
    return "•••" + d[-4:] if d else ""


def mask_email(a):
    name, _, dom = str(a or "").partition("@")
    return (name[:1] + "•••@" + dom) if dom else ""


def log(conn, gid, event, channel, to, provider, status, detail, attempts=1, log_id=None):
    t = g.now_iso()

    def work():
        if log_id:
            conn.execute("UPDATE notification_log SET status = ?, detail = ?, attempts = ?, ts = ? WHERE id = ?", (status, detail, attempts, t, log_id))
            return log_id
        return conn.execute("INSERT INTO notification_log (ts, group_id, event, channel, to_hint, provider, status, attempts, detail) VALUES (?,?,?,?,?,?,?,?,?)",
                            (t, gid, event, channel, to, provider, status, attempts, detail)).lastrowid
    return g.write(conn, work)


def attempt(send):
    """Runs one delivery. Returns (status, detail): the error class only, never text that could carry message content."""
    try:
        send()
        return "sent", None
    except providers.ProviderError as e:
        return "failed", str(e)[:120]
    except Exception as e:
        return "failed", type(e).__name__


def send_event(conn, event, gid, channels=None, log_id=None, attempts=1):
    """Sends one event for a booking group over SMS, WhatsApp (when configured) and email (when the guest gave an address), and logs each delivery.
    `channels` limits it to some channels (a retry); `log_id` updates that log row instead of adding one. Returns {channel: status}."""
    rows = g.group_rows(conn, gid)
    if not rows:
        return {}
    v = g.booking_view(conn, rows)
    lang = rows[0]["lang"] if rows[0]["lang"] in g.LANGS else "en"
    link = g.extras.feedback_link(conn, gid) if event == "feedback" else ""
    m = compose(event, v, lang, link)
    phone, addr = rows[0]["phone"], rows[0]["email"]
    code, out = checkin_code(conn, gid), {}
    want = lambda c: channels is None or c in channels
    if want("sms"):
        st, d = attempt(lambda: providers.sms().send(phone, m["sms"]))
        log(conn, gid, event, "sms", mask_phone(phone), providers.env("SMS_PROVIDER", "mock"), st, d, attempts, log_id)
        out["sms"] = st
    if want("whatsapp"):
        try:
            wa = providers.whatsapp()
        except providers.ProviderError as e:
            wa, st, d = None, "failed", str(e)[:120]
            log(conn, gid, event, "whatsapp", mask_phone(phone), providers.env("WHATSAPP_PROVIDER"), st, d, attempts, log_id)
            out["whatsapp"] = st
        if wa:
            st, d = attempt(lambda: wa.send(phone, m["sms"]))
            log(conn, gid, event, "whatsapp", mask_phone(phone), providers.env("WHATSAPP_PROVIDER"), st, d, attempts, log_id)
            out["whatsapp"] = st
    if want("email"):
        if addr:
            live = v["status"] != "Cancelled" and event != "feedback"
            if event == "feedback":
                text, body = f"{m['intro']}\n\n{link}", f"<p>{html.escape(m['intro'])}</p><p><a href=\"{html.escape(link)}\">{html.escape(link)}</a></p>"
            else:
                text, body = email_bodies(v, lang, m["intro"], code, live)
            inline = [("qr", "image/png", qr.qr_png(qr.qr_matrix(code), 6, 4))] if live else []
            files = [(f"booking-{gid}.pdf", "application/pdf", receipt_pdf(conn, v, code))] if live else []
            st, d = attempt(lambda: providers.email().send(addr, m["subject"], text, body, inline, files))
            log(conn, gid, event, "email", mask_email(addr), providers.env("EMAIL_PROVIDER", "mock"), st, d, attempts, log_id)
            out["email"] = st
        elif channels is None:
            log(conn, gid, event, "email", "", providers.env("EMAIL_PROVIDER", "mock"), "skipped", "no email address given")
            out["email"] = "skipped"
    return out


def retry_failed(conn):
    """Sends again every failed delivery from the last 24 hours that has been tried fewer than MAX_ATTEMPTS times. Returns how many were attempted."""
    cut = (g.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    n = 0
    for r in conn.execute("SELECT id, group_id, event, channel, attempts FROM notification_log WHERE status = 'failed' AND attempts < ? AND ts > ? ORDER BY id", (MAX_ATTEMPTS, cut)).fetchall():
        send_event(conn, r["event"], r["group_id"], channels=(r["channel"],), log_id=r["id"], attempts=r["attempts"] + 1)
        n += 1
    return n


def reminders(conn):
    """Sends the arrival reminder once per booking: for online bookings that are still Confirmed and check in `reminder_days` from today, from `reminder_hour` onwards.
    Off when reminder_days is empty. Returns how many bookings were reminded."""
    s = g.settings(conn)
    if s["reminder_days"] is None or g.now().hour < s["reminder_hour"]:
        return 0
    target = (g.today() + timedelta(days=s["reminder_days"])).isoformat()
    n = 0
    for (gid,) in conn.execute("SELECT COALESCE(group_id, id) FROM bookings WHERE source = 'online' AND status = 'Confirmed' AND check_in = ? GROUP BY COALESCE(group_id, id)", (target,)).fetchall():
        if conn.execute("SELECT 1 FROM notification_log WHERE group_id = ? AND event = 'reminder' LIMIT 1", (gid,)).fetchone():
            continue
        send_event(conn, "reminder", gid)
        n += 1
    return n


def log_view(conn, property_ids, q):
    """Recent deliveries for the staff Messages page, newest first. Filter: status, event, booking id."""
    marks = ",".join("?" * len(property_ids)) or "NULL"
    sql = (f"SELECT n.* FROM notification_log n WHERE n.group_id IN (SELECT COALESCE(group_id, id) FROM bookings WHERE property_id IN ({marks}))")
    args = list(property_ids)
    for col in ("status", "event"):
        if q.get(col):
            sql += f" AND n.{col} = ?"
            args.append(q[col])
    if q.get("booking_id"):
        sql += " AND n.group_id = ?"
        args.append(g.to_int(q["booking_id"], "Booking ID must be a number.", 1, 10**12))
    return [dict(r) for r in conn.execute(sql + " ORDER BY n.id DESC LIMIT 300", args)]
