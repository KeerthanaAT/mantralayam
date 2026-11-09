"""Sevas and packages, the festival and panchanga calendar, weather and river notices, post-stay feedback, and the festival newsletter / WhatsApp broadcast.

Sevas are priced into the booking quote (not taxed, not discounted by promo codes) and stored in `seva_bookings` with the booking; capacity per day is checked inside the
booking transaction. A package is a named set of sevas on fixed days of the stay with its own discount.
Nothing here invents facts: festivals, panchanga entries and notices are entered by staff. The newsletter needs an explicit opt-in (a verified mobile number for SMS and
WhatsApp, a confirmed link for email) and every message carries an unsubscribe link.
"""
import hashlib
import hmac
import re
import secrets
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import providers

g = None  # the guest module, bound by mantralayam_rooms
MAX_SEVA_LINES = 10
BROADCAST_MAX_RECIPIENTS = 1000
BROADCASTS_PER_DAY = 3
CHANNELS = ("sms", "whatsapp", "email")
FESTIVAL_KINDS = ("festival", "ekadashi", "pournami", "amavasya", "other")
NOTICE_KINDS = ("weather", "river", "general")
NOTICE_LEVELS = ("info", "watch", "warning")


def public_url():
    return providers.env("PUBLIC_URL", "http://127.0.0.1:8001").rstrip("/")


def signed(conn, label, gid):
    return hmac.new(g.secret(conn, label).encode(), f"{label}|{gid}".encode(), hashlib.sha256).hexdigest()[:12]


def parse_signed(conn, label, prefix, code):
    m = re.fullmatch(prefix + r"\.(\d{1,12})\.([0-9a-f]{12})", str(code or "").strip(), re.IGNORECASE)
    return int(m[1]) if m and hmac.compare_digest(signed(conn, label, int(m[1])), m[2].lower()) else None


# ---------- sevas and packages ----------
def catalog(conn):
    sevas = [dict(r) for r in conn.execute("SELECT id, name, description, price, max_per_day, per_guest FROM sevas WHERE active = 1 ORDER BY position, id")]
    packages = []
    for p in conn.execute("SELECT id, name, description, discount_percent, min_nights FROM packages WHERE active = 1 ORDER BY id"):
        items = [dict(i) for i in conn.execute("SELECT s.id AS seva_id, s.name, s.price, i.day_offset, i.per_guest FROM package_items i JOIN sevas s ON s.id = i.seva_id WHERE i.package_id = ? AND s.active = 1 ORDER BY i.day_offset, s.id", (p["id"],))]
        if items:
            packages.append(dict(p) | {"items": items})
    return {"sevas": sevas, "packages": packages}


def used(conn, seva_id, day, skip_group=None):
    return conn.execute("SELECT COALESCE(SUM(qty), 0) FROM seva_bookings WHERE seva_id = ? AND seva_date = ? AND status = 'Confirmed' AND group_id IS NOT ?", (seva_id, day, skip_group)).fetchone()[0]


def check_capacity(conn, lines, skip_group=None):
    want = {}
    for ln in lines:
        want[(ln["seva_id"], ln["date"])] = want.get((ln["seva_id"], ln["date"]), 0) + ln["qty"]
    for (sid, day), n in want.items():
        cap = conn.execute("SELECT max_per_day FROM sevas WHERE id = ?", (sid,)).fetchone()
        if cap and cap[0] and used(conn, sid, day, skip_group) + n > cap[0]:
            raise g.UserError("Sorry, that seva is full on that date. Please choose another date or seva.", 409)


