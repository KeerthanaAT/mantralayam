"""Admin reports as plain tables: {"title", "columns", "rows"}. The same table feeds the page, the CSV download and the Google Sheets export.

Reports: arrivals, departures, occupancy (daily arrivals, departures and occupancy), revenue_by_day, revenue_by_property, revenue_by_method, reconciliation (revenue and payment
reconciliation), funnel, searches, soldout, payments_funnel (analytics), feedback.
Parameters (all optional): date (arrivals, departures), from / to (the others), property_ids (already limited to the user's properties by the caller).
Money is in rupees. CSV cells that could be read as a spreadsheet formula are prefixed with an apostrophe.
"""
import csv
import io
from collections import defaultdict
from datetime import date, timedelta

import analytics
import providers

g = None  # the guest module, bound by mantralayam_rooms
MAX_DAYS = 92
LIVE = ("Confirmed", "Checked-In", "Checked-Out")


def marks(ids):
    return ",".join("?" * len(ids)) or "NULL"


def span(p, default_from, default_to, limit=366):
    a, z = p.get("from") or default_from.isoformat(), p.get("to") or default_to.isoformat()
    try:
        a, z = date.fromisoformat(a), date.fromisoformat(z)
    except ValueError:
        raise g.UserError("Dates must look like 2026-12-31.")
    if z < a:
        raise g.UserError("The end date must not be before the start date.")
    if (z - a).days > limit:
        raise g.UserError(f"Please choose a range of at most {limit + 1} days.")
    return a, z


def one_day(p):
    try:
        return date.fromisoformat(p.get("date") or g.today().isoformat())
    except ValueError:
        raise g.UserError("Date must look like 2026-12-31.")


def table(title, columns, rows):
    return {"title": title, "columns": columns, "rows": rows}


# ---------- daily arrivals, departures, occupancy ----------
def _stay_rows(conn, ids, where, d):
    return conn.execute(
        f"SELECT b.id, COALESCE(b.group_id, b.id) AS booking, r.number AS room, p.name AS property, b.guest_name, b.phone, b.guests, b.status, b.arrival_time, b.payment_status, b.amount, b.check_in, b.check_out "
        f"FROM bookings b JOIN rooms r ON r.id = b.room_id JOIN properties p ON p.id = b.property_id WHERE b.property_id IN ({marks(ids)}) AND {where} AND b.status IN ('Confirmed', 'Checked-In', 'Checked-Out') "
        "ORDER BY p.name, r.number", (*ids, d)).fetchall()


def arrivals(conn, ids, p):
    d = one_day(p).isoformat()
    rows = _stay_rows(conn, ids, "b.check_in = ?", d)
    return table(f"Arrivals on {d}", ["Booking", "Room", "Property", "Guest", "Mobile", "Guests", "Status", "Expected arrival", "Payment", "Amount (Rs)", "Check-out"],
                 [[r["booking"], r["room"], r["property"], r["guest_name"], r["phone"], r["guests"], r["status"], r["arrival_time"] or "", r["payment_status"], r["amount"] or 0, r["check_out"]] for r in rows])


def departures(conn, ids, p):
    d = one_day(p).isoformat()
    rows = _stay_rows(conn, ids, "b.check_out = ?", d)
    return table(f"Departures on {d}", ["Booking", "Room", "Property", "Guest", "Mobile", "Guests", "Status", "Payment", "Amount (Rs)", "Check-in"],
                 [[r["booking"], r["room"], r["property"], r["guest_name"], r["phone"], r["guests"], r["status"], r["payment_status"], r["amount"] or 0, r["check_in"]] for r in rows])


