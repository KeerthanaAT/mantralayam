"""Live audit: starts the guest and staff servers on a seeded COPY of the database (mock SMS / WhatsApp / email / Sheets, Razorpay mock, real OTP) and drives them over HTTP
and, when Playwright is installed, a real browser. Every check is recorded; the result is written to TEST_REPORT.md.

Run: python tools/audit_live.py
Optional libraries (the audit marks a check BLOCKED, never PASS, when one is missing): playwright (+ a Chromium or Edge), opencv-python (decodes QR codes), pymupdf (reads the PDF),
axe-core (accessibility scan: a folder holding axe.min.js, named in AUDIT_AXE). Extra import folders: AUDIT_PATHS (os.pathsep separated).
"""
import hashlib
import hmac
import http.client
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

for extra in filter(None, os.environ.get("AUDIT_PATHS", "").split(os.pathsep)):
    sys.path.insert(0, extra)
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

TMP = Path(tempfile.mkdtemp(prefix="audit-"))
DB = TMP / "audit.db"
GP, SP, XP = 18101, 18102, 18103
PW = "Staff#1234"
D = lambda n: (date.today() + timedelta(days=n)).isoformat()
RESULTS = []
LOGS = {"guest": TMP / "guest.log", "staff": TMP / "staff.log"}
PROCS = {}
ENV = {k: v for k, v in os.environ.items() if k not in ("MANTRALAYAM_HTTPS",)} | {
    "MANTRALAYAM_DB": str(DB), "MANTRALAYAM_UPLOADS": str(TMP / "uploads"), "MANTRALAYAM_BACKUPS": str(TMP / "backups"), "PAYMENT_PROVIDER": "mock", "WHATSAPP_PROVIDER": "mock", "SHEETS_PROVIDER": "mock",
    "SMS_PROVIDER": "mock", "EMAIL_PROVIDER": "mock", "TRUST_PROXY": "1", "BOOKING_OTP_REQUIRED": "1", "HEALTH_BACKUP_MAX_HOURS": "30", "PUBLIC_URL": f"http://127.0.0.1:{GP}", "PYTHONIOENCODING": "utf-8"}
ENV.pop("ID_ENCRYPTION_KEY", None)


def rec(area, feature, how, ok, note=""):
    res = "PASS" if ok is True else "BLOCKED" if ok == "blocked" else "FAIL"
    RESULTS.append([area, feature, how, res, note])
    print(f"{'PASS   ' if ok is True else 'BLOCKED' if ok == 'blocked' else 'FAIL   '} [{area}] {feature}" + (f": {note}" if note and ok is not True else ""), flush=True)


def check(area, feature, how, fn):
    """Runs fn() (returns (ok, note) or raises); an exception is a FAIL with its message."""
    try:
        out = fn()
        ok, note = out if isinstance(out, tuple) else (bool(out), "")
    except Exception as e:  # noqa
        ok, note = False, f"{type(e).__name__}: {e}"[:300]
    rec(area, feature, how, ok, note)
    return ok is True


# ---------- HTTP client ----------
class Client:
    def __init__(self, port, ip=None):
        self.port, self.cookies, self.csrf, self.ip = port, {}, "", ip

    def req(self, method, path, body=None, raw=None, headers=None, origin=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = dict(headers or {})
        if origin:
            h.setdefault("Origin", f"http://127.0.0.1:{self.port}")
        if self.ip:
            h["X-Forwarded-For"] = self.ip
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, data, h)
        r = c.getresponse()
        out = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        for k, v in r.getheaders():
            if k.lower() == "set-cookie":
                name, _, val = v.split(";")[0].partition("=")
                if val:
                    self.cookies[name] = val
                else:
                    self.cookies.pop(name, None)
        c.close()
        return r.status, hdrs, out

    def j(self, method, path, body=None, **kw):
        st, h, out = self.req(method, path, body, **kw)
        try:
            return st, json.loads(out)
        except ValueError:
            return st, out

    def post(self, path, body=None, **kw):  # a signed-in POST carries the CSRF token
        return self.j("POST", path, body if body is not None else {}, headers={"X-CSRF-Token": self.csrf} if self.csrf else None, **kw)


def guest(ip="10.9.0.1"):
    return Client(GP, ip)


def staff(user, ip="10.9.1.1"):
    c = Client(SP, ip)
    st, out = c.j("POST", "/api/login", {"username": user, "password": PW})
    assert st == 200, out
    c.csrf = out["csrf"]
    return c