def seva_lines(conn, sevas, package_id, ci, co, guests, skip_group=None):
    """The seva lines of a quote: the package's sevas on their days (with the package discount), then the extra sevas the guest ticked. Each date must fall in the stay
    (check-in to check-out day). Raises UserError for anything that is not offered, not in the stay, or full. Returns [{seva_id, name, date, qty, unit, amount, package_id}]."""
    lines, hundred = [], Decimal(100)
    if package_id:
        pk = conn.execute("SELECT * FROM packages WHERE id = ? AND active = 1", (package_id,)).fetchone()
        if not pk:
            raise g.UserError("This package is not available.")
        nights = (co - ci).days
        if pk["min_nights"] and nights < pk["min_nights"]:
            raise g.UserError(f"This package needs a stay of at least {pk['min_nights']} nights.")
        for it in conn.execute("SELECT i.*, s.name, s.price FROM package_items i JOIN sevas s ON s.id = i.seva_id WHERE i.package_id = ? AND s.active = 1", (pk["id"],)).fetchall():
            d = ci + timedelta(days=it["day_offset"])
            if d > co:
                raise g.UserError("This package does not fit these dates.")
            qty = guests if it["per_guest"] else 1
            unit = g.money(Decimal(str(it["price"])) * (hundred - Decimal(str(pk["discount_percent"] or 0))) / hundred)
            lines.append({"seva_id": it["seva_id"], "name": it["name"], "date": d.isoformat(), "qty": qty, "unit": float(unit), "amount": float(g.money(unit * qty)), "package_id": pk["id"], "package": pk["name"]})
    for x in sevas or []:
        if not isinstance(x, dict):
            raise g.UserError("Some of the values sent are not valid.")
        sv = conn.execute("SELECT * FROM sevas WHERE id = ? AND active = 1", (g.opt_int(x.get("seva_id")),)).fetchone()
        if not sv:
            raise g.UserError("This seva is not available.")
        try:
            d = date.fromisoformat(str(x.get("date")))
        except ValueError:
            raise g.UserError("Please choose a date for each seva.")
        if not ci <= d <= co:
            raise g.UserError("A seva date must fall within your stay.")
        qty = g.to_int(1 if x.get("qty") is None else x.get("qty"), "Please choose how many people for each seva (1 to 50).", 1, 50)
        unit = g.money(sv["price"])
        lines.append({"seva_id": sv["id"], "name": sv["name"], "date": d.isoformat(), "qty": qty, "unit": float(unit), "amount": float(g.money(unit * qty)), "package_id": None, "package": None})
    if len(lines) > MAX_SEVA_LINES:
        raise g.UserError("Please choose at most 10 sevas for one booking.")
    check_capacity(conn, lines, skip_group)
    return lines


def save_sevas(conn, gid, q):
    for ln in q.get("sevas") or []:
        conn.execute("INSERT INTO seva_bookings (group_id, seva_id, package_id, seva_date, qty, unit_price, amount, status, created_at) VALUES (?,?,?,?,?,?,?, 'Confirmed', ?)",
                     (gid, ln["seva_id"], ln["package_id"], ln["date"], ln["qty"], ln["unit"], ln["amount"], g.now_iso()))


def sync_sevas(conn, gid):
    """Sevas follow the stay: when every room of the booking is cancelled, so are its sevas."""
    if not conn.execute("SELECT 1 FROM bookings WHERE (group_id = ? OR (group_id IS NULL AND id = ?)) AND status != 'Cancelled'", (gid, gid)).fetchone():
        conn.execute("UPDATE seva_bookings SET status = 'Cancelled' WHERE group_id = ?", (gid,))


def sevas_for(conn, gid, live_only=True):
    return [dict(r) for r in conn.execute("SELECT b.seva_date AS date, b.qty, b.amount, b.status, s.name FROM seva_bookings b JOIN sevas s ON s.id = b.seva_id WHERE b.group_id = ?" + (" AND b.status = 'Confirmed'" if live_only else "") + " ORDER BY b.seva_date, b.id", (gid,))]


# ---------- festivals and panchanga ----------
def festivals_between(conn, a, z):
    fest = [dict(r) | {"peak": bool(r["peak_period_id"] and r["peak_active"])} for r in conn.execute(
        "SELECT f.id, f.name, f.kind, f.start_date, f.end_date, f.note, f.peak_period_id, p.active AS peak_active, p.surcharge_percent FROM festivals f LEFT JOIN peak_periods p ON p.id = f.peak_period_id "
        "WHERE f.active = 1 AND f.end_date >= ? AND f.start_date <= ? ORDER BY f.start_date, f.id", (a, z))]
    for f in fest:
        f.pop("peak_period_id", None), f.pop("peak_active", None)
    pan = [dict(r) for r in conn.execute("SELECT day, tithi, nakshatra, note FROM panchanga WHERE day >= ? AND day <= ? ORDER BY day", (a, z))]
    return fest, pan


def festivals_public(conn, q, ip):
    """GET /api/public/festivals?from=&to=: festivals (with whether the dates carry peak pricing) and the panchanga entries staff have published. Default: today to 120 days."""
    g.throttle(("search", ip), 60, 60, "Too many searches.")
    try:
        a = date.fromisoformat(q.get("from") or g.today().isoformat())
        z = date.fromisoformat(q.get("to") or (a + timedelta(days=120)).isoformat())
    except ValueError:
        raise g.UserError("Dates must look like 2026-12-31.")
    if z < a or (z - a).days > 400:
        raise g.UserError("Please choose a range of at most 400 days.")
    fest, pan = festivals_between(conn, a.isoformat(), z.isoformat())
    return {"festivals": fest, "panchanga": pan}


