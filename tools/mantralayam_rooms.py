"""Mantralayam guest booking: public JSON API + guest website (stdlib only).

Usage:
  python tools/mantralayam_rooms.py serve [PORT]     # guest site at /, default port 8001
  python tools/mantralayam_rooms.py backup           # verified DB copy in backups/ (keeps 14); exit code 1 on failure
  python tools/mantralayam_rooms.py tick             # every 5 minutes: staff alerts and waitlist ("notify me") messages

Reads and writes the existing SQLite database (.tmp/mantralayam.db, or MANTRALAYAM_DB).
It never reseeds data; migrate() only adds missing columns or tables. Rules live in dispatch() so tests call it directly;
the HTTP handler is a thin wrapper.
"""
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import math
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import analytics
import extras
import idvault
import notifications
import payments
import providers
import reports
import qr as qr_module

ROOT = Path(__file__).resolve().parent.parent
payments.g = notifications.g = analytics.g = reports.g = extras.g = sys.modules[__name__]
DB = Path(os.environ.get("MANTRALAYAM_DB") or ROOT / ".tmp" / "mantralayam.db")
STATIC = {"/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"), "/sw.js": ("sw.js", "application/javascript"), "/": ("guest.html", "text/html"), "/shared.css": ("shared.css", "text/css"), "/tokens.css": ("tokens.css", "text/css"), "/i18n.js": ("i18n.js", "application/javascript")}
LANGS = ("en", "te", "kn", "ta", "hi")  # English is the source language; the others fall back to it per key
LOCALE_URL = re.compile(r"/locales/(" + "|".join(LANGS) + r")\.json\Z")
# Editable facts shown to guests: key -> (label for the admin screen, translatable). Nothing is invented here: a key with no value is a TODO and is not shown to guests.
SITE_CONFIG = {
    "rooms_phone": ("Contact: rooms phone", False), "whatsapp_number": ("Contact: rooms WhatsApp number (with country code)", False), "support_email": ("Contact: support email", False),
    "emergency_contacts": ("Contact: emergency numbers (one per line, for example Police: 100)", True), "medical_contacts": ("Contact: medical numbers (one per line)", True),
    "official_site_url": ("Official booking site address (https://...)", False),
    "cancellation_policy": ("Cancellation policy: details", True), "refund_policy": ("Cancellation policy: refund details", True),
    "id_rules": ("Know before you go: ID proof rules", True), "dress_code": ("Know before you go: dress code", True), "stay_rules": ("Know before you go: rules during the stay", True),
    "packing_guide": ("Know before you go: packing guide", True),
    "map_embed_url": ("Directions: map embed address (Google Maps or OpenStreetMap embed link)", False), "map_url": ("Directions: map link (https://...)", False),
    "distance_station": ("Directions: distance from the railway station", True), "distance_busstand": ("Directions: distance from the bus stand", True),
    "directions": ("Directions: how to reach", True), "transport_tips": ("Directions: transport tips", True),
    "darshan_timings": ("Pilgrim info: darshan timings", True), "pooja_timings": ("Pilgrim info: pooja timings", True), "seva_timings": ("Pilgrim info: seva timings", True),
    "annadanam_timings": ("Pilgrim info: annadanam (meals) timings", True),
    "place_tungabhadra": ("Nearby: Tungabhadra ghat", True), "place_panchamukhi": ("Nearby: Panchamukhi", True), "places_other": ("Nearby: other local temples", True),
    "privacy_policy": ("Privacy policy text", True), "privacy_contact": ("Privacy: who to contact", True)}
MAP_EMBED_PREFIXES = ("https://www.google.com/maps/embed", "https://www.openstreetmap.org/export/embed.html")  # the only map providers; no API key needed
SITE_TEXT_MAX = 2000
CSP_BASE = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
            "script-src 'self'{nonce}{cap}{pay_script}; frame-src https://www.google.com/maps/ https://www.openstreetmap.org/{cap}{pay_frame}; connect-src 'self'{pay_connect}; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