def sms_code(phone):
    """The newest OTP the mock SMS printed for this number (read from the guest server's log: in production the code is only ever sent, never logged)."""
    for _ in range(40):
        txt = LOGS["guest"].read_text(encoding="utf-8", errors="replace")
        hit = re.findall(rf"\[mock SMS to {re.escape(phone)}\] (\d{{6}}) is your", txt)
        if hit:
            return hit[-1]
        time.sleep(0.1)
    raise AssertionError("no SMS code was printed for " + phone)


N = [0]


def phone():
    N[0] += 1
    return f"98{int(time.time()) % 100000:05d}{N[0]:03d}"[:10]


def uniq_ip():
    N[0] += 1
    return f"10.{N[0] // 250 % 250}.{N[0] % 250}.7"


def clear_cooldown(ph):
    c = db()
    c.execute("DELETE FROM rate_hits WHERE key LIKE 'otp-cool|%'")
    c.close()


def verified(cl, ph, purpose="booking"):
    clear_cooldown(ph)
    st, _ = cl.j("POST", "/api/public/otp/send", {"phone": ph, "purpose": purpose})
    assert st == 200
    st, r = cl.j("POST", "/api/public/otp/verify", {"phone": ph, "purpose": purpose, "code": sms_code(ph)})
    assert st == 200, r
    return r["proof"]


def base_req(**kw):
    return dict(property_id=1, room_type_id=2, check_in=D(20), check_out=D(22), rooms=1, guests=2) | kw


def make_booking(cl=None, method="pay_at_counter", ph=None, **kw):
    """hold -> OTP -> book (-> mock checkout for an online option). Returns (status, body, phone)."""
    cl = cl or guest(uniq_ip())
    ph = ph or phone()
    b = base_req(**{k: kw.pop(k) for k in list(kw) if k in ("property_id", "room_type_id", "check_in", "check_out", "rooms", "guests", "children", "extra_beds", "promo_code", "sevas", "package_id", "senior", "ground_floor")})
    st, hold = cl.j("POST", "/api/public/hold", b)
    if st != 200:
        return st, hold, ph
    proof = verified(cl, ph)
    body = b | dict(guest_name="Audit Guest", phone=ph, id_type="Aadhaar", id_number="1234-5678-9012", email="audit@example.org", lang="en", payment_method=method, hold_token=hold["token"], otp_proof=proof) | kw
    st, out = cl.j("POST", "/api/public/book", body)
    if st == 200 and out.get("payment_required"):
        pay = out["payment"]
        st2, r = cl.j("POST", "/api/public/payment/mock", {"order_id": pay["order_id"], "outcome": "success", "method": "upi"})
        st, out = cl.j("POST", "/api/public/payment/verify", r)
    return st, out, ph


# ---------- process control ----------
def start(name, port, extra_env=None, script=None):
    f = open(LOGS.get(name, TMP / f"{name}.log"), "ab")
    PROCS[name] = subprocess.Popen([sys.executable, str(HERE / (script or f"mantralayam_{name}.py")), "serve", str(port)], env=ENV | (extra_env or {}), stdout=f, stderr=subprocess.STDOUT, cwd=ROOT)
    for _ in range(60):
        try:
            http.client.HTTPConnection("127.0.0.1", port, timeout=2).request("GET", "/health")
            return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"{name} did not start; see {f.name}")


def stop(name):
    p = PROCS.pop(name, None)
    if p:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def db():
    c = sqlite3.connect(DB, isolation_level=None, timeout=10)
    c.row_factory = sqlite3.Row
    return c


