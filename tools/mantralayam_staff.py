"""Mantralayam staff app: login, today's operations, room board, bookings, room status, audit (stdlib only).

Usage:
  python tools/mantralayam_staff.py serve [PORT]            # default 8002, separate from the guest site (8001)
  python tools/mantralayam_staff.py set-password USERNAME   # prompts for a new password
  python tools/mantralayam_staff.py reset-totp USERNAME     # turns two-factor sign-in off for a user who lost their phone

Reuses helpers from mantralayam_rooms.py (same database and availability rules) and never edits it.
"""
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import idvault
import mantralayam_rooms as g
import totp
from mantralayam_rooms import UserError

STATIC = {"/": ("staff.html", "text/html"), "/staff": ("staff.html", "text/html"), "/staff.css": ("staff.css", "text/css"),
          "/tokens.css": ("tokens.css", "text/css"), "/shared.css": ("shared.css", "text/css")}
IMAGES = {"/images/logo-transparent.png": "image/png"}  # served from <project root>/images, allowlisted
ALL = {"super_admin", "property_manager", "front_desk", "housekeeping", "viewer"}
MGR = {"super_admin", "property_manager"}
DESK = MGR | {"front_desk"}
CLEANERS = DESK | {"housekeeping"}
KINDS = ("internal_block", "maintenance", "out_of_service")
CLEANING = ("cleaning_required", "in_cleaning")
ISSUES = ("Plumbing", "Electrical", "Furniture", "Cleaning", "Other")
LABEL = {"internal_block": "blocked", "maintenance": "under maintenance", "out_of_service": "out of service",
         "cleaning_required": "waiting for cleaning", "in_cleaning": "being cleaned"}
PRIORITY = ["out_of_service", "maintenance", "blocked", "cleaning", "occupied", "booked", "available"]
COOKIE, TTL = "staff_session", 8 * 3600
ROUNDS = 600_000  # PBKDF2-HMAC-SHA256; older hashes are upgraded to this at the next sign-in
# ponytail: the lost original code's PBKDF2 settings are unknown. Old rows are tried with these; new hashes are self-describing
# ("<rounds>$<salt>" in the salt column), so a wrong guess only means `set-password` is needed, never a lockout of the app.
LEGACY_ROUNDS = int(os.environ.get("STAFF_LEGACY_ROUNDS", 200_000))
SESSIONS = {}  # ponytail: in memory, restarting the server signs everyone out. token -> (user id, idle expiry, time of sign-in)
ABS_TTL = 12 * 3600  # a session ends this long after sign-in however busy it is
PW_MIN = 10
TOTP_ROLES = {"super_admin", "property_manager"}  # roles that sign in with a code from an authenticator app
TOTP_REQUIRED = os.environ.get("TOTP_REQUIRED", "1" if g.HTTPS_ONLY else "0").strip() == "1"  # on for a public (HTTPS) server: those roles must set it up before doing anything else
TOTP_OPEN = {"/api/meta", "/api/logout", "/api/me/totp/setup", "/api/me/totp/enable"}


# ---------- passwords and sessions ----------
def derive(pw, salt, rounds):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, rounds).hex()


def make_hash(pw):
    salt = secrets.token_hex(16)
    return derive(pw, salt.encode(), ROUNDS), f"{ROUNDS}${salt}"


def check_pw(pw, pw_hash, salt_field):
    rounds, _, salt = salt_field.rpartition("$")
    if rounds:
        tries = [(salt.encode(), int(rounds))]
    else:  # legacy row: salt is a bare hex string, stored form unknown
        tries = [(salt.encode(), LEGACY_ROUNDS)] + ([(bytes.fromhex(salt), LEGACY_ROUNDS)] if re.fullmatch(r"([0-9a-f]{2})+", salt) else [])
    return any(hmac.compare_digest(derive(pw, s, r), pw_hash) for s, r in tries)


DUMMY = make_hash("dummy")


def load_user(conn, token):
    s = SESSIONS.get(token or "")
    if not s or s[1] < time.time() or s[2] + ABS_TTL < time.time():
        SESSIONS.pop(token, None)
        raise UserError("Please sign in.", 401)
    u = conn.execute("SELECT id, username, full_name, role, active, totp_enabled FROM users WHERE id = ?", (s[0],)).fetchone()
    if not u or not u["active"]:
        SESSIONS.pop(token, None)
        raise UserError("Your account is no longer active.", 401)
    SESSIONS[token] = (s[0], time.time() + TTL, s[2])
    if u["role"] == "super_admin":
        props = [r[0] for r in conn.execute("SELECT id FROM properties WHERE active = 1 ORDER BY id")]
    else:
        props = [r[0] for r in conn.execute("SELECT up.property_id FROM user_properties up JOIN properties p ON p.id = up.property_id "
                                            "WHERE up.user_id = ? AND p.active = 1 ORDER BY 1", (u["id"],))]
    return dict(u) | {"props": props}


