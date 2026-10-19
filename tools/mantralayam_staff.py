"""Mantralayam staff app: login, today's operations, room board, bookings, room status, audit (stdlib only).

Usage:
  python tools/mantralayam_staff.py serve [PORT]            # default 8002, separate from the guest site (8001)
  python tools/mantralayam_staff.py set-password USERNAME   # prompts for a new password

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import mantralayam_rooms as g
from mantralayam_rooms import UserError

STATIC = {"/": ("staff.html", "text/html"), "/staff": ("staff.html", "text/html"), "/staff.css": ("staff.css", "text/css"),
          "/tokens.css": ("tokens.css", "text/css"), "/shared.css": ("shared.css", "text/css")}
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
ROUNDS = 240_000
# ponytail: the lost original code's PBKDF2 settings are unknown. Old rows are tried with these; new hashes are self-describing
# ("<rounds>$<salt>" in the salt column), so a wrong guess only means `set-password` is needed, never a lockout of the app.
LEGACY_ROUNDS = int(os.environ.get("STAFF_LEGACY_ROUNDS", 200_000))
SESSIONS = {}  # ponytail: in memory, restarting the server signs everyone out
FAILS = defaultdict(list)


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
    if not s or s[1] < time.time():
        SESSIONS.pop(token, None)
        raise UserError("Please sign in.", 401)
    u = conn.execute("SELECT id, username, full_name, role, active FROM users WHERE id = ?", (s[0],)).fetchone()
    if not u or not u["active"]:
        SESSIONS.pop(token, None)
        raise UserError("Your account is no longer active.", 401)
    SESSIONS[token] = (s[0], time.time() + TTL)
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
    key, now = (c.ip, name.lower()), time.time()
    FAILS[key] = [t for t in FAILS[key] if now - t < 600]
    if len(FAILS[key]) >= 5:
        raise UserError("Too many failed attempts. Please wait a few minutes and try again.", 429)
    u = conn.execute("SELECT * FROM users WHERE username = ? AND active = 1", (name,)).fetchone()
    ok = check_pw(str(c.body.get("password") or ""), u["pw_hash"] if u else DUMMY[0], u["salt"] if u else DUMMY[1])  # same work either way
    if not (u and ok):
        FAILS[key].append(now)
        g.write(conn, lambda: audit(conn, None, "auth.login_failed", reason=f"username: {name}", who={"username": name}))
        raise UserError("Incorrect username or password.", 401)
    FAILS.pop(key, None)
    c.token = secrets.token_urlsafe(32)
    SESSIONS[c.token] = (u["id"], now + TTL)
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
    return {"user": {k: u[k] for k in ("id", "username", "full_name", "role")}, "today": g.today().isoformat(), "settings": {
        k: v for k, v in g.settings(conn).items() if k in ("checkin_time", "checkout_time")},
        "properties": [dict(r) for r in conn.execute(f"SELECT id, name FROM properties WHERE id IN ({marks(u['props'])}) ORDER BY id", u["props"])],
        "room_types": [dict(r) for r in conn.execute("SELECT id, name FROM room_types ORDER BY tariff")],
        "id_types": list(g.ID_TYPES), "issues": list(ISSUES), "kinds": list(KINDS),
        "can": {"book": u["role"] in DESK, "status": u["role"] in DESK, "clean": u["role"] in CLEANERS, "audit": u["role"] in MGR}}


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
               "b.cancel_reason, b.source, b.remarks, b.group_id, b.amount, r.number AS room, p.name AS property "
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
            "rooms": dict(counts), "total_rooms": len(rooms)}


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
    return f"Room {room['number']} is {LABEL[e['kind']]} for those dates. Choose different dates or another room." if e else f"Room {room['number']} is not available."


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
                           "created_at, status_at, source, payment_method, payment_status, amount) VALUES (?,?,?,?,?,?,?,?,?,?,'Confirmed',?,?,?,'front_desk','pay_at_counter','due',?)",
                           (pid, room["id"], name, phone, b["id_type"], id_no, guests, ci.isoformat(), co.isoformat(), remarks or None, u["id"], now, now,
                            room["tariff"] * (co - ci).days))
        audit(conn, u, "booking.create", pid, room["id"], None, f"#{cur.lastrowid}: {name}, {ci} to {co}, {guests} guests")
        return cur.lastrowid
    return row(get_booking(conn, u, g.write(conn, work)))


def set_status(conn, c, new, allowed_from, action):
    """Shared by check-in, check-out, no-show and cancel: one transaction, status guard, audit row."""
    def work():
        b = get_booking(conn, c.user, c.args["id"])
        if b["status"] not in allowed_from:
            raise UserError(f"Only {' or '.join(allowed_from)} bookings can be {action['verb']} (this one is {b['status']}).{action.get('hint', '')}", 409)
        today = g.today().isoformat()
        action["check"](conn, b, today)
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
        audit(conn, c.user, action["audit"], b["property_id"], b["room_id"], b["status"], new, reason)
    g.write(conn, work)
    return row(get_booking(conn, c.user, c.args["id"]))


def check_checkin(conn, b, today):
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
    return {"ok": True}


def audit_view(conn, c):
    ids, sql, args = scope(c.user, c.q.get("property_id")), "SELECT l.*, p.name AS property, r.number AS room FROM audit_log l LEFT JOIN properties p ON p.id = l.property_id LEFT JOIN rooms r ON r.id = l.room_id WHERE ", []
    sql += f"(l.property_id IN ({marks(ids)})" + (" OR l.property_id IS NULL)" if c.user["role"] == "super_admin" and not c.q.get("property_id") else ")")
    args += ids
    if c.q.get("q", "").strip():
        t = f"%{c.q['q'].strip()[:60]}%"
        sql += " AND (l.action LIKE ? OR l.username LIKE ? OR l.old_value LIKE ? OR l.new_value LIKE ? OR l.reason LIKE ?)"
        args += [t] * 5
    return [dict(r) for r in conn.execute(sql + " ORDER BY l.id DESC LIMIT 500", args)]


R = lambda method, pattern, fn, roles: (method, re.compile(pattern + r"\Z"), fn, roles)
ROUTES = [R("POST", r"/api/login", login, None), R("POST", r"/api/logout", logout, ALL), R("GET", r"/api/meta", meta, ALL),
          R("GET", r"/api/dashboard", dashboard, ALL), R("GET", r"/api/rooms", rooms_view, ALL), R("GET", r"/api/bookings", bookings_list, ALL),
          R("POST", r"/api/bookings", create_booking, DESK), R("GET", r"/api/room_events", events_list, ALL), R("POST", r"/api/room_events", create_event, DESK),
          R("POST", r"/api/room_events/(?P<id>\d+)/release", lambda conn, c: event_action(conn, c, True), CLEANERS),
          R("POST", r"/api/room_events/(?P<id>\d+)/start_cleaning", lambda conn, c: event_action(conn, c, False), CLEANERS),
          R("GET", r"/api/audit", audit_view, MGR)]
for _name, (_new, _from, _act, _roles) in ACTIONS.items():
    ROUTES.append(R("POST", rf"/api/bookings/(?P<id>\d+)/{_name}", (lambda n, f, a: lambda conn, c: set_status(conn, c, n, f, a))(_new, _from, _act), _roles))


def dispatch(method, path, query, body, ip, token=None):
    """-> (http_status, json_able, cookie_token). cookie_token: str to set, '' to clear, None to leave. Never raises."""
    conn, c = None, None
    try:
        for m, rx, fn, roles in ROUTES:
            hit = rx.match(path) if m == method else None
            if hit:
                break
        else:
            return 404, {"error": "Not found."}, None
        conn = g.connect()
        user = load_user(conn, token) if roles else None
        if roles and user["role"] not in roles:
            raise UserError("Your role is not allowed to do this.", 403)
        c = Ctx(query, body, ip, user, hit.groupdict() | {"_token": token})
        return 200, fn(conn, c), c.token
    except UserError as e:
        return e.status, {"error": str(e)}, ("" if e.status == 401 and token else None)
    except Exception as e:
        if isinstance(e, g.sqlite3.OperationalError) and ("locked" in str(e) or "busy" in str(e)):
            return 503, {"error": "The database is busy. Please try again."}, None
        traceback.print_exc()
        return 500, {"error": "Something went wrong. Please try again."}, None
    finally:
        if conn:
            conn.close()


class Handler(BaseHTTPRequestHandler):
    def send(self, status, data, ctype="application/json", token=None):
        data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (("Content-Security-Policy", g.CSP), ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "same-origin"), ("Cache-Control", "no-store")):
            self.send_header(k, v)
        if token is not None:
            self.send_header("Set-Cookie", f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={TTL if token else 0}")
        self.end_headers()
        self.wfile.write(data)

    def token(self):
        m = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        return m.value if m else None

    def reply(self, method, url, body):
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        st, out, tok = dispatch(method, url.path, q, body, self.client_address[0], self.token())
        self.send(st, out, token=tok)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            return self.send(200, (g.ROOT / "web" / name).read_bytes(), ctype)
        self.reply("GET", url, None)

    def do_POST(self):
        try:
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self.send(415, {"error": "Some of the values sent are not valid."})
            n = int(self.headers.get("Content-Length", 0))
            if n > 20_000:
                return self.send(413, {"error": "Request too large."})
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.send(400, {"error": "Some of the values sent are not valid."})
        self.reply("POST", urlparse(self.path), body)


def set_password(username):
    pw = getpass.getpass("New password (at least 8 characters): ")
    if len(pw) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
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
        print(f"Staff app on http://127.0.0.1:{port}")
        # ponytail: local-only, plain HTTP; the session cookie needs Secure + HTTPS before this is exposed beyond localhost
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
        return 0
    if argv[:1] == ["set-password"] and len(argv) == 2:
        return set_password(argv[1])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