# ---------- seed ----------
def seed():
    olds = sorted((ROOT / "backups").glob("mantralayam-????-??-??-????.db"))
    shutil.copy(olds[0] if olds else ROOT / ".tmp" / "mantralayam.db", DB)  # the oldest backup is the database as it was before these features: the audit starts from it, so every migration runs on real old data
    old = sqlite3.connect(DB)
    before = {t: old.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for (t,) in old.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    ids = {t: [r[0] for r in old.execute(f"SELECT rowid FROM {t} ORDER BY rowid")] for t in ("bookings", "users", "rooms", "properties", "audit_log")}
    cols = {t: [r[1] for r in old.execute(f"PRAGMA table_info({t})")] for t in before}
    table_sum = lambda conn, t: hashlib.sha256(repr([tuple(r) for r in conn.execute(f"SELECT {', '.join(cols[t])} FROM {t} ORDER BY rowid").fetchall()]).encode()).hexdigest()
    sums_before = {t: table_sum(old, t) for t in before}
    old.close()
    sys.path.insert(0, str(HERE))
    os.environ.update({k: ENV[k] for k in ("MANTRALAYAM_DB", "MANTRALAYAM_UPLOADS", "MANTRALAYAM_BACKUPS")})
    import mantralayam_rooms as m
    import mantralayam_staff as s
    m.DB = DB
    cn = m.connect()  # runs every migration on the old database
    after = {t: cn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    sums_after = {t: table_sum(cn, t) for t in before}
    ids["sums"] = (sums_before, sums_after)
    h, salt = s.make_hash(PW)
    cn.execute("UPDATE users SET pw_hash = ?, salt = ?", (h, salt))
    cn.execute("UPDATE room_types SET tariff = 600 WHERE id = 2")
    cn.execute("UPDATE peak_periods SET active = 0")
    cn.execute("UPDATE promo_codes SET active = 0")
    cn.execute("DELETE FROM bookings WHERE source = 'online'")
    cn.execute("UPDATE rooms SET senior_friendly = 1 WHERE property_id = 1 AND floor = 0")
    cn.execute("INSERT INTO peak_periods (name, start_date, end_date, max_nights, max_rooms, note, active, surcharge_percent) VALUES ('Aradhane test', ?, ?, 4, 2, 'Festival crowd', 1, 25)", (D(60), D(62)))
    cn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('tax_percent', '12'), ('advance_percent', '25'), ('extra_bed_price', '200'), ('extra_bed_max', '1'), ('child_price', '100'), ('refund_percent', '50'), ('refund_days', '7'), "
               "('pay_options', 'full,advance,counter'), ('guest_cancel_cutoff_hours', '24'), ('reminder_days', '1'), ('reminder_hour', '0'), ('checkin_requires_id', '1'), ('feedback_days', '1')")
    rooms = [r[0] for r in cn.execute("SELECT id FROM rooms WHERE active = 1")]
    cn.executemany("INSERT INTO room_status_events (room_id, kind, reason, start_date, end_date, active, created_at) VALUES (?, 'internal_block', 'SOLDOUT-TEST', ?, ?, 1, 'now')", [(r, D(70), D(71)) for r in rooms])
    cn.execute("INSERT INTO promo_codes (code, label, auto, kind, value, max_discount, exclude_peak, active, created_at) VALUES ('DEVOTEE10', 'Devotee 10%', 0, 'percent', 10, NULL, 1, 1, 'now')")
    cn.execute("INSERT INTO promo_codes (code, label, auto, kind, value, valid_to, exclude_peak, active, created_at) VALUES ('OLDCODE', 'Expired', 0, 'percent', 20, ?, 1, 1, 'now')", (D(-3),))
    cn.execute("INSERT INTO promo_codes (code, label, auto, kind, value, max_uses, exclude_peak, active, created_at) VALUES ('ONCE', 'One use', 0, 'flat', 100, 1, 1, 1, 'now')")
    cn.execute("INSERT INTO promo_codes (code, label, auto, kind, value, min_rooms, exclude_peak, active, created_at) VALUES ('GROUP3', 'Group rate', 0, 'percent', 15, 3, 1, 1, 'now')")
    cn.close()
    return before, after, ids


def png(color, size=(640, 400)):
    try:
        from PIL import Image
        import io
        b = io.BytesIO()
        Image.new("RGB", size, color).save(b, "PNG")
        return b.getvalue()
    except ImportError:
        import qr
        return qr.qr_png(qr.qr_matrix("photo " + str(color)), 6, 2)


def seed_via_api(adm):
    cfg = {"rooms_phone": "+919440000101", "whatsapp_number": "+919440000102", "support_email": "help@example.org", "emergency_contacts": "Police: 100\nAmbulance: 108", "medical_contacts": "Hospital: 08512-000000",
           "official_site_url": "https://rooms.example.org", "cancellation_policy": "Cancel up to 24 hours before check-in.", "refund_policy": "Half of the amount paid is refunded.", "id_rules": "Carry an original photo ID.",
           "dress_code": "Traditional dress in the temple.", "stay_rules": "Silence after 10 pm.", "packing_guide": "Light cotton clothes.", "map_url": "https://www.openstreetmap.org/#map=15/15.95/77.43",
           "map_embed_url": "https://www.openstreetmap.org/export/embed.html?bbox=77.4,15.9,77.5,16.0", "distance_station": "About 15 km", "distance_busstand": "2 km", "directions": "Take the Mantralayam road from Raichur.",
           "transport_tips": "Autos are available at the bus stand.", "darshan_timings": "Please ask at the Mutt office.", "pooja_timings": "Please ask at the Mutt office.", "seva_timings": "Please ask at the Mutt office.",
           "annadanam_timings": "Please ask at the Mutt office.", "place_tungabhadra": "The river ghat near the Mutt.", "place_panchamukhi": "About 15 km away.", "privacy_policy": "We keep only what a booking needs.", "privacy_contact": "help@example.org"}
    for k, v in cfg.items():
        st, r = adm.post("/api/admin/site_config", {"key": k, "lang": "en", "value": v})
        assert st == 200, (k, r)
    st, r = adm.post("/api/admin/faqs", {"position": 1, "texts": {"en": {"question": "Is breakfast included?", "answer": "No, meals are at the Annadanam hall."}, "te": {"question": "అల్పాహారం ఉందా?", "answer": "లేదు, భోజనం అన్నదాన మందిరంలో."}}})
    assert st == 200, r
    for i, tid in enumerate((1, 2, 3, 4)):
        st, r = adm.req("POST", f"/api/admin/room_types/{tid}/photo", raw=png((60 * i + 40, 90, 140 - 20 * i)), headers={"Content-Type": "image/png", "X-CSRF-Token": adm.csrf})[:2]
        assert st == 200, (tid, r)
    sev = adm.post("/api/admin/sevas", dict(name="Abhisheka", description="Morning seva", price=500, max_per_day=4))[1]["id"]
    sev2 = adm.post("/api/admin/sevas", dict(name="Archana", price=100))[1]["id"]
    pk = adm.post("/api/admin/packages", dict(name="Room + Seva", description="Stay with the morning seva", discount_percent=10, min_nights=2, items=[{"seva_id": sev, "day_offset": 0, "per_guest": True}]))[1]["id"]
    adm.post("/api/admin/festivals", dict(name="Aradhane", kind="festival", start_date=D(80), end_date=D(82), note="Annual festival", make_peak=True, surcharge_percent=30))
    adm.post("/api/admin/panchanga", dict(day=D(80), tithi="Shukla Dashami", nakshatra="Rohini", note=""))
    return sev, sev2, pk


# ---------- independent price calculation ----------
def money(x):
    return Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def expected_quote(tariff, nights_prices, rooms, beds=0, kids=0, discount_pct=0, tax=12, advance=25, bed_price=200, kid_price=100, seva=0):
    """The documented rules, written out separately from the app: room charge = nightly prices x rooms; extras per night; the discount is on the room charge only; tax on the rest; sevas after tax."""
    n = len(nights_prices)
    room = money(sum(Decimal(str(p)) for p in nights_prices) * rooms)
    disc = money(room * Decimal(discount_pct) / 100)
    extras = money(bed_price * beds * n) + money(kid_price * kids * n)
    taxable = room + extras - disc
    t = money(taxable * Decimal(tax) / 100)
    total = taxable + t + Decimal(str(seva))
    return {"room": float(room), "discount": float(disc), "tax": float(t), "total": float(total), "advance": float(money(total * Decimal(advance) / 100))}


# ---------- the browser ----------
try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except ImportError:
    HAVE_PW = False
try:
    import cv2
    import numpy as np
    HAVE_CV = True
except ImportError:
    HAVE_CV = False
try:
    import fitz
    HAVE_PDF = True
except ImportError:
    try:
        import pymupdf as fitz
        HAVE_PDF = True
    except ImportError:
        HAVE_PDF = False


def decode_qr(png_bytes):
    img = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
    return cv2.QRCodeDetector().detectAndDecode(img)[0]


def pdf_text(data):
    doc = fitz.open(stream=data, filetype="pdf")
    return "\n".join(p.get_text() for p in doc)


def write_report(extra_notes=""):
    counts = {k: sum(1 for r in RESULTS if r[3] == k) for k in ("PASS", "FAIL", "BLOCKED")}
    lines = ["# Test report", "", f"Generated by `python tools/audit_live.py` on {date.today().isoformat()}. Every row below was produced by running the check against the live app (mock providers, seeded copy of the database).",
             "", f"**{counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['BLOCKED']} BLOCKED** ({len(RESULTS)} checks)", "", "| Area | Feature | How tested | Result | Notes |", "|---|---|---|---|---|"]
    for a, f, h, r, n in RESULTS:
        lines.append("| " + " | ".join(x.replace("|", "/").replace("\n", " ") for x in (a, f, h, r, n)) + " |")
    (ROOT / "TEST_REPORT_LIVE.md").write_text("\n".join(lines) + "\n" + extra_notes, encoding="utf-8")
    return counts