# ---------- notices (weather, river level) ----------
def active_notices(conn, ci=None, co=None):
    """Notices in force today (or overlapping the stay [ci, co] when given), worst first."""
    t = g.today().isoformat()
    a, z = (ci, co) if ci else (t, t)
    rows = conn.execute("SELECT id, kind, level, text, starts_on, ends_on FROM notices WHERE active = 1 AND starts_on <= ? AND (ends_on IS NULL OR ends_on >= ?) ORDER BY CASE level WHEN 'warning' THEN 0 WHEN 'watch' THEN 1 ELSE 2 END, id", (z, a)).fetchall()
    return [dict(r) for r in rows]


# ---------- feedback ----------
def feedback_link(conn, gid):
    return f"{public_url()}/#/feedback?c=FB1.{gid}.{signed(conn, 'feedback', gid)}"


def feedback_booking(conn, code):
    gid = parse_signed(conn, "feedback", "FB1", code)
    rows = conn.execute("SELECT b.*, p.name AS pname FROM bookings b JOIN properties p ON p.id = b.property_id WHERE b.group_id = ? OR (b.group_id IS NULL AND b.id = ?)", (gid, gid)).fetchall() if gid else []
    if not rows:
        raise g.UserError("This feedback link is not valid.", 404)
    return gid, rows[0]


def feedback_info(conn, q, ip):
    g.throttle(("feedback", ip), 30, 600, "Too many attempts.")
    gid, b = feedback_booking(conn, q.get("code"))
    return {"property": b["pname"], "check_in": b["check_in"], "check_out": b["check_out"], "done": bool(conn.execute("SELECT 1 FROM feedback WHERE group_id = ?", (gid,)).fetchone())}


def feedback_submit(conn, b, ip):
    """POST /api/public/feedback {code, rating 1-5, cleanliness?, service?, comment?}: one per stay. A rating of 1 or 2 raises a staff alert."""
    g.throttle(("feedback", ip), 10, 600, "Too many attempts.")
    if str(b.get("website") or "").strip():
        raise g.UserError("Some of the values sent are not valid.")
    gid, bk = feedback_booking(conn, b.get("code"))
    rating = g.to_int(b.get("rating"), "Please choose a rating from 1 to 5.", 1, 5)
    sub = [g.to_int(b.get(k), "Please choose a rating from 1 to 5.", 1, 5) if b.get(k) not in (None, "") else None for k in ("cleanliness", "service")]
    comment = str(b.get("comment") or "").strip()
    if len(comment) > 1000:
        raise g.UserError("Your comment can be up to 1000 characters.")

    def work():
        if conn.execute("SELECT 1 FROM feedback WHERE group_id = ?", (gid,)).fetchone():
            raise g.UserError("You have already sent feedback for this stay. Thank you.", 409)
        conn.execute("INSERT INTO feedback (group_id, property_id, rating, cleanliness, service, comment, status, created_at) VALUES (?,?,?,?,?,?, 'new', ?)", (gid, bk["property_id"], rating, sub[0], sub[1], comment or None, g.now_iso()))
        g.audit(conn, "feedback.submit", bk["property_id"], None, f"booking #{gid}: {rating}/5", "Post-stay feedback")
    g.write(conn, work)
    if rating <= 2:
        g.alert_raise(conn, "feedback", f"low:{gid}", f"A guest rated booking #{gid} {rating} out of 5. Open Feedback in the staff app.")
    return {"ok": True}


def feedback_requests(conn):
    """Asks each online guest for feedback once, `feedback_days` after check-out (up to 30 days late). Off when the setting is empty. Returns how many were asked."""
    days = g.settings(conn)["feedback_days"]
    if days is None:
        return 0
    last, first = (g.today() - timedelta(days=days)).isoformat(), (g.today() - timedelta(days=days + 30)).isoformat()
    n = 0
    for (gid,) in conn.execute("SELECT COALESCE(group_id, id) FROM bookings WHERE source = 'online' AND status = 'Checked-Out' AND check_out <= ? AND check_out >= ? GROUP BY COALESCE(group_id, id)", (last, first)).fetchall():
        if conn.execute("SELECT 1 FROM notification_log WHERE group_id = ? AND event = 'feedback' LIMIT 1", (gid,)).fetchone() or conn.execute("SELECT 1 FROM feedback WHERE group_id = ?", (gid,)).fetchone():
            continue
        g.notifications.send_event(conn, "feedback", gid)
        n += 1
    return n