def occupancy(conn, ids, p):
    """Per property and day: rooms, rooms blocked (blocked, maintenance, out of service), rooms occupied (a live stay covers the night), arrivals, departures and occupancy %
    of the rooms that could be sold (rooms minus blocked)."""
    a, z = span(p, g.today(), g.today() + timedelta(days=13), MAX_DAYS)
    props = conn.execute(f"SELECT id, name FROM properties WHERE id IN ({marks(ids)}) ORDER BY id", ids).fetchall()
    rows, d = [], a
    while d <= z:
        iso = d.isoformat()
        for pr in props:
            total = conn.execute("SELECT COUNT(*) FROM rooms WHERE property_id = ? AND active = 1", (pr["id"],)).fetchone()[0]
            blocked = conn.execute("SELECT COUNT(DISTINCT e.room_id) FROM room_status_events e JOIN rooms r ON r.id = e.room_id WHERE r.property_id = ? AND r.active = 1 AND e.active = 1 "
                                   "AND e.kind IN ('internal_block', 'maintenance', 'out_of_service') AND e.start_date <= ? AND (e.end_date IS NULL OR e.end_date > ?)", (pr["id"], iso, iso)).fetchone()[0]
            occ = conn.execute("SELECT COUNT(DISTINCT room_id) FROM bookings WHERE property_id = ? AND status IN ('Confirmed', 'Checked-In', 'Checked-Out') AND check_in <= ? AND check_out > ?", (pr["id"], iso, iso)).fetchone()[0]
            arr = conn.execute("SELECT COUNT(*) FROM bookings WHERE property_id = ? AND status IN ('Confirmed', 'Checked-In', 'Checked-Out') AND check_in = ?", (pr["id"], iso)).fetchone()[0]
            dep = conn.execute("SELECT COUNT(*) FROM bookings WHERE property_id = ? AND status IN ('Confirmed', 'Checked-In', 'Checked-Out') AND check_out = ?", (pr["id"], iso)).fetchone()[0]
            sellable = max(total - blocked, 0)
            rows.append([iso, pr["name"], total, blocked, occ, arr, dep, round(100 * occ / sellable, 1) if sellable else 0.0])
        d += timedelta(days=1)
    return table(f"Occupancy {a} to {z}", ["Date", "Property", "Rooms", "Blocked", "Occupied", "Arrivals", "Departures", "Occupancy %"], rows)


# ---------- revenue and reconciliation ----------
def _money_rows(conn, ids, a, z):
    """Paid payments and refunds in [a, z] (by the day the money moved) with the property of their booking."""
    own = "COALESCE((SELECT MIN(property_id) FROM bookings b WHERE COALESCE(b.group_id, b.id) = {0}.group_id), {1}.property_id)"
    pays = [r for r in conn.execute(f"SELECT p.*, {own.format('p', 'p')} AS pid FROM payments p WHERE p.status IN ('paid', 'unfulfilled') AND p.paid_at IS NOT NULL AND substr(p.paid_at, 1, 10) >= ? AND substr(p.paid_at, 1, 10) <= ?", (a.isoformat(), z.isoformat())) if r["pid"] in ids]
    # refunds too, including those of payments that never became a booking (the room was gone): that money went back out
    refs = [r for r in conn.execute(f"SELECT r.*, {own.format('r', 'pp')} AS pid FROM refunds r JOIN payments pp ON pp.id = r.payment_row WHERE r.status IN ('pending', 'processed', 'requested', 'sending') "
                                    "AND substr(r.updated_at, 1, 10) >= ? AND substr(r.updated_at, 1, 10) <= ?", (a.isoformat(), z.isoformat())) if r["pid"] in ids]
    return pays, refs