def audit(conn, user, action, property_id=None, room_id=None, old=None, new=None, reason=None, who=None):
    u = user or who or {}
    conn.execute("INSERT INTO audit_log (ts, user_id, username, role, action, property_id, room_id, old_value, new_value, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (datetime.now().isoformat(timespec="seconds"), u.get("id"), u.get("username"), u.get("role"), action, property_id, room_id, old, new, reason))


class Ctx:
    def __init__(self, q, body, ip, user, args):
        self.q, self.body, self.ip, self.user, self.args, self.token = q or {}, body or {}, ip, user, args, None  # token: str sets cookie, "" clears


def login(conn, c):
    name = str(c.body.get("username") or "").strip()[:60]
    key, now = ("staff-login", c.ip, name.lower()), time.time()
    # counts failures only; the counters live in the database and survive restarts. Three views of the same attack: one username from one address, any
    # username from one address (password spraying), and one username from any address (a spread-out guess at one account).
    limits = [(key, 5, 600), (("staff-login-ip", c.ip), 20, 600), (("staff-login-user", name.lower()), 15, 3600)]
    for k, n, window in limits:
        g.rate(k, n, window, "Too many failed attempts.", add=False)
    pw = str(c.body.get("password") or "")
    u = conn.execute("SELECT * FROM users WHERE username = ? AND active = 1", (name,)).fetchone()
    ok = check_pw(pw, u["pw_hash"] if u else DUMMY[0], u["salt"] if u else DUMMY[1])  # same work either way
    step = None
    if u and ok and u["role"] in TOTP_ROLES and u["totp_enabled"]:
        code = str(c.body.get("totp") or "").strip()
        if not code:
            return {"need_totp": True}  # the password was right; no session until the code is given
        step = totp.verify(totp_secret(conn, u), code, after=u["totp_step"])
        ok = step is not None
    if not (u and ok):
        for k, n, window in limits:
            g.rate(k, 10**6, window, "", add=True)
        g.write(conn, lambda: audit(conn, None, "auth.login_failed", reason=f"username: {name}", who={"username": name}))
        if u and conn.execute("SELECT COUNT(*) FROM rate_hits WHERE key = ? AND ts > ?", (g.rate_key(limits[2][0]), time.time() - 3600)).fetchone()[0] >= 10:
            g.alert_raise(conn, "login", u["username"], f"Repeated failed sign-ins for '{u['username']}' in the last hour. Someone may be guessing the password; check the audit log (auth.login_failed).")
        raise UserError("Incorrect username, password or code." if u and u["totp_enabled"] else "Incorrect username or password.", 401)
    g.rate_clear(key)
    if step is not None:
        g.write(conn, lambda: conn.execute("UPDATE users SET totp_step = ? WHERE id = ?", (step, u["id"])))  # a code works once
    if not u["salt"].partition("$")[0].isdigit() or int(u["salt"].partition("$")[0]) < ROUNDS:  # an older, weaker hash: replace it now that the password is known
        h, s = make_hash(pw)
        g.write(conn, lambda: conn.execute("UPDATE users SET pw_hash = ?, salt = ? WHERE id = ?", (h, s, u["id"])))
    c.token = secrets.token_urlsafe(32)
    SESSIONS[c.token] = (u["id"], now + TTL, now)
    c.user = load_user(conn, c.token)
    g.write(conn, lambda: audit(conn, c.user, "auth.login"))
    return meta(conn, c)


def logout(conn, c):
    SESSIONS.pop(c.args.get("_token"), None)
    g.write(conn, lambda: audit(conn, c.user, "auth.logout"))
    c.token = ""
    return {"ok": True}


def meta(conn, c):
    u = c.user
    return {"csrf": g.csrf_token(c.token or c.args.get("_token")), "user": {k: u[k] for k in ("id", "username", "full_name", "role")}, "today": g.today().isoformat(), "settings": {
        k: v for k, v in g.settings(conn).items() if k in ("checkin_time", "checkout_time")},
        "properties": [dict(r) for r in conn.execute(f"SELECT id, name FROM properties WHERE id IN ({marks(u['props'])}) ORDER BY id", u["props"])],
        "room_types": [dict(r) for r in conn.execute("SELECT id, name FROM room_types ORDER BY tariff")],
        "id_types": list(g.ID_TYPES), "issues": list(ISSUES), "kinds": list(KINDS),
        "can": {"book": u["role"] in DESK, "status": u["role"] in DESK, "clean": u["role"] in CLEANERS, "audit": u["role"] in MGR, "admin": u["role"] == "super_admin",
                "desk": u["role"] in perm("booking.checkin"), "id_reveal": u["role"] in perm("id.reveal"), "payments": u["role"] in perm("payment.view"), "refund": u["role"] in perm("payment.refund"),
                "record_payment": u["role"] in perm("payment.record"), "callbacks": u["role"] in perm("callback.work"), "messages": u["role"] in perm("messages.view"),
                "cancel_paid": u["role"] in perm("booking.cancel_paid"), "notices": u["role"] in perm("notice.edit"), "report_export": u["role"] in perm("report.export")},
        "permissions": allowed_for(u["role"]),
        "totp": {"applies": u["role"] in TOTP_ROLES, "enabled": bool(u["totp_enabled"]), "required": TOTP_REQUIRED and u["role"] in TOTP_ROLES and not u["totp_enabled"]},
        "pages": [{"key": k, "label": l} for k, l, roles in PAGES if u["role"] in roles]}


# ---------- helpers ----------
def marks(ids):
    return ",".join("?" * len(ids)) or "NULL"


def scope(user, pid):
    pid = None if pid in (None, "") else g.to_int(pid, "Please choose a valid property.", 1, 10**9)
    if pid is not None and pid not in user["props"]:
        raise UserError("You do not have access to this property.", 403)
    return [pid] if pid else user["props"]


def day(v, label):
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        raise UserError(f"{label} must be a valid date (YYYY-MM-DD).")


BOOKING_SQL = ("SELECT b.id, b.property_id, b.room_id, b.guest_name, b.phone, b.id_type, b.id_number, b.guests, b.check_in, b.check_out, b.status, "
               "b.cancel_reason, b.source, b.remarks, b.arrival_time, b.darshan_date, b.group_id, b.amount, b.children, b.extra_beds, b.promo_code, b.discount_amount, b.tax_amount, "
               "b.advance_due, b.booker_name, b.arrival_contact_name, b.arrival_contact_phone, r.number AS room, p.name AS property "
               "FROM bookings b JOIN rooms r ON r.id = b.room_id JOIN properties p ON p.id = b.property_id")


def row(r):
    d = dict(r)
    if "id_number" in d:  # staff see only the tail of the ID proof number
        d["id_tail"] = "…" + str(d.pop("id_number") or "")[-4:]
    return d


def get_booking(conn, user, bid):
    b = conn.execute(BOOKING_SQL + " WHERE b.id = ?", (g.to_int(bid, "Booking ID must be a number.", 1, 10**12),)).fetchone()
    if not b or b["property_id"] not in user["props"]:
        raise UserError("Booking not found.", 404)
    return b


def active_events(conn, room_ids, d):
    """Active status events covering day d for the given rooms."""
    return conn.execute(f"SELECT * FROM room_status_events WHERE active = 1 AND room_id IN ({marks(room_ids)}) AND start_date <= ? "
                        "AND (end_date IS NULL OR end_date > ?)", (*room_ids, d, d)).fetchall()


def board(conn, user, pid, d):
    """Per-room status for day d. Priority: out of service > maintenance > blocked > cleaning > occupied > booked > available."""
    rooms = conn.execute(f"SELECT r.id, r.number, r.floor, r.capacity, r.property_id, p.name AS property, t.name AS type FROM rooms r "
                         f"JOIN properties p ON p.id = r.property_id JOIN room_types t ON t.id = r.room_type_id "
                         f"WHERE r.active = 1 AND r.property_id IN ({marks(scope(user, pid))}) ORDER BY p.id, r.floor, r.number", scope(user, pid)).fetchall()
    ids = [r["id"] for r in rooms]
    ev = defaultdict(list)
    for e in active_events(conn, ids, d):
        ev[e["room_id"]].append(e)
    bk = defaultdict(list)
    for b in conn.execute(f"SELECT id, room_id, guest_name, status, check_in, check_out FROM bookings WHERE room_id IN ({marks(ids)}) "
                          "AND ((status = 'Confirmed' AND check_in <= ? AND check_out > ?) OR (status = 'Checked-In' AND check_in <= ?))", (*ids, d, d, d)):
        bk[b["room_id"]].append(b)
    out = []
    for r in rooms:
        found = {}
        for e in ev[r["id"]]:
            k = {"internal_block": "blocked", "in_cleaning": "cleaning", "cleaning_required": "cleaning"}.get(e["kind"], e["kind"])
            found.setdefault(k, (LABEL[e["kind"]].capitalize() + (f": {e['reason']}" if e["reason"] else ""), None, e["id"]))
        for b in bk[r["id"]]:
            found.setdefault("occupied" if b["status"] == "Checked-In" else "booked", (f"{b['guest_name']}, until {b['check_out']}", b["id"], None))
        status = next((s for s in PRIORITY if s in found), "available")
        note, bid, eid = found.get(status, ("", None, None))
        out.append(dict(r) | {"status": status, "note": note, "booking_id": bid, "event_id": eid})
    return out


# ---------- routes ----------
def dashboard(conn, c):
    ids, t = scope(c.user, c.q.get("property_id")), g.today().isoformat()
    base = BOOKING_SQL + f" WHERE b.property_id IN ({marks(ids)}) AND "

    def lst(cond, *args):
        return [row(r) for r in conn.execute(base + cond + " ORDER BY p.name, r.number", (*ids, *args))]
    rooms = board(conn, c.user, c.q.get("property_id"), t)
    counts = defaultdict(int)
    for r in rooms:
        counts[r["status"]] += 1
    return {"today": t, "arrivals": lst("b.status = 'Confirmed' AND b.check_in = ?", t), "overdue": lst("b.status = 'Confirmed' AND b.check_in < ?", t),
            "inhouse": lst("b.status = 'Checked-In' AND b.check_out > ?", t), "due_out": lst("b.status = 'Checked-In' AND b.check_out <= ?", t),
            "rooms": dict(counts), "total_rooms": len(rooms)} | ({"backup": g.last_backup()} if c.user["role"] == "super_admin" else {}) | ({"alerts": g.check_alerts(conn)} if c.user["role"] in MGR else {})


def rooms_view(conn, c):
    return board(conn, c.user, c.q.get("property_id"), day(c.q.get("date") or g.today().isoformat(), "Date").isoformat())


def bookings_list(conn, c):
    ids, sql, args = scope(c.user, c.q.get("property_id")), BOOKING_SQL + " WHERE b.property_id IN (%s)", []
    sql %= marks(ids)
    args += ids
    if c.q.get("status"):
        sql += " AND b.status = ?"
        args.append(c.q["status"])
    if c.q.get("date"):
        sql += " AND b.check_in <= ? AND b.check_out > ?"
        args += [day(c.q["date"], "Date").isoformat()] * 2
    if c.q.get("q", "").strip():
        t = c.q["q"].strip()[:60]
        sql += " AND (b.guest_name LIKE ? OR b.phone LIKE ? OR r.number LIKE ? OR CAST(b.id AS TEXT) = ?)"
        args += [f"%{t}%", f"%{t}%", f"%{t}%", t]
    return [row(r) for r in conn.execute(sql + " ORDER BY b.check_in DESC, b.id DESC LIMIT 300", args)]


def why_not_free(conn, room, ci, co):
    b = conn.execute("SELECT id, guest_name, check_in, check_out FROM bookings WHERE room_id = ? AND status IN ('Confirmed', 'Checked-In') "
                     "AND check_in < ? AND check_out > ?", (room["id"], co, ci)).fetchone()
    if b:
        return f"Room {room['number']} is already booked (booking #{b['id']}, {b['check_in']} to {b['check_out']}). Choose different dates or another room."
    e = conn.execute("SELECT kind FROM room_status_events WHERE room_id = ? AND active = 1 AND start_date < ? AND (end_date IS NULL OR end_date > ?)", (room["id"], co, ci)).fetchone()
    if e:
        return f"Room {room['number']} is {LABEL[e['kind']]} for those dates. Choose different dates or another room."
    h = conn.execute("SELECT expires_at FROM holds WHERE room_id = ? AND expires_at > ? AND check_in < ? AND check_out > ?", (room["id"], g.now_iso(), co, ci)).fetchone()
    if h:
        return f"Room {room['number']} is held for a guest who is booking online, until {h['expires_at'][11:16]}. Try again after that or choose another room."
    return f"Room {room['number']} is not available."


def availability(conn, c):
    """For the walk-in dialog: how many rooms (sleeping the party) are free for the stay, and nearby alternatives when none are. Own properties only."""
    ids = scope(c.user, c.q.get("property_id"))
    ci, co = day(c.q.get("check_in"), "Check-in"), day(c.q.get("check_out"), "Check-out")
    if ci < g.today():
        raise UserError("Check-in date cannot be in the past.")
    if co <= ci:
        raise UserError("Check-out must be after check-in.")
    guests = g.to_int(c.q.get("guests", 1), "Number of guests must be at least 1.", 1, 200)
    free = sum(1 for r in g.free_rooms(conn, ci, co) if r["property_id"] in ids and r["capacity"] >= guests)
    return {"free": free, "alternatives": [] if free else g.alternatives(conn, ci, co, guests, set(ids), max_rooms=1)}


def create_booking(conn, c):
    b, u = c.body, c.user
    pid = scope(u, b.get("property_id"))[0] if b.get("property_id") else None
    if pid is None:
        raise UserError("Please choose a valid property and room.")
    ci, co = day(b.get("check_in"), "Check-in"), day(b.get("check_out"), "Check-out")
    if ci < g.today():
        raise UserError("Check-in date cannot be in the past.")
    if co <= ci:
        raise UserError("Check-out must be after check-in.")
    name, id_no, remarks = (str(b.get(k) or "").strip() for k in ("guest_name", "id_number", "remarks"))
    if not name or len(name) > 100:
        raise UserError("Please enter the guest name.")
    phone = g.norm_phone(b.get("phone"))
    if b.get("id_type") not in g.ID_TYPES:
        raise UserError("Please choose a valid ID proof type.")
    if not id_no or len(id_no) > 30:
        raise UserError("Please enter the ID proof number.")
    guests = g.to_int(b.get("guests"), "Number of guests must be at least 1.", 1, 200)
    if len(remarks) > 500:
        raise UserError("Remarks must be 500 characters or fewer.")
    arrival, darshan = g.arrival_fields(b)

    def work():
        room = conn.execute("SELECT r.*, t.tariff FROM rooms r JOIN room_types t ON t.id = r.room_type_id WHERE r.id = ? AND r.property_id = ? AND r.active = 1",
                            (b.get("room_id"), pid)).fetchone()
        if not room:
            raise UserError("Please choose an active room in the selected property.")
        if guests > room["capacity"]:
            raise UserError(f"Room {room['number']} sleeps at most {room['capacity']} guests. Choose a larger room or split the party.")
        if room["id"] not in {r["id"] for r in g.free_rooms(conn, ci, co, pid)}:
            raise UserError(why_not_free(conn, room, ci.isoformat(), co.isoformat()), 409)
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute("INSERT INTO bookings (property_id, room_id, guest_name, phone, id_type, id_number, guests, check_in, check_out, remarks, status, created_by, "
                           "created_at, status_at, source, payment_method, payment_status, amount, arrival_time, darshan_date) VALUES (?,?,?,?,?,?,?,?,?,?,'Confirmed',?,?,?,'front_desk','pay_at_counter','due',?,?,?)",
                           (pid, room["id"], name, phone, b["id_type"], "****" + id_no[-4:], guests, ci.isoformat(), co.isoformat(), remarks or None, u["id"], now, now,  # only the last four are kept; the full number is recorded, encrypted, at check-in (ID capture)
                            room["tariff"] * (co - ci).days, arrival, darshan))
        audit(conn, u, "booking.create", pid, room["id"], None, f"#{cur.lastrowid}: {name}, {ci} to {co}, {guests} guests")
        return cur.lastrowid
    return row(get_booking(conn, u, g.write(conn, work)))


def set_status(conn, c, new, allowed_from, action):
    """Shared by check-in, check-out, no-show and cancel: one transaction, status guard, audit row."""
    after = {}

    def work():
        b = get_booking(conn, c.user, c.args["id"])
        if b["status"] not in allowed_from:
            raise UserError(f"Only {' or '.join(allowed_from)} bookings can be {action['verb']} (this one is {b['status']}).{action.get('hint', '')}", 409)
        today = g.today().isoformat()
        action["check"](conn, b, today)
        gid = b["group_id"] or b["id"]
        if new == "Cancelled" and g.payments.money_state(conn, gid)["paid"] > 0 and c.user["role"] not in perm("booking.cancel_paid"):
            raise UserError("This booking has been paid. A manager must cancel it, because that starts a refund.", 403)
        live = [r for r in g.payments.group_rows(conn, gid) if r["status"] in LIVE_STATUS]
        reason = c.body.get("reason") if new == "Cancelled" else None
        if new == "Cancelled":
            reason = str(reason or "").strip()
            if not reason or len(reason) > 300:
                raise UserError("Please give a reason for cancelling (up to 300 characters).")
        conn.execute("UPDATE bookings SET status = ?, status_at = ?, cancel_reason = COALESCE(?, cancel_reason) WHERE id = ?",
                     (new, datetime.now().isoformat(timespec="seconds"), reason, b["id"]))
        if new == "Checked-Out":
            conn.execute("INSERT INTO room_status_events (room_id, kind, reason, start_date, created_by, created_at) "
                         "SELECT ?, 'cleaning_required', ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM room_status_events WHERE room_id = ? AND active = 1 AND kind IN ('cleaning_required', 'in_cleaning'))",
                         (b["room_id"], f"Check-out of booking #{b['id']}", today, c.user["id"], datetime.now().isoformat(timespec="seconds"), b["room_id"]))
        refunds = g.payments.request_cancel_refund(conn, gid, [b["id"]], b["amount"] or 0, sum(r["amount"] or 0 for r in live)) if new == "Cancelled" else []
        if new in ("Cancelled", "No-Show"):
            g.extras.sync_sevas(conn, gid)
        audit(conn, c.user, action["audit"], b["property_id"], b["room_id"], b["status"], new, reason + (f" ({len(refunds)} refund(s) recorded)" if refunds else "") if reason else None)
        after.update(gid=gid, online=b["source"] == "online", left=len(live) - 1)
    g.write(conn, work)
    if new == "Cancelled":
        g.payments.process_refunds(conn)
        g.payments.refund_alerts(conn)
        if after["online"]:  # the guest hears about it: cancelled when no room is left, changed when other rooms remain
            g.notify_safe(conn, "cancelled" if not after["left"] else "changed", after["gid"])
    if new in ("Cancelled", "No-Show"):
        g.waitlist_tick_safe(conn)  # the freed room may be what a waiting guest asked for
    return row(get_booking(conn, c.user, c.args["id"]))


def check_checkin(conn, b, today):
    if g.settings(conn)["checkin_requires_id"] and not conn.execute("SELECT 1 FROM id_records WHERE booking_id = ? AND number_enc IS NOT NULL", (b["id"],)).fetchone():
        raise UserError("Record the guest's ID (type and number) before checking in. Use the Front desk screen.", 409)
    if not b["check_in"] <= today < b["check_out"]:
        raise UserError(f"Check-in is only possible between {b['check_in']} and {b['check_out']} (today is {today}).", 409)
    e = active_events(conn, [b["room_id"]], today)
    if e:
        raise UserError(f"Room {b['room']} is {LABEL[e[0]['kind']]}. Assign another room or clear the status first.", 409)
    o = conn.execute("SELECT id FROM bookings WHERE room_id = ? AND status = 'Checked-In' AND id != ?", (b["room_id"], b["id"])).fetchone()
    if o:
        raise UserError(f"Room {b['room']} is still occupied by booking #{o['id']}. Check that guest out first.", 409)


def check_noshow(conn, b, today):
    if b["check_in"] > today:
        raise UserError("Only a Confirmed booking whose check-in date has arrived can be marked No-Show.", 409)


NOOP = lambda conn, b, today: None
ACTIONS = {"checkin": ("Checked-In", ("Confirmed",), dict(verb="checked in", audit="booking.checkin", check=check_checkin), DESK),
           "checkout": ("Checked-Out", ("Checked-In",), dict(verb="checked out", audit="booking.checkout", check=NOOP), DESK),
           "noshow": ("No-Show", ("Confirmed",), dict(verb="marked No-Show", audit="booking.noshow", check=check_noshow), DESK),
           "cancel": ("Cancelled", ("Confirmed",), dict(verb="cancelled", audit="booking.cancel", check=NOOP, hint=" Use Check-out for guests who are in the room."), DESK)}


def events_list(conn, c):
    ids = scope(c.user, c.q.get("property_id"))
    return [dict(r) for r in conn.execute(
        f"SELECT e.*, r.number AS room, r.property_id, p.name AS property FROM room_status_events e JOIN rooms r ON r.id = e.room_id JOIN properties p ON p.id = r.property_id "
        f"WHERE e.active = 1 AND r.property_id IN ({marks(ids)}) ORDER BY p.name, r.number", ids)]


def create_event(conn, c):
    b, u = c.body, c.user
    kind = b.get("kind")
    if kind not in KINDS:
        raise UserError("Please choose a valid status.")
    start = day(b.get("start_date") or g.today().isoformat(), "Start date")
    end = day(b.get("end_date"), "Available-again date") if b.get("end_date") else None
    if kind != "out_of_service" and not end:
        raise UserError("Please choose the date the room becomes available again.")
    if end and end <= start:
        raise UserError("The available-again date must be after the start date.")
    cat = b.get("issue_category") or None
    if kind != "internal_block" and cat not in ISSUES:
        raise UserError("Please choose a valid issue category.")
    reason = str(b.get("reason") or "").strip()
    if not reason or len(reason) > 300:
        raise UserError("Please give a reason (up to 300 characters).")

    def work():
        room = conn.execute("SELECT * FROM rooms WHERE id = ? AND active = 1", (b.get("room_id"),)).fetchone()
        if not room or room["property_id"] not in u["props"]:
            raise UserError("Please choose an active room in a property you can access.")
        far = (end or date(9999, 1, 1)).isoformat()
        hit = conn.execute("SELECT id, check_in, check_out FROM bookings WHERE room_id = ? AND status IN ('Confirmed', 'Checked-In') AND check_in < ? AND check_out > ?",
                           (room["id"], far, start.isoformat())).fetchone()
        if hit:
            raise UserError(f"Room {room['number']} has a booking (#{hit['id']}, {hit['check_in']} to {hit['check_out']}) in that period. Cancel or move the booking first.", 409)
        cur = conn.execute("INSERT INTO room_status_events (room_id, kind, issue_category, reason, start_date, end_date, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (room["id"], kind, cat, reason, start.isoformat(), end.isoformat() if end else None, u["id"], datetime.now().isoformat(timespec="seconds")))
        audit(conn, u, "room.block", room["property_id"], room["id"], None, f"{kind} from {start}" + (f" to {end}" if end else ""), reason)
        return cur.lastrowid
    return {"id": g.write(conn, work)}


def event_action(conn, c, release):
    def work():
        e = conn.execute("SELECT e.*, r.number, r.property_id FROM room_status_events e JOIN rooms r ON r.id = e.room_id WHERE e.id = ? AND e.active = 1",
                         (g.to_int(c.args["id"], "Invalid id.", 1, 10**12),)).fetchone()
        if not e or e["property_id"] not in c.user["props"]:
            raise UserError("That status entry no longer exists or was already cleared.", 404)
        cleaning = e["kind"] in CLEANING
        if c.user["role"] not in (CLEANERS if cleaning else DESK):
            raise UserError("Your role is not allowed to do this.", 403)
        now = datetime.now().isoformat(timespec="seconds")
        if release:
            conn.execute("UPDATE room_status_events SET active = 0, closed_by = ?, closed_at = ? WHERE id = ?", (c.user["id"], now, e["id"]))
            audit(conn, c.user, "room.cleaning_done" if cleaning else "room.release", e["property_id"], e["room_id"], e["kind"], "available")
        else:
            if e["kind"] != "cleaning_required":
                raise UserError("Cleaning has already started for this room." if e["kind"] == "in_cleaning" else "Only rooms waiting for cleaning can be sent to cleaning.", 409)
            conn.execute("UPDATE room_status_events SET kind = 'in_cleaning' WHERE id = ?", (e["id"],))
            audit(conn, c.user, "room.cleaning_start", e["property_id"], e["room_id"], e["kind"], "in_cleaning")
    g.write(conn, work)
    if release:
        g.waitlist_tick_safe(conn)
    return {"ok": True}


def audit_view(conn, c):
    ids, sql, args = scope(c.user, c.q.get("property_id")), "SELECT l.*, p.name AS property, r.number AS room FROM audit_log l LEFT JOIN properties p ON p.id = l.property_id LEFT JOIN rooms r ON r.id = l.room_id WHERE ", []
    sql += f"(l.property_id IN ({marks(ids)})" + (" OR l.property_id IS NULL)" if c.user["role"] == "super_admin" and not c.q.get("property_id") else ")")
    args += ids
    if c.q.get("q", "").strip():
        t = f"%{c.q['q'].strip()[:60]}%"
        sql += " AND (l.action LIKE ? OR l.username LIKE ? OR l.old_value LIKE ? OR l.new_value LIKE ? OR l.reason LIKE ?)"
        args += [t] * 5
    if c.q.get("sensitive") in ("1", "true"):  # only the actions that touch IDs, money, cancellations, blocks, users and settings
        sql += " AND (" + " OR ".join("l.action LIKE ?" for _ in SENSITIVE) + ")"
        args += [x + "%" for x in SENSITIVE]
    return [dict(r) for r in conn.execute(sql + " ORDER BY l.id DESC LIMIT 500", args)]


# ---------- menu pages, extra screens ----------
FRONT = DESK | {"viewer"}  # everyone who may see guest details
PAGES = [("today", "Dashboard", ALL), ("desk", "Front desk", DESK), ("bookings", "Bookings", FRONT), ("callbacks", "Call-backs", FRONT), ("calendar", "Calendar", FRONT), ("rooms", "Rooms", ALL),
         ("housekeeping", "Housekeeping", CLEANERS), ("status", "Room status", ALL), ("payments", "Payments", MGR), ("messages", "Messages", MGR), ("notices", "Notices", DESK), ("feedback", "Feedback", MGR), ("broadcasts", "Broadcasts", MGR), ("guests", "Guests", FRONT), ("rates", "Rooms & rates", MGR), ("reports", "Reports", MGR),
         ("notifications", "Notifications", ALL), ("audit", "Audit log", MGR), ("admin", "Admin", frozenset({"super_admin"})), ("settings", "Settings", ALL)]
STATUS_LIVE = ("Confirmed", "Checked-In", "Checked-Out")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def text(b, key, label, lo=0, hi=200):
    v = str(b.get(key) or "").strip()
    if len(v) < lo or len(v) > hi:
        raise UserError(f"{label} is required (up to {hi} characters)." if lo else f"{label} must be {hi} characters or fewer.")
    return v


def conflict(conn, room, ci, co, skip_id):
    """Why room is not free for [ci, co), ignoring booking skip_id; None if free."""
    b = conn.execute("SELECT id, check_in, check_out FROM bookings WHERE room_id = ? AND id != ? AND status IN ('Confirmed', 'Checked-In') "
                     "AND check_in < ? AND check_out > ?", (room["id"], skip_id, co, ci)).fetchone()
    if b:
        return f"Room {room['number']} is already booked (booking #{b['id']}, {b['check_in']} to {b['check_out']})."
    e = conn.execute("SELECT kind FROM room_status_events WHERE room_id = ? AND active = 1 AND start_date < ? AND (end_date IS NULL OR end_date > ?) "
                     "AND (kind NOT IN ('cleaning_required', 'in_cleaning') OR ? <= ?)", (room["id"], co, ci, ci, g.today().isoformat())).fetchone()
    return f"Room {room['number']} is {LABEL[e['kind']]} for those dates." if e else None


def edit_booking(conn, c):
    b = c.body
    name, remarks = text(b, "guest_name", "Guest name", 1, 100), text(b, "remarks", "Remarks", 0, 500)
    phone = g.norm_phone(b.get("phone"))
    guests = g.to_int(b.get("guests"), "Number of guests must be at least 1.", 1, 200)
    arrival, darshan = g.arrival_fields(b) if "arrival_time" in b or "darshan_date" in b else (None, None)
    touch = "arrival_time" in b or "darshan_date" in b

    def work():
        cur = get_booking(conn, c.user, c.args["id"])
        if cur["status"] not in ("Confirmed", "Checked-In"):
            raise UserError(f"A {cur['status']} booking can no longer be edited.", 409)
        cap = conn.execute("SELECT capacity FROM rooms WHERE id = ?", (cur["room_id"],)).fetchone()[0]
        if guests > cap:
            raise UserError(f"Room {cur['room']} sleeps at most {cap} guests.")
        conn.execute("UPDATE bookings SET guest_name = ?, phone = ?, guests = ?, remarks = ? WHERE id = ?", (name, phone, guests, remarks or None, cur["id"]))
        if touch:
            conn.execute("UPDATE bookings SET arrival_time = ?, darshan_date = ? WHERE id = ?", (arrival, darshan, cur["id"]))
        audit(conn, c.user, "booking.edit", cur["property_id"], cur["room_id"], f"{cur['guest_name']}, {cur['phone']}, {cur['guests']} guests", f"{name}, {phone}, {guests} guests")
        return cur
    cur = g.write(conn, work)
    if cur["source"] == "online":
        g.notify_safe(conn, "changed", cur["group_id"] or cur["id"])
    return row(get_booking(conn, c.user, c.args["id"]))


def reschedule_booking(conn, c):
    b = c.body
    ci, co = day(b.get("check_in"), "Check-in"), day(b.get("check_out"), "Check-out")
    if ci < g.today():
        raise UserError("Check-in date cannot be in the past.")
    if co <= ci:
        raise UserError("Check-out must be after check-in.")

    def work():
        cur = get_booking(conn, c.user, c.args["id"])
        if cur["status"] != "Confirmed":
            raise UserError(f"Only Confirmed bookings can be rescheduled (this one is {cur['status']}).", 409)
        if g.extras.sevas_for(conn, cur["group_id"] or cur["id"]):
            raise UserError("This booking includes sevas, whose dates follow the stay. Cancel the booking and make a new one instead.", 409)
        want = g.opt_int(b.get("room_id")) or cur["room_id"]
        if cur["group_id"] and want != cur["room_id"]:
            raise UserError("A room in an online group booking cannot be moved to another room.")
        room = conn.execute("SELECT r.*, t.tariff FROM rooms r JOIN room_types t ON t.id = r.room_type_id WHERE r.id = ? AND r.property_id = ? AND r.active = 1",
                            (want, cur["property_id"])).fetchone()
        if not room:
            raise UserError("Please choose an active room in the same property.")
        if cur["guests"] > room["capacity"]:
            raise UserError(f"Room {room['number']} sleeps at most {room['capacity']} guests.")
        why = conflict(conn, room, ci.isoformat(), co.isoformat(), cur["id"])
        if why:
            raise UserError(why + " Choose different dates or another room.", 409)
        conn.execute("UPDATE bookings SET room_id = ?, check_in = ?, check_out = ?, amount = ?, tax_amount = NULL, discount_amount = NULL, advance_due = NULL, price_json = NULL WHERE id = ?",
                     (room["id"], ci.isoformat(), co.isoformat(), room["tariff"] * (co - ci).days, cur["id"]))  # the online price breakdown no longer applies
        audit(conn, c.user, "booking.reschedule", cur["property_id"], room["id"], f"room {cur['room']}, {cur['check_in']} to {cur['check_out']}",
              f"room {room['number']}, {ci} to {co}")
        return cur
    cur = g.write(conn, work)
    g.waitlist_tick_safe(conn)
    if cur["source"] == "online":
        g.notify_safe(conn, "changed", cur["group_id"] or cur["id"])
    return row(get_booking(conn, c.user, c.args["id"]))


def calendar_view(conn, c):
    ids = scope(c.user, c.q.get("property_id"))
    start = day(c.q.get("start") or g.today().isoformat(), "Start date")
    n = g.to_int(c.q.get("days") or 7, "Days must be between 1 and 31.", 1, 31)
    days = [(start + timedelta(days=i)).isoformat() for i in range(n)]
    end = (start + timedelta(days=n)).isoformat()
    rooms = conn.execute(f"SELECT r.id, r.number, r.floor, r.property_id, p.name AS property, t.name AS type FROM rooms r JOIN properties p ON p.id = r.property_id "
                         f"JOIN room_types t ON t.id = r.room_type_id WHERE r.active = 1 AND r.property_id IN ({marks(ids)}) ORDER BY p.id, r.floor, r.number", ids).fetchall()
    rid = [r["id"] for r in rooms]
    bks, evs = defaultdict(list), defaultdict(list)
    for x in conn.execute(f"SELECT id, room_id, guest_name, status, check_in, check_out FROM bookings WHERE room_id IN ({marks(rid)}) AND status IN ('Confirmed', 'Checked-In') "
                          "AND check_in < ? AND check_out > ?", (*rid, end, start.isoformat())):
        bks[x["room_id"]].append(x)
    for x in conn.execute(f"SELECT id, room_id, kind, reason, start_date, end_date FROM room_status_events WHERE active = 1 AND room_id IN ({marks(rid)}) AND start_date < ? "
                          "AND (end_date IS NULL OR end_date > ?)", (*rid, end, start.isoformat())):
        evs[x["room_id"]].append(x)
    out = []
    for r in rooms:
        cells = []
        for d in days:
            cell = None
            for e in evs[r["id"]]:
                if e["start_date"] <= d and (e["end_date"] is None or e["end_date"] > d) and (e["kind"] not in CLEANING or d <= g.today().isoformat()):  # a room waiting for cleaning is expected back by tomorrow, like the booking rule
                    cell = {"status": {"internal_block": "blocked", "in_cleaning": "cleaning", "cleaning_required": "cleaning"}.get(e["kind"], e["kind"]), "note": LABEL[e["kind"]],
                            "event_id": e["id"], "reason": e["reason"], "from": e["start_date"], "until": e["end_date"]}
                    break
            if not cell:
                for x in bks[r["id"]]:
                    if x["check_in"] <= d < x["check_out"]:
                        cell = {"status": "occupied" if x["status"] == "Checked-In" else "booked", "note": x["guest_name"], "booking_id": x["id"]}
                        break
            cells.append(cell)
        out.append({"id": r["id"], "number": r["number"], "property": r["property"], "property_id": r["property_id"], "type": r["type"], "cells": cells})
    return {"start": start.isoformat(), "days": days, "rooms": out}


CALLBACK_SQL = ("SELECT c.id, c.property_id, p.name AS property, c.name, c.phone, c.people, c.check_in, c.check_out, c.note, c.status, c.created_at, c.call_time, c.lang, "
                "c.handled_by, u.full_name AS handled_by_name, c.handled_at, c.follow_up_on FROM callback_requests c LEFT JOIN properties p ON p.id = c.property_id "
                "LEFT JOIN users u ON u.id = c.handled_by")
CALLBACK_STATUSES = ("new", "contacted", "follow_up", "closed")


def callbacks_list(conn, c):
    """Newest first. A user sees requests for their own properties plus requests with no property (those are for everyone)."""
    ids, status = scope(c.user, c.q.get("property_id")), c.q.get("status") or None
    if status and status not in CALLBACK_STATUSES:
        raise UserError("Please choose a valid status.")
    sql = CALLBACK_SQL + f" WHERE (c.property_id IN ({marks(ids)})" + (" OR c.property_id IS NULL)" if not c.q.get("property_id") else ")")
    args = list(ids)
    if status:
        sql += " AND c.status = ?"
        args.append(status)
    if c.q.get("due") in ("1", "true"):  # follow-ups whose date has come
        sql += " AND c.status = 'follow_up' AND c.follow_up_on <= ?"
        args.append(g.today().isoformat())
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY c.created_at DESC, c.id DESC LIMIT 300", args)]
    notes, t = callback_notes_for(conn, [r["id"] for r in rows]), g.today().isoformat()
    return [r | {"notes": notes[r["id"]], "next": list(CALLBACK_FLOW[r["status"]]), "overdue": bool(r["status"] == "follow_up" and r["follow_up_on"] and r["follow_up_on"] < t)} for r in rows]


def phone_key(v):
    return re.sub(r"\D", "", str(v or ""))[-10:]


def guests_list(conn, c):
    ids, q = scope(c.user, c.q.get("property_id")), c.q.get("q", "").strip()[:60].lower()
    by = {}
    for b in conn.execute(f"SELECT guest_name, phone, check_in, status, amount FROM bookings WHERE property_id IN ({marks(ids)}) ORDER BY check_in, id", ids):
        gst = by.setdefault(phone_key(b["phone"]), {"name": "", "phone": "", "stays": 0, "last_stay": "", "spent": 0})
        gst.update(name=b["guest_name"], phone=b["phone"], last_stay=b["check_in"])
        if b["status"] in STATUS_LIVE:
            gst["stays"] += 1
            gst["spent"] += b["amount"] or 0
    rows = [v for v in by.values() if not q or q in v["name"].lower() or q in v["phone"]]
    rows.sort(key=lambda v: v["last_stay"], reverse=True)
    return rows[:300]


def guest_profile(conn, c):
    key = phone_key(c.q.get("phone"))
    if len(key) != 10:
        raise UserError("Please give the guest's mobile number.")
    rows = [row(r) for r in conn.execute(BOOKING_SQL + f" WHERE b.property_id IN ({marks(c.user['props'])}) ORDER BY b.check_in DESC, b.id DESC", c.user["props"])
            if phone_key(r["phone"]) == key]
    if not rows:
        raise UserError("Guest not found.", 404)
    return {"name": rows[0]["guest_name"], "phone": rows[0]["phone"], "bookings": rows,
            "spent": sum(b["amount"] or 0 for b in rows if b["status"] in STATUS_LIVE), "stays": sum(1 for b in rows if b["status"] in STATUS_LIVE)}


def guest_edit(conn, c):
    key, name = phone_key(c.body.get("phone")), text(c.body, "guest_name", "Guest name", 1, 100)
    new_phone = g.norm_phone(c.body.get("new_phone") or c.body.get("phone"))

    def work():
        mine = [r for r in conn.execute(f"SELECT id, phone FROM bookings WHERE property_id IN ({marks(c.user['props'])})", c.user["props"]) if phone_key(r["phone"]) == key]
        if not mine:
            raise UserError("Guest not found.", 404)
        for r in mine:
            conn.execute("UPDATE bookings SET guest_name = ?, phone = ? WHERE id = ?", (name, new_phone, r["id"]))
        audit(conn, c.user, "guest.edit", conn.execute("SELECT property_id FROM bookings WHERE id = ?", (mine[0]["id"],)).fetchone()[0], None, key, phone_key(new_phone), f"{len(mine)} booking(s) updated, name {name}")
        return len(mine)
    return {"updated": g.write(conn, work), "phone": new_phone}


def reports(conn, c):
    ids = scope(c.user, c.q.get("property_id"))
    a, z = day(c.q.get("from") or (g.today() - timedelta(days=29)).isoformat(), "From date"), day(c.q.get("to") or g.today().isoformat(), "To date")
    if z < a:
        raise UserError("The end date must not be before the start date.")
    if (z - a).days > 366:
        raise UserError("Please choose a range of at most one year.")
    rows = conn.execute(f"SELECT b.check_in, b.status, b.amount, p.name AS property FROM bookings b JOIN properties p ON p.id = b.property_id "
                        f"WHERE b.property_id IN ({marks(ids)}) AND b.check_in >= ? AND b.check_in <= ?", (*ids, a.isoformat(), z.isoformat())).fetchall()
    stat, daily, prop = defaultdict(int), defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for r in rows:
        stat[r["status"]] += 1
        if r["status"] in STATUS_LIVE:
            for m, k in ((daily, r["check_in"]), (prop, r["property"])):
                m[k][0] += 1
                m[k][1] += r["amount"] or 0
    return {"from": a.isoformat(), "to": z.isoformat(), "total": len(rows), "by_status": dict(stat),
            "bookings": sum(v[0] for v in daily.values()), "revenue": sum(v[1] for v in daily.values()),
            "by_day": [{"date": k, "bookings": v[0], "revenue": v[1]} for k, v in sorted(daily.items())],
            "by_property": [{"property": k, "bookings": v[0], "revenue": v[1]} for k, v in sorted(prop.items())]}


def notifications(conn, c):
    ids = scope(c.user, None)
    since = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    seen = conn.execute("SELECT value FROM settings WHERE key = ?", (f"notif_seen_{c.user['id']}",)).fetchone()
    seen = seen[0] if seen else ""
    items = []
    if c.user["role"] in FRONT:
        for b in conn.execute(BOOKING_SQL.replace("b.amount,", "b.amount, b.created_at, b.status_at,") + " WHERE b.property_id IN (%s) AND (b.created_at >= ? OR (b.status = 'Cancelled' AND b.status_at >= ?))" % marks(ids), (*ids, since, since)):
            nm = f"#{b['id']} {b['guest_name']}, room {b['room']} ({b['property']}), {b['check_in']} to {b['check_out']}"
            if b["created_at"] >= since:
                items.append({"kind": "new", "ts": b["created_at"], "text": f"New booking {nm}" + (" (online)" if b["source"] == "online" else "")})
            if b["status"] == "Cancelled" and b["status_at"] >= since:
                items.append({"kind": "cancelled", "ts": b["status_at"], "text": f"Cancelled booking {nm}" + (f". Reason: {b['cancel_reason']}" if b["cancel_reason"] else "")})
    for e in conn.execute(f"SELECT e.created_at, r.number, p.name FROM room_status_events e JOIN rooms r ON r.id = e.room_id JOIN properties p ON p.id = r.property_id "
                          f"WHERE e.kind = 'cleaning_required' AND e.active = 1 AND r.property_id IN ({marks(ids)})", ids):
        items.append({"kind": "cleaning", "ts": e["created_at"], "text": f"Room {e['number']} ({e['name']}) needs cleaning"})
    if c.user["role"] in FRONT:
        for r in conn.execute(f"SELECT id, name, phone, follow_up_on FROM callback_requests WHERE status = 'follow_up' AND follow_up_on <= ? AND (property_id IS NULL OR property_id IN ({marks(ids)}))", (g.today().isoformat(), *ids)):
            items.append({"kind": "followup", "ts": r["follow_up_on"] + "T00:00:00", "text": f"Call-back follow-up due: {r['name']}, {r['phone']} (call again on {r['follow_up_on']})"})
    items = sorted(items, key=lambda i: i["ts"], reverse=True)[:100]
    for i in items:
        i["unread"] = i["ts"] > seen
    return {"items": items, "unread": sum(i["unread"] for i in items)}


def notifications_seen(conn, c):
    g.write(conn, lambda: conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (f"notif_seen_{c.user['id']}", now_iso())))
    return {"ok": True}


def rate_types(conn, c):
    return [dict(r) for r in conn.execute("SELECT t.*, (SELECT COUNT(*) FROM rooms r WHERE r.room_type_id = t.id) AS rooms FROM room_types t ORDER BY t.tariff")]


def rate_save(conn, c):
    b = c.body
    name, desc = text(b, "name", "Name", 1, 60), text(b, "description", "Description", 0, 300)
    cap = g.to_int(b.get("capacity"), "Capacity must be between 1 and 100.", 1, 100)
    try:
        tariff = float(b.get("tariff"))
    except (TypeError, ValueError):
        tariff = -1
    if not 0 <= tariff <= 1_000_000:
        raise UserError("Tariff must be a number between 0 and 1000000.")
    tid = g.opt_int(b.get("id"))

    def work():
        if conn.execute("SELECT 1 FROM room_types WHERE name = ? AND id IS NOT ?", (name, tid)).fetchone():
            raise UserError("A room type with that name already exists.", 409)
        if tid:
            if not conn.execute("SELECT 1 FROM room_types WHERE id = ?", (tid,)).fetchone():
                raise UserError("Room type not found.", 404)
            conn.execute("UPDATE room_types SET name = ?, capacity = ?, description = ?, tariff = ? WHERE id = ?", (name, cap, desc or None, tariff, tid))
        else:
            tid_ = conn.execute("INSERT INTO room_types (name, capacity, description, tariff) VALUES (?,?,?,?)", (name, cap, desc or None, tariff)).lastrowid
        audit(conn, c.user, "rate.edit" if tid else "rate.create", None, None, None, f"{name}: sleeps {cap}, Rs {tariff:g}")
        return tid or tid_
    return {"id": g.write(conn, work)}


def rate_delete(conn, c):
    def work():
        t = conn.execute("SELECT * FROM room_types WHERE id = ?", (c.args["id"],)).fetchone()
        if not t:
            raise UserError("Room type not found.", 404)
        n = conn.execute("SELECT COUNT(*) FROM rooms WHERE room_type_id = ?", (t["id"],)).fetchone()[0]
        if n:
            raise UserError(f"{t['name']} is used by {n} room(s), so it cannot be deleted. Move or remove those rooms first.", 409)
        conn.execute("DELETE FROM room_types WHERE id = ?", (t["id"],))
        audit(conn, c.user, "rate.delete", None, None, t["name"], None)
    g.write(conn, work)
    return {"ok": True}


def settings_get(conn, c):
    s = g.settings(conn)
    props = [dict(r) for r in conn.execute(f"SELECT id, name, address, contact_person, phone, description, facilities FROM properties WHERE id IN ({marks(c.user['props'])}) ORDER BY id", c.user["props"])]
    return {"properties": props, "checkin_time": s["checkin_time"], "checkout_time": s["checkout_time"], "can_times": c.user["role"] == "super_admin"}


def property_save(conn, c):
    b = c.body
    vals = [text(b, k, k.replace("_", " ").capitalize(), 0, n) or None for k, n in (("address", 300), ("contact_person", 100), ("phone", 30), ("description", 1000), ("facilities", 1000))]
    if vals[2] and not re.fullmatch(r"[\d\s+\-]{6,30}", vals[2]):
        raise UserError("Phone may contain only digits, spaces, + and -.")
    pid = scope(c.user, b.get("id"))[0] if b.get("id") else None
    if pid is None:
        raise UserError("Please choose a property.")
    g.write(conn, lambda: (conn.execute("UPDATE properties SET address = ?, contact_person = ?, phone = ?, description = ?, facilities = ? WHERE id = ?", (*vals, pid)),
                           audit(conn, c.user, "settings.property", pid, None, None, "profile updated")))
    return {"ok": True}


def times_save(conn, c):
    t = {k: str(c.body.get(k) or "") for k in ("checkin_time", "checkout_time")}
    if not all(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v) for v in t.values()):
        raise UserError("Times must look like 14:00.")

    def work():
        for k, v in t.items():
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        audit(conn, c.user, "settings.times", None, None, None, f"check-in {t['checkin_time']}, check-out {t['checkout_time']}")
    g.write(conn, work)
    return {"ok": True}