HTTPS_ONLY = os.environ.get("MANTRALAYAM_HTTPS", "").strip() == "1"  # behind a TLS proxy: Secure cookies, HSTS
TRUST_PROXY = os.environ.get("TRUST_PROXY", "").strip() == "1"  # believe X-Forwarded-For / -Proto from TRUSTED_PROXIES
TRUSTED_PROXIES = {x.strip() for x in os.environ.get("TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if x.strip()}
ALLOWED_HOSTS = {x.strip().lower() for x in os.environ.get("ALLOWED_HOSTS", "").split(",") if x.strip()}  # empty: any Host is accepted
BODY_MAX, STR_MAX = 20_000, 2000
SOCKET_TIMEOUT, MAX_CONNECTIONS = 15, 200  # seconds a client may stall; simultaneous connections per server
IMAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
ID_TYPES = ("Aadhaar", "Voter ID", "Driving Licence", "Passport", "PAN", "Other")
DEFAULTS = {"guest_booking_enabled": "1", "guest_max_rooms": "3", "guest_advance_days": "90", "guest_max_nights": "15",
            "guest_cancel_cutoff_hours": "24", "checkin_time": "14:00", "checkout_time": "11:00",
            "refund_percent": "", "refund_days": "",  # refund_* stay empty (not set) until the office decides; no policy number is assumed
            "tax_percent": "", "advance_percent": "", "extra_bed_price": "", "extra_bed_max": "1", "child_price": "", "child_counts_capacity": "1",  # pricing rules: empty = not charged
            "pay_options": "counter",  # which of full, advance, counter the guest may choose (full and advance need PAYMENT_PROVIDER)
            "reminder_days": "1", "reminder_hour": "9",  # arrival reminder: days before check-in (empty = no reminders) and the earliest hour to send it
            "id_retention_days": "", "checkin_requires_id": "1", "feedback_days": "1",
            "guest_retention_days": ""}  # guest_retention_days: guest names, mobiles and emails of stays that ended this many days ago are erased (empty = kept until someone sets it)  # ID documents are purged this many days after check-out (empty = kept until someone sets it)
IMAGES_DIR = ROOT / "web" / "images"
UPLOADS = Path(os.environ.get("MANTRALAYAM_UPLOADS") or ROOT / "uploads")  # photos staff upload (room types): outside the code folder, served at /uploads/<file>
UPLOAD_NAME = re.compile(r"room-\d+-[0-9a-f]{8}\.(jpg|png|webp)\Z")
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
# A "quick" call-back (the Call me back button) has only a name and phone, so people and dates may be empty; call_time and lang tell staff when and in what language to ring
CALLBACK_DDL = ("CREATE TABLE IF NOT EXISTS {} (id INTEGER PRIMARY KEY, property_id INTEGER REFERENCES properties(id), name TEXT NOT NULL, phone TEXT NOT NULL, "
                "people INTEGER, check_in TEXT, check_out TEXT, note TEXT, "
                "status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'contacted', 'follow_up', 'closed')), created_at TEXT NOT NULL, handled_by INTEGER REFERENCES users(id), handled_at TEXT, "
                "call_time TEXT CHECK (call_time IN ('any', 'morning', 'afternoon', 'evening')), lang TEXT, follow_up_on TEXT)")
CALL_TIMES = ("any", "morning", "afternoon", "evening")


def seed_faqs_from_callbacks(conn):
    """Turns distinct call-back request notes into inactive FAQ drafts (English question, answer TODO) for an admin to answer and review: notes are guest text and may
    contain personal details. Skips notes already used as a question. Returns how many drafts were created."""
    made = 0
    for (note,) in conn.execute("SELECT DISTINCT TRIM(note) FROM callback_requests WHERE note IS NOT NULL AND TRIM(note) <> '' ORDER BY 1 LIMIT 50").fetchall():
        if conn.execute("SELECT 1 FROM faq_text WHERE lang = 'en' AND question = ?", (note,)).fetchone():
            continue
        fid = conn.execute("INSERT INTO faqs (position, active) VALUES (?, 0)", (made + 1,)).lastrowid
        conn.execute("INSERT INTO faq_text (faq_id, lang, question, answer) VALUES (?, 'en', ?, 'TODO')", (fid, note))
        made += 1
    return made


def migrate(conn):
    """Idempotent additive schema changes, run once per process and database file. Never drops or rewrites data."""
    if "active" not in {r[1] for r in conn.execute("PRAGMA table_info(room_types)")}:
        conn.execute("ALTER TABLE room_types ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    have = {r[1] for r in conn.execute("PRAGMA table_info(bookings)")}
    for col in ("arrival_time", "darshan_date"):  # optional arrival details, added without touching existing rows
        if col not in have:
            conn.execute(f"ALTER TABLE bookings ADD COLUMN {col} TEXT")
    conn.execute("CREATE TABLE IF NOT EXISTS holds (id INTEGER PRIMARY KEY, token TEXT NOT NULL, room_id INTEGER NOT NULL REFERENCES rooms(id), "
                 "check_in TEXT NOT NULL, check_out TEXT NOT NULL, expires_at TEXT NOT NULL, ip TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_holds_room ON holds(room_id, expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_holds_token ON holds(token)")
    conn.execute("CREATE TABLE IF NOT EXISTS peak_periods (id INTEGER PRIMARY KEY, name TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, "
                 "max_nights INTEGER, max_rooms INTEGER, note TEXT, active INTEGER NOT NULL DEFAULT 1, CHECK (end_date >= start_date))")
    conn.execute(CALLBACK_DDL.format("callback_requests"))
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(callback_requests)")}
    old_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'callback_requests'").fetchone()[0]
    if cols["check_in"][3] or cols["people"][3] or "call_time" not in cols or "follow_up" not in old_sql:  # older table: people and dates were required, or no follow_up status. Rebuild it, copying every row
        conn.execute("ALTER TABLE callback_requests RENAME TO callback_requests_old")
        conn.execute(CALLBACK_DDL.format("callback_requests"))
        keep = ", ".join(c for c in cols if c in ("id", "property_id", "name", "phone", "people", "check_in", "check_out", "note", "status", "created_at", "handled_by", "handled_at", "call_time", "lang", "follow_up_on"))
        conn.execute(f"INSERT INTO callback_requests ({keep}) SELECT {keep} FROM callback_requests_old")
        conn.execute("DROP TABLE callback_requests_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_callbacks_status ON callback_requests(status, created_at)")
    new_faq = not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'faqs'").fetchone()
    conn.execute("CREATE TABLE IF NOT EXISTS faqs (id INTEGER PRIMARY KEY, position INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1)")
    conn.execute("CREATE TABLE IF NOT EXISTS faq_text (faq_id INTEGER NOT NULL REFERENCES faqs(id), lang TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL, PRIMARY KEY (faq_id, lang))")
    if new_faq:
        seed_faqs_from_callbacks(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS site_config (key TEXT NOT NULL, lang TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (key, lang))")
    migrate_v2(conn)


def add_cols(conn, table, cols):
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for col, ddl in cols.items():
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def migrate_v2(conn):
    """Production hardening and booking-flow tables and columns (all additive)."""
    add_cols(conn, "rooms", {"senior_friendly": "INTEGER NOT NULL DEFAULT 0"})
    add_cols(conn, "users", {"totp_secret": "BLOB", "totp_enabled": "INTEGER NOT NULL DEFAULT 0", "totp_step": "INTEGER NOT NULL DEFAULT 0"})  # staff two-factor sign-in (tools/totp.py)
    add_cols(conn, "peak_periods", {"surcharge_percent": "REAL"})
    add_cols(conn, "bookings", {"children": "INTEGER", "extra_beds": "INTEGER", "tax_amount": "REAL", "discount_amount": "REAL", "promo_code": "TEXT", "advance_due": "REAL",
                                "booker_name": "TEXT", "arrival_contact_name": "TEXT", "arrival_contact_phone": "TEXT", "price_json": "TEXT"})
    conn.execute("CREATE TABLE IF NOT EXISTS rate_hits (key TEXT NOT NULL, ts REAL NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_hits ON rate_hits(key, ts)")
    conn.execute("CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL, "
                 "resolved_at TEXT, emailed_at TEXT, email_note TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(resolved_at, kind, key)")
    conn.execute("CREATE TABLE IF NOT EXISTS promo_codes (id INTEGER PRIMARY KEY, code TEXT COLLATE NOCASE, label TEXT NOT NULL, auto INTEGER NOT NULL DEFAULT 0, "
                 "kind TEXT NOT NULL CHECK (kind IN ('percent', 'flat')), value REAL NOT NULL, max_discount REAL, min_rooms INTEGER, min_guests INTEGER, min_nights INTEGER, "
                 "valid_from TEXT, valid_to TEXT, max_uses INTEGER, per_phone_limit INTEGER, exclude_peak INTEGER NOT NULL DEFAULT 1, property_id INTEGER REFERENCES properties(id), "
                 "room_type_id INTEGER REFERENCES room_types(id), active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, CHECK (auto = 1 OR code IS NOT NULL))")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_promo_code ON promo_codes(code) WHERE code IS NOT NULL")
    conn.execute("CREATE TABLE IF NOT EXISTS otp_codes (id INTEGER PRIMARY KEY, phone10 TEXT NOT NULL, purpose TEXT NOT NULL, salt TEXT NOT NULL, code_hash TEXT NOT NULL, "
                 "created_at TEXT NOT NULL, expires_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, verified_at TEXT, proof_hash TEXT, proof_expires TEXT, used_at TEXT, ip TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_otp_phone ON otp_codes(phone10, purpose, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_otp_proof ON otp_codes(proof_hash)")
    conn.execute("CREATE TABLE IF NOT EXISTS guest_sessions (token_hash TEXT PRIMARY KEY, phone10 TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS guest_profiles (phone10 TEXT PRIMARY KEY, name TEXT, email TEXT, id_type TEXT, consented_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS waitlist (id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL, phone10 TEXT NOT NULL, email TEXT, lang TEXT, "
                 "property_id INTEGER REFERENCES properties(id), room_type_id INTEGER REFERENCES room_types(id), check_in TEXT NOT NULL, check_out TEXT NOT NULL, guests INTEGER NOT NULL, "
                 "rooms INTEGER NOT NULL DEFAULT 1, senior INTEGER NOT NULL DEFAULT 0, ground INTEGER NOT NULL DEFAULT 0, "
                 "status TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'notified', 'booked', 'expired', 'cancelled')), created_at TEXT NOT NULL, notified_at TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_waitlist_status ON waitlist(status, check_in)")
    migrate_v3(conn)
    migrate_v4(conn)


def migrate_v4(conn):
    """Analytics counters (no personal data)."""
    conn.execute("CREATE TABLE IF NOT EXISTS analytics_daily (day TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL DEFAULT '', n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, kind, key))")
    migrate_v5(conn)


def migrate_v5(conn):
    """Sevas, packages, festivals, panchanga, notices, feedback, newsletter, room photos."""
    add_cols(conn, "room_types", {"photo": "TEXT"})
    add_cols(conn, "payments", {"property_id": "INTEGER"})  # which property an order was for, known from the start (a refunded payment that never became a booking has no booking to say)
    conn.execute("UPDATE payments SET property_id = (SELECT MIN(property_id) FROM bookings b WHERE COALESCE(b.group_id, b.id) = payments.group_id) WHERE property_id IS NULL AND group_id IS NOT NULL")
    ddl = ["CREATE TABLE IF NOT EXISTS sevas (id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT, price REAL NOT NULL CHECK (price >= 0), max_per_day INTEGER, per_guest INTEGER NOT NULL DEFAULT 0, "
           "position INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1)",
           "CREATE TABLE IF NOT EXISTS packages (id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT, discount_percent REAL NOT NULL DEFAULT 0 CHECK (discount_percent >= 0 AND discount_percent <= 100), "
           "min_nights INTEGER, active INTEGER NOT NULL DEFAULT 1)",
           "CREATE TABLE IF NOT EXISTS package_items (package_id INTEGER NOT NULL REFERENCES packages(id), seva_id INTEGER NOT NULL REFERENCES sevas(id), day_offset INTEGER NOT NULL DEFAULT 0, "
           "per_guest INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (package_id, seva_id, day_offset))",
           "CREATE TABLE IF NOT EXISTS seva_bookings (id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL, seva_id INTEGER NOT NULL REFERENCES sevas(id), package_id INTEGER, seva_date TEXT NOT NULL, "
           "qty INTEGER NOT NULL, unit_price REAL NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL DEFAULT 'Confirmed' CHECK (status IN ('Confirmed', 'Cancelled')), created_at TEXT NOT NULL)",
           "CREATE INDEX IF NOT EXISTS idx_seva_day ON seva_bookings(seva_id, seva_date, status)",
           "CREATE INDEX IF NOT EXISTS idx_seva_group ON seva_bookings(group_id)",
           "CREATE TABLE IF NOT EXISTS festivals (id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'festival', start_date TEXT NOT NULL, end_date TEXT NOT NULL, note TEXT, "
           "peak_period_id INTEGER REFERENCES peak_periods(id), active INTEGER NOT NULL DEFAULT 1, CHECK (end_date >= start_date))",
           "CREATE TABLE IF NOT EXISTS panchanga (day TEXT PRIMARY KEY, tithi TEXT, nakshatra TEXT, note TEXT)",
           "CREATE TABLE IF NOT EXISTS notices (id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('weather', 'river', 'general')), level TEXT NOT NULL CHECK (level IN ('info', 'watch', 'warning')), "
           "text TEXT NOT NULL, starts_on TEXT NOT NULL, ends_on TEXT, active INTEGER NOT NULL DEFAULT 1, created_by INTEGER, created_name TEXT, created_at TEXT NOT NULL)",
           "CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL UNIQUE, property_id INTEGER NOT NULL, rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5), "
           "cleanliness INTEGER, service INTEGER, comment TEXT, status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'reviewed')), staff_note TEXT, reviewed_by TEXT, reviewed_at TEXT, created_at TEXT NOT NULL)",
           "CREATE TABLE IF NOT EXISTS subscribers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, phone10 TEXT, email TEXT, lang TEXT, sms INTEGER NOT NULL DEFAULT 0, wa INTEGER NOT NULL DEFAULT 0, "
           "em INTEGER NOT NULL DEFAULT 0, ph_ok INTEGER NOT NULL DEFAULT 0, em_ok INTEGER NOT NULL DEFAULT 0, token TEXT NOT NULL UNIQUE, consent_at TEXT NOT NULL, consent_text TEXT, unsubscribed_at TEXT, source TEXT)",
           "CREATE INDEX IF NOT EXISTS idx_sub_phone ON subscribers(phone10)",
           "CREATE TABLE IF NOT EXISTS broadcasts (id INTEGER PRIMARY KEY, title TEXT NOT NULL, texts TEXT NOT NULL, channels TEXT NOT NULL, festival_id INTEGER, created_by INTEGER, created_by_name TEXT, "
           "created_at TEXT NOT NULL, sent_at TEXT, recipients INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0)",
           "CREATE TABLE IF NOT EXISTS broadcast_log (id INTEGER PRIMARY KEY, broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id), subscriber_id INTEGER NOT NULL, channel TEXT NOT NULL, status TEXT NOT NULL, ts TEXT NOT NULL)"]
    for d in ddl:
        conn.execute(d)


def migrate_v3(conn):
    """Payments, refunds, notifications, ID records, call-back notes, secrets and the overlap guard (all additive)."""
    add_cols(conn, "bookings", {"lang": "TEXT"})
    conn.execute("CREATE TABLE IF NOT EXISTS app_secrets (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, order_id TEXT NOT NULL UNIQUE, payment_id TEXT UNIQUE, provider TEXT NOT NULL, "
                 "kind TEXT NOT NULL CHECK (kind IN ('full', 'advance', 'counter')), amount_paise INTEGER NOT NULL CHECK (amount_paise > 0), total_paise INTEGER, currency TEXT NOT NULL DEFAULT 'INR', "
                 "status TEXT NOT NULL CHECK (status IN ('created', 'paid', 'abandoned', 'expired', 'unfulfilled')), method TEXT, group_id INTEGER, hold_token TEXT, payload BLOB, phone10 TEXT, "
                 "created_at TEXT NOT NULL, expires_at TEXT, paid_at TEXT, failure_reason TEXT, ip TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_group ON payments(group_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_open ON payments(status, expires_at)")
    conn.execute("CREATE TABLE IF NOT EXISTS refunds (id INTEGER PRIMARY KEY, payment_row INTEGER NOT NULL REFERENCES payments(id), group_id INTEGER, amount_paise INTEGER NOT NULL CHECK (amount_paise > 0), "
                 "status TEXT NOT NULL CHECK (status IN ('requested', 'sending', 'pending', 'processed', 'failed', 'manual')), reason TEXT, provider_refund_id TEXT UNIQUE, dedupe_key TEXT UNIQUE, "
                 "attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_refunds_group ON refunds(group_id, status)")
    conn.execute("CREATE TABLE IF NOT EXISTS webhook_events (event_id TEXT PRIMARY KEY, kind TEXT, received_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS notification_log (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, group_id INTEGER NOT NULL, event TEXT NOT NULL, "
                 "channel TEXT NOT NULL CHECK (channel IN ('sms', 'whatsapp', 'email')), to_hint TEXT, provider TEXT, status TEXT NOT NULL CHECK (status IN ('sent', 'failed', 'skipped')), "
                 "attempts INTEGER NOT NULL DEFAULT 1, detail TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notif_group ON notification_log(group_id, event)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notif_status ON notification_log(status, ts)")
    conn.execute("CREATE TABLE IF NOT EXISTS id_records (id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL UNIQUE REFERENCES bookings(id), id_type TEXT NOT NULL, last4 TEXT, "
                 "number_enc BLOB, image_enc BLOB, image_mime TEXT, captured_by INTEGER, captured_name TEXT, captured_at TEXT NOT NULL, purged_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS callback_notes (id INTEGER PRIMARY KEY, callback_id INTEGER NOT NULL REFERENCES callback_requests(id), user_id INTEGER, user_name TEXT NOT NULL, "
                 "note TEXT NOT NULL, status_to TEXT, created_at TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cbnotes ON callback_notes(callback_id, id)")
    # the last guard against a double booking: whatever code writes to bookings, a room cannot hold two live stays that overlap
    live = "('Confirmed', 'Checked-In')"
    conn.execute("CREATE TRIGGER IF NOT EXISTS bookings_no_overlap_ins BEFORE INSERT ON bookings WHEN NEW.status IN " + live + " BEGIN "
                 "SELECT RAISE(ABORT, 'room already booked for these dates') WHERE EXISTS (SELECT 1 FROM bookings b WHERE b.room_id = NEW.room_id AND b.status IN " + live +
                 " AND b.check_in < NEW.check_out AND b.check_out > NEW.check_in); END")
    conn.execute("CREATE TRIGGER IF NOT EXISTS bookings_no_overlap_upd BEFORE UPDATE OF room_id, check_in, check_out, status ON bookings WHEN NEW.status IN " + live + " BEGIN "
                 "SELECT RAISE(ABORT, 'room already booked for these dates') WHERE EXISTS (SELECT 1 FROM bookings b WHERE b.id != NEW.id AND b.room_id = NEW.room_id AND b.status IN " + live +
                 " AND b.check_in < NEW.check_out AND b.check_out > NEW.check_in); END")


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
            "checkin_time": s["checkin_time"], "checkout_time": s["checkout_time"],
            "refund_percent": int(s["refund_percent"]) if s["refund_percent"] != "" else None, "refund_days": int(s["refund_days"]) if s["refund_days"] != "" else None,
            "tax_percent": dec_or_none(s["tax_percent"]), "advance_percent": dec_or_none(s["advance_percent"]), "extra_bed_price": dec_or_none(s["extra_bed_price"]),
            "extra_bed_max": int(s["extra_bed_max"] or 0), "child_price": dec_or_none(s["child_price"]), "child_counts_capacity": s["child_counts_capacity"] != "0",
            "pay_options": tuple(x for x in (y.strip() for y in s["pay_options"].split(",")) if x in payments.OPTIONS) or ("counter",),
            "reminder_days": int(s["reminder_days"]) if s["reminder_days"] != "" else None, "reminder_hour": int(s["reminder_hour"] or 9),
            "id_retention_days": int(s["id_retention_days"]) if s["id_retention_days"] != "" else None, "checkin_requires_id": s["checkin_requires_id"] != "0",
            "feedback_days": int(s["feedback_days"]) if s["feedback_days"] != "" else None,
            "guest_retention_days": int(s["guest_retention_days"]) if s["guest_retention_days"] != "" else None}


def dec_or_none(v):
    try:
        return Decimal(str(v).strip()) if str(v).strip() != "" else None
    except ArithmeticError:
        return None


def refund_for(paid, percent):
    """Rupees to refund when `paid` rupees were collected and the policy refunds `percent` (0 to 100), rounded half up to the paisa. No policy set (None) or nothing paid: 0."""
    if percent is None or not paid or paid <= 0:
        return 0
    return float((Decimal(str(paid)) * percent / 100).quantize(Decimal("0.01"), ROUND_HALF_UP))


# ---------- rate limits (in the database, so they survive restarts and are shared by both servers) ----------
def rate_key(key):
    return "|".join(map(str, key)) if isinstance(key, tuple) else str(key)


def rate(key, limit, window, msg, add=True):
    """Counts hits of `key` in the last `window` seconds. At `limit` it raises a 429 UserError with the wait; otherwise it records a hit (when add) and returns.
    Counts live in rate_hits, so they survive restarts and are shared by both servers. Uses its own short connection: never call it inside write()."""
    k, t = rate_key(key), time.time()
    conn = sqlite3.connect(DB, timeout=10, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM rate_hits WHERE key = ? AND ts <= ?", (k, t - window))
            n, first = conn.execute("SELECT COUNT(*), MIN(ts) FROM rate_hits WHERE key = ?", (k,)).fetchone()
            if n < limit and add:
                conn.execute("INSERT INTO rate_hits (key, ts) VALUES (?, ?)", (k, t))
                if random.random() < 0.02:  # housekeeping: nothing is ever needed for longer than a day
                    conn.execute("DELETE FROM rate_hits WHERE ts < ?", (t - 86400,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    if n >= limit:
        wait = max(1, math.ceil(first + window - t))
        raise UserError(f"{msg} Please try again in {f'{wait} seconds' if wait < 90 else f'{math.ceil(wait / 60)} minutes'}.", 429, wait)


def throttle(key, limit, window, msg):
    rate(key, limit, window, msg)


def rate_clear(key):
    conn = sqlite3.connect(DB, timeout=10, isolation_level=None)
    try:
        conn.execute("DELETE FROM rate_hits WHERE key = ?", (rate_key(key),))
    finally:
        conn.close()


def reset_throttle(prefix=""):
    """Forget the counters (tests, or an admin unblocking someone). With a prefix only the keys starting with it."""
    conn = sqlite3.connect(DB, timeout=10, isolation_level=None)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS rate_hits (key TEXT NOT NULL, ts REAL NOT NULL)")
        conn.execute("DELETE FROM rate_hits WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))
    finally:
        conn.close()


# ---------- request security: headers, proxy, origin, input shape ----------
def csp(nonce=None):
    """script-src has no 'unsafe-inline': the page's own inline scripts carry a per-response nonce (see with_nonce)."""
    cap = providers.captcha_config()
    rz = providers.env("PAYMENT_PROVIDER") == "razorpay"  # only a Razorpay deployment lets the page load Razorpay Checkout
    return CSP_BASE.format(nonce=f" 'nonce-{nonce}'" if nonce else "", cap=" https://challenges.cloudflare.com" if cap and cap["provider"] == "turnstile" else "", pay_script=" https://checkout.razorpay.com" if rz else "",
                           pay_frame=" https://api.razorpay.com https://checkout.razorpay.com" if rz else "", pay_connect=" https://api.razorpay.com https://lumberjack.razorpay.com" if rz else "")


def is_https(headers):
    return HTTPS_ONLY or (TRUST_PROXY and headers.get("X-Forwarded-Proto", "").lower() == "https")


def with_nonce(data, ctype):
    """-> (data, nonce). An HTML page marks its inline <script> tags with __NONCE__; each response gets a fresh random value, also named in the CSP header."""
    if ctype != "text/html" or b"__NONCE__" not in data:
        return data, None
    nonce = secrets.token_urlsafe(16)
    return data.replace(b"__NONCE__", nonce.encode()), nonce


def security_headers(headers, nonce=None):
    out = [("Content-Security-Policy", csp(nonce)), ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "same-origin"), ("X-Frame-Options", "DENY"),
           ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(self" + (' "https://checkout.razorpay.com" "https://api.razorpay.com"' if providers.env("PAYMENT_PROVIDER") == "razorpay" else "") + ")"), ("Cross-Origin-Opener-Policy", "same-origin")]
    if is_https(headers):
        out.append(("Strict-Transport-Security", "max-age=31536000; includeSubDomains"))  # no "preload" until the owner decides to submit the domain
    return out


def ip_key(ip):
    """Rate limits and holds count an IPv6 visitor by its /64 block: whoever has one can switch between 2^64 addresses, so the exact address means nothing."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if a.version == 6 and a.ipv4_mapped:
        return str(a.ipv4_mapped)
    return f"{ipaddress.ip_network(f'{a}/64', strict=False).network_address}/64" if a.version == 6 else str(a)


def client_ip(headers, peer):
    return ip_key(client_ip_raw(headers, peer))


def client_ip_raw(headers, peer):
    """The visitor's address. Only when TRUST_PROXY=1 and the connection comes from a trusted proxy is X-Forwarded-For read, from the right:
    the first address that is not itself a trusted proxy (anything a visitor writes further left is ignored)."""
    if not (TRUST_PROXY and peer in TRUSTED_PROXIES):
        return peer
    for hop in reversed([h.strip() for h in headers.get("X-Forwarded-For", "").split(",") if h.strip()]):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return peer  # a malformed header is not trusted at all
        if hop not in TRUSTED_PROXIES:
            return hop
    return peer


def request_ok(headers, method, path=""):
    """None if the request may go on, else a (status, message) to send. Checks the Host allow-list and, for POST, that the Origin is exactly this site
    (a POST with no Origin is refused when MANTRALAYAM_HTTPS=1, except the payment provider's webhook, which is signed instead)."""
    host = headers.get("Host", "").lower()
    if ALLOWED_HOSTS and host.split(":")[0] not in ALLOWED_HOSTS:
        return 400, "Unknown host."
    origin = headers.get("Origin")
    if method == "POST" and origin is not None:
        if urlparse(origin).netloc.lower() != host:
            return 403, "This request came from another site and was blocked."
    elif method == "POST" and HTTPS_ONLY and path != "/api/public/payment/webhook":
        return 403, "This request came from another site and was blocked."
    return None


def clean_body(v, max_str=STR_MAX, depth=0):
    """Checks the shape of JSON input: only objects, lists, text, numbers, booleans and null, nested at most 4 deep, at most 60 keys or items per level,
    text at most max_str characters. Control characters (except new lines and tabs) are dropped. Returns the cleaned copy."""
    bad = UserError("Some of the values sent are not valid.")
    if depth > 4:
        raise bad
    if isinstance(v, str):
        if len(v) > max_str:
            raise bad
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v)
    if isinstance(v, dict):
        if len(v) > 60 or not all(isinstance(k, str) and len(k) <= 64 for k in v):
            raise bad
        return {k: clean_body(x, max_str, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        if len(v) > 60:
            raise bad
        return [clean_body(x, max_str, depth + 1) for x in v]
    if v is None or isinstance(v, (bool, int, float)):
        return v
    raise bad


def csrf_token(session_token):
    """The CSRF token of a cookie session: derived from the (secret) session token, so nothing extra is stored. The page sends it as X-CSRF-Token on every POST."""
    return hashlib.sha256(b"csrf|" + str(session_token or "").encode()).hexdigest()


def csrf_ok(session_token, sent):
    return bool(session_token) and hmac.compare_digest(csrf_token(session_token), str(sent or ""))


def captcha_check(b, ip):
    """The optional CAPTCHA on public forms (CAPTCHA_PROVIDER), on top of the honeypot. Off by default."""
    if not providers.captcha_ok(b.get("captcha_token"), ip):
        raise UserError("Please complete the verification check and try again.", 400)


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
        raise UserError((f"During {s['peak_nights']} online" if s["peak_nights"] else "Online") + f" bookings can be for at most {s['max_nights']} nights. For a longer stay please call the property.")


def peak_periods(conn, ci, co):
    """Active peak periods touching the nights [ci, co): a period's end date is its last night, inclusive."""
    return [dict(r) for r in conn.execute("SELECT id, name, start_date, end_date, max_nights, max_rooms, note, surcharge_percent FROM peak_periods "
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


def room_filters(b):
    """The optional room preferences of a search or booking: senior-citizen-friendly rooms, ground-floor (accessible) rooms."""
    on = lambda v: str(v).lower() in ("1", "true", "yes", "on")
    return {"senior": on(b.get("senior")), "ground": on(b.get("ground_floor"))}


def free_rooms(conn, ci, co, property_id=None, type_id=None, hold=None, senior=False, ground=False, skip=()):
    """Rooms genuinely free for [ci, co): active, no overlapping live booking, no blocking status event.
    Cleaning events only block stays that start today or earlier (the room is expected back by tomorrow).
    Unexpired guest holds block too, except the one whose token is `hold` (the caller's own). `skip`: booking row ids that do not count (a booking being moved).
    senior / ground keep only senior-friendly rooms / ground-floor rooms.
    ponytail: that cleaning rule is inferred, confirm it with the property staff."""
    return conn.execute("""
        SELECT r.id, r.number, r.floor, r.senior_friendly, r.capacity, r.amenities, r.property_id, r.room_type_id,
               p.name AS pname, p.address, t.name AS tname, t.description AS tdesc, t.tariff, t.photo AS tphoto
        FROM rooms r JOIN properties p ON p.id = r.property_id JOIN room_types t ON t.id = r.room_type_id
        WHERE r.active = 1 AND p.active = 1 AND t.active = 1
          AND (:pid IS NULL OR r.property_id = :pid) AND (:tid IS NULL OR r.room_type_id = :tid)
          AND (:senior = 0 OR r.senior_friendly = 1) AND (:ground = 0 OR r.floor = 0)
          AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.room_id = r.id AND b.status IN ('Confirmed', 'Checked-In')
                          AND b.check_in < :co AND b.check_out > :ci AND b.id NOT IN (SELECT value FROM json_each(:skip)))
          AND NOT EXISTS (SELECT 1 FROM room_status_events e WHERE e.room_id = r.id AND e.active = 1
                          AND e.start_date < :co AND (e.end_date IS NULL OR e.end_date > :ci)
                          AND (e.kind NOT IN ('cleaning_required', 'in_cleaning') OR :ci <= :today))
          AND NOT EXISTS (SELECT 1 FROM holds h WHERE h.room_id = r.id AND h.expires_at > :now AND h.check_in < :co AND h.check_out > :ci
                          AND (:hold IS NULL OR h.token != :hold))
        ORDER BY p.id, t.tariff, r.number""",
        dict(ci=ci.isoformat(), co=co.isoformat(), pid=property_id, tid=type_id, today=today().isoformat(), now=now_iso(), hold=hold or None,
             senior=int(senior), ground=int(ground), skip=json.dumps(list(skip)))).fetchall()


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


def is_todo(v):
    return not v or not v.strip() or v.strip().upper().startswith("TODO")


def site_check(key, value):
    """Validates a site config value by kind (phone, WhatsApp, email, links). Raises UserError with a message for the admin."""
    if key == "rooms_phone" and not re.fullmatch(r"\+?[\d\s\-]{6,20}", value):
        raise UserError("The phone number may contain only digits, spaces, + and -.")
    if key == "whatsapp_number" and not re.fullmatch(r"\+?\d[\d\s\-]{8,18}\d", value):
        raise UserError("The WhatsApp number must include the country code, for example +91 98765 43210.")
    if key == "support_email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise UserError("Please enter a valid email address.")
    if key in ("map_url", "official_site_url") and not re.fullmatch(r"https://[^\s<>\"']+", value):
        raise UserError("The address must start with https:// and contain no spaces.")
    if key == "map_embed_url" and not (value.startswith(MAP_EMBED_PREFIXES) and re.fullmatch(r"[^\s<>\"']+", value)):
        raise UserError("Use the embed link from Google Maps (Share > Embed a map) or OpenStreetMap (Share > HTML), starting with " + " or ".join(MAP_EMBED_PREFIXES) + ".")


def site_values(conn, lang):
    """Guest-visible site facts for `lang`, falling back to English per key. Unset, TODO and invalid values are left out, never shown to guests."""
    rows = conn.execute("SELECT key, lang, value FROM site_config WHERE lang IN ('en', ?)", (lang if lang in LANGS else "en",)).fetchall()
    out = {}
    for r in sorted(rows, key=lambda r: r["lang"] != "en"):  # English first, so the chosen language overwrites it
        if r["key"] in SITE_CONFIG and not is_todo(r["value"]):
            try:
                site_check(r["key"], r["value"])
            except UserError:
                continue
            out[r["key"]] = r["value"]
    return out


def faq_list(conn, lang):
    """Active FAQs in order, in `lang` where both question and answer are translated, else in English. Entries whose answer is still TODO are skipped."""
    out = []
    for f in conn.execute("SELECT id FROM faqs WHERE active = 1 ORDER BY position, id").fetchall():
        t = {r["lang"]: r for r in conn.execute("SELECT lang, question, answer FROM faq_text WHERE faq_id = ? AND lang IN ('en', ?)", (f["id"], lang if lang in LANGS else "en"))}
        pick = next((t[k] for k in (lang, "en") if k in t and not is_todo(t[k]["question"]) and not is_todo(t[k]["answer"])), None)
        if pick:
            out.append({"id": f["id"], "question": pick["question"], "answer": pick["answer"]})
    return out


def info(conn, q, ip):
    s = settings(conn)
    types = lambda pid: conn.execute(
        "SELECT DISTINCT t.id, t.name, t.tariff FROM rooms r JOIN room_types t ON t.id = r.room_type_id "
        "WHERE r.active = 1 AND t.active = 1 AND r.property_id = ? ORDER BY t.tariff", (pid,)).fetchall()
    props = [dict(p) | {"room_types": [r["name"] for r in types(p["id"])], "type_ids": [r["id"] for r in types(p["id"])]}
        for p in conn.execute("SELECT id, name, address, description, facilities, contact_person, phone FROM properties WHERE active = 1 ORDER BY id")]
    f = lambda d: None if d is None else float(d)
    return {"site": site_values(conn, (q or {}).get("lang")), "today": today().isoformat(), "last_check_in": (today() + timedelta(days=s["advance"])).isoformat(),
            "settings": {"guest_booking_enabled": s["enabled"], "checkin_time": s["checkin_time"], "checkout_time": s["checkout_time"],
                         "guest_cancel_cutoff_hours": s["cutoff"], "refund_percent": s["refund_percent"], "refund_days": s["refund_days"]},
            "pricing": {"tax_percent": f(s["tax_percent"]), "advance_percent": f(s["advance_percent"]), "extra_bed_price": f(s["extra_bed_price"]), "extra_bed_max": s["extra_bed_max"],
                        "child_price": f(s["child_price"]), "child_counts_capacity": s["child_counts_capacity"]},
            "filters": {"senior": bool(conn.execute("SELECT 1 FROM rooms WHERE active = 1 AND senior_friendly = 1 LIMIT 1").fetchone()),
                        "ground": bool(conn.execute("SELECT 1 FROM rooms WHERE active = 1 AND floor = 0 LIMIT 1").fetchone())},
            "payments": payments.public_config(s), "catalog": extras.catalog(conn), "notices": extras.active_notices(conn),
            "festivals": extras.festivals_between(conn, today().isoformat(), (today() + timedelta(days=120)).isoformat())[0][:6], "staff_url": providers.env("STAFF_URL", "http://127.0.0.1:8002/staff"), "captcha": providers.captcha_config(), "otp_required": OTP_REQUIRED, "otp_ttl": OTP_TTL, "otp_resend": OTP_COOLDOWN,
            "faqs": faq_list(conn, (q or {}).get("lang")),
            "peak_periods": [dict(r) for r in conn.execute("SELECT name, start_date, end_date, max_nights, max_rooms, note, surcharge_percent FROM peak_periods WHERE active = 1 AND end_date >= ? "
                                                           "ORDER BY start_date, id", (today().isoformat(),))],
            "properties": props, "room_types": [dict(r) for r in conn.execute("SELECT id, name FROM room_types WHERE active = 1 AND id IN (SELECT room_type_id FROM rooms WHERE active = 1) ORDER BY tariff")]}


# ---------- prices: nightly rates, the price breakdown, promo and group-rate rules ----------
CENT = Decimal("0.01")


def money(v):
    return Decimal(str(v)).quantize(CENT, ROUND_HALF_UP)


def split(total, n):
    """`total` rupees divided over n booking rows to the paisa, so the rows add up exactly to the total."""
    paise = int(money(total) * 100)
    base, extra = divmod(paise, n)
    return [float(Decimal(base + (i < extra)) / 100) for i in range(n)]


def night_prices(tariff, peaks, ci, co):
    """One entry per night [ci, co): the date, the price (tariff plus the largest surcharge_percent of the peak periods covering that night) and the peak period's name or None."""
    out, d = [], ci
    while d < co:
        iso = d.isoformat()
        hit = [p for p in peaks if p["start_date"] <= iso <= p["end_date"]]
        pct = max((Decimal(str(p.get("surcharge_percent") or 0)) for p in hit), default=Decimal(0))
        out.append({"date": iso, "price": float(money(Decimal(str(tariff)) * (100 + pct) / 100)), "peak": hit[0]["name"] if hit else None})
        d += timedelta(days=1)
    return out


def promo_key(r):
    return r["code"] or f"auto:{r['id']}"


def promo_uses(conn, key, phone=None, skip_group=None):
    """Bookings (not rooms) that used a promo and are not cancelled; optionally only this mobile's, and without the booking group being changed."""
    q = "SELECT COUNT(DISTINCT COALESCE(group_id, id)) FROM bookings WHERE promo_code = ? AND status != 'Cancelled' AND COALESCE(group_id, id) IS NOT ?"
    args = [key, skip_group]
    if phone:
        q += " AND substr(replace(replace(phone, ' ', ''), '-', ''), -10) = ?"
        args.append(phone[-10:])
    return conn.execute(q, args).fetchone()[0]


def check_promo(conn, r, st):
    """Raises UserError when promo row r cannot be used for stay `st`; silent when it can."""
    t = today().isoformat()
    if r["valid_from"] and t < r["valid_from"]:
        raise UserError("This code is not valid yet.")
    if r["valid_to"] and t > r["valid_to"]:
        raise UserError("This code has expired.")
    if (r["property_id"] and r["property_id"] != st["pid"]) or (r["room_type_id"] and r["room_type_id"] != st["tid"]) \
            or (r["min_rooms"] and st["rooms"] < r["min_rooms"]) or (r["min_guests"] and st["guests"] < r["min_guests"]) or (r["min_nights"] and st["nights"] < r["min_nights"]):
        raise UserError("This code does not apply to your rooms, dates or party size.")
    if r["exclude_peak"] and st["peak"]:
        raise UserError("This code cannot be used on peak dates.")
    key = promo_key(r)
    if r["max_uses"] and promo_uses(conn, key, None, st["skip_group"]) >= r["max_uses"]:
        raise UserError("This code has reached its usage limit.")
    if r["per_phone_limit"] and st["phone"] and promo_uses(conn, key, st["phone"], st["skip_group"]) >= r["per_phone_limit"]:
        raise UserError("You have already used this code.")


def promo_pick(conn, code, st, room_charge):
    """The one discount for a stay: the larger of the entered code (which must be valid: a bad code raises) and the best automatic group rate that qualifies.
    Returns {'key', 'code', 'label', 'amount'} or None. A discount applies to the room charge only, never to extra beds, children or tax."""
    code = str(code or "").strip()[:32]
    rows = conn.execute("SELECT * FROM promo_codes WHERE active = 1 AND (auto = 1 OR code = ?)", (code,)).fetchall()
    if code and not any(r["code"] and r["code"].lower() == code.lower() for r in rows):
        raise UserError("This code is not valid.")
    best = None
    for r in rows:
        try:
            check_promo(conn, r, st)
        except UserError:
            if code and r["code"] and r["code"].lower() == code.lower():
                raise
            continue
        amt = room_charge * Decimal(str(r["value"])) / 100 if r["kind"] == "percent" else Decimal(str(r["value"]))
        if r["max_discount"] is not None:
            amt = min(amt, Decimal(str(r["max_discount"])))
        amt = min(money(amt), room_charge)
        if amt > 0 and (best is None or amt > best["amount"]):
            best = {"key": promo_key(r), "code": r["code"], "label": r["label"], "amount": amt}
    return best


def party_fields(b, s, n, guests):
    """Children (counted in `guests`) and extra beds asked for, checked against the configured rules. Returns (children, extra_beds)."""
    children = to_int(b.get("children") or 0, "Number of children must be between 0 and the number of guests.", 0, guests)
    beds = to_int(b.get("extra_beds") or 0, "Extra beds are not available for this many rooms.", 0, 20)
    if beds and (s["extra_bed_price"] is None or beds > n * s["extra_bed_max"]):
        raise UserError("Extra beds are not available for this many rooms.")
    return children, beds


def quote(conn, s, ci, co, *, pid, tid, tariff, rooms, guests, children=0, extra_beds=0, code="", phone=None, skip_group=None, sevas=None, package_id=None):
    """The price breakdown shown before confirming and stored with the booking. s must come from check_stay/parse_stay (it carries the peak periods).
    Order: room charge (nightly prices x rooms) + extra beds + children = subtotal; the promo discount comes off the room charge; tax_percent on the rest;
    advance_percent of the total is the advance. Every rule that is not configured contributes nothing. Amounts are rupees, rounded half up to the paisa."""
    nights = (co - ci).days
    per = night_prices(tariff, s["peak"], ci, co)
    room_charge = money(sum(Decimal(str(x["price"])) for x in per) * rooms)
    beds = money(s["extra_bed_price"] * extra_beds * nights) if s["extra_bed_price"] and extra_beds else Decimal(0)
    kids = money(s["child_price"] * children * nights) if s["child_price"] and children else Decimal(0)
    st = dict(pid=pid, tid=tid, rooms=rooms, guests=guests, nights=nights, peak=bool(s["peak"]), phone=phone, skip_group=skip_group)
    promo = promo_pick(conn, code, st, room_charge)
    discount = promo["amount"] if promo else Decimal(0)
    taxable = room_charge + beds + kids - discount
    tax = money(taxable * s["tax_percent"] / 100) if s["tax_percent"] else Decimal(0)
    lines = extras.seva_lines(conn, sevas, package_id, ci, co, guests, skip_group)  # sevas: not taxed, not discounted by promo codes
    seva_total = sum((Decimal(str(x["amount"])) for x in lines), Decimal(0))
    total = taxable + tax + seva_total
    advance = money(total * s["advance_percent"] / 100) if s["advance_percent"] else Decimal(0)
    f = float
    return {"nights": nights, "night_prices": per, "rooms": rooms, "room_charge": f(room_charge), "extra_beds": extra_beds, "extra_bed_price": f(s["extra_bed_price"] or 0), "extra_bed_charge": f(beds),
            "children": children, "child_price": f(s["child_price"] or 0), "child_charge": f(kids),
            "discount": {"code": promo["code"], "label": promo["label"], "key": promo["key"], "amount": f(discount)} if promo else None,
            "sevas": lines, "seva_charge": f(seva_total), "subtotal": f(room_charge + beds + kids), "tax_percent": f(s["tax_percent"] or 0), "tax": f(tax), "total": f(total),
            "advance_percent": f(s["advance_percent"] or 0), "advance": f(advance), "balance": f(total - advance)}


def calendar(conn, q, ip):
    """GET /api/public/calendar?month=YYYY-MM: per day, whether a room is free for that night, how many, the lowest nightly price, and the peak period if any."""
    throttle(("search", ip), 60, 60, "Too many searches.")
    analytics.safe(conn, "calendar")
    s = settings(conn)
    if not s["enabled"]:
        raise UserError("Online booking is closed right now. Please contact the property directly.", 403)
    try:
        first = date.fromisoformat(str(q.get("month") or today().isoformat()[:7]) + "-01")
    except ValueError:
        raise UserError("Please choose a valid month.")
    last = today() + timedelta(days=s["advance"])
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    if first > last or nxt <= today():
        raise UserError("Please choose a valid month.")
    pid, tid, flt = opt_int(q.get("property_id")), opt_int(q.get("room_type_id")), room_filters(q)
    peaks = peak_periods(conn, first, nxt)
    days, d = [], first
    while d < nxt:  # ponytail: one free_rooms query per day (at most 31), fine at this size
        iso = d.isoformat()
        hit = next((p["name"] for p in peaks if p["start_date"] <= iso <= p["end_date"]), None)
        if d < today() or d > last:
            days.append({"date": iso, "status": "closed", "free": 0, "price": None, "peak": hit})
        else:
            free = free_rooms(conn, d, d + timedelta(days=1), pid, tid, None, **flt)
            price = night_prices(min(r["tariff"] for r in free), peaks, d, d + timedelta(days=1))[0]["price"] if free else None
            days.append({"date": iso, "status": "soldout" if not free else "few" if len(free) <= 2 else "available", "free": len(free), "price": price, "peak": hit})
        d += timedelta(days=1)
    prev = first - timedelta(days=1)
    return {"month": first.isoformat()[:7], "days": days, "weekday_of_first": first.weekday(),
            "prev": prev.isoformat()[:7] if prev >= today().replace(day=1) else None, "next": nxt.isoformat()[:7] if nxt <= last else None}


def search(conn, q, ip):
    throttle(("search", ip), 60, 60, "Too many searches.")
    ci, co, nights, s = parse_stay(conn, q.get("check_in"), q.get("check_out"))
    guests = to_int(q.get("guests"), "Number of guests must be between 1 and 200.", 1, 200)
    flt = room_filters(q)
    groups = defaultdict(list)
    for r in free_rooms(conn, ci, co, opt_int(q.get("property_id")), opt_int(q.get("room_type_id")), str(q.get("hold") or "")[:64], **flt):
        groups[(r["property_id"], r["room_type_id"])].append(r)
    props = {}
    for (pid, tid), rooms in groups.items():
        need = rooms_needed([r["capacity"] for r in rooms], guests)
        if need is None or need > s["max_rooms"]:
            continue
        amenities = list(dict.fromkeys(a.strip() for r in rooms for a in (r["amenities"] or "").split(",") if a.strip()))
        r0 = rooms[0]
        per = night_prices(r0["tariff"], s["peak"], ci, co)
        p = props.setdefault(pid, {"property_id": pid, "property": r0["pname"], "address": r0["address"], "types": []})
        p["types"].append({"room_type_id": tid, "type": r0["tname"], "capacity": max(r["capacity"] for r in rooms), "description": r0["tdesc"],
                           "amenities": amenities, "photo": r0["tphoto"], "tariff": r0["tariff"], "nights": nights, "total_per_room": float(money(sum(Decimal(str(x["price"])) for x in per))),
                           "peak_prices": any(x["price"] != r0["tariff"] for x in per), "ground_floor": any(r["floor"] == 0 for r in rooms),
                           "senior_friendly": any(r["senior_friendly"] for r in rooms), "few_left": len(rooms) <= 2, "rooms_needed": need, "can_book": min(len(rooms), s["max_rooms"])})
    for p in props.values():
        p["types"].sort(key=lambda t: t["tariff"])
    res = {"check_in": ci.isoformat(), "check_out": co.isoformat(), "nights": nights, "guests": guests, "properties": list(props.values()), "peak": s["peak"], "alternatives": [],
           "callback": None, "waitlist": False}
    analytics.safe(conn, "search")
    analytics.safe(conn, "search_ok" if props else "search_soldout")
    analytics.safe(conn, "search_dates", ci.isoformat())
    analytics.safe(conn, "search_nights", nights)
    analytics.safe(conn, "search_guests", min(guests, 20))
    if not groups:  # no room at all for these dates: remember which nights were turned away (counts only)
        for i in range(min(nights, 14)):
            analytics.safe(conn, "soldout_date", (ci + timedelta(days=i)).isoformat())
    if not props:  # nothing for this party: offer nearby dates that really are bookable
        pid = opt_int(q.get("property_id"))
        res["alternatives"] = alternatives(conn, ci, co, guests, {pid} if pid else None, opt_int(q.get("room_type_id")), **flt)
        # "group": rooms are free but the party needs more rooms than may be booked online; "soldout": no dates to offer either. Either way staff should call back.
        res["callback"] = "group" if groups else None if res["alternatives"] else "soldout"
        res["waitlist"] = not groups  # "notify me" is offered when nothing at all is free for these dates
    return res


ALT_SPAN, ALT_MAX = 14, 3


def alternatives(conn, ci, co, guests, property_ids=None, type_id=None, max_rooms=None, limit=ALT_MAX, **flt):
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
        free = [r for r in free_rooms(conn, a, b, None, type_id, None, **flt) if property_ids is None or r["property_id"] in property_ids]
        groups = defaultdict(list)
        for r in free:
            groups[(r["property_id"], r["room_type_id"])].append(r["capacity"])
        cap = s["max_rooms"] if max_rooms is None else min(max_rooms, s["max_rooms"])
        if any((n := rooms_needed(caps, guests)) is not None and n <= cap for caps in groups.values()):
            out.append({"check_in": a.isoformat(), "check_out": b.isoformat(), "nights": nights, "days_away": k})
    return out


def arrival_fields(b):
    """Optional expected arrival time (HH:MM) and darshan date (a real date, today or later), shared by the guest and staff forms. Returns (time or None, ISO date or None)."""
    t, d = str(b.get("arrival_time") or "").strip(), str(b.get("darshan_date") or "").strip()
    if t and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
        raise UserError("Expected arrival time must look like 14:30.")
    if d:
        try:
            dd = date.fromisoformat(d)
        except ValueError:
            raise UserError("Darshan date must be a valid date.")
        if dd < today():
            raise UserError("Darshan date cannot be in the past.")
        d = dd.isoformat()
    return t or None, d or None


def norm_phone(v):
    v = re.sub(r"[\s\-]", "", str(v or ""))
    if not re.fullmatch(r"\+?\d{10,13}", v):
        raise UserError("Mobile / phone number must have 10 to 13 digits.")
    return v


def same_phone(a, b):
    digits = lambda v: re.sub(r"\D", "", str(v or ""))[-10:]
    return len(digits(a)) >= 7 and digits(a) == digits(b)


def find_booking(conn, booking_id, phone):
    bid = to_int(booking_id, "Booking ID must be a number.", 1, 10**12)
    rows = conn.execute("SELECT * FROM bookings WHERE source = 'online' AND (group_id = ? OR (group_id IS NULL AND id = ?)) ORDER BY id", (bid, bid)).fetchall()
    if not rows or not same_phone(rows[0]["phone"], phone):
        raise UserError("We could not find a booking with that ID and mobile number.", 404)
    return rows


def qr_data_uri(code):
    return "data:image/png;base64," + base64.b64encode(qr_module.qr_png(qr_module.qr_matrix(code), 5, 2)).decode()


def booking_view(conn, rows):
    b, s = rows[0], settings(conn)
    prop = conn.execute("SELECT name, address, contact_person, phone FROM properties WHERE id = ?", (b["property_id"],)).fetchone()
    rt = conn.execute("SELECT t.name, t.tariff FROM rooms r JOIN room_types t ON t.id = r.room_type_id WHERE r.id = ?", (b["room_id"],)).fetchone()
    live = [r for r in rows if r["status"] != "Cancelled"]
    status = live[0]["status"] if live else "Cancelled"
    nights = (date.fromisoformat(b["check_out"]) - date.fromisoformat(b["check_in"])).days
    deadline = datetime.fromisoformat(f"{b['check_in']}T{s['checkin_time']}") - timedelta(hours=s["cutoff"])
    total = sum(r["amount"] if r["amount"] is not None else rt["tariff"] * nights for r in rows)
    can_cancel = status == "Confirmed" and datetime.now() < deadline
    gid = b["group_id"] or b["id"]
    code = notifications.checkin_code(conn, gid) if status in ("Confirmed", "Checked-In") else None
    pay = payments.summary(conn, gid, float(money(total)))
    net_paise = payments.to_paise(pay["net"])
    refund_amount = pay["refunded"] + pay["refund_in_progress"] if pay["refund_status"] != "none" else payments.rupees(payments.refund_paise(net_paise, s["refund_percent"]))  # what was refunded, else what the policy gives for the money held
    return {"booking_id": gid, "status": status, "property": dict(prop), "room_type": rt["name"], "rooms": len(rows),
            "guests": sum(r["guests"] for r in rows), "children": sum(r["children"] or 0 for r in rows), "extra_beds": sum(r["extra_beds"] or 0 for r in rows),
            "check_in": b["check_in"], "check_out": b["check_out"],
            "check_in_time": s["checkin_time"], "check_out_time": s["checkout_time"], "nights": nights, "guest_name": b["guest_name"], "booker_name": b["booker_name"],
            "mobile": b["phone"], "arrival_contact_name": b["arrival_contact_name"], "arrival_contact_phone": b["arrival_contact_phone"],
            "special_requests": b["remarks"], "arrival_time": b["arrival_time"], "darshan_date": b["darshan_date"], "amount": float(money(total)),
            "advance_due": float(money(sum(r["advance_due"] or 0 for r in rows))), "promo_code": b["promo_code"], "created_at": b["created_at"],
            "price": json.loads(b["price_json"]) if b["price_json"] else None,
            "payment": {"method": "Online payment" if pay["provider"] and pay["provider"] != "counter" else "Pay at counter", "status": rows[0]["payment_status"], "channel": pay["method"], "kind": pay["kind"],
                        "paid": pay["paid"], "refunded": pay["refunded"], "refund_in_progress": pay["refund_in_progress"], "refund_status": pay["refund_status"], "balance": pay["balance"] if live else 0.0},
            "cancel_reason": b["cancel_reason"], "refund": {"percent": s["refund_percent"], "days": s["refund_days"], "amount": refund_amount},
            "cancel_deadline": deadline.isoformat(timespec="minutes"), "can_cancel": can_cancel, "checkin_code": code, "checkin_qr": qr_data_uri(code) if code else None, "sevas": extras.sevas_for(conn, gid), "notices": extras.active_notices(conn, b["check_in"], b["check_out"]) if live else [],
            "can_change": can_cancel and not any(r["payment_status"] in ("paid", "partial") for r in rows)}


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
    if not OTP_REQUIRED:  # with OTP the code check below is the human check (the CAPTCHA already guarded sending the code)
        captcha_check(b, ip)
    token = str(b.get("hold_token") or "")[:64]
    r = parse_request(conn, b)
    ci, co, nights, s, pid, tid, n, guests, children, beds, flt = (r[k] for k in ("ci", "co", "nights", "s", "pid", "tid", "n", "guests", "children", "beds", "flt"))
    kind = {"pay_at_counter": "counter", "pay_full": "full", "pay_advance": "advance"}.get(b.get("payment_method"))
    if kind is None or kind not in payments.options(settings(conn)):
        raise UserError("Please choose a valid payment option.")
    name, remarks, id_no = (str(b.get(k) or "").strip() for k in ("guest_name", "remarks", "id_number"))
    email = str(b.get("email") or "").strip()
    if not name or len(name) > 100:
        raise UserError("Please enter the guest name.")
    phone = norm_phone(b.get("phone"))
    booker, arrival_name, arrival_phone = check_contact(b)
    if b.get("id_type") not in ID_TYPES:
        raise UserError("Please choose a valid ID proof type.")
    if not id_no or len(id_no) > 30:
        raise UserError("Please enter the ID proof number.")
    id_no = "****" + id_no[-4:]  # data minimisation: the full number is asked for, and stored encrypted, at the front desk (staff app, ID capture)
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 120:
        raise UserError("Please enter a valid email address, or leave it empty.")
    if len(remarks) > 500:
        raise UserError("Special requests must be 500 characters or fewer.")
    arrival, darshan = arrival_fields(b)
    proof = str(b.get("otp_proof") or "")[:64]
    if OTP_REQUIRED and not proof:
        raise UserError("Please verify your mobile number with the code we send.", 403)
    if str(b.get("promo_code") or "").strip():
        throttle(("promo", ip), 15, 600, "Too many code attempts.")
    throttle(("book", phone[-10:]), 5, 600, "Too many booking attempts for this mobile number.")
    p = dict(pid=pid, tid=tid, n=n, guests=guests, children=children, beds=beds, ci=ci.isoformat(), co=co.isoformat(), flt=flt, name=name, phone=phone, email=email, remarks=remarks,
             id_type=b["id_type"], id_no=id_no, booker=booker, arrival_name=arrival_name, arrival_phone=arrival_phone, arrival=arrival, darshan=darshan, promo=str(b.get("promo_code") or ""),
             sevas=r["sevas"], package=r["package"], token=token, save=b.get("save_details") is True, lang=b.get("lang") if b.get("lang") in LANGS else "en", proof=proof)
    analytics.safe(conn, "submit")
    if kind != "counter":
        analytics.safe(conn, "payment_start", kind)
        return payments.start(conn, p, kind, ip)  # the booking is made when the payment is confirmed
    gid = write(conn, lambda: place(conn, p))
    notify_safe(conn, "confirmed", gid)
    return booking_view(conn, find_booking(conn, gid, phone))


def plan_booking(conn, p):
    """Checks a booking request `p` (the validated fields of book(), JSON-safe) against the rules and the rooms free now, and prices it. Writes nothing.
    Raises UserError when the stay no longer passes the rules, the rooms are gone or the party does not fit. Returns the rooms, the split of guests, and the quote."""
    ci, co, nights, s = parse_stay(conn, p["ci"], p["co"])
    n = room_count({"rooms": p["n"]}, s)
    if not conn.execute("SELECT 1 FROM properties WHERE id = ? AND active = 1", (p["pid"],)).fetchone():
        raise UserError("Please choose a valid property and room type.")
    token = p["token"]
    free = free_rooms(conn, ci, co, p["pid"], p["tid"], token, **p["flt"])  # the guest's own live hold does not block them; anyone else's does
    if len(free) < n:
        raise UserError("Sorry, those rooms are no longer available for your dates. Please search again.", 409)
    held = {r[0] for r in conn.execute("SELECT room_id FROM holds WHERE token = ? AND expires_at > ?", (token, now_iso()))} if token else set()
    rooms = pick_rooms(free, n, held)
    guests, children, beds = p["guests"], p["children"], p["beds"]
    shares, bed_split, kid_split = spread(guests, n), split_party(beds, n), split_party(children, n)
    counted = spread(guests if s["child_counts_capacity"] else guests - children, n)
    if any(sh > r["capacity"] + bd for sh, bd, r in zip(counted, bed_split, rooms)):
        raise UserError(f"{n} room{'s' if n > 1 else ''} of this type cannot host {guests} guests. Please add a room or reduce the guests.")
    q = quote(conn, s, ci, co, pid=p["pid"], tid=p["tid"], tariff=rooms[0]["tariff"], rooms=n, guests=guests, children=children, extra_beds=beds, code=p["promo"], phone=p["phone"], sevas=p.get("sevas"), package_id=p.get("package"))
    return dict(ci=ci, co=co, n=n, s=s, rooms=rooms, shares=shares, bed_split=bed_split, kid_split=kid_split, q=q)


def place(conn, p, q=None, method="pay_at_counter", use_otp=True):
    """Creates the booking rows for request `p`. Call it inside write(): the rooms are checked and booked in one transaction, so a double booking is impossible.
    `q`: the quote to store instead of a fresh one (an online payment books at the price the guest saw and paid). use_otp: consume the guest's mobile-verification proof here
    (counter bookings; an online payment consumed it when it started). Returns the booking ID (the group id)."""
    verified = bool(p.get("verified"))
    if use_otp and p.get("proof"):
        verified = use_proof(conn, p["proof"], p["phone"], OTP_REQUIRED)
    elif use_otp and OTP_REQUIRED:
        raise UserError("Please verify your mobile number with the code we send.", 403)
    plan = plan_booking(conn, p)
    q, n, rooms = q or plan["q"], plan["n"], plan["rooms"]
    extras.check_capacity(conn, q.get("sevas") or [])  # the quote was only a look: the seva places are counted again here, inside the transaction
    part = {k: split(q[k], n) for k in ("total", "tax", "advance")} | {"discount": split(q["discount"]["amount"] if q["discount"] else 0, n)}
    key = q["discount"]["key"] if q["discount"] else None
    now, ids = datetime.now().isoformat(timespec="seconds"), []
    for i, r in enumerate(rooms):
        cur = conn.execute(
            "INSERT INTO bookings (property_id, room_id, guest_name, phone, id_type, id_number, guests, check_in, check_out, remarks, status, "
            "created_at, status_at, source, email, payment_method, payment_status, amount, arrival_time, darshan_date, children, extra_beds, tax_amount, discount_amount, "
            "promo_code, advance_due, booker_name, arrival_contact_name, arrival_contact_phone, price_json, lang) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'Confirmed',?,?,'online',?,?,'due',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["pid"], r["id"], p["name"], p["phone"], p["id_type"], p["id_no"], plan["shares"][i], p["ci"], p["co"], p["remarks"] or None, now, now, p["email"] or None, method, part["total"][i],
             p["arrival"], p["darshan"], plan["kid_split"][i], plan["bed_split"][i], part["tax"][i], part["discount"][i], key, part["advance"][i], p["booker"], p["arrival_name"], p["arrival_phone"],
             json.dumps(q) if i == 0 else None, p.get("lang") or "en"))
        ids.append(cur.lastrowid)
    conn.execute(f"UPDATE bookings SET group_id = ? WHERE id IN ({','.join('?' * len(ids))})", (ids[0], *ids))
    extras.save_sevas(conn, ids[0], q)
    if p["token"]:
        conn.execute("DELETE FROM holds WHERE token = ?", (p["token"],))
    conn.execute("UPDATE waitlist SET status = 'booked' WHERE status IN ('waiting', 'notified') AND phone10 = ? AND check_in < ? AND check_out > ?", (p10(p["phone"]), p["co"], p["ci"]))
    analytics.safe(conn, "booked", "online" if method == "online" else "counter")
    if verified and p.get("save") is True:  # opt-in, and only for a mobile number that was just verified
        profile_save(conn, p["phone"], p["name"] if not p["booker"] else p["booker"], p["email"], p["id_type"])
    audit(conn, "booking.online_create", p["pid"], None, f"#{ids[0]}: {n} room(s), {p['ci']} to {p['co']}, {p['guests']} guests, total {q['total']:.2f}" + (f", code {key}" if key else "") + (", paid online" if method == "online" else ""),
          f"Online booking, mobile {p['phone']}" + (" (booked for someone else)" if p["booker"] else ""))
    return ids[0]


def hold(conn, b, ip):
    """Reserves the chosen rooms for HOLD_MINUTES while the guest fills in the confirm step. Passing the previous `token` replaces that hold."""
    throttle(("hold", ip), 10, 600, "Too many room selections from this device.")
    ci, co, nights, s = parse_stay(conn, b.get("check_in"), b.get("check_out"))
    pid = to_int(b.get("property_id"), "Please choose a valid property and room type.", 1, 10**9)
    tid = to_int(b.get("room_type_id"), "Please choose a valid property and room type.", 1, 10**9)
    n = room_count(b, s)
    old, flt = str(b.get("token") or "")[:64], room_filters(b)

    def work():
        conn.execute("DELETE FROM holds WHERE expires_at <= ?", ((now() - timedelta(days=1)).isoformat(timespec="seconds"),))  # lapsed holds stay a day: the alerts count them
        if old:
            conn.execute("DELETE FROM holds WHERE token = ?", (old,))
        if conn.execute("SELECT COUNT(DISTINCT token) FROM holds WHERE ip = ? AND expires_at > ?", (ip, now_iso())).fetchone()[0] >= MAX_HOLDS_PER_IP:
            raise UserError("You already have rooms on hold. Please finish that booking or wait for the hold to expire.", 429, HOLD_MINUTES * 60)
        total = conn.execute("SELECT COUNT(*) FROM rooms WHERE active = 1").fetchone()[0]
        if total and (conn.execute("SELECT COUNT(DISTINCT room_id) FROM holds WHERE expires_at > ?", (now_iso(),)).fetchone()[0] + n) * 100 > HOLD_GLOBAL_PERCENT * total and total >= 6:
            raise UserError("Many rooms are being chosen right now. Please try again in a few minutes.", 429, 120)
        free = free_rooms(conn, ci, co, pid, tid, None, **flt)
        if len(free) < n:
            raise UserError("Sorry, those rooms are no longer available for your dates. Please search again.", 409)
        tok, exp = secrets.token_urlsafe(16), (now() + timedelta(minutes=HOLD_MINUTES)).isoformat(timespec="seconds")
        for r in pick_rooms(free, n):
            conn.execute("INSERT INTO holds (token, room_id, check_in, check_out, expires_at, ip) VALUES (?,?,?,?,?,?)", (tok, r["id"], ci.isoformat(), co.isoformat(), exp, ip))
        analytics.safe(conn, "hold")
        return {"token": tok, "expires_at": exp, "seconds": HOLD_MINUTES * 60, "rooms": n}
    return write(conn, work)


def release(conn, b, ip):
    token = str(b.get("token") or "")[:64]
    if token:
        write(conn, lambda: conn.execute("DELETE FROM holds WHERE token = ?", (token,)))
    return {"ok": True}


def lookup(conn, b, ip):
    throttle(("lookup", ip), 20, 600, "Too many attempts.")
    return lookup_guard(b.get("phone"), lambda: booking_view(conn, find_booking(conn, b.get("booking_id"), b.get("phone"))))


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
        live = [r for r in rows if r["status"] == "Confirmed"]
        for r in live:
            conn.execute("UPDATE bookings SET status = 'Cancelled', status_at = ?, cancel_reason = ? WHERE id = ?",
                         (now, f"{reason} (cancelled online by guest, mobile {rows[0]['phone']})", r["id"]))
        gid = rows[0]["group_id"] or rows[0]["id"]
        owed = sum(r["amount"] or 0 for r in live)
        extras.sync_sevas(conn, gid)
        refunds = payments.request_cancel_refund(conn, gid, [r["id"] for r in live], owed, owed)
        pct = settings(conn)["refund_percent"]
        audit(conn, "booking.online_cancel", rows[0]["property_id"], "Confirmed", "Cancelled", reason + (f" (refund policy {pct}%)" if pct is not None else "") + (f"; {len(refunds)} refund(s) recorded" if refunds else ""))
        return gid

    gid = lookup_guard(b.get("phone"), lambda: write(conn, work))
    after_cancel(conn, gid)
    return booking_view(conn, find_booking(conn, gid, b.get("phone")))


def after_cancel(conn, gid):
    """After a cancellation committed (guest or staff): send the refund to the provider, tell the guest, and offer the freed rooms to the waiting list."""
    payments.process_refunds(conn)
    payments.refund_alerts(conn)
    if group_rows(conn, gid)[0]["source"] == "online":
        notify_safe(conn, "cancelled", gid)
    waitlist_tick_safe(conn)  # the freed rooms may be what a waiting guest asked for


CALLBACK_MAX_DAYS, CALLBACK_MAX_NIGHTS = 366, 60


def callback(conn, b, ip):
    """A group (or sold-out) guest, or anyone who taps Call me back, asks the property to ring them. Same per-IP throttle helper and honeypot as booking."""
    throttle(("callback-ip", ip), 5, 600, "Too many call-back requests from this device.")
    if str(b.get("website") or "").strip():  # honeypot: hidden from people, only bots fill it
        throttle(("callback-ip", ip), 5, 600, "Too many call-back requests from this device.")
        raise UserError("Some of the values sent are not valid.")
    captcha_check(b, ip)
    name, note = str(b.get("name") or "").strip(), str(b.get("note") or "").strip()
    if not name or len(name) > 100:
        raise UserError("Please enter your name.")
    phone = norm_phone(b.get("phone"))
    call_time = b.get("call_time") or "any"
    if call_time not in CALL_TIMES:
        raise UserError("Please choose a valid time to call.")
    lang = b.get("lang") if b.get("lang") in LANGS else None
    quick = not any(str(b.get(k) or "").strip() for k in ("check_in", "check_out", "people"))  # the Call me back button sends only name, phone, time and language
    people = ci = co = None
    if not quick:
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
        cid = conn.execute("INSERT INTO callback_requests (property_id, name, phone, people, check_in, check_out, note, created_at, call_time, lang) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (pid, name, phone, people, ci and ci.isoformat(), co and co.isoformat(), note or None, now_iso(), call_time, lang)).lastrowid
        audit(conn, "callback.request", pid, None, f"#{cid}: " + (f"{people} people, {ci} to {co}" if ci else f"quick call-back, {call_time}, {lang or 'any language'}"), f"Call-back request, mobile {phone}")
        return cid
    write(conn, work)
    return {"ok": True}


def health(conn, q, ip):
    """GET /health for uptime monitors: 200 and status ok when the database answers and the newest backup is recent; otherwise 503. Reveals no paths or error text."""
    db = "ok"
    try:
        c = connect()
        try:
            c.execute("SELECT 1 FROM properties LIMIT 1").fetchone()
        finally:
            c.close()
    except (UserError, sqlite3.Error):
        db = "fail"
    bk = backup_state()
    ok = db == "ok" and bk["status"] in ("ok", "disabled")
    return (200 if ok else 503), {"status": "ok" if ok else "fail", "checks": {"database": db, "backup": bk["status"]}}


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
    old_umask = os.umask(0o077)  # backups hold guest names and mobiles: readable by the service user only
    try:
        BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        os.chmod(dest, 0o600)
        os.umask(old_umask)
    except (OSError, sqlite3.Error, BackupError) as e:
        os.umask(old_umask)
        part.unlink(missing_ok=True)
        msg = str(e) or type(e).__name__
        try:
            backup_log(False, msg)
        except OSError:
            pass
        backup_alert(False, msg)
        raise BackupError(msg) from e
    for old in sorted(BACKUPS.glob(BACKUP_GLOB))[:-keep]:  # names sort by time; non-matching files are never touched
        old.unlink(missing_ok=True)
    backup_log(True, dest.name)
    backup_alert(True, dest.name)
    return dest


def parse_request(conn, b):
    """The stay and party of a quote or booking request, validated against the settings and peak rules. Returns a dict."""
    ci, co, nights, s = parse_stay(conn, b.get("check_in"), b.get("check_out"))
    pid, tid = to_int(b.get("property_id"), "Please choose a valid property and room type.", 1, 10**9), to_int(b.get("room_type_id"), "Please choose a valid property and room type.", 1, 10**9)
    n = room_count(b, s)
    guests = to_int(b.get("guests"), "Number of guests must be between 1 and 200.", 1, 200)
    if guests < n:
        raise UserError("There must be at least one guest per room. Reduce the number of rooms.")
    children, beds = party_fields(b, s, n, guests)
    sevas = b.get("sevas") if isinstance(b.get("sevas"), list) else None
    return dict(ci=ci, co=co, nights=nights, s=s, pid=pid, tid=tid, n=n, guests=guests, children=children, beds=beds, flt=room_filters(b), sevas=sevas, package=opt_int(b.get("package_id")))


def quote_api(conn, b, ip):
    """POST /api/public/quote: the price breakdown for a stay, before the guest confirms. Same inputs as booking; nothing is reserved."""
    throttle(("quote", ip), 60, 60, "Too many searches.")
    r = parse_request(conn, b)
    t = conn.execute("SELECT t.tariff FROM room_types t JOIN rooms x ON x.room_type_id = t.id WHERE t.id = ? AND x.property_id = ? AND t.active = 1 AND x.active = 1", (r["tid"], r["pid"])).fetchone()
    if not t:
        raise UserError("Please choose a valid property and room type.")
    try:
        phone = norm_phone(b.get("phone"))
    except UserError:
        phone = None  # the per-mobile code limit is checked again when booking
    if str(b.get("promo_code") or "").strip():
        throttle(("promo", ip), 15, 600, "Too many code attempts.")
    return quote(conn, r["s"], r["ci"], r["co"], pid=r["pid"], tid=r["tid"], tariff=t["tariff"], rooms=r["n"], guests=r["guests"], children=r["children"], extra_beds=r["beds"],
                 code=b.get("promo_code"), phone=phone, sevas=r["sevas"], package_id=r["package"])


def check_contact(b):
    """Optional booking-on-behalf fields: who booked (when not the guest staying) and who to call on arrival. Returns (booker_name, arrival_name, arrival_phone)."""
    booker, an = (str(b.get(k) or "").strip() for k in ("booker_name", "arrival_contact_name"))
    ap = str(b.get("arrival_contact_phone") or "").strip()
    if len(booker) > 100 or len(an) > 100:
        raise UserError("Names must be 100 characters or fewer.")
    return booker or None, an or None, norm_phone(ap) if ap else None


def split_party(total, n):
    return spread(total, n) if total else [0] * n


def plan_change(conn, rows, check_in, check_out):
    """Checks a date change for an online booking and prices it, without writing. Returns the plan: which room each booking row gets, the new quote and the old and new totals.
    Rules: the booking is Confirmed, before the same cutoff as cancelling, nothing recorded as paid; the new stay passes the normal online rules (advance window, nights,
    peak limits); every row keeps its room when that room is free on the new dates, else gets another free room of the same type and building."""
    v = booking_view(conn, rows)
    if v["status"] != "Confirmed":
        raise UserError("This booking can no longer be changed online.", 409)
    if not v["can_cancel"]:
        when = datetime.fromisoformat(v["cancel_deadline"]).strftime("%d %b %Y, %I:%M %p")
        raise UserError(f"Online changes closed on {when}. Please call {v['property']['phone']} to change the dates.", 409)
    if any(r["payment_status"] in ("paid", "partial") for r in rows):
        raise UserError(f"A payment is recorded for this booking, so only the property can change the dates. Please call {v['property']['phone']}.", 409)
    if extras.sevas_for(conn, rows[0]["group_id"] or rows[0]["id"]):
        raise UserError(f"This booking includes sevas, so only the property can change its dates. Please call {v['property']['phone']}.", 409)
    ci, co, nights, s = parse_stay(conn, check_in, check_out)
    if (ci.isoformat(), co.isoformat()) == (v["check_in"], v["check_out"]):
        raise UserError("The new dates are the same as your current dates.")
    live = [r for r in rows if r["status"] == "Confirmed"]
    n = room_count({"rooms": len(live)}, s)
    pid = live[0]["property_id"]
    tid = conn.execute("SELECT room_type_id FROM rooms WHERE id = ?", (live[0]["room_id"],)).fetchone()[0]
    free = {r["id"]: r for r in free_rooms(conn, ci, co, pid, tid, None, skip=[r["id"] for r in rows])}
    spare = pick_rooms([r for r in free.values() if r["id"] not in {x["room_id"] for x in live}], n)
    assign = []
    for r in live:
        room = free.get(r["room_id"]) or (spare.pop(0) if spare else None)
        if not room:
            raise UserError("Sorry, your rooms are not available for the new dates. Please try other dates.", 409)
        if (r["guests"] if s["child_counts_capacity"] else r["guests"] - (r["children"] or 0)) > room["capacity"] + (r["extra_beds"] or 0):
            raise UserError("Sorry, the rooms free on the new dates are too small for your party. Please try other dates.", 409)
        assign.append((r, room))
    code = rows[0]["promo_code"] if rows[0]["promo_code"] and not rows[0]["promo_code"].startswith("auto:") else ""
    gid, dropped = rows[0]["group_id"] or rows[0]["id"], False
    args = dict(pid=pid, tid=tid, tariff=assign[0][1]["tariff"], rooms=n, guests=v["guests"], children=v["children"], extra_beds=v["extra_beds"], phone=rows[0]["phone"], skip_group=gid)
    try:
        q = quote(conn, s, ci, co, code=code, **args)
    except UserError:  # the code does not fit the new dates (expired, peak): the stay is repriced without it and the guest is told
        q, dropped = quote(conn, s, ci, co, code="", **args), True
    return {"ci": ci, "co": co, "assign": assign, "quote": q, "old_total": v["amount"], "new_total": q["total"], "promo_dropped": dropped, "view": v}


def apply_change(conn, plan):
    q, n = plan["quote"], len(plan["assign"])
    part = {k: split(q[k], n) for k in ("total", "tax", "advance")} | {"discount": split(q["discount"]["amount"] if q["discount"] else 0, n)}
    key = q["discount"]["key"] if q["discount"] else None
    for i, (r, room) in enumerate(plan["assign"]):
        conn.execute("UPDATE bookings SET room_id = ?, check_in = ?, check_out = ?, amount = ?, tax_amount = ?, discount_amount = ?, advance_due = ?, promo_code = ?, price_json = ? WHERE id = ?",
                     (room["id"], plan["ci"].isoformat(), plan["co"].isoformat(), part["total"][i], part["tax"][i], part["discount"][i], part["advance"][i], key,
                      json.dumps(q) if i == 0 else None, r["id"]))
    v = plan["view"]
    audit(conn, "booking.online_change_dates", plan["assign"][0][0]["property_id"], f"{v['check_in']} to {v['check_out']}, total {plan['old_total']:.2f}",
          f"{plan['ci']} to {plan['co']}, total {plan['new_total']:.2f}", "Dates changed online by guest")


def change_dates(conn, b, ip):
    """POST /api/public/booking/change {booking_id, phone, check_in, check_out, confirm}: without confirm=true only shows the new price and what changes."""
    throttle(("lookup", ip), 20, 600, "Too many attempts.")
    confirm = b.get("confirm") is True

    def run():
        rows = find_booking(conn, b.get("booking_id"), b.get("phone"))
        plan = plan_change(conn, rows, b.get("check_in"), b.get("check_out"))
        if confirm:
            apply_change(conn, plan)
        return rows, plan
    if confirm:
        rows, plan = lookup_guard(b.get("phone"), lambda: write(conn, run))
        waitlist_tick_safe(conn)  # the old dates are free again
        notify_safe(conn, "changed", rows[0]["group_id"] or rows[0]["id"])
        return {"changed": True, "booking": booking_view(conn, find_booking(conn, rows[0]["group_id"] or rows[0]["id"], b.get("phone")))}
    rows, plan = lookup_guard(b.get("phone"), run)
    return {"changed": False, "old": {"check_in": plan["view"]["check_in"], "check_out": plan["view"]["check_out"], "total": plan["old_total"]},
            "new": {"check_in": plan["ci"].isoformat(), "check_out": plan["co"].isoformat(), "quote": plan["quote"]}, "difference": float(money(plan["new_total"] - plan["old_total"])),
            "promo_dropped": plan["promo_dropped"], "rooms_moved": sum(r["room_id"] != room["id"] for r, room in plan["assign"])}


OTP_TTL, OTP_MAX_ATTEMPTS, OTP_COOLDOWN, PROOF_TTL, SESSION_TTL = 300, 5, 30, 900, 24 * 3600  # seconds


OTP_REQUIRED = os.environ.get("BOOKING_OTP_REQUIRED", "1").strip() != "0"  # "0" only until an SMS provider is set up


OTP_PURPOSES = ("booking", "login", "subscribe")
# SMS spend protection (F1): codes go to Indian numbers only, and the whole site may send at most this many code SMS per hour and per day
SMS_HOURLY_LIMIT, SMS_DAILY_LIMIT = int(providers.env("SMS_HOURLY_LIMIT") or 60), int(providers.env("SMS_DAILY_LIMIT") or 300)
SMS_PREFIXES = tuple(x.strip() for x in (providers.env("SMS_ALLOWED_PREFIXES") or "+91").split(",") if x.strip())
LOOKUP_FAIL_LIMIT = int(providers.env("LOOKUP_FAIL_LIMIT") or 8)  # wrong booking ID + mobile pairs per mobile number per hour, from any address
HOLD_GLOBAL_PERCENT = int(providers.env("HOLD_GLOBAL_PERCENT") or 50)  # no new hold while this share of all rooms is already on hold


def sms_destination(phone):
    """Only numbers inside SMS_ALLOWED_PREFIXES (default +91) get a code: an SMS-pumping fraudster cannot make the site text premium or foreign numbers."""
    digits = re.sub(r"\D", "", phone)
    if phone.startswith("+"):
        ok = any(phone.startswith(p) for p in SMS_PREFIXES)
    else:  # no country code: a domestic number
        ok = "+91" in SMS_PREFIXES and (len(digits) == 10 or (len(digits) == 11 and digits[0] == "0") or (len(digits) == 12 and digits.startswith("91")))
    if ok and "+91" in SMS_PREFIXES and (phone.startswith("+91") or not phone.startswith("+")) and digits[-10:][0] not in "6789":
        ok = False
    if not ok:
        raise UserError("We can only send codes to Indian mobile numbers (+91).")


def sms_budget(conn):
    """The site-wide cap on code SMS per hour and per day (SMS_HOURLY_LIMIT, SMS_DAILY_LIMIT). Past it nothing is sent and the staff get an alert."""
    try:
        throttle(("sms-hour",), SMS_HOURLY_LIMIT, 3600, "Too many attempts.")
        throttle(("sms-day",), SMS_DAILY_LIMIT, 86400, "Too many attempts.")
    except UserError:
        alert_raise(conn, "sms", "budget", f"The limit of {SMS_HOURLY_LIMIT} code SMS per hour or {SMS_DAILY_LIMIT} per day was reached. Codes are paused. If this is not a busy day, someone may be abusing the code form.")
        raise UserError("We cannot send more codes right now. Please try again later, or call the property.", 503)


def lookup_guard(phone, fn):
    """Runs fn() (a booking lookup, change or cancel by booking ID + mobile). Wrong pairs are counted per mobile number across all addresses, so a stolen or guessed
    number cannot be used to scan booking IDs from many devices. Never call it inside write(); wrap the write() instead."""
    key = ("lookup-fail", p10(phone))
    rate(key, LOOKUP_FAIL_LIMIT, 3600, "Too many attempts for this mobile number.", add=False)
    try:
        return fn()
    except UserError as e:
        if e.status == 404:
            rate(key, 10 ** 6, 3600, "", add=True)
        raise


GUEST_COOKIE = "guest_session"


LOCALES = {}


def tr(lang, key, **kw):
    """Server-side text (SMS, receipts) from web/locales: the visitor's language, English where that key is still TODO."""
    for code in (lang if lang in LANGS else "en", "en"):
        if code not in LOCALES:
            LOCALES[code] = json.loads((ROOT / "web" / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        v = LOCALES[code].get(key)
        if v and not v.startswith("TODO"):
            return v.format(**kw)
    return key


def p10(phone):
    return re.sub(r"\D", "", str(phone or ""))[-10:]


def sha(v):
    return hashlib.sha256(str(v).encode()).hexdigest()


def secret(conn, name):
    """A server secret shared by both servers through the database (check-in codes, the development ID key). APP_SECRET in the environment overrides it. Created on first use."""
    env_value = providers.env("APP_SECRET")
    if env_value:
        return hashlib.sha256(f"{name}|{env_value}".encode()).hexdigest()
    row = conn.execute("SELECT value FROM app_secrets WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    conn.execute("INSERT OR IGNORE INTO app_secrets (name, value) VALUES (?, ?)", (name, secrets.token_hex(32)))
    return conn.execute("SELECT value FROM app_secrets WHERE name = ?", (name,)).fetchone()[0]


def vault_key(conn):
    """The master key for stored ID documents and pending-payment details: ID_ENCRYPTION_KEY, which a public deployment must set (serve_checks insists). On a development
    machine without it a key is generated into the database, which protects nothing against someone holding the database file."""
    return providers.env("ID_ENCRYPTION_KEY") or secret(conn, "dev_vault_key")


def group_rows(conn, gid):
    return payments.group_rows(conn, gid)


def notify_safe(conn, event, gid):
    """Sends the booking notifications for an event; a delivery problem is logged and never reaches the guest or the booking."""
    try:
        return notifications.send_event(conn, event, gid)
    except Exception:
        traceback.print_exc()
        return {}


def otp_send(conn, b, ip):
    """POST /api/public/otp/send {phone, purpose: booking|login}. Sends a 6-digit code by SMS (provider interface; the mock prints it in development).
    Limits: 10 codes per hour per device, 5 per hour and one per 30 seconds per mobile number. Replaces any earlier unused code."""
    purpose = b.get("purpose")
    if purpose not in OTP_PURPOSES:
        raise UserError("Some of the values sent are not valid.")
    throttle(("otp-ip", ip), 10, 3600, "Too many verification codes requested from this device.")
    captcha_check(b, ip)
    phone = norm_phone(b.get("phone"))
    sms_destination(phone)
    k = p10(phone)
    throttle(("otp-cool", k), 1, OTP_COOLDOWN, "Please wait a little before asking for another code.")
    throttle(("otp-phone", k), 5, 3600, "Too many verification codes requested for this mobile number.")
    reply = {"ok": True, "expires_in": OTP_TTL, "resend_after": OTP_COOLDOWN}
    if purpose == "login" and not conn.execute(
            "SELECT 1 FROM bookings WHERE source = 'online' AND substr(replace(replace(phone, ' ', ''), '-', ''), -10) = ? UNION SELECT 1 FROM guest_profiles WHERE phone10 = ? LIMIT 1", (k, k)).fetchone():
        return reply  # same answer whether or not the number has bookings, and no SMS is spent on strangers
    sms_budget(conn)
    if HTTPS_ONLY and providers.env("SMS_PROVIDER", "mock") == "mock":  # production must never print codes to a console
        print("SMS_PROVIDER is 'mock' while MANTRALAYAM_HTTPS=1: set SMS_PROVIDER, no code was sent", file=sys.stderr)
        raise UserError("We could not send the code. Please try again, or call the property.", 503)
    code, salt = f"{secrets.randbelow(10**6):06d}", secrets.token_hex(8)
    t = now()

    def store():
        conn.execute("DELETE FROM otp_codes WHERE phone10 = ? AND purpose = ? AND used_at IS NULL", (k, purpose))
        conn.execute("DELETE FROM otp_codes WHERE created_at < ?", ((t - timedelta(days=1)).isoformat(timespec="seconds"),))
        return conn.execute("INSERT INTO otp_codes (phone10, purpose, salt, code_hash, created_at, expires_at, ip) VALUES (?,?,?,?,?,?,?)",
                            (k, purpose, salt, otp_hash(salt, code), t.isoformat(timespec="seconds"), (t + timedelta(seconds=OTP_TTL)).isoformat(timespec="seconds"), ip)).lastrowid
    oid = write(conn, store)
    try:
        providers.sms().send(phone, tr(b.get("lang"), "sms.otp", code=code, minutes=OTP_TTL // 60))
    except providers.ProviderError as e:
        write(conn, lambda: conn.execute("DELETE FROM otp_codes WHERE id = ?", (oid,)))
        print(f"OTP SMS failed: {e}", file=sys.stderr)
        raise UserError("We could not send the code. Please try again, or call the property.", 503)
    return reply


def otp_hash(salt, code):
    return hmac.new(salt.encode(), code.encode(), "sha256").hexdigest()


def otp_verify(conn, b, ip):
    """POST /api/public/otp/verify {phone, purpose, code}. A code lives OTP_TTL seconds and allows OTP_MAX_ATTEMPTS wrong tries.
    booking: returns a one-use proof (valid PROOF_TTL seconds) that book() consumes, plus the visitor's saved details if they opted in earlier.
    login: starts a guest session (the handler turns `_session` into the cookie)."""
    purpose = b.get("purpose")
    if purpose not in OTP_PURPOSES:
        raise UserError("Some of the values sent are not valid.")
    throttle(("otp-verify-ip", ip), 30, 600, "Too many attempts.")
    k, code = p10(norm_phone(b.get("phone"))), str(b.get("code") or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise UserError("Please enter the 6-digit code.")

    def work():
        row = conn.execute("SELECT * FROM otp_codes WHERE phone10 = ? AND purpose = ? AND verified_at IS NULL ORDER BY id DESC LIMIT 1", (k, purpose)).fetchone()
        if not row or row["expires_at"] <= now_iso():
            return "expired"
        if row["attempts"] >= OTP_MAX_ATTEMPTS:
            return "locked"
        if not hmac.compare_digest(otp_hash(row["salt"], code), row["code_hash"]):
            conn.execute("UPDATE otp_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
            return "locked" if row["attempts"] + 1 >= OTP_MAX_ATTEMPTS else "wrong"
        proof = secrets.token_urlsafe(24)
        conn.execute("UPDATE otp_codes SET verified_at = ?, proof_hash = ?, proof_expires = ? WHERE id = ?",
                     (now_iso(), sha(proof), (now() + timedelta(seconds=PROOF_TTL)).isoformat(timespec="seconds"), row["id"]))
        return proof
    r = write(conn, work)  # the attempt count is saved before any error is raised
    if r == "expired":
        raise UserError("The code has expired or was not requested. Please ask for a new code.")
    if r == "locked":
        raise UserError("Too many wrong codes. Please ask for a new code.", 429)
    if r == "wrong":
        raise UserError("That code is not correct. Please check it and try again.")
    if purpose in ("booking", "subscribe"):
        return {"ok": True, "proof": r, "expires_in": PROOF_TTL, "profile": profile_for(conn, k) if purpose == "booking" else None}
    tok, t = secrets.token_urlsafe(32), now()

    def start():
        conn.execute("DELETE FROM guest_sessions WHERE expires_at <= ?", (now_iso(),))
        conn.execute("INSERT INTO guest_sessions (token_hash, phone10, created_at, expires_at) VALUES (?,?,?,?)",
                     (sha(tok), k, t.isoformat(timespec="seconds"), (t + timedelta(seconds=SESSION_TTL)).isoformat(timespec="seconds")))
        audit(conn, "guest.login", None, None, None, "My bookings sign-in")
    write(conn, start)
    return {"ok": True, "csrf": csrf_token(tok), "_session": tok}


def use_proof(conn, proof, phone, required=True, purpose="booking"):
    """Consumes the booking proof from otp_verify for this mobile number (inside the booking transaction, so a failed booking keeps it). True when verified."""
    row = conn.execute("SELECT id FROM otp_codes WHERE proof_hash = ? AND purpose = ? AND phone10 = ? AND used_at IS NULL AND proof_expires > ?", (sha(proof), purpose, p10(phone), now_iso())).fetchone()
    if not row:
        if required:
            raise UserError("Please verify your mobile number with the code we send.", 403)
        return False
    conn.execute("UPDATE otp_codes SET used_at = ? WHERE id = ?", (now_iso(), row["id"]))
    return True


def session_phone(conn, token):
    r = conn.execute("SELECT phone10 FROM guest_sessions WHERE token_hash = ? AND expires_at > ?", (sha(token), now_iso())).fetchone() if token else None
    return r[0] if r else None


def profile_for(conn, k):
    r = conn.execute("SELECT name, email, id_type, consented_at FROM guest_profiles WHERE phone10 = ?", (k,)).fetchone()
    return dict(r) if r else None


def profile_save(conn, phone, name, email, id_type):
    """Saved details for repeat visitors. Only called with the guest's opt-in. The ID number is never saved: it is typed again each time."""
    k, t = p10(phone), now_iso()
    conn.execute("INSERT INTO guest_profiles (phone10, name, email, id_type, consented_at, updated_at) VALUES (?,?,?,?,?,?) "
                 "ON CONFLICT(phone10) DO UPDATE SET name = excluded.name, email = excluded.email, id_type = excluded.id_type, updated_at = excluded.updated_at", (k, name, email or None, id_type, t, t))


def guest_booking_ids(conn, k):
    return [r[0] for r in conn.execute(
        "SELECT COALESCE(group_id, id) FROM bookings WHERE source = 'online' AND substr(replace(replace(phone, ' ', ''), '-', ''), -10) = ? "
        "GROUP BY COALESCE(group_id, id) ORDER BY MAX(check_in) DESC, 1 DESC LIMIT 50", (k,))]


def me_session(conn, b, ip, sess):
    return {"ok": True, "phone_last4": sess["phone"][-4:], "csrf": csrf_token(sess["token"]), "profile": profile_for(conn, sess["phone"])}


def me_bookings(conn, b, ip, sess):
    out = [booking_view(conn, find_booking(conn, gid, sess["phone"])) for gid in guest_booking_ids(conn, sess["phone"])]
    wl = [dict(r) for r in conn.execute("SELECT w.id, w.check_in, w.check_out, w.guests, w.status, p.name AS property FROM waitlist w LEFT JOIN properties p ON p.id = w.property_id "
                                        "WHERE w.phone10 = ? AND w.status IN ('waiting', 'notified') ORDER BY w.id DESC", (sess["phone"],))]
    return {"bookings": out, "waitlist": wl, "profile": profile_for(conn, sess["phone"])}


def me_cancel(conn, b, ip, sess):
    return cancel(conn, dict(b) | {"phone": sess["phone"]}, ip)


def me_change(conn, b, ip, sess):
    return change_dates(conn, dict(b) | {"phone": sess["phone"]}, ip)


def me_update(conn, b, ip, sess):
    """Changes the details that do not affect rooms or price: arrival time, darshan date, special requests, who to call on arrival."""
    arrival, darshan = arrival_fields(b)
    remarks = str(b.get("special_requests") or "").strip()
    if len(remarks) > 500:
        raise UserError("Special requests must be 500 characters or fewer.")
    _, an, ap = check_contact({"arrival_contact_name": b.get("arrival_contact_name"), "arrival_contact_phone": b.get("arrival_contact_phone")})

    def work():
        rows = find_booking(conn, b.get("booking_id"), sess["phone"])
        if booking_view(conn, rows)["status"] != "Confirmed":
            raise UserError("This booking can no longer be changed online.", 409)
        ids = [r["id"] for r in rows if r["status"] == "Confirmed"]
        conn.execute(f"UPDATE bookings SET arrival_time = ?, darshan_date = ?, remarks = ?, arrival_contact_name = ?, arrival_contact_phone = ? WHERE id IN ({','.join('?' * len(ids))})",
                     (arrival, darshan, remarks or None, an, ap, *ids))
        audit(conn, "booking.online_update", rows[0]["property_id"], None, f"#{rows[0]['group_id'] or rows[0]['id']}", "Details changed online by guest")
        return rows[0]["group_id"] or rows[0]["id"]
    gid = write(conn, work)
    notify_safe(conn, "changed", gid)
    return booking_view(conn, find_booking(conn, gid, sess["phone"]))


def me_profile_delete(conn, b, ip, sess):
    write(conn, lambda: (conn.execute("DELETE FROM guest_profiles WHERE phone10 = ?", (sess["phone"],)), audit(conn, "guest.profile_delete", None, None, None, "Saved details removed by guest")))
    return {"ok": True}


def me_waitlist_cancel(conn, b, ip, sess):
    n = write(conn, lambda: conn.execute("UPDATE waitlist SET status = 'cancelled' WHERE id = ? AND phone10 = ? AND status IN ('waiting', 'notified')", (opt_int(b.get("id")), sess["phone"])).rowcount)
    if not n:
        raise UserError("We could not find that request.", 404)
    return {"ok": True}


def me_logout(conn, b, ip, sess):
    write(conn, lambda: conn.execute("DELETE FROM guest_sessions WHERE token_hash = ?", (sha(sess["token"]),)))
    return {"ok": True, "_session": ""}


def money_text(v):
    return f"{v:,.2f}"


def receipt_rows(v, lang):
    """(label, value) pairs for the details part of a receipt, in the guest's language."""
    T = lambda key, **kw: tr(lang, key, **kw)
    return [(T("receipt.id"), f"#{v['booking_id']}"), (T("receipt.guest"), v["guest_name"]), (T("receipt.property"), v["property"]["name"]),
            (T("receipt.room"), f"{v['rooms']} × {v['room_type']}"), (T("receipt.stay"), f"{v['check_in']} → {v['check_out']} ({v['nights']})"), (T("receipt.status"), tr(lang, "status." + v["status"]))]


def receipt_lines(v, lang):
    """(label, rupees) pairs for the money part: the price breakdown, the total, what was paid and refunded and what is left."""
    T = lambda key, **kw: tr(lang, key, **kw)
    p, pay, lines = v["price"] or {}, v["payment"], []
    if p:
        lines.append((T("receipt.room_charge"), p["room_charge"]))
        if p["extra_bed_charge"]:
            lines.append((T("receipt.extra_beds"), p["extra_bed_charge"]))
        if p["child_charge"]:
            lines.append((T("receipt.children"), p["child_charge"]))
        if p["discount"]:
            lines.append((tr(lang, "receipt.discount") + f" ({p['discount']['code'] or p['discount']['label']})", -p["discount"]["amount"]))
        if p["tax"]:
            lines.append((tr(lang, "receipt.tax", percent=f"{p['tax_percent']:g}"), p["tax"]))
        for sv in p.get("sevas") or []:
            lines.append((f"{sv['name']} × {sv['qty']} ({sv['date']})", sv["amount"]))
    lines.append((T("receipt.total"), v["amount"]))
    if pay["paid"]:
        lines.append((T("receipt.paid"), pay["paid"]))
        if pay["refunded"] or pay["refund_in_progress"]:
            lines.append((T("receipt.refunded"), -(pay["refunded"] + pay["refund_in_progress"])))
        if pay["balance"] > 0:
            lines.append((T("receipt.balance"), pay["balance"]))
    elif v["advance_due"]:
        lines += [(T("receipt.advance"), v["advance_due"]), (T("receipt.balance"), v["amount"] - v["advance_due"])]
    return lines


def receipt_note(v, lang):
    paid = v["payment"]["paid"]
    return tr(lang, "receipt.pay_note" if not paid else "receipt.paid_note" if v["payment"]["balance"] <= 0 else "receipt.part_note")


def receipt_table(v, lang):
    e = html.escape
    return ("<table>" + "".join(f"<tr><th>{e(k)}</th><td>{e(str(x))}</td></tr>" for k, x in receipt_rows(v, lang))
            + "".join(f"<tr><th>{e(k)}</th><td class=r>₹{money_text(x)}</td></tr>" for k, x in receipt_lines(v, lang)) + "</table>")


def notice_html(v, lang):
    """The weather / river notices for the stay, for the printable receipt."""
    if not v.get("notices"):
        return ""
    return f"<h2 style=\"font-size:1.1rem\">{html.escape(tr(lang, 'notice.heading'))}</h2><ul>" + "".join(f"<li><strong>{html.escape(tr(lang, 'notice.' + n['kind']))}:</strong> {html.escape(n['text'])}</li>" for n in v["notices"]) + "</ul>"


def receipt_html(v, lang, code=None):
    """A printable receipt page for one booking (Save as PDF from the browser's print dialog). Every value is escaped. With a check-in code it shows the QR code."""
    e, T = html.escape, lambda key, **kw: html.escape(tr(lang, key, **kw))
    qr_html = ""
    if code and v["status"] in ("Confirmed", "Checked-In"):
        png = base64.b64encode(qr_module.qr_png(qr_module.qr_matrix(code), 6, 4)).decode()
        qr_html = f"<p style=\"text-align:center\"><img src=\"data:image/png;base64,{png}\" width=180 height=180 alt=\"{T('receipt.qr_alt')}\"><br><code>{e(code)}</code></p><p>{T('receipt.qr_note')}</p>"
    return (f"<!doctype html><html lang=\"{e(lang if lang in LANGS else 'en')}\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{T('receipt.title')} #{v['booking_id']}</title><style>body{{font:16px/1.5 Lora,Georgia,serif;max-width:560px;margin:2rem auto;padding:0 1rem;color:#222}}"
            f"table{{width:100%;border-collapse:collapse}}th,td{{padding:.4rem .2rem;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}}.r{{text-align:right}}"
            f"h1{{font-size:1.4rem}}p{{color:#555}}</style></head><body><h1>{T('receipt.title')}</h1>{receipt_table(v, lang)}{notice_html(v, lang)}{qr_html}<p>{e(receipt_note(v, lang))}</p></body></html>")


def pdf_reply(conn, v):
    code = notifications.checkin_code(conn, v["booking_id"])
    return {"_bytes": notifications.receipt_pdf(conn, v, code), "_ctype": "application/pdf", "_filename": f"booking-{v['booking_id']}.pdf"}


def receipt_pdf_api(conn, b, ip):
    """POST /api/public/booking/receipt {booking_id, phone}: the booking receipt as a PDF (booking ID and mobile number, like lookup)."""
    throttle(("lookup", ip), 20, 600, "Too many attempts.")
    return lookup_guard(b.get("phone"), lambda: pdf_reply(conn, booking_view(conn, find_booking(conn, b.get("booking_id"), b.get("phone")))))


def me_receipt_pdf(conn, b, ip, sess):
    return pdf_reply(conn, booking_view(conn, find_booking(conn, b.get("booking_id"), sess["phone"])))


def me_receipt(conn, b, ip, sess):
    v = booking_view(conn, find_booking(conn, b.get("booking_id"), sess["phone"]))
    return {"_html": receipt_html(v, b.get("lang"), notifications.checkin_code(conn, v["booking_id"])), "_filename": f"receipt-{v['booking_id']}.html"}


WAIT_MAX_PER_PHONE, NOTIFY_GRACE_HOURS = 3, 3


def bookable_groups(conn, s, ci, co, guests, pid, tid, flt):
    """Building/type groups where the party fits within the online room limit: [(property_name, free_rooms_in_group, rooms_needed, property_id, room_type_id)]."""
    groups = defaultdict(list)
    for r in free_rooms(conn, ci, co, pid, tid, None, **flt):
        groups[(r["property_id"], r["room_type_id"])].append(r)
    out = []
    for (gp, gt), rooms in groups.items():
        need = rooms_needed([r["capacity"] for r in rooms], guests)
        if need is not None and need <= s["max_rooms"]:
            out.append((rooms[0]["pname"], len(rooms), need, gp, gt))
    return out


def waitlist_join(conn, b, ip):
    """POST /api/public/waitlist: "notify me" for a sold-out stay. Only accepted while nothing bookable is free for those dates."""
    throttle(("waitlist-ip", ip), 5, 600, "Too many requests from this device.")
    if str(b.get("website") or "").strip():
        throttle(("waitlist-ip", ip), 5, 600, "Too many requests from this device.")
        raise UserError("Some of the values sent are not valid.")
    captcha_check(b, ip)
    ci, co, nights, s = parse_stay(conn, b.get("check_in"), b.get("check_out"))
    guests = to_int(b.get("guests"), "Number of guests must be between 1 and 200.", 1, 200)
    name, email = str(b.get("name") or "").strip(), str(b.get("email") or "").strip()
    if not name or len(name) > 100:
        raise UserError("Please enter your name.")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 120:
        raise UserError("Please enter a valid email address, or leave it empty.")
    phone = norm_phone(b.get("phone"))
    pid, tid, flt, lang = opt_int(b.get("property_id")), opt_int(b.get("room_type_id")), room_filters(b), b.get("lang") if b.get("lang") in LANGS else "en"
    throttle(("waitlist", p10(phone)), 3, 600, "Too many requests for this mobile number.")

    def work():
        if pid and not conn.execute("SELECT 1 FROM properties WHERE id = ? AND active = 1", (pid,)).fetchone():
            raise UserError("Please choose a valid property and room type.")
        if bookable_groups(conn, s, ci, co, guests, pid, tid, flt):
            raise UserError("Rooms are available for those dates. Please search and book instead.", 409)
        same = conn.execute("SELECT id FROM waitlist WHERE phone10 = ? AND status IN ('waiting', 'notified') AND check_in = ? AND check_out = ? AND IFNULL(property_id, 0) = ? AND IFNULL(room_type_id, 0) = ?",
                            (p10(phone), ci.isoformat(), co.isoformat(), pid or 0, tid or 0)).fetchone()
        if same:
            return same[0]
        if conn.execute("SELECT COUNT(*) FROM waitlist WHERE phone10 = ? AND status IN ('waiting', 'notified')", (p10(phone),)).fetchone()[0] >= WAIT_MAX_PER_PHONE:
            raise UserError("You already have the most waiting requests allowed. Please cancel one or wait for it to expire.", 409)
        return conn.execute("INSERT INTO waitlist (name, phone, phone10, email, lang, property_id, room_type_id, check_in, check_out, guests, rooms, senior, ground, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                            (name, phone, p10(phone), email or None, lang, pid, tid, ci.isoformat(), co.isoformat(), guests, int(flt["senior"]), int(flt["ground"]), now_iso())).lastrowid
    return {"ok": True, "id": write(conn, work)}


def waitlist_cancel(conn, b, ip):
    throttle(("lookup", ip), 20, 600, "Too many attempts.")
    wid = opt_int(b.get("id"))
    n = write(conn, lambda: conn.execute("UPDATE waitlist SET status = 'cancelled' WHERE id = ? AND status IN ('waiting', 'notified') AND phone10 = ?", (wid, p10(b.get("phone")))).rowcount) if wid else 0
    if not n:
        raise UserError("We could not find that request.", 404)
    return {"ok": True}


def waitlist_tick(conn):
    """Expires waitlist requests whose check-in has passed, then tells waiting guests (oldest first) that their stay is bookable again, by SMS and, if they gave one, email.
    A guest is told once. Rooms already promised to an earlier notified guest in the last NOTIFY_GRACE_HOURS are not promised twice. Nothing is held for them.
    Safe to call often, from any process: after cancellations, by the `tick` command, and on a timer. Returns how many guests were told."""
    write(conn, lambda: conn.execute("UPDATE waitlist SET status = 'expired' WHERE status IN ('waiting', 'notified') AND check_in < ?", (today().isoformat(),)))
    base, told = settings(conn), 0
    if not base["enabled"]:
        return 0
    link = site_values(conn, "en").get("official_site_url")
    for w in conn.execute("SELECT * FROM waitlist WHERE status = 'waiting' ORDER BY id").fetchall():
        ci, co, s = date.fromisoformat(w["check_in"]), date.fromisoformat(w["check_out"]), dict(base)
        try:
            check_stay(conn, s, ci, co)
        except UserError:
            continue
        pending = conn.execute("SELECT property_id, room_type_id FROM waitlist WHERE status = 'notified' AND notified_at > ? AND check_in < ? AND check_out > ?",
                               ((now() - timedelta(hours=NOTIFY_GRACE_HOURS)).isoformat(timespec="seconds"), w["check_out"], w["check_in"])).fetchall()
        hit = next(((pn, gp, gt) for pn, free, need, gp, gt in bookable_groups(conn, s, ci, co, w["guests"], w["property_id"], w["room_type_id"], {"senior": bool(w["senior"]), "ground": bool(w["ground"])})
                    if free - sum(1 for p in pending if p[0] in (None, gp) and p[1] in (None, gt)) >= need), None)
        if not hit:
            continue
        text = tr(w["lang"], "sms.waitlist", property=hit[0], check_in=w["check_in"], check_out=w["check_out"], link=link or tr(w["lang"], "sms.site_fallback"))
        try:
            providers.sms().send(w["phone"], text)
            if w["email"]:
                providers.email().send(w["email"], tr(w["lang"], "sms.waitlist_subject"), text)
        except providers.ProviderError as e:
            print(f"Waitlist notification failed: {e}", file=sys.stderr)
            continue
        write(conn, lambda: conn.execute("UPDATE waitlist SET status = 'notified', notified_at = ? WHERE id = ?", (now_iso(), w["id"])))
        told += 1
    return told


def waitlist_tick_safe(conn):
    try:
        return waitlist_tick(conn)
    except Exception:  # never fail a cancellation because a notification could not be sent
        traceback.print_exc()
        return 0


BACKUP_MAX_HOURS = float(os.environ.get("HEALTH_BACKUP_MAX_HOURS", "30"))  # a backup older than this is "stale"; 0 turns the backup check off


HOLD_ALERT_PERCENT = int(os.environ.get("HOLD_ALERT_PERCENT", "25"))  # alert when this share of all rooms is on hold at once


HOLD_LAPSE_ALERT = int(os.environ.get("HOLD_LAPSE_ALERT", "20"))  # alert when this many holds lapsed unbooked in the last hour


def backup_state():
    """{'status': ok | failed | stale | none | disabled, 'age_hours', 'detail'} from the latest backup run in backups/backup.log."""
    if not BACKUP_MAX_HOURS:
        return {"status": "disabled", "age_hours": None, "detail": ""}
    last = last_backup()
    if not last:
        return {"status": "none", "age_hours": None, "detail": ""}
    age = (now() - datetime.fromisoformat(last["ts"])).total_seconds() / 3600
    return {"status": "failed" if not last["ok"] else "stale" if age > BACKUP_MAX_HOURS else "ok", "age_hours": round(age, 1), "detail": last["detail"]}


def alert_raise(conn, kind, key, message):
    """Opens an alert (once per kind and key while it stays open; the text is refreshed) and emails it to the staff addresses the first time."""
    def work():
        row = conn.execute("SELECT id FROM alerts WHERE kind = ? AND key = ? AND resolved_at IS NULL", (kind, key)).fetchone()
        if row:
            conn.execute("UPDATE alerts SET message = ? WHERE id = ?", (message, row[0]))
            return row[0], False
        return conn.execute("INSERT INTO alerts (kind, key, message, created_at) VALUES (?,?,?,?)", (kind, key, message, now_iso())).lastrowid, True
    aid, new = write(conn, work)
    if new:
        alert_email(conn, aid, message)
    return aid


def alert_email(conn, aid, message):
    to = [x.strip() for x in providers.env("STAFF_ALERT_EMAILS").split(",") if x.strip()]
    note = "no STAFF_ALERT_EMAILS configured" if not to else ""
    for addr in to:
        try:
            providers.email().send(addr, "[Mantralayam Rooms] Alert: " + message[:80], message + "\n\nOpen the staff app dashboard for details.")
        except providers.ProviderError as e:
            note = f"email to {addr} failed: {e}"
    write(conn, lambda: conn.execute("UPDATE alerts SET emailed_at = ?, email_note = ? WHERE id = ?", (now_iso() if to and not note else None, note or None, aid)))


def alert_resolve(conn, kind, key=None):
    write(conn, lambda: conn.execute("UPDATE alerts SET resolved_at = ? WHERE kind = ? AND (? IS NULL OR key = ?) AND resolved_at IS NULL", (now_iso(), kind, key, key)))


def alerts_open(conn):
    return [dict(r) for r in conn.execute("SELECT id, kind, key, message, created_at, emailed_at, email_note FROM alerts WHERE resolved_at IS NULL ORDER BY id")]


def check_alerts(conn):
    """Evaluates the alert conditions: a failed, stale or missing backup, many rooms on hold at once (unpaid reservations that lock rooms), and many holds lapsing unbooked.
    Opens, refreshes or resolves alerts and emails new ones. Returns the open alerts. Cheap; run it every few minutes (`tick`) and whenever the dashboard loads."""
    bk = backup_state()
    texts = {"failed": f"The last backup FAILED: {bk['detail']}", "stale": f"No successful backup for {bk['age_hours']} hours (limit {BACKUP_MAX_HOURS:g}).", "none": "No backup has run yet."}
    if bk["status"] in texts:
        alert_raise(conn, "backup", "status", texts[bk["status"]])
    else:
        alert_resolve(conn, "backup")
    t = now_iso()
    rooms = conn.execute("SELECT COUNT(*) FROM rooms WHERE active = 1").fetchone()[0]
    held = conn.execute("SELECT COUNT(*) FROM holds WHERE expires_at > ?", (t,)).fetchone()[0]
    soon = conn.execute("SELECT COUNT(*) FROM holds WHERE expires_at > ? AND expires_at <= ?", (t, (now() + timedelta(minutes=2)).isoformat(timespec="seconds"))).fetchone()[0]
    if held >= 3 and rooms and held * 100 >= HOLD_ALERT_PERCENT * rooms:
        alert_raise(conn, "holds", "pressure", f"{held} of {rooms} rooms are on hold right now (unpaid, not yet booked); {soon} expire within 2 minutes. Possible room hoarding.")
    else:
        alert_resolve(conn, "holds", "pressure")
    sent = lambda key, span: conn.execute("SELECT COUNT(*) FROM rate_hits WHERE key = ? AND ts > ?", (key, time.time() - span)).fetchone()[0]
    if sent("sms-hour", 3600) < SMS_HOURLY_LIMIT and sent("sms-day", 86400) < SMS_DAILY_LIMIT:
        alert_resolve(conn, "sms")
    if not conn.execute("SELECT 1 FROM rate_hits WHERE key LIKE 'staff-login-user|%' AND ts > ? GROUP BY key HAVING COUNT(*) >= 10", (time.time() - 3600,)).fetchone():
        alert_resolve(conn, "login")
    lapsed = conn.execute("SELECT COUNT(DISTINCT token) FROM holds WHERE expires_at <= ? AND expires_at > ?", (t, (now() - timedelta(hours=1)).isoformat(timespec="seconds"))).fetchone()[0]
    if lapsed >= HOLD_LAPSE_ALERT:
        alert_raise(conn, "holds", "lapsed", f"{lapsed} room holds expired without a booking in the last hour. Guests may be failing to finish booking, or someone is blocking rooms.")
    else:
        alert_resolve(conn, "holds", "lapsed")
    return alerts_open(conn)


def erase_guest(conn, phone, reason, why="Erasure requested"):
    """Right to erasure (DPDP): removes the personal data of one guest (by mobile number) from bookings, saved details, sign-in codes and sessions, the waiting list, call-backs,
    subscriptions and stored IDs. Booking rows, dates and amounts stay (anonymised) because the accounts need them. Refuses while the guest has a stay that is not over."""
    k = p10(phone)
    if len(k) != 10:
        raise UserError("Please give the 10-digit mobile number.")
    like = "substr(replace(replace(phone, ' ', ''), '-', ''), -10) = ?"

    def work():
        rows = conn.execute(f"SELECT id, status, check_out FROM bookings WHERE {like}", (k,)).fetchall()
        callbacks = conn.execute(f"SELECT id FROM callback_requests WHERE {like}", (k,)).fetchall()
        if not rows and not callbacks and not conn.execute("SELECT 1 FROM guest_profiles WHERE phone10 = ? UNION SELECT 1 FROM subscribers WHERE phone10 = ? UNION SELECT 1 FROM waitlist WHERE phone10 = ?", (k, k, k)).fetchone():
            raise UserError("No guest found with that mobile number.", 404)
        if any(r["status"] in BUSY and r["check_out"] >= today().isoformat() for r in rows):
            raise UserError("This guest has a stay that is not over yet. Cancel it or wait until after check-out.", 409)
        ids = [r["id"] for r in rows]
        mk = ",".join("?" * len(ids)) or "NULL"
        conn.execute(f"UPDATE bookings SET guest_name = 'Erased guest', phone = '', email = NULL, remarks = NULL, booker_name = NULL, arrival_contact_name = NULL, arrival_contact_phone = NULL, "
                     f"id_number = '' WHERE id IN ({mk})", ids)
        conn.execute(f"UPDATE id_records SET number_enc = NULL, image_enc = NULL, image_mime = NULL, last4 = NULL, purged_at = COALESCE(purged_at, ?) WHERE booking_id IN ({mk})", (now_iso(), *ids))
        conn.execute(f"UPDATE callback_requests SET name = 'Erased guest', phone = '', note = NULL WHERE {like}", (k,))
        conn.execute("UPDATE waitlist SET name = 'Erased guest', phone = '', phone10 = '', email = NULL WHERE phone10 = ?", (k,))
        conn.execute("UPDATE subscribers SET name = 'Erased guest', phone = NULL, phone10 = NULL, email = NULL, unsubscribed_at = COALESCE(unsubscribed_at, ?) WHERE phone10 = ?", (now_iso(), k))
        conn.execute("UPDATE payments SET phone10 = NULL WHERE phone10 = ?", (k,))
        for t in ("guest_profiles", "guest_sessions", "otp_codes"):
            conn.execute(f"DELETE FROM {t} WHERE phone10 = ?", (k,))
        audit(conn, "guest.erase", None, None, f"{len(ids)} booking(s) anonymised, mobile ending {k[-4:]}", why + (f": {reason}" if reason else ""))
        return len(ids)
    return write(conn, work)


def purge_guests(conn, limit=100):
    """Retention: when guest_retention_days is set, guests whose last stay ended more than that many days ago (and who have nothing upcoming) are erased. Returns how many."""
    days = settings(conn)["guest_retention_days"]
    if days is None:
        return 0
    cut = (today() - timedelta(days=days)).isoformat()
    phones = [r[0] for r in conn.execute(
        "SELECT substr(replace(replace(phone, ' ', ''), '-', ''), -10) AS k FROM bookings WHERE phone != '' GROUP BY k "
        "HAVING MAX(check_out) < ? AND SUM(status IN ('Confirmed', 'Checked-In')) = 0 LIMIT ?", (cut, limit))]
    n = 0
    for k in phones:
        try:
            erase_guest(conn, k, f"{days} days after the last stay", "Retention")
            n += 1
        except UserError:
            pass
    return n


def scrub_ids(conn):
    """One-off for data created before ID numbers were kept to the last four characters: every full number still in bookings.id_number is moved into the encrypted ID
    store (id_records) and replaced by ****last4. Needs ID_ENCRYPTION_KEY. Safe to repeat. Returns (moved, already_masked)."""
    key = vault_key(conn)

    def work():
        moved = masked = 0
        for r in conn.execute("SELECT id, id_type, id_number FROM bookings WHERE id_number != '' AND id_number NOT LIKE '****%'").fetchall():
            has = conn.execute("SELECT 1 FROM id_records WHERE booking_id = ? AND number_enc IS NOT NULL", (r["id"],)).fetchone()
            if not has:
                conn.execute("INSERT INTO id_records (booking_id, id_type, last4, number_enc, captured_name, captured_at) VALUES (?,?,?,?,?,?) "
                             "ON CONFLICT(booking_id) DO UPDATE SET id_type = excluded.id_type, last4 = excluded.last4, number_enc = excluded.number_enc, captured_name = excluded.captured_name, captured_at = excluded.captured_at, purged_at = NULL",
                             (r["id"], r["id_type"], r["id_number"][-4:], idvault.seal(key, r["id_number"].encode(), f"id:{r['id']}".encode()), "moved from booking", now_iso()))
                moved += 1
            conn.execute("UPDATE bookings SET id_number = ? WHERE id = ?", ("****" + r["id_number"][-4:], r["id"]))
        return moved, masked
    return write(conn, work)


def rekey(conn):
    """Rewrites every stored ID number, ID photo and pending-payment blob that is still in the old (version 1) format in the current format (scrypt-stretched key).
    Needs the same ID_ENCRYPTION_KEY that wrote them. Returns how many were rewritten."""
    key = vault_key(conn)

    def work():
        n = 0
        for r in conn.execute("SELECT booking_id, number_enc, image_enc FROM id_records").fetchall():
            for col, aad in (("number_enc", f"id:{r['booking_id']}"), ("image_enc", f"img:{r['booking_id']}")):
                if r[col] is not None and idvault.version_of(r[col]) == idvault.V1:
                    conn.execute(f"UPDATE id_records SET {col} = ? WHERE booking_id = ?", (idvault.seal(key, idvault.open_(key, bytes(r[col]), aad.encode()), aad.encode()), r["booking_id"]))
                    n += 1
        for r in conn.execute("SELECT id, order_id, payload FROM payments WHERE payload IS NOT NULL").fetchall():
            if idvault.version_of(r["payload"]) == idvault.V1:
                conn.execute("UPDATE payments SET payload = ? WHERE id = ?", (idvault.seal(key, idvault.open_(key, bytes(r["payload"]), r["order_id"].encode()), r["order_id"].encode()), r["id"]))
                n += 1
        return n
    return write(conn, work)


def purge_ids(conn):
    """Retention: when id_retention_days is set, ID documents and ID numbers of stays that ended (checked out, cancelled, no-show) more than that many days ago are erased.
    Returns how many records were cleared. Does nothing while the setting is empty."""
    days = settings(conn)["id_retention_days"]
    if days is None:
        return 0
    cut = (today() - timedelta(days=days)).isoformat()
    done = "status IN ('Checked-Out', 'Cancelled', 'No-Show') AND check_out < ?"

    def work():
        n = conn.execute(f"UPDATE id_records SET number_enc = NULL, image_enc = NULL, image_mime = NULL, last4 = NULL, purged_at = ? WHERE purged_at IS NULL AND booking_id IN (SELECT id FROM bookings WHERE {done})",
                         (now_iso(), cut)).rowcount
        m = conn.execute(f"UPDATE bookings SET id_number = '' WHERE id_number != '' AND {done}", (cut,)).rowcount
        if n or m:
            audit(conn, "id.purge", None, None, f"{n} ID record(s), {m} ID number(s) erased", f"Retention: {days} days after the stay")
        return n + m
    return write(conn, work)


def tick():
    """The periodic job (every 5 minutes from cron or a systemd timer): alerts, waitlist, payment expiry and refunds, reminders, message retries, ID retention."""
    conn = connect()
    try:
        out = {"alerts": len(check_alerts(conn)), "waitlist_notified": waitlist_tick(conn)}
        for name, fn in (("payments", lambda: payments.tick(conn)), ("reminders", lambda: notifications.reminders(conn)), ("messages_retried", lambda: notifications.retry_failed(conn)),
                         ("id_purged", lambda: purge_ids(conn)), ("guests_purged", lambda: purge_guests(conn)), ("feedback_asked", lambda: extras.feedback_requests(conn)), ("analytics_purged", lambda: analytics.purge(conn))):
            try:
                out[name] = fn()
            except Exception:  # one failing job must not stop the others
                traceback.print_exc()
                out[name] = "error"
        payments.refund_alerts(conn)
    finally:
        conn.close()
    out["heartbeat"] = providers.heartbeat()
    return out


def backup_alert(ok, detail):
    """Best effort: a backup result opens or clears the backup alert at once (the backup itself must never fail because of this)."""
    try:
        conn = connect()
        try:
            alert_resolve(conn, "backup") if ok else alert_raise(conn, "backup", "status", f"The last backup FAILED: {detail}")
        finally:
            conn.close()
    except Exception as e:
        print(f"Could not update the backup alert: {e}", file=sys.stderr)


ROUTES = {("GET", "/health"): health, ("GET", "/api/public/info"): info, ("GET", "/api/public/search"): search, ("GET", "/api/public/calendar"): calendar,
          ("POST", "/api/public/quote"): quote_api, ("POST", "/api/public/book"): book, ("POST", "/api/public/hold"): hold, ("POST", "/api/public/release"): release,
          ("POST", "/api/public/callback"): callback, ("POST", "/api/public/booking/lookup"): lookup, ("POST", "/api/public/booking/cancel"): cancel,
          ("POST", "/api/public/booking/change"): change_dates, ("POST", "/api/public/waitlist"): waitlist_join, ("POST", "/api/public/waitlist/cancel"): waitlist_cancel,
          ("POST", "/api/public/otp/send"): otp_send, ("POST", "/api/public/otp/verify"): otp_verify,
          ("POST", "/api/public/payment/verify"): payments.confirm, ("POST", "/api/public/payment/cancel"): payments.abandon, ("GET", "/api/public/payment/status"): payments.status,
          ("POST", "/api/public/payment/mock"): payments.mock_checkout, ("POST", "/api/public/booking/receipt"): receipt_pdf_api,
          ("GET", "/api/public/festivals"): extras.festivals_public, ("GET", "/api/public/feedback/info"): extras.feedback_info, ("POST", "/api/public/feedback"): extras.feedback_submit,
          ("POST", "/api/public/subscribe"): extras.subscribe, ("POST", "/api/public/subscribe/confirm"): extras.subscribe_confirm, ("POST", "/api/public/unsubscribe"): extras.unsubscribe}
# routes that need a signed-in guest (cookie session); fn(conn, data, ip, {"phone", "token"}). POSTs also need the X-CSRF-Token header.
ME_ROUTES = {("GET", "/api/me/session"): me_session, ("GET", "/api/me/bookings"): me_bookings, ("GET", "/api/me/receipt"): me_receipt, ("GET", "/api/me/receipt.pdf"): me_receipt_pdf,
             ("POST", "/api/me/booking/cancel"): me_cancel, ("POST", "/api/me/booking/change"): me_change, ("POST", "/api/me/booking/update"): me_update,
             ("POST", "/api/me/profile/delete"): me_profile_delete, ("POST", "/api/me/waitlist/cancel"): me_waitlist_cancel, ("POST", "/api/me/logout"): me_logout}


def dispatch(method, path, query, body, ip, session=None, csrf=None):
    """-> (http_status, json_able). Never raises. `session`: the guest_session cookie value and `csrf`: the X-CSRF-Token header, both only for /api/me/ routes.
    A reply may carry `_session` (token to set as the cookie, '' to clear it), `_html` and `_filename` (a download); the HTTP handler turns those into headers."""
    me = ME_ROUTES.get((method, path))
    fn = me or ROUTES.get((method, path))
    if not fn:
        return 404, {"error": "Not found."}
    conn = None
    try:
        data = clean_body(query if method == "GET" else body, 200 if method == "GET" else STR_MAX)
        conn = None if fn is health else connect()
        if me:
            phone = session_phone(conn, session)
            if not phone:
                raise UserError("Please sign in with your mobile number.", 401)
            if method == "POST" and not csrf_ok(session, csrf):
                raise UserError("Your session could not be checked. Please refresh the page and try again.", 403)
            return 200, me(conn, data, ip, {"phone": phone, "token": session})
        out = fn(conn, data, ip)
        return out if isinstance(out, tuple) else (200, out)
    except UserError as e:
        return e.status, {"error": str(e)} | ({"retry_after": e.retry_after} if e.retry_after else {})
    except sqlite3.OperationalError as e:
        if "locked" in str(e) or "busy" in str(e):
            return 503, {"error": "The database is busy. Please try again."}
        traceback.print_exc()
        return 500, {"error": "Something went wrong. Please try again."}
    except sqlite3.IntegrityError as e:
        if "already booked" in str(e):  # the database's own overlap guard: the transaction check should have caught it first
            return 409, {"error": "Sorry, those rooms are no longer available for your dates. Please search again."}
        traceback.print_exc()
        return 500, {"error": "Something went wrong. Please try again."}
    except Exception:
        traceback.print_exc()
        return 500, {"error": "Something went wrong. Please try again."}
    finally:
        if conn:
            conn.close()


def payment_webhook(raw, signature, event_id=None):
    """The provider's webhook: -> (http status, json). Needs the raw body (the signature covers it), so the handler calls this before any JSON parsing."""
    conn = None
    try:
        conn = connect()
        return payments.webhook(conn, raw, signature, event_id)
    except UserError as e:
        return e.status, {"error": str(e)}
    except Exception:  # a 500 makes the provider retry later; the handler is idempotent
        traceback.print_exc()
        return 500, {"error": "Something went wrong."}
    finally:
        if conn:
            conn.close()


def content_length(headers, limit):
    """The declared body size. A missing length is 0; a negative, non-numeric or over-limit one is refused (UserError 413/400) before anything is read."""
    raw = headers.get("Content-Length")
    if raw is None:
        return 0
    if not raw.isdigit():
        raise UserError("Some of the values sent are not valid.", 400)
    if int(raw) > limit:
        raise UserError("Request too large.", 413)
    return int(raw)


class BoundedServer(ThreadingHTTPServer):
    """One thread per connection, but at most MAX_CONNECTIONS at once; beyond that a new connection is closed at once instead of piling up threads."""
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    timeout = SOCKET_TIMEOUT  # a client that stalls (slow headers, a body that never arrives) is dropped instead of holding a thread forever

    def log_message(self, fmt, *args):
        """The default access log, without the query string (it can carry mobile numbers or booking codes)."""
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), (fmt % args).split("?")[0]))

    def send(self, status, data, ctype="application/json", cache=None, cookies=(), filename=None):
        retry = data.get("retry_after") if status == 429 and isinstance(data, dict) else None
        data = data if isinstance(data, bytes) else json.dumps(data).encode()
        data, nonce = with_nonce(data, ctype)
        self.send_response(status)
        if retry:
            self.send_header("Retry-After", str(retry))
        self.send_header("Content-Type", ctype + ("" if ctype.startswith(("image/", "application/pdf")) else "; charset=utf-8"))
        self.send_header("Content-Length", str(len(data)))
        for k, v in security_headers(self.headers, nonce):
            self.send_header(k, v)
        for c in cookies:
            self.send_header("Set-Cookie", c)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if ctype == "application/json" or filename:
            self.send_header("Cache-Control", "no-store")
        elif cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def cookie(self, name):
        m = SimpleCookie(self.headers.get("Cookie", "")).get(name)
        return m.value if m else None

    def guest_cookie(self, token):
        return f"{GUEST_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL if token else 0}" + ("; Secure" if is_https(self.headers) else "")

    def api(self, method, url, q, body):
        ip = client_ip(self.headers, self.client_address[0])
        st, out = dispatch(method, url.path, q, body, ip, self.cookie(GUEST_COOKIE), self.headers.get("X-CSRF-Token"))
        cookies = ()
        if isinstance(out, dict) and "_session" in out:
            cookies = (self.guest_cookie(out.pop("_session")),)
        if isinstance(out, dict) and "_html" in out:
            return self.send(st, out["_html"].encode(), "text/html", cookies=cookies, filename=out["_filename"])
        if isinstance(out, dict) and "_bytes" in out:
            return self.send(st, out["_bytes"], out["_ctype"], cookies=cookies, filename=out["_filename"])
        self.send(st, out, cookies=cookies)

    def do_GET(self):
        bad = request_ok(self.headers, "GET", urlparse(self.path).path)
        if bad:
            return self.send(bad[0], {"error": bad[1]})
        url = urlparse(self.path)
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            return self.send(200, (ROOT / "web" / name).read_bytes(), ctype)
        loc = LOCALE_URL.fullmatch(url.path)
        if loc:
            return self.send(200, (ROOT / "web" / "locales" / f"{loc[1]}.json").read_bytes())
        if url.path.startswith("/uploads/"):
            got = read_upload(url.path.rsplit("/", 1)[1])
            return self.send(200, got[0], got[1], cache="public, max-age=86400") if got else self.send(404, b"", "text/plain")
        if url.path.startswith("/images/"):
            name = url.path.rsplit("/", 1)[1]
            f = IMAGES_DIR / name
            ctype = IMAGE_TYPES.get(f.suffix.lower())
            if not IMAGE_NAME.fullmatch(name) or not ctype or not f.is_file():  # one plain file name: no slash, backslash, drive letter or ..
                return self.send(404, b"", "text/plain")
            return self.send(200, f.read_bytes(), ctype, cache="public, max-age=3600")
        self.api("GET", url, {k: v[0] for k, v in parse_qs(url.query).items()}, None)

    do_HEAD = do_GET  # uptime monitors often use HEAD; send() leaves the body out

    def do_POST(self):
        bad = request_ok(self.headers, "POST", urlparse(self.path).path)
        if bad:
            return self.send(bad[0], {"error": bad[1]})
        if urlparse(self.path).path == "/api/public/payment/webhook":  # from the payment provider's servers: no Origin, a signed raw body
            try:
                n = content_length(self.headers, 100_000)
            except UserError as e:
                return self.send(e.status, {"error": str(e)})
            st, out = payment_webhook(self.rfile.read(n), self.headers.get("X-Razorpay-Signature"), self.headers.get("X-Razorpay-Event-Id"))
            return self.send(st, out)
        try:
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self.send(415, {"error": "Some of the values sent are not valid."})
            n = content_length(self.headers, BODY_MAX)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except UserError as e:
            return self.send(e.status, {"error": str(e)})
        except ValueError:
            return self.send(400, {"error": "Some of the values sent are not valid."})
        self.api("POST", urlparse(self.path), None, body)


def read_upload(name):
    """-> (bytes, content type) of an uploaded photo, or None. Only names this app generates are served (no paths, nothing else in the folder)."""
    if not UPLOAD_NAME.fullmatch(name or ""):
        return None
    f = UPLOADS / name
    return (f.read_bytes(), IMAGE_TYPES["." + name.rsplit(".", 1)[1]]) if f.is_file() else None


def serve_checks():
    """Reasons to refuse starting a public (HTTPS) deployment with development settings."""
    problems = []
    if HTTPS_ONLY and not providers.env("ID_ENCRYPTION_KEY"):
        problems.append("ID_ENCRYPTION_KEY is not set: stored ID documents and pending payment details need an encryption key kept outside the database (see docs/DEPLOYMENT.md)")
    if HTTPS_ONLY and len(providers.env("ID_ENCRYPTION_KEY")) < 32:
        problems.append("ID_ENCRYPTION_KEY is shorter than 32 characters: use a random one, for example python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    if HTTPS_ONLY and not providers.env("APP_SECRET"):
        problems.append("APP_SECRET is not set: without it the check-in QR signing secret is kept inside the database, and so inside every backup")
    if HTTPS_ONLY and not ALLOWED_HOSTS:
        problems.append("ALLOWED_HOSTS is not set: list the host names this server answers to (for example rooms.example.org,staff.example.org)")
    if providers.env("PAYMENT_PROVIDER") == "mock" and (HTTPS_ONLY or TRUST_PROXY or ALLOWED_HOSTS):
        problems.append("PAYMENT_PROVIDER is 'mock': it simulates payments without taking money (use razorpay, or leave it empty)")
    if providers.env("PAYMENT_PROVIDER") == "razorpay" and not providers.env("RAZORPAY_WEBHOOK_SECRET"):
        problems.append("RAZORPAY_WEBHOOK_SECRET is not set: payment webhooks cannot be verified, so payments confirmed only by webhook would be lost")
    if HTTPS_ONLY and providers.env("SMS_PROVIDER", "mock") == "mock" and OTP_REQUIRED:
        problems.append("SMS_PROVIDER is 'mock': guests would never receive their codes (set SMS_PROVIDER=http, or BOOKING_OTP_REQUIRED=0 until then)")
    return problems


def serve_warnings():
    """Things worth fixing on a public deployment that do not stop the server from starting."""
    out = []
    if HTTPS_ONLY and OTP_REQUIRED and not providers.captcha_config():
        out.append("CAPTCHA_PROVIDER is not set: the code form is protected by rate limits and the SMS budget only. A CAPTCHA is the best defence against SMS pumping.")
    if HTTPS_ONLY and not (providers.env("STAFF_ALERT_EMAILS")):
        out.append("STAFF_ALERT_EMAILS is not set: alerts (failed backup, SMS budget, room hoarding) will only show on the staff dashboard.")
    return out


def main(argv):
    if argv[:1] == ["backup"]:
        try:
            print(f"Backup written to {backup()}")
        except BackupError as e:
            print(f"Backup FAILED: {e}", file=sys.stderr)
            return 1
        return 0
    if argv[:1] == ["tick"]:  # every 5 minutes: alerts and waitlist notifications
        print(json.dumps(tick()))
        return 0
    if argv[:1] in (["scrub-ids"], ["rekey"]) or argv[:1] == ["erase"]:  # maintenance on the live database: back it up first (python tools/mantralayam_rooms.py backup)
        conn = connect()
        try:
            if argv[0] == "scrub-ids":
                moved, _ = scrub_ids(conn)
                print(f"{moved} full ID number(s) moved into the encrypted store and masked in bookings.")
            elif argv[0] == "rekey":
                print(f"{rekey(conn)} stored value(s) rewritten in the current encryption format.")
            elif len(argv) >= 2:
                reason = " ".join(argv[2:])
                print(f"{erase_guest(conn, argv[1], reason, 'Erasure from the command line')} booking(s) anonymised.")
            else:
                print("usage: erase <10-digit mobile> [reason]", file=sys.stderr)
                return 2
        except (UserError, idvault.VaultError) as e:
            print(f"Failed: {e}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        return 0
    if argv[:1] != ["serve"]:
        print(__doc__, file=sys.stderr)
        return 2
    for w in serve_warnings():
        print("Warning: " + w, file=sys.stderr)
    problems = serve_checks()
    if problems:
        print("Refusing to start:\n- " + "\n- ".join(problems), file=sys.stderr)
        return 1
    port = int(argv[1]) if len(argv) > 1 else 8001
    print(f"Serving on http://127.0.0.1:{port}")
    # Listens on localhost only: in production a reverse proxy (deploy/Caddyfile) terminates HTTPS in front of it
    BoundedServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