def revenue_by_day(conn, ids, p):
    """Booked value (live bookings by check-in date) beside money received (by the day it was paid) and refunded. Differences are what reconciliation explains."""
    a, z = span(p, g.today() - timedelta(days=29), g.today())
    days = defaultdict(lambda: defaultdict(float))
    for r in conn.execute(f"SELECT check_in, COUNT(DISTINCT COALESCE(group_id, id)) AS n, SUM(COALESCE(amount, 0)) AS v FROM bookings WHERE property_id IN ({marks(ids)}) AND status IN ('Confirmed', 'Checked-In', 'Checked-Out') "
                          "AND check_in >= ? AND check_in <= ? GROUP BY check_in", (*ids, a.isoformat(), z.isoformat())):
        days[r["check_in"]]["bookings"], days[r["check_in"]]["value"] = r["n"], r["v"]
    pays, refs = _money_rows(conn, ids, a, z)
    for r in pays:
        days[r["paid_at"][:10]]["counter" if r["provider"] == "counter" else "online"] += r["amount_paise"] / 100
    for r in refs:
        days[r["updated_at"][:10]]["refunded"] += r["amount_paise"] / 100
    rows = [[k, int(v["bookings"]), round(v["value"], 2), round(v["online"], 2), round(v["counter"], 2), round(v["refunded"], 2), round(v["online"] + v["counter"] - v["refunded"], 2)] for k, v in sorted(days.items())]
    if rows:
        rows.append(["Total", *[round(sum(r[i] for r in rows), 2) if i > 1 else sum(r[i] for r in rows) for i in range(1, 7)]])
    return table(f"Revenue {a} to {z}", ["Date", "Bookings (by check-in)", "Booked value (Rs)", "Received online (Rs)", "Received at counter (Rs)", "Refunded (Rs)", "Net received (Rs)"], rows)


def revenue_by_property(conn, ids, p):
    a, z = span(p, g.today() - timedelta(days=29), g.today())
    names = {r["id"]: r["name"] for r in conn.execute(f"SELECT id, name FROM properties WHERE id IN ({marks(ids)})", ids)}
    agg = defaultdict(lambda: defaultdict(float))
    for r in conn.execute(f"SELECT property_id, COUNT(DISTINCT COALESCE(group_id, id)) AS n, SUM(COALESCE(amount, 0)) AS v FROM bookings WHERE property_id IN ({marks(ids)}) AND status IN ('Confirmed', 'Checked-In', 'Checked-Out') "
                          "AND check_in >= ? AND check_in <= ? GROUP BY property_id", (*ids, a.isoformat(), z.isoformat())):
        agg[r["property_id"]]["bookings"], agg[r["property_id"]]["value"] = r["n"], r["v"]
    pays, refs = _money_rows(conn, ids, a, z)
    for r in pays:
        agg[r["pid"]]["received"] += r["amount_paise"] / 100
    for r in refs:
        agg[r["pid"]]["refunded"] += r["amount_paise"] / 100
    return table(f"Revenue by property {a} to {z}", ["Property", "Bookings", "Booked value (Rs)", "Received (Rs)", "Refunded (Rs)", "Net received (Rs)"],
                 [[names[k], int(v["bookings"]), round(v["value"], 2), round(v["received"], 2), round(v["refunded"], 2), round(v["received"] - v["refunded"], 2)] for k, v in sorted(agg.items())])


def revenue_by_method(conn, ids, p):
    a, z = span(p, g.today() - timedelta(days=29), g.today())
    pays, refs = _money_rows(conn, ids, a, z)
    agg = defaultdict(lambda: [0, 0.0])
    for r in pays:
        k = ("Counter: " if r["provider"] == "counter" else "Online: ") + (r["method"] or "unknown")
        agg[k][0] += 1
        agg[k][1] += r["amount_paise"] / 100
    rows = [[k, v[0], round(v[1], 2)] for k, v in sorted(agg.items())]
    rows.append(["Refunds", len(refs), round(-sum(r["amount_paise"] for r in refs) / 100, 2)])
    return table(f"Money by method {a} to {z}", ["Method", "Payments", "Amount (Rs)"], rows)


FLAGS = {"unpaid": "Balance due", "overpaid": "Overpaid", "refund_open": "Refund in progress", "refund_manual": "Refund needs a person", "mismatch": "Money held after cancelling"}