def profile_save(conn, c):
    name = text(c.body, "full_name", "Name", 1, 100)
    g.write(conn, lambda: (conn.execute("UPDATE users SET full_name = ? WHERE id = ?", (name, c.user["id"])), audit(conn, c.user, "user.profile", reason="name changed")))
    c.user["full_name"] = name
    return meta(conn, c)


def totp_secret(conn, u):
    row = conn.execute("SELECT totp_secret FROM users WHERE id = ?", (u["id"],)).fetchone()
    if not row or row[0] is None:
        raise UserError("Two-factor sign-in is not set up for this account.", 409)
    return idvault.open_(g.vault_key(conn), bytes(row[0]), f"totp:{u['id']}".encode()).decode()


def reauth(conn, c):
    """Asks for the signed-in user's password again before a security change. Wrong tries are counted like password_change."""
    key = ("staff-pw", c.ip, c.user["username"].lower())
    g.rate(key, 5, 600, "Too many failed attempts.", add=False)
    u = conn.execute("SELECT pw_hash, salt FROM users WHERE id = ?", (c.user["id"],)).fetchone()
    if not check_pw(str(c.body.get("password") or ""), u["pw_hash"], u["salt"]):
        g.rate(key, 10**6, 600, "", add=True)
        raise UserError("Your password is not correct.", 403)


