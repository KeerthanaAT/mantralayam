"""Mantralayam guest booking: public JSON API + guest website (stdlib only).

Usage:
  python tools/mantralayam_rooms.py serve [PORT]     # guest site at /, default port 8001
  python tools/mantralayam_rooms.py backup           # verified DB copy in backups/ (keeps 14); exit code 1 on failure

Reads and writes the existing SQLite database (.tmp/mantralayam.db, or MANTRALAYAM_DB).
It never reseeds data; migrate() only adds missing columns or tables. Rules live in dispatch() so tests call it directly;
the HTTP handler is a thin wrapper.
"""
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import time
import traceback
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("MANTRALAYAM_DB") or ROOT / ".tmp" / "mantralayam.db")
STATIC = {"/": ("guest.html", "text/html"), "/shared.css": ("shared.css", "text/css"), "/tokens.css": ("tokens.css", "text/css")}
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
ID_TYPES = ("Aadhaar", "Voter ID", "Driving Licence", "Passport", "PAN", "Other")
DEFAULTS = {"guest_booking_enabled": "1", "guest_max_rooms": "3", "guest_advance_days": "90", "guest_max_nights": "15",
            "guest_cancel_cutoff_hours": "24", "checkin_time": "14:00", "checkout_time": "11:00"}
IMAGES_DIR = ROOT / "web" / "images"
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
BUSY = ("Confirmed", "Checked-In")
BACKUPS = Path(os.environ.get("MANTRALAYAM_BACKUPS") or ROOT / "backups")
BACKUP_KEEP = 14
BACKUP_GLOB = "mantralayam-????-??-??-????.db"


class UserError(Exception):
    def __init__(self, msg, status=400, retry_after=None):
        super().__init__(msg)
        self.status = status
        self.retry_after = retry_after  # seconds; sent as the Retry-After header on 429


def today():
    return date.today()


def now():
    return datetime.now()


def now_iso():
    return now().isoformat(timespec="seconds")


MIGRATED = set()