def reconciliation(conn, ids, p):
    a, z = span(p, g.today() - timedelta(days=7), g.today() + timedelta(days=30))
    out = g.payments.reconcile(conn, ids, a.isoformat(), z.isoformat())
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM properties")}
    return table(f"Reconciliation, check-ins {a} to {z}", ["Booking", "Property", "Guest", "Mobile", "Check-in", "Check-out", "Status", "Total (Rs)", "Paid (Rs)", "Refunded (Rs)", "Refund in progress (Rs)", "Balance (Rs)", "Flags"],
                 [[r["booking_id"], names.get(r["property_id"], ""), r["guest"], r["phone"], r["check_in"], r["check_out"], r["status"], r["total"], r["paid"], r["refunded"], r["refund_in_progress"], r["balance"],
                   ", ".join(FLAGS[f] for f in r["flags"])] for r in out["rows"]])


# ---------- analytics ----------
def funnel(conn, ids, p):
    a, z = span(p, g.today() - timedelta(days=29), g.today())
    return table(f"Booking funnel {a} to {z}", ["Step", "Count", "% of the step before", "Dropped here"], [[s["label"], s["count"], s["of_previous"] if s["of_previous"] is not None else "", s["dropped"]] for s in analytics.funnel(conn, a.isoformat(), z.isoformat())])


def searches(conn, ids, p):
    """What people searched for: check-in dates, nights and party sizes (counts only)."""
    a, z = span(p, g.today() - timedelta(days=29), g.today())
    rows = []
    for kind, label in (("search_dates", "Check-in date"), ("search_nights", "Nights"), ("search_guests", "Guests")):
        items = sorted(analytics.totals(conn, a.isoformat(), z.isoformat(), kind), key=lambda r: (-r["n"], r["key"]))[:15]
        rows += [[label, r["key"], r["n"]] for r in items]
    return table(f"Searches {a} to {z}", ["What", "Value", "Searches"], rows)


def soldout(conn, ids, p):
    """Dates (the next 60 nights) with no room free, and how many searches met a sold-out night on each date (demand turned away)."""
    a, z = span(p, g.today(), g.today() + timedelta(days=59), 120)
    demand = {r["key"]: r["n"] for r in analytics.totals(conn, (g.today() - timedelta(days=90)).isoformat(), g.today().isoformat(), "soldout_date")}
    total_rooms = conn.execute(f"SELECT COUNT(*) FROM rooms WHERE active = 1 AND property_id IN ({marks(ids)})", ids).fetchone()[0]
    rows, d = [], a
    while d <= z:
        free = sum(1 for r in g.free_rooms(conn, d, d + timedelta(days=1)) if r["property_id"] in ids)
        if free == 0 or demand.get(d.isoformat()):
            rows.append([d.isoformat(), total_rooms, free, "Sold out" if free == 0 else "", demand.get(d.isoformat(), 0)])
        d += timedelta(days=1)
    return table(f"Sold-out dates {a} to {z}", ["Night", "Rooms", "Free", "Status", "Searches that met a sold-out night (last 90 days)"], rows)


def payments_funnel(conn, ids, p):
    a, z = span(p, g.today() - timedelta(days=29), g.today())
    by = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM payments WHERE provider != 'counter' AND substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ? GROUP BY status", (a.isoformat(), z.isoformat()))}
    started = sum(by.values())
    names = {"paid": "Paid and booked", "abandoned": "Closed the payment window", "expired": "Ran out of time", "created": "Still open", "unfulfilled": "Paid but rooms gone (refunded)"}
    return table(f"Online payments {a} to {z}", ["Outcome", "Count", "% of started"], [["Started", started, 100.0 if started else 0.0]] + [[names[k], by.get(k, 0), round(100 * by.get(k, 0) / started, 1) if started else 0.0] for k in names])