def totp_setup(conn, c):
    """POST /api/me/totp/setup {password}: makes a new secret for the signed-in manager or super admin. It only counts once totp_enable has seen a valid code."""
    if c.user["role"] not in TOTP_ROLES:
        raise UserError("Two-factor sign-in is for managers and super admins.", 403)
    if c.user["totp_enabled"]:
        raise UserError("Two-factor sign-in is already on. Ask a super admin to reset it if you changed phone.", 409)
    reauth(conn, c)
    secret = totp.new_secret()
    sealed = idvault.seal(g.vault_key(conn), secret.encode(), f"totp:{c.user['id']}".encode())
    g.write(conn, lambda: conn.execute("UPDATE users SET totp_secret = ?, totp_enabled = 0, totp_step = 0 WHERE id = ?", (sealed, c.user["id"])))
    return {"secret": secret, "uri": totp.uri(secret, c.user["username"])}


def totp_enable(conn, c):
    """POST /api/me/totp/enable {code}: turns two-factor sign-in on once the authenticator app shows a valid code."""
    if c.user["role"] not in TOTP_ROLES:
        raise UserError("Two-factor sign-in is for managers and super admins.", 403)
    key = ("staff-totp", c.ip, c.user["username"].lower())
    g.rate(key, 5, 600, "Too many failed attempts.", add=False)
    step = totp.verify(totp_secret(conn, c.user), c.body.get("code"))
    if step is None:
        g.rate(key, 10**6, 600, "", add=True)
        raise UserError("That code is not correct. Check the code in your authenticator app and try again.", 400)

    def work():
        conn.execute("UPDATE users SET totp_enabled = 1, totp_step = ? WHERE id = ?", (step, c.user["id"]))
        audit(conn, c.user, "user.totp_enable")
    g.write(conn, work)
    for t in [t for t, v in SESSIONS.items() if v[0] == c.user["id"] and t != c.args.get("_token")]:
        SESSIONS.pop(t, None)
    return {"ok": True}


def password_change(conn, c):
    cur, new = str(c.body.get("current") or ""), str(c.body.get("new") or "")
    key = ("staff-pw", c.ip, c.user["username"].lower())
    g.rate(key, 5, 600, "Too many failed attempts.", add=False)
    u = conn.execute("SELECT pw_hash, salt FROM users WHERE id = ?", (c.user["id"],)).fetchone()
    if not check_pw(cur, u["pw_hash"], u["salt"]):
        g.rate(key, 10**6, 600, "", add=True)
        raise UserError("Your current password is not correct.")
    if len(new) < PW_MIN or len(new) > 200:
        raise UserError(f"The new password must be at least {PW_MIN} characters.")
    if new.lower() == c.user["username"].lower():
        raise UserError("The new password must not be your username.")
    if new == cur:
        raise UserError("The new password must be different from the current one.")
    h, salt = make_hash(new)
    g.write(conn, lambda: (conn.execute("UPDATE users SET pw_hash = ?, salt = ? WHERE id = ?", (h, salt, c.user["id"])), audit(conn, c.user, "user.password_change")))
    for t in [t for t, v in SESSIONS.items() if v[0] == c.user["id"] and t != c.args.get("_token")]:
        SESSIONS.pop(t, None)  # other devices must sign in again
    return {"ok": True}


# ---------- admin screens (super_admin only) ----------
SUPER = frozenset({"super_admin"})
SETTING_RANGES = {"guest_max_rooms": (1, 20, "Most rooms per online booking"), "guest_advance_days": (1, 730, "Days ahead guests can book"),
                  "guest_max_nights": (1, 60, "Most nights per online booking"), "guest_cancel_cutoff_hours": (0, 168, "Cancellation cutoff (hours)")}
OPTIONAL_RANGES = {"refund_percent": (0, 100, "Refund percentage"), "refund_days": (0, 90, "Days to receive a refund"),  # empty = not set, so no policy number is assumed
                   "extra_bed_max": (0, 5, "Most extra beds per room"), "reminder_days": (0, 14, "Reminder: days before arrival"), "feedback_days": (0, 30, "Ask for feedback: days after check-out"), "guest_retention_days": (30, 3650, "Keep guest names, mobiles and emails (days after the last stay)"), "id_retention_days": (1, 3650, "Keep ID documents (days after the stay)")}
# pricing rules the guest sees in the price breakdown: empty = not charged. Rupee or percent amounts may have up to 2 decimals.
PRICING_RANGES = {"tax_percent": (0, 100, "Tax (%)"), "advance_percent": (0, 100, "Advance (% of the total)"), "extra_bed_price": (0, 100000, "Extra bed price per night (Rs)"),
                  "child_price": (0, 100000, "Child charge per night (Rs)")}
TIME_RE = r"([01]\d|2[0-3]):[0-5]\d"
LIVE_SQL = "status IN ('Confirmed', 'Checked-In') AND (check_out > :today OR status = 'Checked-In')"
FUTURE_SQL = {"rooms": f"SELECT COUNT(*) FROM bookings WHERE room_id = :id AND {LIVE_SQL}",
              "room_types": f"SELECT COUNT(*) FROM bookings WHERE room_id IN (SELECT id FROM rooms WHERE room_type_id = :id) AND {LIVE_SQL}",
              "properties": f"SELECT COUNT(*) FROM bookings WHERE property_id = :id AND {LIVE_SQL}"}
ACTIVE_LABEL = {"properties": "property", "room_types": "room type", "rooms": "room", "users": "user", "peak_periods": "peak period", "faqs": "faq", "promo_codes": "promo code"}


def raw_settings(conn):
    return g.DEFAULTS | {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings") if r["key"] in g.DEFAULTS}


def room_photo_upload(conn, c):
    """POST /api/admin/room_types/<id>/photo with the image as the raw body (JPEG, PNG or WebP, at most 1.5 MB). The file gets a name made of the room type and a hash of its
    content (so a changed photo is never served stale), the old file is removed, and guests see it on the room cards."""
    data = c.body.get("_raw") or b""
    mime = next((m for magic, m in IMAGE_MAGIC if data.startswith(magic) and (m != "image/webp" or data[8:12] == b"WEBP")), None)
    if not mime:
        raise UserError("The photo must be a JPEG, PNG or WebP image.")
    if len(data) > 1_500_000:
        raise UserError("The photo is too large (at most 1.5 MB). Please shrink it first.", 413)
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime]

    def work():
        rt = conn.execute("SELECT id, name, photo FROM room_types WHERE id = ?", (g.to_int(c.args["id"], "Invalid id.", 1, 10**12),)).fetchone()
        if not rt:
            raise UserError("Room type not found.", 404)
        name = f"room-{rt['id']}-{hashlib.sha256(data).hexdigest()[:8]}.{ext}"
        g.UPLOADS.mkdir(parents=True, exist_ok=True)
        (g.UPLOADS / name).write_bytes(data)
        conn.execute("UPDATE room_types SET photo = ? WHERE id = ?", (name, rt["id"]))
        audit(conn, c.user, "admin.room_photo", None, None, rt["photo"], name, rt["name"])
        return rt["photo"], name
    old, name = g.write(conn, work)
    if old and old != name:
        try:
            (g.UPLOADS / old).unlink()
        except OSError:
            pass
    return {"photo": name}


def room_photo_remove(conn, c):
    def work():
        rt = conn.execute("SELECT id, name, photo FROM room_types WHERE id = ?", (g.to_int(c.args["id"], "Invalid id.", 1, 10**12),)).fetchone()
        if not rt:
            raise UserError("Room type not found.", 404)
        conn.execute("UPDATE room_types SET photo = NULL WHERE id = ?", (rt["id"],))
        audit(conn, c.user, "admin.room_photo", None, None, rt["photo"], None, rt["name"])
        return rt["photo"]
    old = g.write(conn, work)
    if old:
        try:
            (g.UPLOADS / old).unlink()
        except OSError:
            pass
    return {"ok": True}


def admin_data(conn, c):
    q = lambda sql: [dict(r) for r in conn.execute(sql)]
    users = q("SELECT id, username, full_name, role, active FROM users ORDER BY id")
    for u in users:
        u["property_ids"] = [r[0] for r in conn.execute("SELECT property_id FROM user_properties WHERE user_id = ? ORDER BY 1", (u["id"],))]
    return {"properties": q("SELECT * FROM properties ORDER BY id"), "room_types": q("SELECT * FROM room_types ORDER BY tariff"),
            "rooms": q("SELECT r.*, p.name AS property, t.name AS type FROM rooms r JOIN properties p ON p.id = r.property_id JOIN room_types t ON t.id = r.room_type_id ORDER BY r.property_id, r.number"),
            "peak_periods": q("SELECT * FROM peak_periods ORDER BY start_date DESC, id DESC"),
            "site_config": site_config_view(conn), "langs": list(g.LANGS), "faqs": faq_admin_view(conn),
            "users": users, "roles": q("SELECT name, label FROM roles ORDER BY rowid"), "settings": raw_settings(conn),
            "setting_ranges": {k: list(v) for k, v in (SETTING_RANGES | OPTIONAL_RANGES | PRICING_RANGES).items()},
            "promos": promo_admin_view(conn), "permissions": [{"key": k, "label": label, "roles": sorted(roles)} for k, (roles, label) in PERMS.items()],
            "extras": extras_admin_view(conn), "payment_provider": g.providers.env("PAYMENT_PROVIDER") or None, "whatsapp_provider": g.providers.env("WHATSAPP_PROVIDER") or None}


def dec(v, label, lo, hi):
    """A number from lo to hi with at most 2 decimals, as a string for the settings table or a column."""
    t = str(v).strip()
    if not re.fullmatch(r"\d{1,7}(\.\d{1,2})?", t) or not lo <= float(t) <= hi:
        raise UserError(f"{label} must be a number from {lo} to {hi}, with at most 2 decimals.")
    return float(t) if "." in t else int(t)


def exists(conn, table, id_, active_only=False):
    return conn.execute(f"SELECT 1 FROM {table} WHERE id = ?" + (" AND active = 1" if active_only else ""), (id_,)).fetchone()


def admin_property(conn, c):
    b = c.body
    name = text(b, "name", "Name", 1, 100)
    vals = [text(b, k, k.replace("_", " ").capitalize(), 0, n) or None for k, n in (("address", 300), ("contact_person", 100), ("phone", 30), ("description", 1000), ("facilities", 1000))]
    if vals[2] and not re.fullmatch(r"[\d\s+\-]{6,30}", vals[2]):
        raise UserError("Phone may contain only digits, spaces, + and -.")
    pid = g.opt_int(b.get("id"))

    def work():
        if conn.execute("SELECT 1 FROM properties WHERE name = ? AND id IS NOT ?", (name, pid)).fetchone():
            raise UserError("A property with that name already exists.", 409)
        if pid:
            if not exists(conn, "properties", pid):
                raise UserError("Property not found.", 404)
            conn.execute("UPDATE properties SET name = ?, address = ?, contact_person = ?, phone = ?, description = ?, facilities = ? WHERE id = ?", (name, *vals, pid))
            new = pid
        else:
            new = conn.execute("INSERT INTO properties (name, address, contact_person, phone, description, facilities) VALUES (?,?,?,?,?,?)", (name, *vals)).lastrowid
        audit(conn, c.user, "admin.property_edit" if pid else "admin.property_create", new, None, None, name)
        return new
    return {"id": g.write(conn, work)}


def admin_room(conn, c):
    b, rid = c.body, g.opt_int(c.body.get("id"))
    number, amenities = text(b, "number", "Room number", 1, 20), text(b, "amenities", "Amenities", 0, 300) or None
    floor = g.to_int(b.get("floor", 0), "Floor must be a whole number from -5 to 200.", -5, 200)
    cap = g.to_int(b.get("capacity"), "Capacity must be between 1 and 100.", 1, 100)
    tid = g.to_int(b.get("room_type_id"), "Please choose a room type.", 1, 10**9)
    pid = g.to_int(b.get("property_id"), "Please choose a property.", 1, 10**9)
    senior = 1 if b.get("senior_friendly") is True else 0

    def work():
        old = conn.execute("SELECT * FROM rooms WHERE id = ?", (rid,)).fetchone() if rid else None
        if rid and not old:
            raise UserError("Room not found.", 404)
        if old and old["property_id"] != pid:
            raise UserError("A room cannot move to another property.")
        if not exists(conn, "properties", pid, True) or not exists(conn, "room_types", tid, True):
            raise UserError("Please choose an active property and room type.")
        if conn.execute("SELECT 1 FROM rooms WHERE property_id = ? AND number = ? AND id IS NOT ?", (pid, number, rid)).fetchone():
            raise UserError("That property already has a room with this number.", 409)
        if old and old["room_type_id"] != tid and conn.execute(FUTURE_SQL["rooms"], dict(id=rid, today=g.today().isoformat())).fetchone()[0]:
            raise UserError("This room has current or future bookings, so its room type cannot change.", 409)
        if rid:
            conn.execute("UPDATE rooms SET number = ?, floor = ?, room_type_id = ?, capacity = ?, amenities = ?, senior_friendly = ? WHERE id = ?", (number, floor, tid, cap, amenities, senior, rid))
            new = rid
        else:
            new = conn.execute("INSERT INTO rooms (property_id, number, floor, room_type_id, capacity, amenities, senior_friendly) VALUES (?,?,?,?,?,?,?)", (pid, number, floor, tid, cap, amenities, senior)).lastrowid
        audit(conn, c.user, "admin.room_edit" if rid else "admin.room_create", pid, new, None, f"room {number}, sleeps {cap}" + (", senior-friendly" if senior else ""))
        return new
    return {"id": g.write(conn, work)}


def admin_user(conn, c):
    b, uid = c.body, g.opt_int(c.body.get("id"))
    username, name = text(b, "username", "Username", 3, 40), text(b, "full_name", "Full name", 1, 100)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", username):
        raise UserError("Username may contain only letters, digits, dot, dash and underscore.")
    pw = str(b.get("password") or "")
    if (not uid or pw) and not PW_MIN <= len(pw) <= 200:
        raise UserError(f"The password must be {PW_MIN} to 200 characters.")
    ids = b.get("property_ids") or []
    if not isinstance(ids, list):
        raise UserError("Please choose properties from the list.")
    ids = sorted({g.to_int(i, "Please choose properties from the list.", 1, 10**9) for i in ids})

    def work():
        role = b.get("role")
        if role not in {r[0] for r in conn.execute("SELECT name FROM roles")}:
            raise UserError("Please choose a valid role.")
        old = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone() if uid else None
        if uid and not old:
            raise UserError("User not found.", 404)
        if uid == c.user["id"] and role != "super_admin":
            raise UserError("You cannot remove your own super admin role.", 409)
        if conn.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE AND id IS NOT ?", (username, uid)).fetchone():
            raise UserError("That username is already taken.", 409)
        if role != "super_admin" and not ids:
            raise UserError("Please assign at least one property to this user.")
        if any(not exists(conn, "properties", i, True) for i in ids):
            raise UserError("Please choose active properties only.")
        if uid:
            conn.execute("UPDATE users SET username = ?, full_name = ?, role = ? WHERE id = ?", (username, name, role, uid))
            if pw:
                h, salt = make_hash(pw)
                conn.execute("UPDATE users SET pw_hash = ?, salt = ? WHERE id = ?", (h, salt, uid))
            new = uid
        else:
            h, salt = make_hash(pw)
            new = conn.execute("INSERT INTO users (username, full_name, role, pw_hash, salt) VALUES (?,?,?,?,?)", (username, name, role, h, salt)).lastrowid
        if uid and b.get("reset_totp") is True:
            conn.execute("UPDATE users SET totp_secret = NULL, totp_enabled = 0, totp_step = 0 WHERE id = ?", (uid,))
            audit(conn, c.user, "admin.totp_reset", None, None, None, username)
        conn.execute("DELETE FROM user_properties WHERE user_id = ?", (new,))
        if role != "super_admin":
            conn.executemany("INSERT INTO user_properties (user_id, property_id) VALUES (?,?)", [(new, i) for i in ids])
        audit(conn, c.user, "admin.user_edit" if uid else "admin.user_create", None, None, old and old["role"], f"{username} ({role})",
              "password reset" if uid and pw else None)
        return new
    new = g.write(conn, work)
    if uid and (pw or b.get("reset_totp") is True) and uid != c.user["id"]:
        for t in [t for t, v in SESSIONS.items() if v[0] == uid]:
            SESSIONS.pop(t, None)  # a reset password signs that user out everywhere
    return {"id": new}


def admin_active(conn, c):
    kind, id_ = c.args["kind"], int(c.args["id"])
    on = c.body.get("active")
    if not isinstance(on, bool):
        raise UserError("Some of the values sent are not valid.")

    def work():
        r = conn.execute(f"SELECT * FROM {kind} WHERE id = ?", (id_,)).fetchone()
        if not r:
            raise UserError("Not found.", 404)
        if not on:
            if kind == "users" and id_ == c.user["id"]:
                raise UserError("You cannot deactivate your own account.", 409)
            n = conn.execute(FUTURE_SQL[kind], dict(id=id_, today=g.today().isoformat())).fetchone()[0] if kind in FUTURE_SQL else 0
            if n:
                raise UserError(f"This {ACTIVE_LABEL[kind]} has {n} current or future booking(s), so it cannot be deactivated. Move or cancel them first.", 409)
        conn.execute(f"UPDATE {kind} SET active = ? WHERE id = ?", (int(on), id_))
        label = r["name"] if "name" in r.keys() else r["username"] if "username" in r.keys() else f"room {r['number']}" if "number" in r.keys() else (r["code"] or r["label"]) if "label" in r.keys() else f"FAQ #{id_}"
        audit(conn, c.user, f"admin.{ACTIVE_LABEL[kind].replace(' ', '_')}_{'activate' if on else 'deactivate'}",
              r["property_id"] if kind == "rooms" else id_ if kind == "properties" else None, id_ if kind == "rooms" else None, None, label)
    g.write(conn, work)
    return {"ok": True}