def migrate(conn):
    """Idempotent additive schema changes, run once per process and database file. Never drops or rewrites data."""
    if "active" not in {r[1] for r in conn.execute("PRAGMA table_info(room_types)")}:
        conn.execute("ALTER TABLE room_types ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    conn.execute("CREATE TABLE IF NOT EXISTS holds (id INTEGER PRIMARY KEY, token TEXT NOT NULL, room_id INTEGER NOT NULL REFERENCES rooms(id), "
                 "check_in TEXT NOT NULL, check_out TEXT NOT NULL, expires_at TEXT NOT NULL, ip TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_holds_room ON holds(room_id, expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_holds_token ON holds(token)")
    conn.execute("CREATE TABLE IF NOT EXISTS peak_periods (id INTEGER PRIMARY KEY, name TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, "
                 "max_nights INTEGER, max_rooms INTEGER, note TEXT, active INTEGER NOT NULL DEFAULT 1, CHECK (end_date >= start_date))")
    conn.execute("CREATE TABLE IF NOT EXISTS callback_requests (id INTEGER PRIMARY KEY, property_id INTEGER REFERENCES properties(id), name TEXT NOT NULL, phone TEXT NOT NULL, "
                 "people INTEGER NOT NULL, check_in TEXT NOT NULL, check_out TEXT NOT NULL, note TEXT, "
                 "status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'contacted', 'closed')), created_at TEXT NOT NULL, handled_by INTEGER REFERENCES users(id), handled_at TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_callbacks_status ON callback_requests(status, created_at)")


def connect():
    if not DB.exists():
        raise UserError("The booking system is not set up yet. Please call the property.", 503)
    conn = sqlite3.connect(DB, timeout=10, isolation_level=None)  # manual BEGIN IMMEDIATE for writes
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(DB) not in MIGRATED:
        try:
            write(conn, lambda: migrate(conn))
        except sqlite3.OperationalError:  # another process migrated first
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'peak_periods'").fetchone():
                raise
        MIGRATED.add(str(DB))
    return conn


def settings(conn):
    s = DEFAULTS | {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    return {"enabled": s["guest_booking_enabled"] == "1", "max_rooms": int(s["guest_max_rooms"]), "advance": int(s["guest_advance_days"]),
            "max_nights": int(s["guest_max_nights"]), "cutoff": int(s["guest_cancel_cutoff_hours"]),
            "checkin_time": s["checkin_time"], "checkout_time": s["checkout_time"]}


# ponytail: in-memory per-process counters, reset on restart; move to the DB if run as several processes
HITS = defaultdict(deque)


def throttle(key, limit, window, msg):
    t, q = time.monotonic(), HITS[key]
    while q and t - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        wait = max(1, math.ceil(q[0] + window - t))
        raise UserError(f"{msg} Please try again in {f'{wait} seconds' if wait < 90 else f'{math.ceil(wait / 60)} minutes'}.", 429, wait)
    q.append(t)


def to_int(v, msg, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise UserError(msg)
    if not lo <= n <= hi:
        raise UserError(msg)
    return n


def opt_int(v):
    return None if v in (None, "") else to_int(v, "Please choose a valid property and room type.", 1, 10**9)


def parse_stay(conn, check_in, check_out):
    """Validates an online stay against the settings. Returns (ci, co, nights, settings)."""
    s = settings(conn)
    if not s["enabled"]:
        raise UserError("Online booking is closed right now. Please contact the property directly.", 403)
    try:
        ci = date.fromisoformat(check_in)
    except (TypeError, ValueError):
        raise UserError("Check-in must be a valid date (YYYY-MM-DD).")
    try:
        co = date.fromisoformat(check_out)
    except (TypeError, ValueError):
        raise UserError("Check-out must be a valid date (YYYY-MM-DD).")
    check_stay(conn, s, ci, co)
    return ci, co, (co - ci).days, s


def check_stay(conn, s, ci, co):
    """The date rules for an online stay (past, order, advance window, nights, peak limits). Raises UserError; tightens s for peak periods."""
    last = today() + timedelta(days=s["advance"])
    if ci < today():
        raise UserError("Check-in date cannot be in the past.")
    if co <= ci:
        raise UserError("Check-out must be after check-in.")
    if ci > last:
        raise UserError(f"Online booking is open up to {s['advance']} days ahead (check-in on or before {last.isoformat()}).")
    apply_peak(conn, s, ci, co)
    if (co - ci).days > s["max_nights"]:
        during = f"During {s['peak_nights']} online" if s["peak_nights"] else "Online"
        raise UserError(f"{during} bookings can be for at most {s['max_nights']} nights. For a longer stay please call the property.")


def peak_periods(conn, ci, co):
    """Active peak periods touching the nights [ci, co): a period's end date is its last night, inclusive."""
    return [dict(r) for r in conn.execute("SELECT id, name, start_date, end_date, max_nights, max_rooms, note FROM peak_periods "
                                          "WHERE active = 1 AND start_date < ? AND end_date >= ? ORDER BY start_date, id", (co.isoformat(), ci.isoformat()))]


def apply_peak(conn, s, ci, co):
    """Tightens s['max_nights'] and s['max_rooms'] to the strictest overlapping peak period (never loosens them).
    Adds s['peak'] (the periods), s['peak_nights'] and s['peak_rooms'] (names of the periods that set the limit, else '')."""
    s["peak"] = peak_periods(conn, ci, co)
    for key in ("max_nights", "max_rooms"):
        caps = [(p[key], p["name"]) for p in s["peak"] if p[key]]
        low = min((v for v, _ in caps), default=None)
        if low is not None and low < s[key]:
            s[key] = low
            s["peak_" + key[4:]] = ", ".join(n for v, n in caps if v == low)
        else:
            s["peak_" + key[4:]] = ""


def room_count(b, s):
    try:
        n = int(b.get("rooms"))
    except (TypeError, ValueError):
        n = 0
    if n > s["max_rooms"] and s["peak_rooms"]:
        raise UserError(f"During {s['peak_rooms']} you can book at most {s['max_rooms']} room{'s' if s['max_rooms'] > 1 else ''} online. Please call the property to book more.")
    return to_int(n, "Please choose how many rooms you need.", 1, s["max_rooms"])


HOLD_MINUTES = 10
MAX_HOLDS_PER_IP = 3  # live holds (one per room selection) a single address may keep at once


def free_rooms(conn, ci, co, property_id=None, type_id=None, hold=None):
    """Rooms genuinely free for [ci, co): active, no overlapping live booking, no blocking status event.
    Cleaning events only block stays that start today or earlier (the room is expected back by tomorrow).
    Unexpired guest holds block too, except the one whose token is `hold` (the caller's own).
    ponytail: that cleaning rule is inferred, confirm it with the property staff."""
    return conn.execute("""
        SELECT r.id, r.number, r.capacity, r.amenities, r.property_id, r.room_type_id,
               p.name AS pname, p.address, t.name AS tname, t.description AS tdesc, t.tariff
        FROM rooms r JOIN properties p ON p.id = r.property_id JOIN room_types t ON t.id = r.room_type_id
        WHERE r.active = 1 AND p.active = 1 AND t.active = 1
          AND (:pid IS NULL OR r.property_id = :pid) AND (:tid IS NULL OR r.room_type_id = :tid)
          AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.room_id = r.id AND b.status IN ('Confirmed', 'Checked-In')
                          AND b.check_in < :co AND b.check_out > :ci)
          AND NOT EXISTS (SELECT 1 FROM room_status_events e WHERE e.room_id = r.id AND e.active = 1
                          AND e.start_date < :co AND (e.end_date IS NULL OR e.end_date > :ci)
                          AND (e.kind NOT IN ('cleaning_required', 'in_cleaning') OR :ci <= :today))
          AND NOT EXISTS (SELECT 1 FROM holds h WHERE h.room_id = r.id AND h.expires_at > :now AND h.check_in < :co AND h.check_out > :ci
                          AND (:hold IS NULL OR h.token != :hold))
        ORDER BY p.id, t.tariff, r.number""",
        dict(ci=ci.isoformat(), co=co.isoformat(), pid=property_id, tid=type_id, today=today().isoformat(), now=now_iso(), hold=hold or None)).fetchall()


def pick_rooms(free, n, held=()):
    """The n rooms to give a booking: the guest's own held rooms first, then the largest. ponytail: rooms of one type are near-identical, so no fancier packing."""
    return sorted(free, key=lambda r: (r["id"] not in held, -r["capacity"], r["number"]))[:n]


def rooms_needed(capacities, guests):
    """Fewest rooms whose capacities cover the party (largest first), or None."""
    total = 0
    for k, c in enumerate(sorted(capacities, reverse=True), 1):
        total += c
        if total >= guests:
            return k


def spread(guests, n):
    """Guests spread evenly over n rooms: at least one each."""
    return [guests // n + (i < guests % n) for i in range(n)]


def info(conn, q, ip):
    s = settings(conn)
    types = lambda pid: conn.execute(
        "SELECT DISTINCT t.id, t.name, t.tariff FROM rooms r JOIN room_types t ON t.id = r.room_type_id "
        "WHERE r.active = 1 AND t.active = 1 AND r.property_id = ? ORDER BY t.tariff", (pid,)).fetchall()
    props = [dict(p) | {"room_types": [r["name"] for r in types(p["id"])], "type_ids": [r["id"] for r in types(p["id"])]}
        for p in conn.execute("SELECT id, name, address, description, facilities, contact_person, phone FROM properties WHERE active = 1 ORDER BY id")]
    return {"today": today().isoformat(), "last_check_in": (today() + timedelta(days=s["advance"])).isoformat(),
            "settings": {"guest_booking_enabled": s["enabled"], "checkin_time": s["checkin_time"], "checkout_time": s["checkout_time"],
                         "guest_cancel_cutoff_hours": s["cutoff"]},
            "peak_periods": [dict(r) for r in conn.execute("SELECT name, start_date, end_date, max_nights, max_rooms, note FROM peak_periods WHERE active = 1 AND end_date >= ? "
                                                           "ORDER BY start_date, id", (today().isoformat(),))],
            "properties": props, "room_types": [dict(r) for r in conn.execute("SELECT id, name FROM room_types WHERE active = 1 AND id IN (SELECT room_type_id FROM rooms WHERE active = 1) ORDER BY tariff")]}


def search(conn, q, ip):
    throttle(("search", ip), 60, 60, "Too many searches.")
    ci, co, nights, s = parse_stay(conn, q.get("check_in"), q.get("check_out"))
    guests = to_int(q.get("guests"), "Number of guests must be between 1 and 200.", 1, 200)
    groups = defaultdict(list)
    for r in free_rooms(conn, ci, co, opt_int(q.get("property_id")), opt_int(q.get("room_type_id")), str(q.get("hold") or "")[:64]):
        groups[(r["property_id"], r["room_type_id"])].append(r)
    props = {}
    for (pid, tid), rooms in groups.items():
        need = rooms_needed([r["capacity"] for r in rooms], guests)
        if need is None or need > s["max_rooms"]:
            continue
        amenities = list(dict.fromkeys(a.strip() for r in rooms for a in (r["amenities"] or "").split(",") if a.strip()))
        r0 = rooms[0]
        p = props.setdefault(pid, {"property_id": pid, "property": r0["pname"], "address": r0["address"], "types": []})
        p["types"].append({"room_type_id": tid, "type": r0["tname"], "capacity": max(r["capacity"] for r in rooms), "description": r0["tdesc"],
                           "amenities": amenities, "tariff": r0["tariff"], "nights": nights, "total_per_room": r0["tariff"] * nights,
                           "few_left": len(rooms) <= 2, "rooms_needed": need, "can_book": min(len(rooms), s["max_rooms"])})
    for p in props.values():
        p["types"].sort(key=lambda t: t["tariff"])
    res = {"check_in": ci.isoformat(), "check_out": co.isoformat(), "nights": nights, "guests": guests, "properties": list(props.values()), "peak": s["peak"], "alternatives": [],
           "callback": None}
    if not props:  # nothing for this party: offer nearby dates that really are bookable
        pid = opt_int(q.get("property_id"))
        res["alternatives"] = alternatives(conn, ci, co, guests, {pid} if pid else None, opt_int(q.get("room_type_id")))
        # "group": rooms are free but the party needs more rooms than may be booked online; "soldout": no dates to offer either. Either way staff should call back.
        res["callback"] = "group" if groups else None if res["alternatives"] else "soldout"
    return res


ALT_SPAN, ALT_MAX = 14, 3


def alternatives(conn, ci, co, guests, property_ids=None, type_id=None, max_rooms=None, limit=ALT_MAX):
    """Up to `limit` nearby stays to offer when [ci, co) is sold out: same number of nights, same party, check-in within ALT_SPAN days either side.
    Reuses free_rooms and the booking rules (check_stay: not past, guest_advance_days, guest_max_nights incl. peak limits), so every result is bookable now.
    property_ids limits which properties count (staff scope); max_rooms caps rooms for the party (None: the stay's online limit; staff walk-ins pass 1).
    Nearest first, earlier before later on a tie. ponytail: one free_rooms query per candidate day (at most 28), fine at this size."""
    nights, base, out = (co - ci).days, settings(conn), []
    for k in sorted(range(-ALT_SPAN, ALT_SPAN + 1), key=lambda k: (abs(k), k)):
        if k == 0 or len(out) >= limit:
            continue
        a, b = ci + timedelta(days=k), co + timedelta(days=k)
        s = dict(base)
        try:
            check_stay(conn, s, a, b)
        except UserError:
            continue
        free = [r for r in free_rooms(conn, a, b, None, type_id) if property_ids is None or r["property_id"] in property_ids]
        groups = defaultdict(list)
        for r in free:
            groups[(r["property_id"], r["room_type_id"])].append(r["capacity"])
        cap = s["max_rooms"] if max_rooms is None else min(max_rooms, s["max_rooms"])
        if any((n := rooms_needed(caps, guests)) is not None and n <= cap for caps in groups.values()):
            out.append({"check_in": a.isoformat(), "check_out": b.isoformat(), "nights": nights, "days_away": k})
    return out


def norm_phone(v):
    v = re.sub(r"[\s\-]", "", str(v or ""))
    if not re.fullmatch(r"\+?\d{10,13}", v):
        raise UserError("Mobile / phone number must have 10 to 13 digits.")
    return v


def same_phone(a, b):
    digits = lambda v: re.sub(r"\D", "", str(v or ""))[-10:]
    return digits(a) == digits(b)


def find_booking(conn, booking_id, phone):
    bid = to_int(booking_id, "Booking ID must be a number.", 1, 10**12)
    rows = conn.execute("SELECT * FROM bookings WHERE source = 'online' AND (group_id = ? OR (group_id IS NULL AND id = ?)) ORDER BY id", (bid, bid)).fetchall()
    if not rows or not same_phone(rows[0]["phone"], phone):
        raise UserError("We could not find a booking with that ID and mobile number.", 404)
    return rows


def booking_view(conn, rows):
    b, s = rows[0], settings(conn)
    prop = conn.execute("SELECT name, address, contact_person, phone FROM properties WHERE id = ?", (b["property_id"],)).fetchone()
    rt = conn.execute("SELECT t.name, t.tariff FROM rooms r JOIN room_types t ON t.id = r.room_type_id WHERE r.id = ?", (b["room_id"],)).fetchone()
    live = [r for r in rows if r["status"] != "Cancelled"]
    status = live[0]["status"] if live else "Cancelled"
    nights = (date.fromisoformat(b["check_out"]) - date.fromisoformat(b["check_in"])).days
    deadline = datetime.fromisoformat(f"{b['check_in']}T{s['checkin_time']}") - timedelta(hours=s["cutoff"])
    return {"booking_id": b["group_id"] or b["id"], "status": status, "property": dict(prop), "room_type": rt["name"], "rooms": len(rows),
            "guests": sum(r["guests"] for r in rows), "check_in": b["check_in"], "check_out": b["check_out"],
            "check_in_time": s["checkin_time"], "check_out_time": s["checkout_time"], "nights": nights, "guest_name": b["guest_name"],
            "mobile": b["phone"], "special_requests": b["remarks"], "amount": sum(r["amount"] if r["amount"] is not None else rt["tariff"] * nights for r in rows),
            "payment": {"method": "Pay at counter"}, "cancel_reason": b["cancel_reason"],
            "cancel_deadline": deadline.isoformat(timespec="minutes"), "can_cancel": status == "Confirmed" and datetime.now() < deadline}


def audit(conn, action, property_id, old, new, reason=None):
    conn.execute("INSERT INTO audit_log (ts, username, role, action, property_id, old_value, new_value, reason) VALUES (?,?,?,?,?,?,?,?)",
                 (datetime.now().isoformat(timespec="seconds"), "guest", "guest", action, property_id, old, new, reason))


def write(conn, fn):
    """Runs fn() in one IMMEDIATE transaction so check + write cannot interleave."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        out = fn()
        conn.execute("COMMIT")
        return out
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def book(conn, b, ip):
    throttle(("book-ip", ip), 10, 600, "Too many booking attempts from this device.")
    if str(b.get("website") or "").strip():  # honeypot: the field is hidden from people, only bots fill it
        throttle(("book-ip", ip), 10, 600, "Too many booking attempts from this device.")
        raise UserError("Some of the values sent are not valid.")
    token = str(b.get("hold_token") or "")[:64]
    ci, co, nights, s = parse_stay(conn, b.get("check_in"), b.get("check_out"))
    if b.get("payment_method") != "pay_at_counter":
        raise UserError("Please choose a valid payment option.")
    pid, tid = to_int(b.get("property_id"), "Please choose a valid property and room type.", 1, 10**9), to_int(b.get("room_type_id"), "Please choose a valid property and room type.", 1, 10**9)
    n = room_count(b, s)
    guests = to_int(b.get("guests"), "Number of guests must be between 1 and 200.", 1, 200)
    if guests < n:
        raise UserError("There must be at least one guest per room. Reduce the number of rooms.")
    name, remarks, id_no = (str(b.get(k) or "").strip() for k in ("guest_name", "remarks", "id_number"))
    email = str(b.get("email") or "").strip()
    if not name or len(name) > 100:
        raise UserError("Please enter the guest name.")
    phone = norm_phone(b.get("phone"))
    if b.get("id_type") not in ID_TYPES:
        raise UserError("Please choose a valid ID proof type.")
    if not id_no or len(id_no) > 30:
        raise UserError("Please enter the ID proof number.")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 120:
        raise UserError("Please enter a valid email address, or leave it empty.")
    if len(remarks) > 500:
        raise UserError("Special requests must be 500 characters or fewer.")
    throttle(("book", phone[-10:]), 5, 600, "Too many booking attempts for this mobile number.")

    def work():
        if not conn.execute("SELECT 1 FROM properties WHERE id = ? AND active = 1", (pid,)).fetchone():
            raise UserError("Please choose a valid property and room type.")
        free = free_rooms(conn, ci, co, pid, tid, token)  # the guest's own live hold does not block them; anyone else's does
        if len(free) < n:
            raise UserError("Sorry, those rooms are no longer available for your dates. Please search again.", 409)
        held = {r[0] for r in conn.execute("SELECT room_id FROM holds WHERE token = ? AND expires_at > ?", (token, now_iso()))} if token else set()
        rooms = pick_rooms(free, n, held)
        shares = spread(guests, n)
        if any(sh > r["capacity"] for sh, r in zip(shares, rooms)):
            raise UserError(f"{n} room{'s' if n > 1 else ''} of this type cannot host {guests} guests. Please add a room or reduce the guests.")
        now, ids = datetime.now().isoformat(timespec="seconds"), []
        for sh, r in zip(shares, rooms):
            cur = conn.execute(
                "INSERT INTO bookings (property_id, room_id, guest_name, phone, id_type, id_number, guests, check_in, check_out, remarks, status, "
                "created_at, status_at, source, email, payment_method, payment_status, amount) VALUES (?,?,?,?,?,?,?,?,?,?,'Confirmed',?,?,'online',?,'pay_at_counter','due',?)",
                (pid, r["id"], name, phone, b["id_type"], id_no, sh, ci.isoformat(), co.isoformat(), remarks or None, now, now, email or None, r["tariff"] * nights))
            ids.append(cur.lastrowid)
        conn.execute(f"UPDATE bookings SET group_id = ? WHERE id IN ({','.join('?' * len(ids))})", (ids[0], *ids))
        if token:
            conn.execute("DELETE FROM holds WHERE token = ?", (token,))
        audit(conn, "booking.online_create", pid, None, f"#{ids[0]}: {n} room(s), {ci} to {co}, {guests} guests", f"Online booking, mobile {phone}")
        return ids[0]

    gid = write(conn, work)
    return booking_view(conn, find_booking(conn, gid, phone))


def hold(conn, b, ip):
    """Reserves the chosen rooms for HOLD_MINUTES while the guest fills in the confirm step. Passing the previous `token` replaces that hold."""
    throttle(("hold", ip), 10, 600, "Too many room selections from this device.")
    ci, co, nights, s = parse_stay(conn, b.get("check_in"), b.get("check_out"))
    pid = to_int(b.get("property_id"), "Please choose a valid property and room type.", 1, 10**9)
    tid = to_int(b.get("room_type_id"), "Please choose a valid property and room type.", 1, 10**9)
    n = room_count(b, s)
    old = str(b.get("token") or "")[:64]

    def work():
        conn.execute("DELETE FROM holds WHERE expires_at <= ?", (now_iso(),))
        if old:
            conn.execute("DELETE FROM holds WHERE token = ?", (old,))
        if conn.execute("SELECT COUNT(*) FROM holds WHERE ip = ?", (ip,)).fetchone()[0] >= MAX_HOLDS_PER_IP:
            raise UserError("You already have rooms on hold. Please finish that booking or wait for the hold to expire.", 429, HOLD_MINUTES * 60)
        free = free_rooms(conn, ci, co, pid, tid)
        if len(free) < n:
            raise UserError("Sorry, those rooms are no longer available for your dates. Please search again.", 409)
        tok, exp = secrets.token_urlsafe(16), (now() + timedelta(minutes=HOLD_MINUTES)).isoformat(timespec="seconds")
        for r in pick_rooms(free, n):
            conn.execute("INSERT INTO holds (token, room_id, check_in, check_out, expires_at, ip) VALUES (?,?,?,?,?,?)", (tok, r["id"], ci.isoformat(), co.isoformat(), exp, ip))
        return {"token": tok, "expires_at": exp, "seconds": HOLD_MINUTES * 60, "rooms": n}
    return write(conn, work)


def release(conn, b, ip):
    token = str(b.get("token") or "")[:64]
    if token:
        write(conn, lambda: conn.execute("DELETE FROM holds WHERE token = ?", (token,)))
    return {"ok": True}


def lookup(conn, b, ip):
    throttle(("lookup", ip), 20, 600, "Too many attempts.")
    return booking_view(conn, find_booking(conn, b.get("booking_id"), b.get("phone")))


def cancel(conn, b, ip):
    throttle(("lookup", ip), 20, 600, "Too many attempts.")
    reason = str(b.get("reason") or "").strip()
    if not reason or len(reason) > 300:
        raise UserError("Please give a reason for cancelling (up to 300 characters).")

    def work():
        rows = find_booking(conn, b.get("booking_id"), b.get("phone"))
        v = booking_view(conn, rows)
        if v["status"] != "Confirmed":
            raise UserError("This booking can no longer be cancelled online.", 409)
        if not v["can_cancel"]:
            when = datetime.fromisoformat(v["cancel_deadline"]).strftime("%d %b %Y, %I:%M %p")
            raise UserError(f"Online cancellation closed on {when}. Please call {v['property']['phone']} to cancel.", 409)
        now = datetime.now().isoformat(timespec="seconds")
        for r in rows:
            if r["status"] == "Confirmed":
                conn.execute("UPDATE bookings SET status = 'Cancelled', status_at = ?, cancel_reason = ? WHERE id = ?",
                             (now, f"{reason} (cancelled online by guest, mobile {rows[0]['phone']})", r["id"]))
        audit(conn, "booking.online_cancel", rows[0]["property_id"], "Confirmed", "Cancelled", reason)
        return rows[0]["group_id"] or rows[0]["id"]

    gid = write(conn, work)
    return booking_view(conn, find_booking(conn, gid, b.get("phone")))


CALLBACK_MAX_DAYS, CALLBACK_MAX_NIGHTS = 366, 60


def callback(conn, b, ip):
    """A group (or sold-out) guest asks the property to call them back. Same per-IP throttle helper and honeypot as booking."""
    throttle(("callback-ip", ip), 5, 600, "Too many call-back requests from this device.")
    if str(b.get("website") or "").strip():  # honeypot: hidden from people, only bots fill it
        throttle(("callback-ip", ip), 5, 600, "Too many call-back requests from this device.")
        raise UserError("Some of the values sent are not valid.")
    name, note = str(b.get("name") or "").strip(), str(b.get("note") or "").strip()
    if not name or len(name) > 100:
        raise UserError("Please enter your name.")
    phone = norm_phone(b.get("phone"))
    people = to_int(b.get("people"), "Number of people must be between 1 and 500.", 1, 500)
    try:
        ci, co = date.fromisoformat(b.get("check_in")), date.fromisoformat(b.get("check_out"))
    except (TypeError, ValueError):
        raise UserError("Please choose your preferred check-in and check-out dates.")
    if ci < today():
        raise UserError("Check-in date cannot be in the past.")
    if co <= ci:
        raise UserError("Check-out must be after check-in.")
    if ci > today() + timedelta(days=CALLBACK_MAX_DAYS) or (co - ci).days > CALLBACK_MAX_NIGHTS:
        raise UserError(f"Please choose dates within the next {CALLBACK_MAX_DAYS} days and at most {CALLBACK_MAX_NIGHTS} nights.")
    if len(note) > 500:
        raise UserError("The note must be 500 characters or fewer.")
    pid = opt_int(b.get("property_id"))
    throttle(("callback", phone[-10:]), 3, 600, "Too many call-back requests for this mobile number.")

    def work():
        if pid and not conn.execute("SELECT 1 FROM properties WHERE id = ? AND active = 1", (pid,)).fetchone():
            raise UserError("Please choose a valid property, or leave it empty.")
        cid = conn.execute("INSERT INTO callback_requests (property_id, name, phone, people, check_in, check_out, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (pid, name, phone, people, ci.isoformat(), co.isoformat(), note or None, now_iso())).lastrowid
        audit(conn, "callback.request", pid, None, f"#{cid}: {people} people, {ci} to {co}", f"Call-back request, mobile {phone}")
        return cid
    write(conn, work)
    return {"ok": True}


def health(conn, q, ip):
    conn.execute("SELECT 1 FROM properties LIMIT 1")
    return {"ok": True}


class BackupError(Exception):
    pass


def backup_log(ok, detail):
    with open(BACKUPS / "backup.log", "a", encoding="utf-8") as f:
        f.write(f"{now_iso()}\t{'OK' if ok else 'FAILED'}\t{' '.join(str(detail).split())}\n")


def last_backup():
    """{'ts', 'ok', 'detail'} of the latest backup run (from backups/backup.log), or None if none has run."""
    try:
        line = [ln for ln in (BACKUPS / "backup.log").read_text(encoding="utf-8").splitlines() if ln.strip()][-1]
        ts, status, detail = (line.split("\t") + ["", ""])[:3]
        return {"ts": ts, "ok": status == "OK", "detail": detail}
    except (OSError, IndexError):
        return None


def backup(keep=BACKUP_KEEP):
    """Consistent copy of the database (SQLite online backup API, safe while the servers run) to backups/mantralayam-YYYY-MM-DD-HHMM.db.
    The copy must pass PRAGMA integrity_check before it replaces anything; only then are all but the newest `keep` deleted.
    Raises BackupError on any failure (and logs it). Returns the new path."""
    dest = BACKUPS / f"mantralayam-{datetime.now():%Y-%m-%d-%H%M}.db"
    part = dest.with_name(dest.name + ".part")
    try:
        BACKUPS.mkdir(parents=True, exist_ok=True)
        if not DB.exists():
            raise BackupError(f"database not found: {DB}")
        src, dst = sqlite3.connect(DB, timeout=30), sqlite3.connect(part)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        chk = sqlite3.connect(part)
        try:
            result = [r[0] for r in chk.execute("PRAGMA integrity_check")]
        finally:
            chk.close()
        if result != ["ok"]:
            raise BackupError("integrity_check failed on the copy: " + "; ".join(result[:3]))
        os.replace(part, dest)  # same-minute reruns replace the earlier copy
    except (OSError, sqlite3.Error, BackupError) as e:
        part.unlink(missing_ok=True)
        msg = str(e) or type(e).__name__
        try:
            backup_log(False, msg)
        except OSError:
            pass
        raise BackupError(msg) from e
    for old in sorted(BACKUPS.glob(BACKUP_GLOB))[:-keep]:  # names sort by time; non-matching files are never touched
        old.unlink(missing_ok=True)
    backup_log(True, dest.name)
    return dest


ROUTES = {("GET", "/health"): health, ("GET", "/api/public/info"): info, ("GET", "/api/public/search"): search, ("POST", "/api/public/book"): book, ("POST", "/api/public/hold"): hold, ("POST", "/api/public/release"): release, ("POST", "/api/public/callback"): callback,
          ("POST", "/api/public/booking/lookup"): lookup, ("POST", "/api/public/booking/cancel"): cancel}


def dispatch(method, path, query, body, ip):
    """-> (http_status, json_able). Never raises."""
    fn = ROUTES.get((method, path))
    if not fn:
        return 404, {"error": "Not found."}
    conn = None
    try:
        conn = connect()
        return 200, fn(conn, query if method == "GET" else body, ip)
    except UserError as e:
        return e.status, {"error": str(e)} | ({"retry_after": e.retry_after} if e.retry_after else {})
    except sqlite3.OperationalError as e:
        if "locked" in str(e) or "busy" in str(e):
            return 503, {"error": "The database is busy. Please try again."}
        traceback.print_exc()
        return 500, {"error": "Something went wrong. Please try again."}
    except Exception:
        traceback.print_exc()
        return 500, {"error": "Something went wrong. Please try again."}
    finally:
        if conn:
            conn.close()


class Handler(BaseHTTPRequestHandler):
    def send(self, status, data, ctype="application/json", cache=None):
        retry = data.get("retry_after") if status == 429 and isinstance(data, dict) else None
        data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        if retry:
            self.send_header("Retry-After", str(retry))
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if ctype == "application/json":
            self.send_header("Cache-Control", "no-store")
        elif cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            return self.send(200, (ROOT / "web" / name).read_bytes(), ctype)
        if url.path.startswith("/images/"):
            f = IMAGES_DIR / url.path.rsplit("/", 1)[1]
            ctype = IMAGE_TYPES.get(f.suffix.lower())
            if not ctype or not f.is_file():
                return self.send(404, b"", "text/plain")
            return self.send(200, f.read_bytes(), ctype, cache="public, max-age=3600")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.send(*dispatch("GET", url.path, q, None, self.client_address[0]))

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 20_000:
                return self.send(413, {"error": "Request too large."})
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.send(400, {"error": "Some of the values sent are not valid."})
        self.send(*dispatch("POST", urlparse(self.path).path, None, body, self.client_address[0]))


def main(argv):
    if argv[:1] == ["backup"]:
        try:
            print(f"Backup written to {backup()}")
        except BackupError as e:
            print(f"Backup FAILED: {e}", file=sys.stderr)
            return 1
        return 0
    if argv[:1] != ["serve"]:
        print(__doc__, file=sys.stderr)
        return 2
    port = int(argv[1]) if len(argv) > 1 else 8001
    print(f"Serving on http://127.0.0.1:{port}")
    # ponytail: local-only, plain HTTP; put it behind HTTPS and a reverse proxy before exposing it publicly
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
