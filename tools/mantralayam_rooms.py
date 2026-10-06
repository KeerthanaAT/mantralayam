"""Mantralayam guest booking: public JSON API + guest website (stdlib only).

Usage:
  python tools/mantralayam_rooms.py serve [PORT]     # guest site at /, default port 8001

Reads and writes the existing SQLite database (.tmp/mantralayam.db, or MANTRALAYAM_DB).
It never creates tables or reseeds data. Rules live in dispatch() so tests call it directly;
the HTTP handler is a thin wrapper.
"""
import json
import os
import re
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
BUSY = ("Confirmed", "Checked-In")


class UserError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def today():
    return date.today()


def connect():
    if not DB.exists():
        raise UserError("The booking system is not set up yet. Please call the property.", 503)
    conn = sqlite3.connect(DB, timeout=10, isolation_level=None)  # manual BEGIN IMMEDIATE for writes
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def settings(conn):
    s = DEFAULTS | {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    return {"enabled": s["guest_booking_enabled"] == "1", "max_rooms": int(s["guest_max_rooms"]), "advance": int(s["guest_advance_days"]),
            "max_nights": int(s["guest_max_nights"]), "cutoff": int(s["guest_cancel_cutoff_hours"]),
            "checkin_time": s["checkin_time"], "checkout_time": s["checkout_time"]}


# ponytail: in-memory per-process counters, reset on restart; move to the DB if run as several processes
HITS = defaultdict(deque)


def throttle(key, limit, window, msg):
    now, q = time.monotonic(), HITS[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise UserError(msg, 429)
    q.append(now)


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
    last = today() + timedelta(days=s["advance"])
    if ci < today():
        raise UserError("Check-in date cannot be in the past.")
    if co <= ci:
        raise UserError("Check-out must be after check-in.")
    if ci > last:
        raise UserError(f"Online booking is open up to {s['advance']} days ahead (check-in on or before {last.isoformat()}).")
    if (co - ci).days > s["max_nights"]:
        raise UserError(f"Online bookings can be for at most {s['max_nights']} nights. For a longer stay please call the property.")
    return ci, co, (co - ci).days, s


def free_rooms(conn, ci, co, property_id=None, type_id=None):
    """Rooms genuinely free for [ci, co): active, no overlapping live booking, no blocking status event.
    Cleaning events only block stays that start today or earlier (the room is expected back by tomorrow).
    ponytail: that cleaning rule is inferred, confirm it with the property staff."""
    return conn.execute("""
        SELECT r.id, r.number, r.capacity, r.amenities, r.property_id, r.room_type_id,
               p.name AS pname, p.address, t.name AS tname, t.description AS tdesc, t.tariff
        FROM rooms r JOIN properties p ON p.id = r.property_id JOIN room_types t ON t.id = r.room_type_id
        WHERE r.active = 1 AND p.active = 1
          AND (:pid IS NULL OR r.property_id = :pid) AND (:tid IS NULL OR r.room_type_id = :tid)
          AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.room_id = r.id AND b.status IN ('Confirmed', 'Checked-In')
                          AND b.check_in < :co AND b.check_out > :ci)
          AND NOT EXISTS (SELECT 1 FROM room_status_events e WHERE e.room_id = r.id AND e.active = 1
                          AND e.start_date < :co AND (e.end_date IS NULL OR e.end_date > :ci)
                          AND (e.kind NOT IN ('cleaning_required', 'in_cleaning') OR :ci <= :today))
        ORDER BY p.id, t.tariff, r.number""",
        dict(ci=ci.isoformat(), co=co.isoformat(), pid=property_id, tid=type_id, today=today().isoformat())).fetchall()


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
    props = [dict(p) | {"room_types": [r["name"] for r in conn.execute(
        "SELECT DISTINCT t.name, t.tariff FROM rooms r JOIN room_types t ON t.id = r.room_type_id "
        "WHERE r.active = 1 AND r.property_id = ? ORDER BY t.tariff", (p["id"],))]}
        for p in conn.execute("SELECT id, name, address, description, facilities, contact_person, phone FROM properties WHERE active = 1 ORDER BY id")]
    return {"today": today().isoformat(), "last_check_in": (today() + timedelta(days=s["advance"])).isoformat(),
            "settings": {"guest_booking_enabled": s["enabled"], "checkin_time": s["checkin_time"], "checkout_time": s["checkout_time"],
                         "guest_cancel_cutoff_hours": s["cutoff"]},
            "properties": props, "room_types": [dict(r) for r in conn.execute("SELECT id, name FROM room_types ORDER BY tariff")]}


def search(conn, q, ip):
    throttle(("search", ip), 60, 60, "Too many searches. Please wait a minute and try again.")
    ci, co, nights, s = parse_stay(conn, q.get("check_in"), q.get("check_out"))
    guests = to_int(q.get("guests"), "Number of guests must be between 1 and 200.", 1, 200)
    groups = defaultdict(list)
    for r in free_rooms(conn, ci, co, opt_int(q.get("property_id")), opt_int(q.get("room_type_id"))):
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
    return {"check_in": ci.isoformat(), "check_out": co.isoformat(), "nights": nights, "guests": guests, "properties": list(props.values())}


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
    ci, co, nights, s = parse_stay(conn, b.get("check_in"), b.get("check_out"))
    if b.get("payment_method") != "pay_at_counter":
        raise UserError("Please choose a valid payment option.")
    pid, tid = to_int(b.get("property_id"), "Please choose a valid property and room type.", 1, 10**9), to_int(b.get("room_type_id"), "Please choose a valid property and room type.", 1, 10**9)
    n = to_int(b.get("rooms"), "Please choose how many rooms you need.", 1, s["max_rooms"])
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
    throttle(("book", phone[-10:]), 5, 600, "Too many booking attempts for this mobile number. Please wait a few minutes, or call the property.")

    def work():
        if not conn.execute("SELECT 1 FROM properties WHERE id = ? AND active = 1", (pid,)).fetchone():
            raise UserError("Please choose a valid property and room type.")
        free = free_rooms(conn, ci, co, pid, tid)
        if len(free) < n:
            raise UserError("Sorry, those rooms are no longer available for your dates. Please search again.", 409)
        # ponytail: takes the n largest free rooms of the type; rooms of one type are near-identical, so no fancier packing
        rooms = sorted(free, key=lambda r: (-r["capacity"], r["number"]))[:n]
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
        audit(conn, "booking.online_create", pid, None, f"#{ids[0]}: {n} room(s), {ci} to {co}, {guests} guests", f"Online booking, mobile {phone}")
        return ids[0]

    gid = write(conn, work)
    return booking_view(conn, find_booking(conn, gid, phone))


def lookup(conn, b, ip):
    throttle(("lookup", ip), 20, 600, "Too many attempts. Please wait a few minutes, or call the property.")
    return booking_view(conn, find_booking(conn, b.get("booking_id"), b.get("phone")))


def cancel(conn, b, ip):
    throttle(("lookup", ip), 20, 600, "Too many attempts. Please wait a few minutes, or call the property.")
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


ROUTES = {("GET", "/api/public/info"): info, ("GET", "/api/public/search"): search, ("POST", "/api/public/book"): book,
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
        return e.status, {"error": str(e)}
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
    def send(self, status, data, ctype="application/json"):
        data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if ctype == "application/json":
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            return self.send(200, (ROOT / "web" / name).read_bytes(), ctype)
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
    if argv[:1] != ["serve"]:
        print(__doc__, file=sys.stderr)
        return 2
    port = int(argv[1]) if len(argv) > 1 else 8001
    print(f"Serving on http://127.0.0.1:{port}")
    # ponytail: local-only, plain HTTP; put it behind HTTPS and a reverse proxy before exposing it publicly
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
