"""First-party, privacy-respecting analytics: daily counters only.

Nothing identifies a visitor: no IP address, cookie, visit id, user agent, phone number or name is stored, and nothing is sent to a third party. The guest site and API
add one to a counter (day, kind, key) at a few points; the funnel is the ratio between those counters, so it needs no tracking of individuals. Counters older than
RETENTION_DAYS are deleted by the periodic job. Recording never raises into the guest's request.
Kinds: search, search_ok, search_soldout, search_dates (key = check-in date), search_nights, search_guests, soldout_date (key = a night that was sold out when searched),
calendar, hold, submit, payment_start, booked (key = counter or online).
"""
import sys
import traceback
from datetime import timedelta

g = None  # the guest module, bound by mantralayam_rooms
RETENTION_DAYS = 400
FUNNEL = (("search", "Searched for rooms"), ("search_ok", "Saw rooms for their dates"), ("hold", "Chose a room (opened the booking form)"),
          ("submit", "Submitted the booking form"), ("booked", "Booking confirmed"))


def record(conn, kind, key="", n=1):
    conn.execute("INSERT INTO analytics_daily (day, kind, key, n) VALUES (?,?,?,?) ON CONFLICT(day, kind, key) DO UPDATE SET n = n + excluded.n", (g.today().isoformat(), kind, str(key)[:40], n))


def safe(conn, kind, key="", n=1):
    try:
        record(conn, kind, key, n)
    except Exception:
        print("analytics: could not record", kind, file=sys.stderr)
        traceback.print_exc()


def totals(conn, start, end, kind=None):
    q = "SELECT kind, key, SUM(n) AS n FROM analytics_daily WHERE day >= ? AND day <= ?" + (" AND kind = ?" if kind else "") + " GROUP BY kind, key"
    return conn.execute(q, (start, end) + ((kind,) if kind else ())).fetchall()


def funnel(conn, start, end):
    by = {}
    for r in totals(conn, start, end):
        by[r["kind"]] = by.get(r["kind"], 0) + r["n"]
    out, prev = [], None
    for k, label in FUNNEL:
        n = by.get(k, 0)
        out.append({"stage": k, "label": label, "count": n, "of_previous": round(100 * n / prev, 1) if prev else None, "dropped": (prev - n) if prev is not None and prev >= n else 0})
        prev = n
    return out


def purge(conn):
    cut = (g.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    return g.write(conn, lambda: conn.execute("DELETE FROM analytics_daily WHERE day < ?", (cut,)).rowcount)
