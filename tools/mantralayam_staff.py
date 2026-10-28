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
        "can": {"book": u["role"] in DESK, "status": u["role"] in DESK, "clean": u["role"] in CLEANERS, "audit": u["role"] in MGR, "admin": u["role"] == "super_admin"},
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
            "rooms": dict(counts), "total_rooms": len(rooms)} | ({"backup": g.last_backup()} if c.user["role"] == "super_admin" else {})


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


# ---------- menu pages, extra screens ----------
FRONT = DESK | {"viewer"}  # everyone who may see guest details
PAGES = [("today", "Dashboard", ALL), ("bookings", "Bookings", FRONT), ("calendar", "Calendar", FRONT), ("rooms", "Rooms", ALL),
         ("status", "Room status", ALL), ("guests", "Guests", FRONT), ("rates", "Rooms & rates", MGR), ("reports", "Reports", MGR),
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

    def work():
        cur = get_booking(conn, c.user, c.args["id"])
        if cur["status"] not in ("Confirmed", "Checked-In"):
            raise UserError(f"A {cur['status']} booking can no longer be edited.", 409)
        cap = conn.execute("SELECT capacity FROM rooms WHERE id = ?", (cur["room_id"],)).fetchone()[0]
        if guests > cap:
            raise UserError(f"Room {cur['room']} sleeps at most {cap} guests.")
        conn.execute("UPDATE bookings SET guest_name = ?, phone = ?, guests = ?, remarks = ? WHERE id = ?", (name, phone, guests, remarks or None, cur["id"]))
        audit(conn, c.user, "booking.edit", cur["property_id"], cur["room_id"], f"{cur['guest_name']}, {cur['phone']}, {cur['guests']} guests", f"{name}, {phone}, {guests} guests")
    g.write(conn, work)
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
        conn.execute("UPDATE bookings SET room_id = ?, check_in = ?, check_out = ?, amount = ? WHERE id = ?",
                     (room["id"], ci.isoformat(), co.isoformat(), room["tariff"] * (co - ci).days, cur["id"]))
        audit(conn, c.user, "booking.reschedule", cur["property_id"], room["id"], f"room {cur['room']}, {cur['check_in']} to {cur['check_out']}",
              f"room {room['number']}, {ci} to {co}")
    g.write(conn, work)
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
                if e["start_date"] <= d and (e["end_date"] is None or e["end_date"] > d):
                    cell = {"status": {"internal_block": "blocked", "in_cleaning": "cleaning", "cleaning_required": "cleaning"}.get(e["kind"], e["kind"]), "note": LABEL[e["kind"]]}
                    break
            if not cell:
                for x in bks[r["id"]]:
                    if x["check_in"] <= d < x["check_out"]:
                        cell = {"status": "occupied" if x["status"] == "Checked-In" else "booked", "note": x["guest_name"], "booking_id": x["id"]}
                        break
            cells.append(cell)
        out.append({"id": r["id"], "number": r["number"], "property": r["property"], "property_id": r["property_id"], "type": r["type"], "cells": cells})
    return {"start": start.isoformat(), "days": days, "rooms": out}


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
        audit(conn, c.user, "guest.edit", None, None, key, phone_key(new_phone), f"{len(mine)} booking(s) updated, name {name}")
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


def password_change(conn, c):
    cur, new = str(c.body.get("current") or ""), str(c.body.get("new") or "")
    key, now = (c.ip, "pw:" + c.user["username"].lower()), time.time()
    FAILS[key] = [t for t in FAILS[key] if now - t < 600]
    if len(FAILS[key]) >= 5:
        raise UserError("Too many failed attempts. Please wait a few minutes and try again.", 429)
    u = conn.execute("SELECT pw_hash, salt FROM users WHERE id = ?", (c.user["id"],)).fetchone()
    if not check_pw(cur, u["pw_hash"], u["salt"]):
        FAILS[key].append(now)
        raise UserError("Your current password is not correct.")
    if len(new) < 8 or len(new) > 200:
        raise UserError("The new password must be at least 8 characters.")
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
TIME_RE = r"([01]\d|2[0-3]):[0-5]\d"
LIVE_SQL = "status IN ('Confirmed', 'Checked-In') AND (check_out > :today OR status = 'Checked-In')"
FUTURE_SQL = {"rooms": f"SELECT COUNT(*) FROM bookings WHERE room_id = :id AND {LIVE_SQL}",
              "room_types": f"SELECT COUNT(*) FROM bookings WHERE room_id IN (SELECT id FROM rooms WHERE room_type_id = :id) AND {LIVE_SQL}",
              "properties": f"SELECT COUNT(*) FROM bookings WHERE property_id = :id AND {LIVE_SQL}"}
ACTIVE_LABEL = {"properties": "property", "room_types": "room type", "rooms": "room", "users": "user", "peak_periods": "peak period"}


def raw_settings(conn):
    return g.DEFAULTS | {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings") if r["key"] in g.DEFAULTS}


def admin_data(conn, c):
    q = lambda sql: [dict(r) for r in conn.execute(sql)]
    users = q("SELECT id, username, full_name, role, active FROM users ORDER BY id")
    for u in users:
        u["property_ids"] = [r[0] for r in conn.execute("SELECT property_id FROM user_properties WHERE user_id = ? ORDER BY 1", (u["id"],))]
    return {"properties": q("SELECT * FROM properties ORDER BY id"), "room_types": q("SELECT * FROM room_types ORDER BY tariff"),
            "rooms": q("SELECT r.*, p.name AS property, t.name AS type FROM rooms r JOIN properties p ON p.id = r.property_id JOIN room_types t ON t.id = r.room_type_id ORDER BY r.property_id, r.number"),
            "peak_periods": q("SELECT * FROM peak_periods ORDER BY start_date DESC, id DESC"),
            "users": users, "roles": q("SELECT name, label FROM roles ORDER BY rowid"), "settings": raw_settings(conn),
            "setting_ranges": {k: list(v) for k, v in SETTING_RANGES.items()}}


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
            conn.execute("UPDATE rooms SET number = ?, floor = ?, room_type_id = ?, capacity = ?, amenities = ? WHERE id = ?", (number, floor, tid, cap, amenities, rid))
            new = rid
        else:
            new = conn.execute("INSERT INTO rooms (property_id, number, floor, room_type_id, capacity, amenities) VALUES (?,?,?,?,?,?)", (pid, number, floor, tid, cap, amenities)).lastrowid
        audit(conn, c.user, "admin.room_edit" if rid else "admin.room_create", pid, new, None, f"room {number}, sleeps {cap}")
        return new
    return {"id": g.write(conn, work)}


def admin_user(conn, c):
    b, uid = c.body, g.opt_int(c.body.get("id"))
    username, name = text(b, "username", "Username", 3, 40), text(b, "full_name", "Full name", 1, 100)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", username):
        raise UserError("Username may contain only letters, digits, dot, dash and underscore.")
    pw = str(b.get("password") or "")
    if (not uid or pw) and not 8 <= len(pw) <= 200:
        raise UserError("The password must be 8 to 200 characters.")
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
        conn.execute("DELETE FROM user_properties WHERE user_id = ?", (new,))
        if role != "super_admin":
            conn.executemany("INSERT INTO user_properties (user_id, property_id) VALUES (?,?)", [(new, i) for i in ids])
        audit(conn, c.user, "admin.user_edit" if uid else "admin.user_create", None, None, old and old["role"], f"{username} ({role})",
              "password reset" if uid and pw else None)
        return new
    new = g.write(conn, work)
    if uid and pw and uid != c.user["id"]:
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
        label = r["name"] if "name" in r.keys() else r["username"] if "username" in r.keys() else f"room {r['number']}"
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

    def work():
        if pid and not exists(conn, "peak_periods", pid):
            raise UserError("Peak period not found.", 404)
        vals = (name, ci.isoformat(), co.isoformat(), nights, rooms, note)
        if pid:
            conn.execute("UPDATE peak_periods SET name = ?, start_date = ?, end_date = ?, max_nights = ?, max_rooms = ?, note = ? WHERE id = ?", (*vals, pid))
            new = pid
        else:
            new = conn.execute("INSERT INTO peak_periods (name, start_date, end_date, max_nights, max_rooms, note) VALUES (?,?,?,?,?,?)", vals).lastrowid
        audit(conn, c.user, "admin.peak_period_edit" if pid else "admin.peak_period_create", None, None, None,
              f"{name}: {ci} to {co}, max nights {nights or 'none'}, max rooms {rooms or 'none'}")
        return new
    return {"id": g.write(conn, work)}


def admin_settings(conn, c):
    b, new = c.body, {}
    for k, (lo, hi, label) in SETTING_RANGES.items():
        if k in b:
            new[k] = str(g.to_int(b[k], f"{label} must be a whole number from {lo} to {hi}.", lo, hi))
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


R = lambda method, pattern, fn, roles: (method, re.compile(pattern + r"\Z"), fn, roles)
ROUTES = [R("POST", r"/api/login", login, None), R("POST", r"/api/logout", logout, ALL), R("GET", r"/api/meta", meta, ALL),
          R("GET", r"/api/dashboard", dashboard, ALL), R("GET", r"/api/rooms", rooms_view, ALL), R("GET", r"/api/bookings", bookings_list, FRONT),
          R("POST", r"/api/bookings", create_booking, DESK), R("GET", r"/api/availability", availability, DESK), R("GET", r"/api/room_events", events_list, ALL), R("POST", r"/api/room_events", create_event, DESK),
          R("POST", r"/api/room_events/(?P<id>\d+)/release", lambda conn, c: event_action(conn, c, True), CLEANERS),
          R("POST", r"/api/room_events/(?P<id>\d+)/start_cleaning", lambda conn, c: event_action(conn, c, False), CLEANERS),
          R("GET", r"/api/audit", audit_view, MGR),
          R("POST", r"/api/bookings/(?P<id>\d+)/edit", edit_booking, DESK), R("POST", r"/api/bookings/(?P<id>\d+)/reschedule", reschedule_booking, DESK),
          R("GET", r"/api/calendar", calendar_view, FRONT), R("GET", r"/api/guests", guests_list, FRONT), R("GET", r"/api/guests/profile", guest_profile, FRONT),
          R("POST", r"/api/guests/edit", guest_edit, DESK), R("GET", r"/api/reports", reports, MGR),
          R("GET", r"/api/notifications", notifications, ALL), R("POST", r"/api/notifications/seen", notifications_seen, ALL),
          R("GET", r"/api/room_types", rate_types, MGR), R("POST", r"/api/room_types", rate_save, MGR), R("POST", r"/api/room_types/(?P<id>\d+)/delete", rate_delete, MGR),
          R("GET", r"/api/settings", settings_get, MGR), R("POST", r"/api/settings/property", property_save, MGR),
          R("POST", r"/api/settings/times", times_save, frozenset({"super_admin"})),
          R("GET", r"/api/admin/data", admin_data, SUPER), R("POST", r"/api/admin/properties", admin_property, SUPER),
          R("POST", r"/api/admin/room_types", rate_save, SUPER), R("POST", r"/api/admin/rooms", admin_room, SUPER),
          R("POST", r"/api/admin/users", admin_user, SUPER), R("POST", r"/api/admin/settings", admin_settings, SUPER),
          R("POST", r"/api/admin/peak_periods", admin_peak, SUPER),
          R("POST", r"/api/admin/(?P<kind>properties|room_types|rooms|users|peak_periods)/(?P<id>\d+)/active", admin_active, SUPER),
          R("POST", r"/api/me/profile", profile_save, ALL), R("POST", r"/api/me/password", password_change, ALL)]
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
        if url.path in IMAGES:
            return self.send(200, (g.ROOT / url.path.lstrip("/")).read_bytes(), IMAGES[url.path])
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