def admin_peak(conn, c):
    b, pid = c.body, g.opt_int(c.body.get("id"))
    name, note = text(b, "name", "Name", 1, 100), text(b, "note", "Note", 0, 500) or None
    ci, co = day(b.get("start_date"), "Start date"), day(b.get("end_date"), "End date")
    if co < ci:
        raise UserError("The end date cannot be before the start date.")
    if (co - ci).days > 366:
        raise UserError("A peak period can last at most 366 days.")
    limit = lambda k, label, hi: None if b.get(k) in (None, "") else g.to_int(b.get(k), f"{label} must be a whole number from 1 to {hi}, or empty for no limit.", 1, hi)
    nights, rooms = limit("max_nights", "Most nights", 60), limit("max_rooms", "Most rooms", 20)
    surcharge = None if b.get("surcharge_percent") in (None, "") else dec(b.get("surcharge_percent"), "Price increase (%)", 0, 500)

    def work():
        if pid and not exists(conn, "peak_periods", pid):
            raise UserError("Peak period not found.", 404)
        vals = (name, ci.isoformat(), co.isoformat(), nights, rooms, note, surcharge)
        if pid:
            conn.execute("UPDATE peak_periods SET name = ?, start_date = ?, end_date = ?, max_nights = ?, max_rooms = ?, note = ?, surcharge_percent = ? WHERE id = ?", (*vals, pid))
            new = pid
        else:
            new = conn.execute("INSERT INTO peak_periods (name, start_date, end_date, max_nights, max_rooms, note, surcharge_percent) VALUES (?,?,?,?,?,?,?)", vals).lastrowid
        audit(conn, c.user, "admin.peak_period_edit" if pid else "admin.peak_period_create", None, None, None,
              f"{name}: {ci} to {co}, max nights {nights or 'none'}, max rooms {rooms or 'none'}, price increase {surcharge or 0}%")
        return new
    return {"id": g.write(conn, work)}


def promo_admin_view(conn):
    out = []
    for r in conn.execute("SELECT pc.*, p.name AS property, t.name AS room_type FROM promo_codes pc LEFT JOIN properties p ON p.id = pc.property_id "
                          "LEFT JOIN room_types t ON t.id = pc.room_type_id ORDER BY pc.active DESC, pc.id DESC"):
        out.append(dict(r) | {"uses": g.promo_uses(conn, g.promo_key(r))})
    return out


def admin_promo(conn, c):
    """Create or edit a promo/donor code (code set) or an automatic pilgrim group rate (auto: applies without a code when the stay qualifies).
    Rules: percent or flat rupees off the room charge, optional cap, minimum rooms/guests/nights, valid dates, total and per-mobile usage limits, building or room type,
    and whether peak periods are excluded (the default). Usage counts bookings that are not cancelled."""
    b, pid = c.body, g.opt_int(c.body.get("id"))
    auto = b.get("auto") is True
    code = text(b, "code", "Code", 0, 32).upper().replace(" ", "") or None
    if code and not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        raise UserError("A code may use only letters, numbers, - and _, 3 to 32 characters.")
    if not auto and not code:
        raise UserError("Please enter a code, or tick automatic group rate.")
    label = text(b, "label", "Label", 1, 100)
    kind = b.get("kind")
    if kind not in ("percent", "flat"):
        raise UserError("Please choose percent or flat amount.")
    value = dec(b.get("value"), "Value", 0.01, 100 if kind == "percent" else 1000000)
    opt = lambda k, label_, hi: None if b.get(k) in (None, "") else g.to_int(b.get(k), f"{label_} must be a whole number from 1 to {hi}, or empty.", 1, hi)
    cap = None if b.get("max_discount") in (None, "") else dec(b.get("max_discount"), "Largest discount", 1, 1000000)
    vf, vt = (None if b.get(k) in (None, "") else day(b.get(k), lbl).isoformat() for k, lbl in (("valid_from", "Valid from"), ("valid_to", "Valid until")))
    if vf and vt and vt < vf:
        raise UserError("The last valid date cannot be before the first.")
    vals = [label, 1 if auto else 0, kind, value, cap, opt("min_rooms", "Minimum rooms", 100), opt("min_guests", "Minimum guests", 1000), opt("min_nights", "Minimum nights", 60), vf, vt,
            opt("max_uses", "Total uses", 10**7), opt("per_phone_limit", "Uses per mobile number", 1000), 0 if b.get("exclude_peak") is False else 1,
            g.opt_int(b.get("property_id")), g.opt_int(b.get("room_type_id"))]

    def work():
        if pid and not exists(conn, "promo_codes", pid):
            raise UserError("Promo code not found.", 404)
        if code and conn.execute("SELECT 1 FROM promo_codes WHERE code = ? AND id IS NOT ?", (code, pid)).fetchone():
            raise UserError("That code already exists.", 409)
        if pid:
            conn.execute("UPDATE promo_codes SET code = ?, label = ?, auto = ?, kind = ?, value = ?, max_discount = ?, min_rooms = ?, min_guests = ?, min_nights = ?, valid_from = ?, valid_to = ?, "
                         "max_uses = ?, per_phone_limit = ?, exclude_peak = ?, property_id = ?, room_type_id = ? WHERE id = ?", (code, *vals, pid))
            new = pid
        else:
            new = conn.execute("INSERT INTO promo_codes (code, label, auto, kind, value, max_discount, min_rooms, min_guests, min_nights, valid_from, valid_to, max_uses, per_phone_limit, "
                               "exclude_peak, property_id, room_type_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (code, *vals, g.now_iso())).lastrowid
        audit(conn, c.user, "admin.promo_edit" if pid else "admin.promo_create", None, None, None, f"{code or 'auto'}: {label}, {value}{'%' if kind == 'percent' else ' Rs'} off, uses {vals[10] or 'unlimited'}, until {vt or 'no end'}")
        return new
    return {"id": g.write(conn, work)}


def site_config_view(conn):
    """Every known key with its values per language; `todo` is true while English is unset or still a TODO placeholder."""
    vals = {}
    for r in conn.execute("SELECT key, lang, value FROM site_config"):
        vals.setdefault(r["key"], {})[r["lang"]] = r["value"]
    return [{"key": k, "label": label, "translatable": tr, "values": vals.get(k, {}), "todo": g.is_todo(vals.get(k, {}).get("en"))} for k, (label, tr) in g.SITE_CONFIG.items()]


def faq_admin_view(conn):
    out = {}
    for f in conn.execute("SELECT id, position, active FROM faqs ORDER BY position, id"):
        out[f["id"]] = dict(f) | {"texts": {}}
    for r in conn.execute("SELECT faq_id, lang, question, answer FROM faq_text"):
        out[r["faq_id"]]["texts"][r["lang"]] = {"question": r["question"], "answer": r["answer"]}
    return list(out.values())


def admin_faq(conn, c):
    """Create or edit a FAQ: English question and answer are required; other languages are optional (empty = not translated, English is shown)."""
    b, fid = c.body, g.opt_int(c.body.get("id"))
    pos = g.to_int(b.get("position", 0), "Order must be a whole number from 0 to 9999.", 0, 9999)
    texts = b.get("texts")
    if not isinstance(texts, dict) or set(texts) - set(g.LANGS):
        raise UserError("Some of the values sent are not valid.")
    clean = {}
    for lang, t in texts.items():
        if not isinstance(t, dict):
            raise UserError("Some of the values sent are not valid.")
        q, a = text(t, "question", "Question", 0, 300), text(t, "answer", "Answer", 0, g.SITE_TEXT_MAX)
        if bool(q) != bool(a):
            raise UserError(f"Please fill in both the question and the answer for {lang}, or leave both empty.")
        if q:
            clean[lang] = (q, a)
    if "en" not in clean:
        raise UserError("The English question and answer are required.")

    def work():
        if fid and not exists(conn, "faqs", fid):
            raise UserError("FAQ not found.", 404)
        if fid:
            conn.execute("UPDATE faqs SET position = ? WHERE id = ?", (pos, fid))
            conn.execute("DELETE FROM faq_text WHERE faq_id = ?", (fid,))
            new = fid
        else:
            new = conn.execute("INSERT INTO faqs (position, active) VALUES (?, 1)", (pos,)).lastrowid
        conn.executemany("INSERT INTO faq_text (faq_id, lang, question, answer) VALUES (?,?,?,?)", [(new, lang, q, a) for lang, (q, a) in clean.items()])
        audit(conn, c.user, "admin.faq_edit" if fid else "admin.faq_create", None, None, None, clean["en"][0][:200])
        return new
    return {"id": g.write(conn, work)}


def admin_site_config(conn, c):
    b = c.body
    key, lang, value = b.get("key"), b.get("lang", "en"), str(b.get("value") or "").strip()
    if key not in g.SITE_CONFIG:
        raise UserError("Unknown site info item.")
    if lang not in g.LANGS or (lang != "en" and not g.SITE_CONFIG[key][1]):
        raise UserError("Please choose a valid language for this item.")
    if len(value) > g.SITE_TEXT_MAX:
        raise UserError(f"The text must be {g.SITE_TEXT_MAX} characters or fewer.")
    if value and not g.is_todo(value):
        g.site_check(key, value)  # phone, WhatsApp, email and link items are checked by kind

    def work():
        old = conn.execute("SELECT value FROM site_config WHERE key = ? AND lang = ?", (key, lang)).fetchone()
        if value:  # an empty value removes the row, which puts the item back to TODO
            conn.execute("INSERT OR REPLACE INTO site_config (key, lang, value, updated_at) VALUES (?,?,?,?)", (key, lang, value, now_iso()))
        else:
            conn.execute("DELETE FROM site_config WHERE key = ? AND lang = ?", (key, lang))
        audit(conn, c.user, "admin.site_config", None, None, old and old[0][:200], value[:200] or "(cleared)", f"{key} [{lang}]")
    g.write(conn, work)
    return {"site_config": site_config_view(conn)}


def admin_guest_erase(conn, c):
    """POST /api/admin/guest_erase {phone, reason}: erases one guest's personal data on request (DPDP right to erasure). Booking rows stay, anonymised."""
    reason = text(c.body, "reason", "Reason", 3, 200)
    n = g.erase_guest(conn, c.body.get("phone"), reason, f"Erasure requested, done by {c.user['username']}")
    g.write(conn, lambda: audit(conn, c.user, "admin.guest_erase", None, None, None, f"{n} booking(s) anonymised", reason))
    return {"anonymised": n}


def admin_settings(conn, c):
    b, new = c.body, {}
    for k, (lo, hi, label) in SETTING_RANGES.items():
        if k in b:
            new[k] = str(g.to_int(b[k], f"{label} must be a whole number from {lo} to {hi}.", lo, hi))
    for k, (lo, hi, label) in OPTIONAL_RANGES.items():
        if k in b:
            new[k] = "" if str(b[k]).strip() == "" else str(g.to_int(b[k], f"{label} must be a whole number from {lo} to {hi}, or empty if not decided.", lo, hi))
    for k, (lo, hi, label) in PRICING_RANGES.items():
        if k in b:
            new[k] = "" if str(b[k]).strip() == "" else str(dec(b[k], label, lo, hi))
    if "reminder_hour" in b:
        new["reminder_hour"] = str(g.to_int(b["reminder_hour"], "Reminder hour must be a whole number from 0 to 23.", 0, 23))
    if "pay_options" in b:
        opts = b["pay_options"]
        if not isinstance(opts, list) or not opts or not all(o in g.payments.OPTIONS for o in opts):
            raise UserError("Choose at least one payment option: full, advance or counter.")
        new["pay_options"] = ",".join(o for o in g.payments.OPTIONS if o in opts)
    if "checkin_requires_id" in b:
        if not isinstance(b["checkin_requires_id"], bool):
            raise UserError("Some of the values sent are not valid.")
        new["checkin_requires_id"] = "1" if b["checkin_requires_id"] else "0"
    if "child_counts_capacity" in b:
        if not isinstance(b["child_counts_capacity"], bool):
            raise UserError("Some of the values sent are not valid.")
        new["child_counts_capacity"] = "1" if b["child_counts_capacity"] else "0"
    for k in ("checkin_time", "checkout_time"):
        if k in b:
            if not re.fullmatch(TIME_RE, str(b[k])):
                raise UserError("Times must look like 14:00.")
            new[k] = str(b[k])
    if "guest_booking_enabled" in b:
        if not isinstance(b["guest_booking_enabled"], bool):
            raise UserError("Some of the values sent are not valid.")
        new["guest_booking_enabled"] = "1" if b["guest_booking_enabled"] else "0"

    def work():
        old = raw_settings(conn)
        changed = {k: v for k, v in new.items() if old[k] != v}
        for k, v in changed.items():
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        audit(conn, c.user, "admin.settings", None, None, ", ".join(f"{k}={old[k]}" for k in changed) or None, ", ".join(f"{k}={v}" for k, v in changed.items()) or "no change")
    g.write(conn, work)
    return {"settings": raw_settings(conn)}


# ---------- permissions ----------
# What each role may do. Routes use these sets, the staff page asks /api/meta (can), and Admin shows the matrix. Front desk works the counter; a manager (property_manager)
# also sees money, IDs and the audit log and may cancel paid bookings; the super admin also changes settings and users. Housekeeping only touches room cleaning.
PERMS = {
    "booking.create": (DESK, "Create walk-in bookings, edit and reschedule bookings"),
    "booking.checkin": (DESK, "Check guests in and out, mark no-shows"),
    "booking.cancel": (DESK, "Cancel a booking that has no money received"),
    "booking.cancel_paid": (MGR, "Cancel a booking that has been paid (this starts the refund)"),
    "id.capture": (DESK, "Record a guest's ID at check-in (type, number, photo)"),
    "id.reveal": (MGR, "See a stored ID number or photo (each view is logged)"),
    "payment.record": (DESK, "Record money taken at the counter"),
    "payment.view": (MGR, "See payments, refunds and the reconciliation"),
    "payment.refund": (MGR, "Mark refunds done, retry them, refund by hand"),
    "housekeeping.update": (CLEANERS, "Mark rooms as needing cleaning, being cleaned, ready"),
    "room.block": (DESK, "Block a room or report maintenance"),
    "callback.work": (DESK, "Work the call-back queue and add follow-up notes"),
    "messages.view": (MGR, "See the guest message log (SMS, WhatsApp, email)"),
    "notice.edit": (DESK, "Post and switch off weather and river-level notices shown to guests"),
    "feedback.view": (MGR, "Read guest feedback and ratings"),
    "broadcast.send": (MGR, "Send festival messages to subscribers (SMS, WhatsApp, email)"),
    "report.view": (MGR, "See reports: arrivals, departures, occupancy, revenue, reconciliation, analytics"),
    "report.export": (MGR, "Download reports as CSV or send them to Google Sheets (logged)"),
    "audit.view": (MGR, "Read the audit log"),
    "admin": (SUPER, "Change settings, rates, rooms and users"),
}
SENSITIVE = ("broadcast.", "report.", "id.", "payment.", "refund.", "booking.cancel", "booking.online_cancel", "booking.reschedule", "booking.edit", "room.block", "room.release", "callback.close", "admin.", "user.", "auth.login_failed", "guest.")


def allowed_for(role):
    return [k for k, (roles, _) in PERMS.items() if role in roles]


def perm(key):
    return PERMS[key][0]


# ---------- front desk: scan or type the booking, capture the ID, take payment, check in ----------
LIVE_STATUS = ("Confirmed", "Checked-In")
IMAGE_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"RIFF", "image/webp"))
IMAGE_MAX = 2_500_000
UPLOAD_MAX = 3_000_000


def group_rows(conn, user, gid):
    rows = conn.execute(BOOKING_SQL + " WHERE (b.group_id = ? OR (b.group_id IS NULL AND b.id = ?)) AND b.property_id IN (%s) ORDER BY b.id" % marks(user["props"]), (gid, gid, *user["props"])).fetchall()
    if not rows:
        raise UserError("No booking found for that code at your properties.", 404)
    return rows


def id_info(conn, ids):
    out = {}
    for r in conn.execute(f"SELECT booking_id, id_type, last4, number_enc IS NOT NULL AS has_number, image_enc IS NOT NULL AS has_image, captured_name, captured_at, purged_at FROM id_records WHERE booking_id IN ({marks(ids)})", ids):
        out[r["booking_id"]] = {"id_type": r["id_type"], "last4": r["last4"], "has_number": bool(r["has_number"]), "has_image": bool(r["has_image"]), "captured_by": r["captured_name"], "captured_at": r["captured_at"], "purged": bool(r["purged_at"])}
    return out


def desk_lookup(conn, c):
    """GET /api/checkin/lookup?code=: the booking behind a scanned QR code (a signed check-in code) or a typed booking ID, with its payments and ID status."""
    code = str(c.q.get("code") or "").strip()[:80]
    if not code:
        raise UserError("Scan the guest's QR code, or type the booking ID.")
    gid = g.notifications.parse_code(conn, code)
    via = "qr"
    if gid is None:
        if re.fullmatch(r"\d{1,12}", code):
            gid, via = int(code), "id"
        elif code.upper().startswith("MR1."):
            raise UserError("This QR code is not valid. Check the booking ID with the guest.")
        else:
            raise UserError("Please scan the booking QR code or type the booking ID (digits only).")
    rows = group_rows(conn, c.user, gid)
    live = [r for r in rows if r["status"] != "Cancelled"]
    total = sum(r["amount"] or 0 for r in live)
    ids = id_info(conn, [r["id"] for r in rows])
    payments = [dict(p) | {"payment_id": (p["payment_id"] or "")[-8:]} for p in conn.execute(
        "SELECT id, provider, kind, status, method, payment_id, amount_paise, paid_at, created_at FROM payments WHERE group_id = ? ORDER BY id", (gid,))]
    refunds = [dict(r) for r in conn.execute("SELECT id, amount_paise, status, reason, created_at FROM refunds WHERE group_id = ? ORDER BY id", (gid,))]
    s = g.settings(conn)
    return {"booking_id": gid, "via": via, "rows": [row(r) | {"id_record": ids.get(r["id"])} for r in rows], "total": float(g.money(total)),
            "money": g.payments.summary(conn, gid, float(g.money(total))), "sevas": g.extras.sevas_for(conn, gid), "payments": payments, "refunds": refunds,
            "requires_id": s["checkin_requires_id"], "id_types": list(g.ID_TYPES), "source": rows[0]["source"], "email": g.group_rows(conn, gid)[0]["email"]}