def seva_schedule(conn, ids, p):
    """Every seva booked for the period, by date: who, how many, and the booking they belong to (for the priests' and the desk's list)."""
    a, z = span(p, g.today(), g.today() + timedelta(days=6), 92)
    rows = conn.execute("SELECT sb.seva_date, s.name AS seva, sb.qty, sb.group_id, (SELECT MIN(guest_name) FROM bookings b WHERE COALESCE(b.group_id, b.id) = sb.group_id) AS guest, "
                        "(SELECT MIN(phone) FROM bookings b WHERE COALESCE(b.group_id, b.id) = sb.group_id) AS phone, pk.name AS package, (SELECT MIN(property_id) FROM bookings b WHERE COALESCE(b.group_id, b.id) = sb.group_id) AS pid "
                        "FROM seva_bookings sb JOIN sevas s ON s.id = sb.seva_id LEFT JOIN packages pk ON pk.id = sb.package_id WHERE sb.status = 'Confirmed' AND sb.seva_date >= ? AND sb.seva_date <= ? ORDER BY sb.seva_date, s.name, sb.id",
                        (a.isoformat(), z.isoformat())).fetchall()
    return table(f"Sevas {a} to {z}", ["Date", "Seva", "Places", "Booking", "Guest", "Mobile", "Package"], [[r["seva_date"], r["seva"], r["qty"], r["group_id"], r["guest"], r["phone"], r["package"] or ""] for r in rows if r["pid"] in ids])


def feedback_report(conn, ids, p):
    a, z = span(p, g.today() - timedelta(days=89), g.today())
    rows = conn.execute(f"SELECT f.created_at, pr.name AS property, f.group_id, f.rating, f.cleanliness, f.service, f.comment, f.status FROM feedback f JOIN properties pr ON pr.id = f.property_id "
                        f"WHERE f.property_id IN ({marks(ids)}) AND substr(f.created_at, 1, 10) >= ? AND substr(f.created_at, 1, 10) <= ? ORDER BY f.id DESC", (*ids, a.isoformat(), z.isoformat())).fetchall()
    return table(f"Feedback {a} to {z}", ["Date", "Property", "Booking", "Rating", "Cleanliness", "Service", "Comment", "Status"], [[r["created_at"][:10], r["property"], r["group_id"], r["rating"], r["cleanliness"] or "", r["service"] or "", r["comment"] or "", r["status"]] for r in rows])


REPORTS = {"seva_schedule": (seva_schedule, "Seva schedule"), "feedback": (feedback_report, "Guest feedback"), "arrivals": (arrivals, "Arrivals (one day)"), "departures": (departures, "Departures (one day)"), "occupancy": (occupancy, "Occupancy by day"),
           "revenue_by_day": (revenue_by_day, "Revenue by day"), "revenue_by_property": (revenue_by_property, "Revenue by property"), "revenue_by_method": (revenue_by_method, "Money by payment method"),
           "reconciliation": (reconciliation, "Payment reconciliation"), "funnel": (funnel, "Booking funnel"), "searches": (searches, "Searches"), "soldout": (soldout, "Sold-out dates"),
           "payments_funnel": (payments_funnel, "Online payment outcomes")}


def build(conn, ids, name, params):
    if name not in REPORTS:
        raise g.UserError("Please choose a valid report.")
    return REPORTS[name][0](conn, ids, params)


# ---------- CSV and Sheets ----------
def safe_cell(v):
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def to_csv(t):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow([safe_cell(c) for c in t["columns"]])
    for r in t["rows"]:
        w.writerow([safe_cell(c) for c in r])
    return ("﻿" + out.getvalue()).encode("utf-8")  # a BOM so Excel reads Indian-language names correctly


def export_sheets(t, name):
    """Writes the table to its own tab of the configured spreadsheet. Returns {"exported": "sheets", "url": ...}, or {"exported": "csv", "message": ...} when Google Sheets is not
    configured or the call failed: the page then downloads the CSV instead."""
    try:
        sh = providers.sheets()
    except providers.ProviderError as e:
        return {"exported": "csv", "message": f"Google Sheets is not set up correctly ({e}). The CSV was downloaded instead."}
    if not sh:
        return {"exported": "csv", "message": "Google Sheets is not configured (GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEETS_ID), so the CSV was downloaded instead."}
    try:
        url = sh.write(name, [[safe_cell(c) for c in t["columns"]]] + [[safe_cell(c) for c in r] for r in t["rows"]])
    except providers.ProviderError as e:
        return {"exported": "csv", "message": f"Google Sheets did not accept the export ({e}). The CSV was downloaded instead."}
    return {"exported": "sheets", "url": url}