# ---------- newsletter and WhatsApp broadcasts ----------
def subscribe(conn, b, ip):
    """POST /api/public/subscribe: opt in to festival-date messages. Needs the consent box, a verified mobile number for SMS or WhatsApp (the code sent by SMS proves it is the
    person's own number), and for email a click on the link we send. Choices can be changed by signing up again; every message has an unsubscribe link."""
    g.throttle(("subscribe-ip", ip), 5, 3600, "Too many sign-ups from this device.")
    if str(b.get("website") or "").strip():
        g.throttle(("subscribe-ip", ip), 5, 3600, "Too many sign-ups from this device.")
        raise g.UserError("Some of the values sent are not valid.")
    if b.get("consent") is not True:
        raise g.UserError("Please tick the box to agree to receive these messages.")
    name = str(b.get("name") or "").strip()[:100]
    email = str(b.get("email") or "").strip()
    if email and (not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 120):
        raise g.UserError("Please enter a valid email address, or leave it empty.")
    phone = g.norm_phone(b.get("phone")) if str(b.get("phone") or "").strip() else None
    sms, wa, em = (b.get(k) is True for k in ("sms", "whatsapp", "email_ok"))
    if not (sms or wa or em):
        raise g.UserError("Please choose at least one way to hear from us.")
    if not (phone and (sms or wa)):  # with a verified mobile number the SMS code is the human check; otherwise the optional CAPTCHA applies
        g.captcha_check(b, ip)
    if (sms or wa) and not phone:
        raise g.UserError("Please give a mobile number for SMS or WhatsApp messages.")
    if em and not email:
        raise g.UserError("Please give an email address for email messages.")
    lang = b.get("lang") if b.get("lang") in g.LANGS else "en"
    proof = str(b.get("otp_proof") or "")[:64]
    token, consent = secrets.token_urlsafe(24), g.tr("en", "sub.consent")

    def work():
        ph_ok = bool(phone and (sms or wa)) and g.use_proof(conn, proof, phone, True, "subscribe")
        k = g.p10(phone) if phone else None
        old = conn.execute("SELECT * FROM subscribers WHERE (? IS NOT NULL AND phone10 = ?) OR (? != '' AND lower(email) = lower(?)) ORDER BY id LIMIT 1", (k, k, email, email)).fetchone()
        t = g.now_iso()
        em_ok = int(bool(old and old["em_ok"] and email and old["email"] and old["email"].lower() == email.lower()))
        if old:
            conn.execute("UPDATE subscribers SET name = ?, phone = ?, phone10 = ?, email = ?, lang = ?, sms = ?, wa = ?, em = ?, ph_ok = ?, em_ok = ?, consent_at = ?, consent_text = ?, unsubscribed_at = NULL WHERE id = ?",
                         (name or old["name"], phone, k, email or None, lang, int(sms), int(wa), int(em), int(ph_ok), em_ok, t, consent, old["id"]))
            return old["id"], old["token"], em_ok
        sid = conn.execute("INSERT INTO subscribers (name, phone, phone10, email, lang, sms, wa, em, ph_ok, em_ok, token, consent_at, consent_text, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'site')",
                           (name or None, phone, k, email or None, lang, int(sms), int(wa), int(em), int(ph_ok), em_ok, token, t, consent)).lastrowid
        return sid, token, em_ok
    sid, tok, em_ok = g.write(conn, work)
    pending = bool(em and not em_ok)
    if pending:
        try:
            providers.email().send(email, g.tr(lang, "sub.confirm_subject"), g.tr(lang, "sub.confirm_text", link=f"{public_url()}/#/subscribe-confirm?t={tok}"))
        except providers.ProviderError:
            raise g.UserError("We could not send the confirmation email. Please try again later.", 503)
    return {"ok": True, "pending_email": pending, "active": bool(sms or wa) and bool(phone) or not pending}


def subscribe_confirm(conn, b, ip):
    g.throttle(("subscribe-ip", ip), 20, 3600, "Too many attempts.")
    n = g.write(conn, lambda: conn.execute("UPDATE subscribers SET em_ok = 1 WHERE token = ? AND em = 1 AND unsubscribed_at IS NULL", (str(b.get("token") or "")[:64],)).rowcount)
    if not n:
        raise g.UserError("This confirmation link is not valid any more.", 404)
    return {"ok": True}


def unsubscribe(conn, b, ip):
    """POST /api/public/unsubscribe {token}: stops every message to this person. Safe to repeat."""
    g.throttle(("subscribe-ip", ip), 20, 3600, "Too many attempts.")
    n = g.write(conn, lambda: conn.execute("UPDATE subscribers SET unsubscribed_at = COALESCE(unsubscribed_at, ?), sms = 0, wa = 0, em = 0 WHERE token = ?", (g.now_iso(), str(b.get("token") or "")[:64])).rowcount)
    if not n:
        raise g.UserError("This unsubscribe link is not valid.", 404)
    return {"ok": True}