def id_capture(conn, c):
    """POST /api/bookings/<id>/id {id_type, id_number}: records the guest's ID for every live room of the booking. The number is stored encrypted (idvault, key from
    ID_ENCRYPTION_KEY); the booking's own copy is reduced to the last four characters. Staff see only the last four afterwards; managers can reveal the rest (logged)."""
    b, u = c.body, c.user
    id_type, id_no = b.get("id_type"), str(b.get("id_number") or "").strip()
    if id_type not in g.ID_TYPES:
        raise UserError("Please choose a valid ID proof type.")
    if not id_no or len(id_no) > 30:
        raise UserError("Please enter the ID proof number.")

    def work():
        first = get_booking(conn, u, c.args["id"])
        rows = [r for r in group_rows(conn, u, first["group_id"] or first["id"]) if r["status"] in LIVE_STATUS]
        if first["status"] not in LIVE_STATUS:
            raise UserError(f"A {first['status']} booking has no check-in to record an ID for.", 409)
        key, t = g.vault_key(conn), now_iso()
        for r in rows:
            conn.execute("INSERT INTO id_records (booking_id, id_type, last4, number_enc, captured_by, captured_name, captured_at) VALUES (?,?,?,?,?,?,?) "
                         "ON CONFLICT(booking_id) DO UPDATE SET id_type = excluded.id_type, last4 = excluded.last4, number_enc = excluded.number_enc, captured_by = excluded.captured_by, "
                         "captured_name = excluded.captured_name, captured_at = excluded.captured_at, purged_at = NULL",
                         (r["id"], id_type, id_no[-4:], idvault.seal(key, id_no.encode(), f"id:{r['id']}".encode()), u["id"], u["full_name"], t))
            conn.execute("UPDATE bookings SET id_type = ?, id_number = ? WHERE id = ?", (id_type, "****" + id_no[-4:], r["id"]))
        audit(conn, u, "id.capture", first["property_id"], first["room_id"], None, f"{id_type} recorded for booking #{first['group_id'] or first['id']} ({len(rows)} room(s))")
        return first["id"]
    return {"ok": True, "booking": row(get_booking(conn, u, g.write(conn, work))) | {"id_record": id_info(conn, [int(c.args["id"])]).get(int(c.args["id"]))}}


def id_image_upload(conn, c):
    """POST /api/bookings/<id>/id_image with the photo as the raw body (JPEG, PNG or WebP, at most 2.5 MB; the page shrinks phone photos first). Stored encrypted."""
    data = c.body.get("_raw") or b""
    mime = next((m for magic, m in IMAGE_MAGIC if data.startswith(magic) and (m != "image/webp" or data[8:12] == b"WEBP")), None)
    if not mime:
        raise UserError("The photo must be a JPEG, PNG or WebP image.")
    if len(data) > IMAGE_MAX:
        raise UserError("The photo is too large (at most 2.5 MB).", 413)

    def work():
        first = get_booking(conn, c.user, c.args["id"])
        rows = [r for r in group_rows(conn, c.user, first["group_id"] or first["id"]) if r["status"] in LIVE_STATUS]
        have = {r[0] for r in conn.execute(f"SELECT booking_id FROM id_records WHERE booking_id IN ({marks([r['id'] for r in rows])}) AND number_enc IS NOT NULL", [r["id"] for r in rows])}
        if not have:
            raise UserError("Record the ID type and number first, then add the photo.", 409)
        key = g.vault_key(conn)
        for r in rows:
            if r["id"] in have:
                conn.execute("UPDATE id_records SET image_enc = ?, image_mime = ? WHERE booking_id = ?", (idvault.seal(key, data, f"img:{r['id']}".encode()), mime, r["id"]))
        audit(conn, c.user, "id.photo", first["property_id"], first["room_id"], None, f"ID photo stored for booking #{first['group_id'] or first['id']}")
    g.write(conn, work)
    return {"ok": True}


def id_record_for(conn, c, what):
    first = get_booking(conn, c.user, c.args["id"])
    rec = conn.execute("SELECT * FROM id_records WHERE booking_id = ?", (first["id"],)).fetchone()
    if not rec or rec["purged_at"] or (what == "number" and rec["number_enc"] is None) or (what == "image" and rec["image_enc"] is None):
        raise UserError("There is no stored ID for this booking (it was never recorded, or it was erased under the retention setting).", 404)
    return first, rec


def id_reveal(conn, c):
    """POST /api/bookings/<id>/id_reveal {reason}: the full ID number, for a manager, with a reason. Always written to the audit log."""
    reason = text(c.body, "reason", "Reason", 3, 200)
    first, rec = id_record_for(conn, c, "number")
    try:
        number = idvault.open_(g.vault_key(conn), bytes(rec["number_enc"]), f"id:{first['id']}".encode()).decode()
    except idvault.VaultError:
        raise UserError("The stored ID could not be read (the encryption key may have changed). Ask the administrator.", 500)
    g.write(conn, lambda: audit(conn, c.user, "id.reveal", first["property_id"], first["room_id"], None, f"Number of booking #{first['id']} shown", reason))
    return {"id_type": rec["id_type"], "id_number": number, "has_image": rec["image_enc"] is not None, "captured_by": rec["captured_name"], "captured_at": rec["captured_at"]}


def id_image_get(conn, c):
    """GET /api/bookings/<id>/id_image?reason=: the stored photo, for a manager, with a reason. Logged."""
    reason = text(c.q, "reason", "Reason", 3, 200)
    first, rec = id_record_for(conn, c, "image")
    try:
        data = idvault.open_(g.vault_key(conn), bytes(rec["image_enc"]), f"img:{first['id']}".encode())
    except idvault.VaultError:
        raise UserError("The stored photo could not be read (the encryption key may have changed). Ask the administrator.", 500)
    g.write(conn, lambda: audit(conn, c.user, "id.view_image", first["property_id"], first["room_id"], None, f"Photo of booking #{first['id']} shown", reason))
    return {"_bytes": data, "_ctype": rec["image_mime"] or "image/jpeg"}


def record_payment(conn, c):
    """POST /api/bookings/<id>/payment {amount, mode}: money taken at the counter, recorded so the booking reconciles."""
    amount, mode = dec(c.body.get("amount"), "Amount", 0.01, 10_000_000), c.body.get("mode")
    if mode not in ("cash", "upi", "card"):
        raise UserError("Please choose how it was paid: cash, UPI or card.")

    def work():
        first = get_booking(conn, c.user, c.args["id"])
        gid = first["group_id"] or first["id"]
        if first["status"] == "Cancelled":
            raise UserError("A cancelled booking cannot take a payment.", 409)
        g.payments.record_counter(conn, gid, amount, mode, c.user["id"])
        audit(conn, c.user, "payment.counter", first["property_id"], None, None, f"booking #{gid}: Rs {amount} by {mode}")
        return gid
    gid = g.write(conn, work)
    return {"ok": True, "money": g.payments.summary(conn, gid, float(g.money(sum(r["amount"] or 0 for r in g.payments.group_rows(conn, gid) if r["status"] != "Cancelled"))))}


# ---------- housekeeping board ----------
BLOCKING = ("internal_block", "maintenance", "out_of_service")


def housekeeping_view(conn, c):
    """GET /api/housekeeping?property_id=: every room in one of: blocked, to_clean, cleaning, ready, occupied. 'ready' is a vacant room nobody needs to clean or has blocked.
    Rooms with a guest arriving today that are not ready are flagged."""
    ids, t = scope(c.user, c.q.get("property_id")), g.today().isoformat()
    rooms = conn.execute(f"SELECT r.id, r.number, r.floor, r.property_id, p.name AS property, t.name AS type FROM rooms r JOIN properties p ON p.id = r.property_id JOIN room_types t ON t.id = r.room_type_id "
                         f"WHERE r.active = 1 AND r.property_id IN ({marks(ids)}) ORDER BY p.id, r.floor, r.number", ids).fetchall()
    rid = [r["id"] for r in rooms]
    ev = defaultdict(list)
    for e in active_events(conn, rid, t):
        ev[e["room_id"]].append(e)
    occupied = {b["room_id"]: b for b in conn.execute(f"SELECT room_id, guest_name, check_out FROM bookings WHERE status = 'Checked-In' AND room_id IN ({marks(rid)})", rid)}
    arriving = {b["room_id"]: b for b in conn.execute(f"SELECT room_id, guest_name, arrival_time FROM bookings WHERE status = 'Confirmed' AND check_in = ? AND room_id IN ({marks(rid)})", (t, *rid))}
    ready_since = {r[0]: r[1] for r in conn.execute(f"SELECT room_id, MAX(closed_at) FROM room_status_events WHERE active = 0 AND kind IN ('cleaning_required', 'in_cleaning') AND room_id IN ({marks(rid)}) GROUP BY room_id", rid)}
    out = []
    for r in rooms:
        block = next((e for e in ev[r["id"]] if e["kind"] in BLOCKING), None)
        clean = next((e for e in ev[r["id"]] if e["kind"] in CLEANING), None)
        state = "blocked" if block else "cleaning" if clean and clean["kind"] == "in_cleaning" else "to_clean" if clean else "occupied" if r["id"] in occupied else "ready"
        e = block or clean
        arr = arriving.get(r["id"])
        out.append(dict(r) | {"state": state, "event_id": e["id"] if e else None, "kind": e["kind"] if e else None, "reason": e["reason"] if e else None, "until": e["end_date"] if e else None,
                              "guest": occupied[r["id"]]["guest_name"] if r["id"] in occupied else None, "ready_since": ready_since.get(r["id"]) if state == "ready" else None,
                              "arrival": {"guest": arr["guest_name"], "time": arr["arrival_time"]} if arr else None, "flag": bool(arr and state not in ("ready",))})
    counts = defaultdict(int)
    for r in out:
        counts[r["state"]] += 1
    return {"today": t, "rooms": out, "counts": dict(counts)}


def room_needs_cleaning(conn, c):
    """POST /api/rooms/<id>/needs_cleaning: marks a room as waiting for cleaning (for example a room left without a check-out)."""
    def work():
        room = conn.execute("SELECT * FROM rooms WHERE id = ? AND active = 1", (g.to_int(c.args["id"], "Invalid id.", 1, 10**12),)).fetchone()
        if not room or room["property_id"] not in c.user["props"]:
            raise UserError("Room not found.", 404)
        if conn.execute("SELECT 1 FROM room_status_events WHERE room_id = ? AND active = 1 AND kind IN ('cleaning_required', 'in_cleaning', 'internal_block', 'maintenance', 'out_of_service')", (room["id"],)).fetchone():
            raise UserError(f"Room {room['number']} already has a status: clear it first.", 409)
        conn.execute("INSERT INTO room_status_events (room_id, kind, reason, start_date, created_by, created_at) VALUES (?, 'cleaning_required', ?, ?, ?, ?)",
                     (room["id"], str(c.body.get("reason") or "Marked for cleaning").strip()[:200] or "Marked for cleaning", g.today().isoformat(), c.user["id"], now_iso()))
        audit(conn, c.user, "room.cleaning_required", room["property_id"], room["id"], "available", "cleaning_required")
    g.write(conn, work)
    return {"ok": True}


# ---------- call-backs: workflow and follow-up notes ----------
CALLBACK_FLOW = {"new": ("contacted", "closed"), "contacted": ("follow_up", "closed"), "follow_up": ("contacted", "closed"), "closed": ("follow_up",)}  # closed -> follow_up reopens
CALLBACK_LABEL = {"new": "New", "contacted": "Contacted", "follow_up": "Follow-up", "closed": "Closed"}


def callback_notes_for(conn, ids):
    out = defaultdict(list)
    for n in conn.execute(f"SELECT id, callback_id, user_name, note, status_to, created_at FROM callback_notes WHERE callback_id IN ({marks(ids)}) ORDER BY id", ids):
        out[n["callback_id"]].append({k: n[k] for k in ("id", "user_name", "note", "status_to", "created_at")})
    return out


def callback_get(conn, user, cid):
    r = conn.execute("SELECT * FROM callback_requests WHERE id = ?", (g.to_int(cid, "Invalid id.", 1, 10**12),)).fetchone()
    if not r or (r["property_id"] is not None and r["property_id"] not in user["props"]):
        raise UserError("Call-back request not found.", 404)
    return r


def callback_view(conn, cid):
    r = dict(conn.execute(CALLBACK_SQL + " WHERE c.id = ?", (cid,)).fetchone())
    return r | {"notes": callback_notes_for(conn, [cid])[cid], "next": list(CALLBACK_FLOW[r["status"]]), "overdue": bool(r["status"] == "follow_up" and r["follow_up_on"] and r["follow_up_on"] < g.today().isoformat())}


def callback_status(conn, c):
    """POST /api/callbacks/<id>/status {status, note, follow_up_on}. The workflow is new -> contacted -> follow_up <-> contacted -> closed (a closed request can be reopened
    as follow_up). Closing and reopening need a note (the outcome); follow_up needs the date to call again. Every change is kept as a timestamped note with the staff member's name."""
    new, note = c.body.get("status"), str(c.body.get("note") or "").strip()
    if new not in CALLBACK_LABEL or new == "new":
        raise UserError("Please choose a valid status.")
    if len(note) > 500:
        raise UserError("A note can be up to 500 characters.")
    due = None
    if new == "follow_up":
        due = day(c.body.get("follow_up_on"), "Follow-up date")
        if due < g.today() or due > g.today() + timedelta(days=90):
            raise UserError("The follow-up date must be from today up to 90 days ahead.")
        due = due.isoformat()

    def work():
        r = callback_get(conn, c.user, c.args["id"])
        if new not in CALLBACK_FLOW[r["status"]]:
            raise UserError(f"A {CALLBACK_LABEL[r['status']].lower()} request cannot go to {CALLBACK_LABEL[new].lower()}. Next steps: " + ", ".join(CALLBACK_LABEL[s].lower() for s in CALLBACK_FLOW[r["status"]]) + ".", 409)
        if (new == "closed" or r["status"] == "closed") and len(note) < 3:
            raise UserError("Please add a note: how it ended (when closing) or why it is reopened.")
        conn.execute("UPDATE callback_requests SET status = ?, handled_by = ?, handled_at = ?, follow_up_on = ? WHERE id = ?", (new, c.user["id"], now_iso(), due, r["id"]))
        conn.execute("INSERT INTO callback_notes (callback_id, user_id, user_name, note, status_to, created_at) VALUES (?,?,?,?,?,?)", (r["id"], c.user["id"], c.user["full_name"], note, new, now_iso()))
        audit(conn, c.user, f"callback.{new}", r["property_id"], None, r["status"], new, f"Call-back #{r['id']}: {r['name']}, {r['phone']}" + (f". Follow up on {due}" if due else ""))
        return r["id"]
    return callback_view(conn, g.write(conn, work))


def callback_note(conn, c):
    """POST /api/callbacks/<id>/note {note}: a follow-up note on a request, kept with the time and the staff member's name."""
    note = text(c.body, "note", "Note", 1, 500)

    def work():
        r = callback_get(conn, c.user, c.args["id"])
        conn.execute("INSERT INTO callback_notes (callback_id, user_id, user_name, note, status_to, created_at) VALUES (?,?,?,?,NULL,?)", (r["id"], c.user["id"], c.user["full_name"], note, now_iso()))
        audit(conn, c.user, "callback.note", r["property_id"], None, None, f"Call-back #{r['id']}: {r['name']}")
        return r["id"]
    return callback_view(conn, g.write(conn, work))


# ---------- payments, refunds and the guest message log (managers) ----------
def money_scope(conn, user, bid):
    first = get_booking(conn, user, bid)
    return first, first["group_id"] or first["id"]


def payments_reconcile(conn, c):
    """GET /api/payments/reconcile?start=&end=&property_id=: every booking checking in during the period against the money recorded, with flags and totals."""
    ids = scope(c.user, c.q.get("property_id"))
    start = day(c.q.get("start") or (g.today() - timedelta(days=7)).isoformat(), "Start date")
    end = day(c.q.get("end") or (g.today() + timedelta(days=30)).isoformat(), "End date")
    if end < start or (end - start).days > 366:
        raise UserError("The period must be at most a year, and end after it starts.")
    out = g.payments.reconcile(conn, ids, start.isoformat(), end.isoformat())
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM properties")}
    return out | {"start": start.isoformat(), "end": end.isoformat(), "rows": [r | {"property": names.get(r["property_id"])} for r in out["rows"]]}


def refunds_list(conn, c):
    ids, status = scope(c.user, c.q.get("property_id")), c.q.get("status") or None
    if status and status not in ("requested", "sending", "pending", "processed", "failed", "manual"):
        raise UserError("Please choose a valid status.")
    sql = ("SELECT r.id, r.group_id, r.amount_paise, r.status, r.reason, r.attempts, r.error, r.created_at, r.updated_at, p.provider, p.kind, p.method, "
           "(SELECT MIN(guest_name) FROM bookings b WHERE COALESCE(b.group_id, b.id) = r.group_id) AS guest FROM refunds r JOIN payments p ON p.id = r.payment_row "
           f"WHERE r.group_id IN (SELECT COALESCE(group_id, id) FROM bookings WHERE property_id IN ({marks(ids)}))")
    args = list(ids)
    if status:
        sql += " AND r.status = ?"
        args.append(status)
    return [dict(r) | {"amount": g.payments.rupees(r["amount_paise"])} for r in conn.execute(sql + " ORDER BY r.id DESC LIMIT 200", args)]


def refund_action(conn, c, done):
    note = str(c.body.get("note") or "").strip()[:200]
    if done and len(note) < 3:
        raise UserError("Please say how it was refunded (for example the bank reference).")
    rf = conn.execute("SELECT r.*, (SELECT MIN(property_id) FROM bookings b WHERE COALESCE(b.group_id, b.id) = r.group_id) AS pid FROM refunds r WHERE r.id = ?", (g.to_int(c.args["id"], "Invalid id.", 1, 10**12),)).fetchone()
    if not rf or rf["pid"] not in c.user["props"]:
        raise UserError("Refund not found.", 404)
    g.payments.refund_manual(conn, c.user, rf["id"], done, note)
    g.write(conn, lambda: audit(conn, c.user, "refund.done" if done else "refund.retry", rf["pid"], None, rf["status"], "processed" if done else "requested", f"Refund #{rf['id']} of Rs {g.payments.rupees(rf['amount_paise']):.2f}: {note}"))
    return {"ok": True}


def manual_refund(conn, c):
    """POST /api/payments/refund {booking_id, amount, reason}: a refund the policy did not give (goodwill, an overcharge), taken from what the booking still holds."""
    amount, reason = g.payments.to_paise(dec(c.body.get("amount"), "Amount", 0.01, 10_000_000)), text(c.body, "reason", "Reason", 3, 200)
    first, gid = money_scope(conn, c.user, c.body.get("booking_id"))

    def work():
        left = g.payments.money_state(conn, gid)["net"]
        if amount > left:
            raise UserError(f"Only Rs {g.payments.rupees(max(left, 0)):.2f} is held for this booking, so a larger refund is not possible.", 409)
        pays = conn.execute("SELECT * FROM payments WHERE group_id = ? AND status = 'paid' ORDER BY id DESC", (gid,)).fetchall()
        due, made = amount, []
        for p in pays:
            room = p["amount_paise"] - conn.execute("SELECT COALESCE(SUM(amount_paise), 0) FROM refunds WHERE payment_row = ? AND status != 'failed'", (p["id"],)).fetchone()[0]
            part = min(due, max(room, 0))
            if part <= 0:
                continue
            t = now_iso()
            made.append(conn.execute("INSERT INTO refunds (payment_row, group_id, amount_paise, status, reason, dedupe_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                                     (p["id"], gid, part, "manual" if p["provider"] == "counter" else "requested", f"By {c.user['full_name']}: {reason}", f"manual:{gid}:{p['id']}:{secrets.token_hex(4)}", t, t)).lastrowid)
            due -= part
        g.payments.apply_status(conn, gid)
        audit(conn, c.user, "refund.manual", first["property_id"], None, None, f"booking #{gid}: Rs {g.payments.rupees(amount):.2f}", reason)
        return made
    made = g.write(conn, work)
    g.payments.process_refunds(conn)
    return {"ok": True, "refunds": made}


REPORT_PARAMS = ("date", "from", "to")


def report_args(c, src):
    ids = scope(c.user, src.get("property_id"))
    return ids, {k: str(src.get(k) or "")[:10] for k in REPORT_PARAMS}


def report_list(conn, c):
    return [{"key": k, "label": label} for k, (_, label) in g.reports.REPORTS.items()]


def report_data(conn, c):
    """GET /api/reports/data?report=&date=&from=&to=&property_id=: one report as {title, columns, rows}."""
    ids, params = report_args(c, c.q)
    return g.reports.build(conn, ids, str(c.q.get("report") or ""), params)


def report_csv(conn, c):
    """GET /api/reports/csv?report=...: the same table as a CSV download. Logged: reports can hold guest names and mobile numbers."""
    ids, params = report_args(c, c.q)
    name = str(c.q.get("report") or "")
    t = g.reports.build(conn, ids, name, params)
    g.write(conn, lambda: audit(conn, c.user, "report.export", None, None, None, f"{name} as CSV, {len(t['rows'])} row(s)"))
    return {"_bytes": g.reports.to_csv(t), "_ctype": "text/csv", "_filename": f"{name}-{g.today().isoformat()}.csv"}


def report_export(conn, c):
    """POST /api/reports/export {report, date, from, to, property_id, target}: to Google Sheets when it is configured; otherwise {exported: "csv"} and the page downloads the CSV."""
    ids, params = report_args(c, c.body)
    name = str(c.body.get("report") or "")
    t = g.reports.build(conn, ids, name, params)
    out = g.reports.export_sheets(t, g.reports.REPORTS[name][1])
    g.write(conn, lambda: audit(conn, c.user, "report.export", None, None, None, f"{name} to {'Google Sheets' if out['exported'] == 'sheets' else 'CSV (Sheets not available)'}, {len(t['rows'])} row(s)"))
    return out


def messages_view(conn, c):
    return g.notifications.log_view(conn, scope(c.user, c.q.get("property_id")), c.q)


# ---------- sevas, packages, festivals, panchanga (super admin) ----------
def opt_range(v, label, lo, hi):
    return None if str(v if v is not None else "").strip() == "" else g.to_int(v, f"{label} must be a whole number from {lo} to {hi}, or empty.", lo, hi)


def admin_seva(conn, c):
    b = c.body
    name, desc = text(b, "name", "Name", 1, 100), text(b, "description", "Description", 0, 500)
    price, cap, pos = dec(b.get("price"), "Price", 0, 1_000_000), opt_range(b.get("max_per_day"), "Places per day", 1, 10_000), g.to_int(b.get("position") or 0, "Order must be a whole number.", 0, 1000)
    per_guest = 1 if b.get("per_guest") is True else 0

    def work():
        if b.get("id"):
            if not conn.execute("SELECT 1 FROM sevas WHERE id = ?", (b["id"],)).fetchone():
                raise UserError("Seva not found.", 404)
            conn.execute("UPDATE sevas SET name = ?, description = ?, price = ?, max_per_day = ?, per_guest = ?, position = ? WHERE id = ?", (name, desc or None, price, cap, per_guest, pos, b["id"]))
            sid = b["id"]
        else:
            sid = conn.execute("INSERT INTO sevas (name, description, price, max_per_day, per_guest, position) VALUES (?,?,?,?,?,?)", (name, desc or None, price, cap, per_guest, pos)).lastrowid
        audit(conn, c.user, "admin.seva", None, None, None, f"{name}, Rs {price}" + (f", {cap} a day" if cap else ""))
        return sid
    return {"id": g.write(conn, work)}


def admin_package(conn, c):
    b = c.body
    name, desc = text(b, "name", "Name", 1, 100), text(b, "description", "Description", 0, 500)
    disc, nights = dec(b.get("discount_percent") or 0, "Discount (%)", 0, 100), opt_range(b.get("min_nights"), "Minimum nights", 1, 60)
    items = b.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 10 or not all(isinstance(i, dict) for i in items):
        raise UserError("A package needs between 1 and 10 sevas.")
    clean = [(g.to_int(i.get("seva_id"), "Please choose a seva.", 1, 10**9), g.to_int(i.get("day_offset") or 0, "The day must be a whole number from 0 to 30.", 0, 30), 1 if i.get("per_guest") is True else 0) for i in items]

    def work():
        for sid, _, _ in clean:
            if not conn.execute("SELECT 1 FROM sevas WHERE id = ?", (sid,)).fetchone():
                raise UserError("Please choose a seva that exists.")
        if b.get("id"):
            if not conn.execute("SELECT 1 FROM packages WHERE id = ?", (b["id"],)).fetchone():
                raise UserError("Package not found.", 404)
            conn.execute("UPDATE packages SET name = ?, description = ?, discount_percent = ?, min_nights = ? WHERE id = ?", (name, desc or None, disc, nights, b["id"]))
            pid = b["id"]
            conn.execute("DELETE FROM package_items WHERE package_id = ?", (pid,))
        else:
            pid = conn.execute("INSERT INTO packages (name, description, discount_percent, min_nights) VALUES (?,?,?,?)", (name, desc or None, disc, nights)).lastrowid
        conn.executemany("INSERT OR REPLACE INTO package_items (package_id, seva_id, day_offset, per_guest) VALUES (?,?,?,?)", [(pid, *x) for x in clean])
        audit(conn, c.user, "admin.package", None, None, None, f"{name}: {len(clean)} seva(s), {disc}% off")
        return pid
    return {"id": g.write(conn, work)}


def admin_festival(conn, c):
    """Saves a festival. With make_peak the festival also owns a peak period (surcharge and limits) for its dates, so the guest price and booking limits follow the calendar;
    unticking it, or deactivating the festival, switches that peak period off."""
    b = c.body
    name, note = text(b, "name", "Name", 1, 100), text(b, "note", "Note", 0, 300)
    kind = b.get("kind") if b.get("kind") in g.extras.FESTIVAL_KINDS else None
    if not kind:
        raise UserError("Please choose a valid kind.")
    start, end = day(b.get("start_date"), "Start date"), day(b.get("end_date") or b.get("start_date"), "End date")
    if end < start:
        raise UserError("The end date must not be before the start date.")
    peak = b.get("make_peak") is True
    sur = dec(b.get("surcharge_percent") or 0, "Surcharge (%)", 0, 300) if peak else None
    mn, mr = (opt_range(b.get("max_nights"), "Most nights", 1, 60), opt_range(b.get("max_rooms"), "Most rooms", 1, 20)) if peak else (None, None)

    def work():
        old = conn.execute("SELECT * FROM festivals WHERE id = ?", (b["id"],)).fetchone() if b.get("id") else None
        if b.get("id") and not old:
            raise UserError("Festival not found.", 404)
        pid = old["peak_period_id"] if old else None
        if peak:
            args = (name, start.isoformat(), end.isoformat(), mn, mr, note or None, float(sur))
            if pid:
                conn.execute("UPDATE peak_periods SET name = ?, start_date = ?, end_date = ?, max_nights = ?, max_rooms = ?, note = ?, surcharge_percent = ?, active = 1 WHERE id = ?", (*args, pid))
            else:
                pid = conn.execute("INSERT INTO peak_periods (name, start_date, end_date, max_nights, max_rooms, note, surcharge_percent, active) VALUES (?,?,?,?,?,?,?,1)", args).lastrowid
        elif pid:
            conn.execute("UPDATE peak_periods SET active = 0 WHERE id = ?", (pid,))
        if old:
            conn.execute("UPDATE festivals SET name = ?, kind = ?, start_date = ?, end_date = ?, note = ?, peak_period_id = ? WHERE id = ?", (name, kind, start.isoformat(), end.isoformat(), note or None, pid, old["id"]))
            fid = old["id"]
        else:
            fid = conn.execute("INSERT INTO festivals (name, kind, start_date, end_date, note, peak_period_id) VALUES (?,?,?,?,?,?)", (name, kind, start.isoformat(), end.isoformat(), note or None, pid)).lastrowid
        audit(conn, c.user, "admin.festival", None, None, None, f"{name}, {start} to {end}" + (f", peak pricing +{sur}%" if peak else ""))
        return fid
    return {"id": g.write(conn, work)}


def admin_extra_active(conn, c):
    """POST /api/admin/extras/<kind>/<id>/active {active}: switches a seva, package or festival on or off (a festival's peak period goes with it)."""
    kind, on = c.args["kind"], 1 if c.body.get("active") is True else 0

    def work():
        r = conn.execute(f"SELECT * FROM {kind} WHERE id = ?", (c.args["id"],)).fetchone()
        if not r:
            raise UserError("Not found.", 404)
        conn.execute(f"UPDATE {kind} SET active = ? WHERE id = ?", (on, r["id"]))
        if kind == "festivals" and r["peak_period_id"]:
            conn.execute("UPDATE peak_periods SET active = ? WHERE id = ?", (on, r["peak_period_id"]))
        audit(conn, c.user, f"admin.{kind}_active", None, None, str(r["active"]), str(on), r["name"])
    g.write(conn, work)
    return {"ok": True}


def admin_panchanga(conn, c):
    """POST /api/admin/panchanga {day, tithi, nakshatra, note} or {delete: day} or {import: text}. Entries are what staff publish; nothing is calculated or assumed."""
    b = c.body

    def clean(day_, tithi, nak, note):
        d = day(day_, "Date").isoformat()
        vals = [str(x or "").strip() for x in (tithi, nak, note)]
        if any(len(x) > 100 for x in vals) or not any(vals):
            raise UserError("Please give the tithi, nakshatra or a note (up to 100 characters each).")
        return d, *vals

    def work():
        if b.get("delete"):
            conn.execute("DELETE FROM panchanga WHERE day = ?", (day(b["delete"], "Date").isoformat(),))
            n = 1
        elif "import" in b:
            lines = [ln for ln in str(b["import"]).splitlines() if ln.strip()]
            if len(lines) > 400:
                raise UserError("Please import at most 400 lines at a time.")
            for i, ln in enumerate(lines, 1):
                parts = [x.strip() for x in next(__import__("csv").reader([ln]))] + ["", "", ""]
                try:
                    row_ = clean(*parts[:4])
                except UserError as e:
                    raise UserError(f"Line {i}: {e}")
                conn.execute("INSERT OR REPLACE INTO panchanga (day, tithi, nakshatra, note) VALUES (?,?,?,?)", row_)
            n = len(lines)
        else:
            conn.execute("INSERT OR REPLACE INTO panchanga (day, tithi, nakshatra, note) VALUES (?,?,?,?)", clean(b.get("day"), b.get("tithi"), b.get("nakshatra"), b.get("note")))
            n = 1
        audit(conn, c.user, "admin.panchanga", None, None, None, f"{n} entr{'y' if n == 1 else 'ies'}")
    g.write(conn, work)
    return {"ok": True}


def extras_admin_view(conn):
    packages = [dict(p) | {"items": [dict(i) for i in conn.execute("SELECT i.seva_id, s.name, i.day_offset, i.per_guest FROM package_items i JOIN sevas s ON s.id = i.seva_id WHERE i.package_id = ? ORDER BY i.day_offset", (p["id"],))]}
                for p in conn.execute("SELECT * FROM packages ORDER BY active DESC, id")]
    fest = [dict(f) | {"peak": dict(conn.execute("SELECT surcharge_percent, max_nights, max_rooms, active FROM peak_periods WHERE id = ?", (f["peak_period_id"],)).fetchone()) if f["peak_period_id"] else None}
            for f in conn.execute("SELECT * FROM festivals ORDER BY active DESC, start_date DESC, id DESC")]
    return {"sevas": [dict(r) for r in conn.execute("SELECT * FROM sevas ORDER BY active DESC, position, id")], "packages": packages, "festivals": fest,
            "panchanga": [dict(r) for r in conn.execute("SELECT * FROM panchanga WHERE day >= ? ORDER BY day LIMIT 400", ((g.today() - timedelta(days=7)).isoformat(),))], "festival_kinds": list(g.extras.FESTIVAL_KINDS)}


# ---------- notices: weather and river level ----------
def notices_list(conn, c):
    since = (g.today() - timedelta(days=30)).isoformat()
    return [dict(r) for r in conn.execute("SELECT * FROM notices WHERE active = 1 OR COALESCE(ends_on, starts_on) >= ? ORDER BY active DESC, starts_on DESC, id DESC LIMIT 100", (since,))]


def notice_save(conn, c):
    """POST /api/notices {id?, kind, level, text, starts_on, ends_on?, active}: a weather or river-level notice, shown on the guest site and in the confirmations of stays it covers."""
    b = c.body
    if b.get("kind") not in g.extras.NOTICE_KINDS or b.get("level") not in g.extras.NOTICE_LEVELS:
        raise UserError("Please choose a valid kind and level.")
    body_text = text(b, "text", "The notice", 5, 300)
    start = day(b.get("starts_on") or g.today().isoformat(), "Start date")
    end = day(b["ends_on"], "End date") if b.get("ends_on") else None
    if end and (end < start or (end - start).days > 90):
        raise UserError("The end date must be on or after the start date, and within 90 days.")
    active = 0 if b.get("active") is False else 1

    def work():
        if b.get("id"):
            if not conn.execute("SELECT 1 FROM notices WHERE id = ?", (b["id"],)).fetchone():
                raise UserError("Notice not found.", 404)
            conn.execute("UPDATE notices SET kind = ?, level = ?, text = ?, starts_on = ?, ends_on = ?, active = ? WHERE id = ?", (b["kind"], b["level"], body_text, start.isoformat(), end.isoformat() if end else None, active, b["id"]))
            nid = b["id"]
        else:
            nid = conn.execute("INSERT INTO notices (kind, level, text, starts_on, ends_on, active, created_by, created_name, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                               (b["kind"], b["level"], body_text, start.isoformat(), end.isoformat() if end else None, active, c.user["id"], c.user["full_name"], now_iso())).lastrowid
        audit(conn, c.user, "notice.save", None, None, None, f"{b['kind']} {b['level']}: {body_text[:80]}" + ("" if active else " (switched off)"))
        return nid
    return {"id": g.write(conn, work)}


# ---------- feedback ----------
def feedback_list(conn, c):
    ids = scope(c.user, c.q.get("property_id"))
    sql, args = f"SELECT f.*, p.name AS property, (SELECT MIN(guest_name) FROM bookings b WHERE COALESCE(b.group_id, b.id) = f.group_id) AS guest FROM feedback f JOIN properties p ON p.id = f.property_id WHERE f.property_id IN ({marks(ids)})", list(ids)
    lo, hi = g.to_int(c.q.get("min") or 1, "Rating must be 1 to 5.", 1, 5), g.to_int(c.q.get("max") or 5, "Rating must be 1 to 5.", 1, 5)
    sql += " AND f.rating >= ? AND f.rating <= ?"
    args += [lo, hi]
    if c.q.get("status") in ("new", "reviewed"):
        sql += " AND f.status = ?"
        args.append(c.q["status"])
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY f.id DESC LIMIT 300", args)]
    allr = conn.execute(f"SELECT rating, cleanliness, service, property_id FROM feedback WHERE property_id IN ({marks(ids)})", ids).fetchall()
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM properties")}
    return {"rows": rows, "summary": {"count": len(allr), "rating": avg([r["rating"] for r in allr]), "cleanliness": avg([r["cleanliness"] for r in allr if r["cleanliness"]]), "service": avg([r["service"] for r in allr if r["service"]]),
                                      "distribution": {n: sum(1 for r in allr if r["rating"] == n) for n in (5, 4, 3, 2, 1)},
                                      "by_property": [{"property": names[pid], "count": len(xs), "rating": avg(xs)} for pid in sorted({r["property_id"] for r in allr}) for xs in [[r["rating"] for r in allr if r["property_id"] == pid]]]}}


def feedback_review(conn, c):
    note = text(c.body, "staff_note", "Note", 0, 300)

    def work():
        f = conn.execute("SELECT * FROM feedback WHERE id = ?", (g.to_int(c.args["id"], "Invalid id.", 1, 10**12),)).fetchone()
        if not f or f["property_id"] not in c.user["props"]:
            raise UserError("Feedback not found.", 404)
        conn.execute("UPDATE feedback SET status = 'reviewed', staff_note = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?", (note or None, c.user["full_name"], now_iso(), f["id"]))
        audit(conn, c.user, "feedback.review", f["property_id"], None, f["status"], "reviewed", f"Booking #{f['group_id']}: {f['rating']}/5")
        return f["group_id"]
    gid = g.write(conn, work)
    g.alert_resolve(conn, "feedback", f"low:{gid}")
    return {"ok": True}


# ---------- newsletter and broadcasts ----------
def broadcasts_view(conn, c):
    return {"counts": g.extras.counts(conn), "limit": g.extras.BROADCASTS_PER_DAY, "max": g.extras.BROADCAST_MAX_RECIPIENTS, "langs": list(g.LANGS), "whatsapp": bool(g.providers.env("WHATSAPP_PROVIDER")),
            "history": [dict(r) for r in conn.execute("SELECT id, title, channels, created_by_name, created_at, recipients, sent, failed FROM broadcasts ORDER BY id DESC LIMIT 50")],
            "festivals": [dict(r) for r in conn.execute("SELECT id, name, start_date, end_date FROM festivals WHERE active = 1 AND end_date >= ? ORDER BY start_date LIMIT 20", (g.today().isoformat(),))],
            "subscribers": [{"id": r["id"], "name": r["name"], "phone": g.notifications.mask_phone(r["phone"]), "email": g.notifications.mask_email(r["email"]), "sms": bool(r["sms"] and r["ph_ok"]), "whatsapp": bool(r["wa"] and r["ph_ok"]),
                             "email_ok": bool(r["em"] and r["em_ok"]), "pending_email": bool(r["em"] and not r["em_ok"]), "unsubscribed": bool(r["unsubscribed_at"]), "consent_at": r["consent_at"]}
                            for r in conn.execute("SELECT * FROM subscribers ORDER BY id DESC LIMIT 300")]}


def broadcast_send(conn, c):
    out = g.extras.send_broadcast(conn, c.user, c.body)
    g.write(conn, lambda: audit(conn, c.user, "broadcast.send", None, None, None, f"#{out['id']} {str(c.body.get('title'))[:60]}: {out['sent']} sent, {out['failed']} failed to {out['recipients']} people"))
    return out


def subscriber_unsubscribe(conn, c):
    """For a person who asks by phone to stop: the same effect as their unsubscribe link."""
    def work():
        r = conn.execute("SELECT id FROM subscribers WHERE id = ?", (g.to_int(c.args["id"], "Invalid id.", 1, 10**12),)).fetchone()
        if not r:
            raise UserError("Subscriber not found.", 404)
        conn.execute("UPDATE subscribers SET unsubscribed_at = COALESCE(unsubscribed_at, ?), sms = 0, wa = 0, em = 0 WHERE id = ?", (now_iso(), r["id"]))
        audit(conn, c.user, "broadcast.unsubscribe", None, None, None, f"Subscriber #{r['id']} removed at their request")
    g.write(conn, work)
    return {"ok": True}


R = lambda method, pattern, fn, roles: (method, re.compile(pattern + r"\Z"), fn, roles)
ROUTES = [R("POST", r"/api/login", login, None), R("POST", r"/api/logout", logout, ALL), R("GET", r"/api/meta", meta, ALL),
          R("GET", r"/api/dashboard", dashboard, ALL), R("GET", r"/api/rooms", rooms_view, ALL), R("GET", r"/api/bookings", bookings_list, FRONT),
          R("POST", r"/api/bookings", create_booking, DESK), R("GET", r"/api/availability", availability, DESK), R("GET", r"/api/room_events", events_list, ALL), R("POST", r"/api/room_events", create_event, DESK),
          R("POST", r"/api/room_events/(?P<id>\d+)/release", lambda conn, c: event_action(conn, c, True), CLEANERS),
          R("POST", r"/api/room_events/(?P<id>\d+)/start_cleaning", lambda conn, c: event_action(conn, c, False), CLEANERS),
          R("GET", r"/api/audit", audit_view, MGR),
          R("POST", r"/api/bookings/(?P<id>\d+)/edit", edit_booking, DESK), R("POST", r"/api/bookings/(?P<id>\d+)/reschedule", reschedule_booking, DESK),
          R("GET", r"/api/callbacks", callbacks_list, FRONT), R("POST", r"/api/callbacks/(?P<id>\d+)/status", callback_status, perm("callback.work")),
          R("POST", r"/api/callbacks/(?P<id>\d+)/note", callback_note, perm("callback.work")),
          R("GET", r"/api/checkin/lookup", desk_lookup, perm("booking.checkin")), R("POST", r"/api/bookings/(?P<id>\d+)/id", id_capture, perm("id.capture")),
          R("POST", r"/api/bookings/(?P<id>\d+)/id_image", id_image_upload, perm("id.capture")), R("POST", r"/api/bookings/(?P<id>\d+)/id_reveal", id_reveal, perm("id.reveal")),
          R("GET", r"/api/bookings/(?P<id>\d+)/id_image", id_image_get, perm("id.reveal")), R("POST", r"/api/bookings/(?P<id>\d+)/payment", record_payment, perm("payment.record")),
          R("GET", r"/api/housekeeping", housekeeping_view, CLEANERS), R("POST", r"/api/rooms/(?P<id>\d+)/needs_cleaning", room_needs_cleaning, perm("housekeeping.update")),
          R("POST", r"/api/admin/room_types/(?P<id>\d+)/photo", room_photo_upload, SUPER), R("POST", r"/api/admin/room_types/(?P<id>\d+)/photo_remove", room_photo_remove, SUPER),
          R("POST", r"/api/admin/sevas", admin_seva, SUPER), R("POST", r"/api/admin/packages", admin_package, SUPER), R("POST", r"/api/admin/festivals", admin_festival, SUPER), R("POST", r"/api/admin/panchanga", admin_panchanga, SUPER),
          R("POST", r"/api/admin/extras/(?P<kind>sevas|packages|festivals)/(?P<id>\d+)/active", admin_extra_active, SUPER),
          R("GET", r"/api/notices", notices_list, FRONT), R("POST", r"/api/notices", notice_save, perm("notice.edit")),
          R("GET", r"/api/feedback", feedback_list, perm("feedback.view")), R("POST", r"/api/feedback/(?P<id>\d+)/review", feedback_review, perm("feedback.view")),
          R("GET", r"/api/broadcasts", broadcasts_view, perm("broadcast.send")), R("POST", r"/api/broadcasts", broadcast_send, perm("broadcast.send")),
          R("POST", r"/api/subscribers/(?P<id>\d+)/unsubscribe", subscriber_unsubscribe, perm("broadcast.send")),
          R("GET", r"/api/reports/list", report_list, perm("report.view")), R("GET", r"/api/reports/data", report_data, perm("report.view")),
          R("GET", r"/api/reports/csv", report_csv, perm("report.export")), R("POST", r"/api/reports/export", report_export, perm("report.export")),
          R("GET", r"/api/payments/reconcile", payments_reconcile, perm("payment.view")), R("GET", r"/api/payments/refunds", refunds_list, perm("payment.view")),
          R("POST", r"/api/payments/refund", manual_refund, perm("payment.refund")), R("POST", r"/api/refunds/(?P<id>\d+)/done", lambda conn, c: refund_action(conn, c, True), perm("payment.refund")),
          R("POST", r"/api/refunds/(?P<id>\d+)/retry", lambda conn, c: refund_action(conn, c, False), perm("payment.refund")), R("GET", r"/api/notification_log", messages_view, perm("messages.view")),
          R("GET", r"/api/calendar", calendar_view, FRONT), R("GET", r"/api/guests", guests_list, FRONT), R("GET", r"/api/guests/profile", guest_profile, FRONT),
          R("POST", r"/api/guests/edit", guest_edit, MGR), R("GET", r"/api/reports", reports, MGR),
          R("GET", r"/api/notifications", notifications, ALL), R("POST", r"/api/notifications/seen", notifications_seen, ALL),
          R("GET", r"/api/room_types", rate_types, MGR), R("POST", r"/api/room_types", rate_save, SUPER), R("POST", r"/api/room_types/(?P<id>\d+)/delete", rate_delete, SUPER),  # room types are shared by every property: super admin only
          R("GET", r"/api/settings", settings_get, MGR), R("POST", r"/api/settings/property", property_save, MGR),
          R("POST", r"/api/settings/times", times_save, frozenset({"super_admin"})),
          R("GET", r"/api/admin/data", admin_data, SUPER), R("POST", r"/api/admin/properties", admin_property, SUPER),
          R("POST", r"/api/admin/room_types", rate_save, SUPER), R("POST", r"/api/admin/rooms", admin_room, SUPER),
          R("POST", r"/api/admin/users", admin_user, SUPER), R("POST", r"/api/admin/settings", admin_settings, SUPER),
          R("POST", r"/api/admin/peak_periods", admin_peak, SUPER), R("POST", r"/api/admin/site_config", admin_site_config, SUPER), R("POST", r"/api/admin/promos", admin_promo, SUPER), R("POST", r"/api/admin/faqs", admin_faq, SUPER),
          R("POST", r"/api/admin/(?P<kind>properties|room_types|rooms|users|peak_periods|faqs|promo_codes)/(?P<id>\d+)/active", admin_active, SUPER),
          R("POST", r"/api/me/profile", profile_save, ALL), R("POST", r"/api/me/password", password_change, ALL),
          R("POST", r"/api/me/totp/setup", totp_setup, ALL), R("POST", r"/api/me/totp/enable", totp_enable, ALL), R("POST", r"/api/admin/guest_erase", admin_guest_erase, SUPER)]
for _name, (_new, _from, _act, _roles) in ACTIONS.items():
    ROUTES.append(R("POST", rf"/api/bookings/(?P<id>\d+)/{_name}", (lambda n, f, a: lambda conn, c: set_status(conn, c, n, f, a))(_new, _from, _act), _roles))


def dispatch(method, path, query, body, ip, token=None, csrf=None, check_csrf=False):
    """-> (http_status, json_able, cookie_token). cookie_token: str to set, '' to clear, None to leave. Never raises.
    check_csrf=True (what the HTTP handler always passes) makes every signed-in POST carry the X-CSRF-Token header (`csrf`) that belongs to the session."""
    conn, c = None, None
    try:
        for m, rx, fn, roles in ROUTES:
            hit = rx.match(path) if m == method else None
            if hit:
                break
        else:
            return 404, {"error": "Not found."}, None
        body = body if isinstance(body, dict) and "_raw" in body else g.clean_body(body, 20_000) if body is not None else None
        conn = g.connect()
        user = load_user(conn, token) if roles else None
        if roles and user["role"] not in roles:
            raise UserError("Your role is not allowed to do this.", 403)
        if check_csrf and roles and method == "POST" and not g.csrf_ok(token, csrf):
            raise UserError("Your session could not be checked. Please refresh the page and sign in again.", 403)
        if user and TOTP_REQUIRED and user["role"] in TOTP_ROLES and not user["totp_enabled"] and path not in TOTP_OPEN:
            raise UserError("Set up two-factor sign-in first (Settings > Two-factor sign-in).", 403)
        c = Ctx(query, body, ip, user, hit.groupdict() | {"_token": token})
        return 200, fn(conn, c), c.token
    except UserError as e:
        return e.status, {"error": str(e)}, ("" if e.status == 401 and token else None)
    except Exception as e:
        if isinstance(e, g.sqlite3.OperationalError) and ("locked" in str(e) or "busy" in str(e)):
            return 503, {"error": "The database is busy. Please try again."}, None
        if isinstance(e, g.sqlite3.IntegrityError) and "already booked" in str(e):  # the database's own overlap guard
            return 409, {"error": "That room is already booked for those dates. Choose different dates or another room."}, None
        traceback.print_exc()
        return 500, {"error": "Something went wrong. Please try again."}, None
    finally:
        if conn:
            conn.close()


class Handler(BaseHTTPRequestHandler):
    timeout = g.SOCKET_TIMEOUT  # a stalled client is dropped instead of holding a thread

    def log_message(self, fmt, *args):
        """The default access log without the query string (guest mobile numbers and booking codes travel in it)."""
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), (fmt % args).split("?")[0]))

    def send(self, status, data, ctype="application/json", token=None):
        filename = None
        if isinstance(data, dict) and "_bytes" in data:  # a stored file (an ID photo) or a download (a CSV report)
            data, ctype, filename = data["_bytes"], data["_ctype"], data.get("_filename")
        data = data if isinstance(data, bytes) else json.dumps(data).encode()
        data, nonce = g.with_nonce(data, ctype)
        self.send_response(status)
        self.send_header("Content-Type", ctype + ("" if ctype.startswith("image/") else "; charset=utf-8"))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        for k, v in (*g.security_headers(self.headers, nonce), ("Cache-Control", "no-store")):
            self.send_header(k, v.replace("camera=()", "camera=(self)") if k == "Permissions-Policy" else v)  # the staff page scans guests' QR codes with the camera
        if token is not None:
            self.send_header("Set-Cookie", f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={TTL if token else 0}" + ("; Secure" if g.is_https(self.headers) else ""))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def token(self):
        m = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        return m.value if m else None

    def reply(self, method, url, body):
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        st, out, tok = dispatch(method, url.path, q, body, g.client_ip(self.headers, self.client_address[0]), self.token(), self.headers.get("X-CSRF-Token"), check_csrf=True)
        self.send(st, out, token=tok)

    def do_GET(self):
        bad = g.request_ok(self.headers, "GET", urlparse(self.path).path)
        if bad:
            return self.send(bad[0], {"error": bad[1]})
        url = urlparse(self.path)
        if url.path == "/health":
            return self.send(*g.dispatch("GET", "/health", {}, None, ""))
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            return self.send(200, (g.ROOT / "web" / name).read_bytes(), ctype)
        if url.path.startswith("/uploads/"):  # room photos, so the admin page can show them
            got = g.read_upload(url.path.rsplit("/", 1)[1])
            return self.send(200, got[0], got[1]) if got else self.send(404, b"", "text/plain")
        if url.path in IMAGES:
            return self.send(200, (g.ROOT / url.path.lstrip("/")).read_bytes(), IMAGES[url.path])
        self.reply("GET", url, None)

    do_HEAD = do_GET

    def do_POST(self):
        bad = g.request_ok(self.headers, "POST", urlparse(self.path).path)
        if bad:
            return self.send(bad[0], {"error": bad[1]})
        try:
            ctype = self.headers.get("Content-Type") or ""
            if ctype.startswith("image/") and re.fullmatch(r"/api/(bookings/\d+/id_image|admin/room_types/\d+/photo)", urlparse(self.path).path):  # the raw uploads: an ID photo, a room photo
                tok = self.token()  # signed in, with the right CSRF token, before a single byte of the photo is read
                if not (tok in SESSIONS and SESSIONS[tok][1] >= time.time() and g.csrf_ok(tok, self.headers.get("X-CSRF-Token"))):
                    return self.send(401, {"error": "Please sign in."})
                try:
                    n = g.content_length(self.headers, UPLOAD_MAX)
                except UserError:
                    return self.send(413, {"error": "The photo is too large (at most 2.5 MB)."})
                return self.reply("POST", urlparse(self.path), {"_raw": self.rfile.read(n)})
            if not ctype.startswith("application/json"):
                return self.send(415, {"error": "Some of the values sent are not valid."})
            n = g.content_length(self.headers, 20_000)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except UserError as e:
            return self.send(e.status, {"error": str(e)})
        except ValueError:
            return self.send(400, {"error": "Some of the values sent are not valid."})
        self.reply("POST", urlparse(self.path), body)


def set_password(username):
    pw = getpass.getpass("New password (at least 8 characters): ")
    if len(pw) < PW_MIN:
        print(f"Password must be at least {PW_MIN} characters.", file=sys.stderr)
        return 1
    if pw != getpass.getpass("Repeat password: "):
        print("Passwords do not match.", file=sys.stderr)
        return 1
    conn = g.connect()
    try:
        u = conn.execute("SELECT id, username, role FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
        if not u:
            print("No active user with that username.", file=sys.stderr)
            return 1
        h, s = make_hash(pw)
        g.write(conn, lambda: (conn.execute("UPDATE users SET pw_hash = ?, salt = ? WHERE id = ?", (h, s, u["id"])),
                               audit(conn, None, "user.password_set", reason="set from the command line", who=dict(u))))
    finally:
        conn.close()
    print(f"Password updated for {username}.")
    return 0


def main(argv):
    if argv[:1] == ["serve"]:
        port = int(argv[1]) if len(argv) > 1 else 8002
        problems = g.serve_checks()
        if problems:
            print("Refusing to start:\n- " + "\n- ".join(problems), file=sys.stderr)
            return 1
        print(f"Staff app on http://127.0.0.1:{port}")
        # Listens on localhost only: in production a reverse proxy (deploy/Caddyfile) terminates HTTPS; MANTRALAYAM_HTTPS=1 then adds Secure cookies and HSTS
        g.BoundedServer(("127.0.0.1", port), Handler).serve_forever()
        return 0
    if argv[:1] == ["set-password"] and len(argv) == 2:
        return set_password(argv[1])
    if argv[:1] == ["reset-totp"] and len(argv) == 2:  # a manager lost their phone: they set up the authenticator app again at the next sign-in
        conn = g.connect()
        try:
            n = g.write(conn, lambda: conn.execute("UPDATE users SET totp_secret = NULL, totp_enabled = 0, totp_step = 0 WHERE username = ?", (argv[1],)).rowcount)
        finally:
            conn.close()
        print("Two-factor sign-in reset." if n else "No such user.")
        return 0 if n else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