def audience(conn, channels):
    cond = {"sms": "sms = 1 AND ph_ok = 1", "whatsapp": "wa = 1 AND ph_ok = 1", "email": "em = 1 AND em_ok = 1 AND email IS NOT NULL"}
    out = {}
    for ch in channels:
        out[ch] = [dict(r) for r in conn.execute(f"SELECT id, name, phone, email, lang, token FROM subscribers WHERE unsubscribed_at IS NULL AND {cond[ch]} ORDER BY id")]
    return out


def counts(conn):
    return {ch: len(rows) for ch, rows in audience(conn, CHANNELS).items()}


def send_broadcast(conn, user, b):
    """Sends a message to every opted-in subscriber on the chosen channels, in their language (English where no text for it was written), each with an unsubscribe link.
    Sent in this request (at most BROADCAST_MAX_RECIPIENTS people, BROADCASTS_PER_DAY a day). `user`: {id, full_name}."""
    title = str(b.get("title") or "").strip()
    texts = {k: str(v).strip() for k, v in (b.get("texts") or {}).items() if k in g.LANGS and str(v).strip()} if isinstance(b.get("texts"), dict) else {}
    chans = [c for c in CHANNELS if isinstance(b.get("channels"), list) and c in b["channels"]]
    if not title or len(title) > 80:
        raise g.UserError("Please give the message a title (up to 80 characters).")
    if len(texts.get("en", "")) < 10 or any(len(v) > 600 for v in texts.values()):
        raise g.UserError("Please write the message in English (at least 10 characters). Each language can have up to 600 characters.")
    if not chans:
        raise g.UserError("Please choose at least one channel.")
    if b.get("confirm") is not True:
        raise g.UserError("Please confirm that you want to send this message.")
    if conn.execute("SELECT COUNT(*) FROM broadcasts WHERE created_at >= ?", (g.today().isoformat(),)).fetchone()[0] >= BROADCASTS_PER_DAY:
        raise g.UserError(f"At most {BROADCASTS_PER_DAY} broadcasts can be sent in a day.", 429)
    aud = audience(conn, chans)
    people = {r["id"] for rows in aud.values() for r in rows}
    if not people:
        raise g.UserError("Nobody has opted in on these channels yet.")
    if len(people) > BROADCAST_MAX_RECIPIENTS:
        raise g.UserError(f"This would reach more than {BROADCAST_MAX_RECIPIENTS} people. Please contact the administrator to send it in parts.")
    fid = g.opt_int(b.get("festival_id"))
    bid = g.write(conn, lambda: conn.execute("INSERT INTO broadcasts (title, texts, channels, festival_id, created_by, created_by_name, created_at, recipients) VALUES (?,?,?,?,?,?,?,?)",
                                             (title, __import__("json").dumps(texts), ",".join(chans), fid, user["id"], user["full_name"], g.now_iso(), len(people)))).lastrowid
    sent = failed = 0
    senders = {"sms": lambda to, text, sub: providers.sms().send(to, text), "whatsapp": None, "email": lambda to, text, sub: providers.email().send(to, sub, text)}
    for ch, rows in aud.items():
        try:
            wa = providers.whatsapp() if ch == "whatsapp" else None
        except providers.ProviderError:
            wa = None
        for r in rows:
            lang = r["lang"] if r["lang"] in texts else "en"
            text = texts[lang] + "\n\n" + g.tr(lang, "sub.stop", link=f"{public_url()}/#/unsubscribe?t={r['token']}")
            try:
                if ch == "whatsapp":
                    if not wa:
                        raise providers.ProviderError("WhatsApp is not configured")
                    wa.send(r["phone"], text)
                else:
                    senders[ch](r["phone"] if ch == "sms" else r["email"], text, title)
                status = "sent"
            except providers.ProviderError:
                status = "failed"
            except Exception:
                status = "failed"
            sent += status == "sent"
            failed += status == "failed"
            g.write(conn, lambda: conn.execute("INSERT INTO broadcast_log (broadcast_id, subscriber_id, channel, status, ts) VALUES (?,?,?,?,?)", (bid, r["id"], ch, status, g.now_iso())))
    g.write(conn, lambda: conn.execute("UPDATE broadcasts SET sent = ?, failed = ?, sent_at = ? WHERE id = ?", (sent, failed, g.now_iso(), bid)))
    return {"id": bid, "recipients": len(people), "sent": sent, "failed": failed}
