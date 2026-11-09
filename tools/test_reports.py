"""Self-check for the admin reports (daily arrivals, departures, occupancy; revenue and reconciliation), CSV and Google Sheets export with its CSV fallback, and the
privacy-respecting analytics (searches, funnel, sold-out dates). Runs on a COPY of the real database. Run: python tools/test_reports.py   (or pytest)
"""
import csv
import io
import os
import shutil
import sys
from datetime import timedelta

import test_staff_desk as td  # its harness: a copy of the database, logins, helpers to make online bookings
import mantralayam_rooms as g
import providers
import reports

D, call = td.D, td.call
SENSITIVE_WORDS = ("127.0.0.1", "5.5.5.5", "Test", "98765")


def data(name, user="manager", **q):
    return call(user, "GET", "/api/reports/data", {"report": name} | q)


def rows_of(name, **q):
    st, t = data(name, **q)
    assert st == 200, t
    return t["rows"]


def test_permissions_and_validation():
    for user in ("frontdesk", "housekeeping", "viewer"):
        assert call(user, "GET", "/api/reports/data", {"report": "arrivals"})[0] == 403 and call(user, "GET", "/api/reports/csv", {"report": "arrivals"})[0] == 403
        assert call(user, "POST", "/api/reports/export", {"report": "arrivals"})[0] == 403
    assert call("manager", "GET", "/api/reports/list")[0] == 200 and {r["key"] for r in call("manager", "GET", "/api/reports/list")[1]} == set(reports.REPORTS)
    assert data("bogus")[0] == 400 and data("arrivals", date="x")[0] == 400 and data("occupancy", **{"from": D(5), "to": D(1)})[0] == 400 and data("occupancy", **{"from": D(0), "to": D(200)})[0] == 400
    assert data("arrivals", property_id=3)[0] == 403
    assert {"report.view", "report.export"} <= set(call("manager", "GET", "/api/meta")[1]["permissions"]) and "report.view" not in call("frontdesk", "GET", "/api/meta")[1]["permissions"]


def test_arrivals_departures_occupancy():
    td.settings_(pay_options="counter")
    b = td.online(start=70, nights=2)
    arr = rows_of("arrivals", date=D(70))
    row = next(r for r in arr if r[0] == b["booking_id"])
    assert row[3] == "Online Guest" and row[6] == "Confirmed" and row[10] == D(72) and row[9] == b["amount"]
    assert not any(r[0] == b["booking_id"] for r in rows_of("arrivals", date=D(71)))
    dep = rows_of("departures", date=D(72))
    assert any(r[0] == b["booking_id"] and r[9] == D(70) for r in dep) and not any(r[0] == b["booking_id"] for r in rows_of("departures", date=D(70)))
    c = td.db()
    c.execute("UPDATE bookings SET status = 'Cancelled' WHERE group_id = ?", (b["booking_id"],))
    assert not any(r[0] == b["booking_id"] for r in rows_of("arrivals", date=D(70))), "a cancelled booking is not an arrival"
    c.execute("UPDATE bookings SET status = 'Confirmed' WHERE group_id = ?", (b["booking_id"],))
    occ = rows_of("occupancy", **{"from": D(69), "to": D(73)})
    pid = c.execute("SELECT property_id FROM bookings WHERE group_id = ?", (b["booking_id"],)).fetchone()[0]
    name = c.execute("SELECT name FROM properties WHERE id = ?", (pid,)).fetchone()[0]
    mine = {r[0]: r for r in occ if r[1] == name}
    assert set(mine) == {D(69), D(70), D(71), D(72), D(73)}
    assert mine[D(70)][4] == mine[D(69)][4] + 1 and mine[D(71)][4] == mine[D(70)][4] and mine[D(72)][4] == mine[D(71)][4] - 1, "occupied nights are D70 and D71"
    assert mine[D(70)][5] == mine[D(69)][5] + 1 and mine[D(72)][6] == mine[D(71)][6] + 1, "arrival on D70, departure on D72"
    r = mine[D(70)]
    assert r[7] == round(100 * r[4] / (r[2] - r[3]), 1), "occupancy % is of the rooms that could be sold"
    # a blocked room is out of the denominator
    room = c.execute("SELECT id FROM rooms WHERE property_id = ? AND active = 1 AND id NOT IN (SELECT room_id FROM bookings WHERE status IN ('Confirmed', 'Checked-In') AND check_in < ? AND check_out > ?) AND id NOT IN (SELECT room_id FROM room_status_events WHERE active = 1) LIMIT 1", (pid, D(71), D(70))).fetchone()[0]
    c.execute("INSERT INTO room_status_events (room_id, kind, reason, start_date, end_date, active, created_at) VALUES (?, 'maintenance', 'test', ?, ?, 1, 'now')", (room, D(70), D(72)))
    after = {r[0]: r for r in rows_of("occupancy", **{"from": D(70), "to": D(70)}) if r[1] == name}[D(70)]
    assert after[3] == r[3] + 1 and after[7] == round(100 * after[4] / (after[2] - after[3]), 1)
    assert call("frontdesk", "GET", "/api/reports/data", {"report": "occupancy"})[0] == 403
    c.execute("DELETE FROM room_status_events WHERE reason = 'test'")
    c.close()


def test_revenue_and_reconciliation():
    os.environ["PAYMENT_PROVIDER"] = "mock"
    try:
        td.settings_(pay_options="full,advance,counter", advance_percent=25, refund_percent=50)
        full = td.online("pay_full", start=75, nights=2)  # 1200 online
        adv = td.online("pay_advance", start=76, nights=1)  # 150 of 600
        cnt = td.online(start=77, nights=1)  # nothing paid
        call("manager", "POST", f"/api/bookings/{td.db().execute('SELECT id FROM bookings WHERE group_id = ?', (adv['booking_id'],)).fetchone()[0]}/payment", {"amount": 450, "mode": "cash"})
        call("manager", "POST", f"/api/bookings/{td.db().execute('SELECT id FROM bookings WHERE group_id = ?', (full['booking_id'],)).fetchone()[0]}/cancel", {"reason": "x"})  # refund 600
        today = D(0)
        by_day = rows_of("revenue_by_day", **{"from": D(0), "to": D(0)})
        total = by_day[-1]
        assert total[0] == "Total" and total[3] >= 1350 and total[4] >= 450 and total[5] >= 600 and total[6] == round(total[3] + total[4] - total[5], 2), total
        booked = {r[0]: r for r in rows_of("revenue_by_day", **{"from": D(75), "to": D(77)})}
        assert booked[D(76)][2] >= 600 and booked[D(77)][2] >= 600 and D(75) not in booked or booked[D(75)][2] == 0, "a cancelled booking is not booked value"
        by_method = {r[0]: r for r in rows_of("revenue_by_method", **{"from": today, "to": today})}
        assert by_method["Counter: cash"][2] >= 450 and by_method["Refunds"][2] <= -600 and any(k.startswith("Online:") for k in by_method)
        props = rows_of("revenue_by_property", **{"from": today, "to": today})
        assert props and all(len(r) == 6 for r in props)
        rec = {r[0]: r for r in rows_of("reconciliation", **{"from": D(70), "to": D(80)})}
        assert rec[adv["booking_id"]][8] == 600.0 and rec[adv["booking_id"]][11] == 0.0 and rec[adv["booking_id"]][12] == "", "advance + counter cash = fully paid"
        assert "Money held after cancelling" in rec[full["booking_id"]][12] and rec[full["booking_id"]][9] == 600.0, rec[full["booking_id"]]
        assert cnt["booking_id"] not in rec or rec[cnt["booking_id"]][12] == "Balance due"
        pf = {r[0]: r for r in rows_of("payments_funnel", **{"from": today, "to": today})}
        assert pf["Started"][1] >= 2 and pf["Paid and booked"][1] >= 2
    finally:
        os.environ.pop("PAYMENT_PROVIDER", None)
        td.settings_(pay_options="counter")
        c = td.db()
        c.execute("DELETE FROM settings WHERE key IN ('advance_percent', 'refund_percent')")
        c.close()


def test_revenue_counts_money_that_came_in_and_went_back_for_a_lost_room():
    """Regression (found by the live audit): a payment that arrived after the room was gone has no booking, so the report left out both the money in and its refund."""
    c = td.db()
    c.execute("DELETE FROM payments WHERE order_id = 'order_ghost'")
    now = g.now_iso()
    pid = c.execute("INSERT INTO payments (order_id, payment_id, provider, kind, amount_paise, total_paise, status, method, created_at, paid_at, expires_at, property_id) VALUES ('order_ghost', 'pay_ghost', 'mock', 'full', 50000, 50000, 'unfulfilled', 'upi', ?, ?, ?, 1)", (now, now, now)).lastrowid
    c.execute("INSERT INTO refunds (payment_row, group_id, amount_paise, status, reason, dedupe_key, created_at, updated_at) VALUES (?, NULL, 50000, 'processed', 'Rooms no longer available', 'unfulfilled:ghost', ?, ?)", (pid, now, now))
    before_total = rows_of("revenue_by_day", **{"from": D(0), "to": D(0)})[-1]
    by = {r[0]: r for r in rows_of("revenue_by_method", **{"from": D(0), "to": D(0)})}
    assert by["Online: upi"][2] >= 500 and by["Refunds"][2] <= -500
    assert before_total[3] >= 500 and before_total[5] >= 500 and abs(before_total[6] - (before_total[3] + before_total[4] - before_total[5])) < 0.01
    only2 = call("manager", "GET", "/api/reports/data", {"report": "revenue_by_day", "from": D(0), "to": D(0), "property_id": 2})[1]["rows"]
    assert not only2 or only2[-1][3] < before_total[3], "the lost-room payment is attributed to the property it was for (1), not to others"
    c.execute("DELETE FROM refunds WHERE dedupe_key = 'unfulfilled:ghost'")
    c.execute("DELETE FROM payments WHERE order_id = 'order_ghost'")
    c.close()


def test_csv_download_is_safe_and_logged():
    b = td.online(start=80, nights=1, guest_name="=HYPERLINK(\"http://x\")", )
    st, out = call("manager", "GET", "/api/reports/csv", {"report": "arrivals", "date": D(80)})
    assert st == 200 and out["_ctype"] == "text/csv" and out["_filename"] == f"arrivals-{D(0)}.csv"
    text = out["_bytes"].decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][0] == "Booking" and any(r[3] == "'=HYPERLINK(\"http://x\")" for r in rows[1:]), "a formula in a guest name is defused"
    assert out["_bytes"].startswith(b"\xef\xbb\xbf")
    assert reports.safe_cell("=1+1") == "'=1+1" and reports.safe_cell("@x") == "'@x" and reports.safe_cell("-5") == "'-5" and reports.safe_cell(-5) == -5 and reports.safe_cell("ok") == "ok"
    log = td.db().execute("SELECT username, new_value FROM audit_log WHERE action = 'report.export' ORDER BY id DESC LIMIT 1").fetchone()
    assert log["username"] == "manager" and "arrivals as CSV" in log["new_value"]
    assert call("frontdesk", "GET", "/api/reports/csv", {"report": "arrivals"})[0] == 403


def test_sheets_export_and_fallback():
    for k in ("SHEETS_PROVIDER", "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SHEETS_ID"):
        os.environ.pop(k, None)
    td.online(start=81, nights=1)
    # not configured: the page is told to download the CSV
    st, out = call("manager", "POST", "/api/reports/export", {"report": "arrivals", "date": D(81), "target": "sheets"})
    assert st == 200 and out["exported"] == "csv" and "not configured" in out["message"]
    # mock provider: the table is written as header + rows, formulas defused
    os.environ["SHEETS_PROVIDER"] = "mock"
    providers.SHEETS_OUTBOX.clear()
    st, out = call("manager", "POST", "/api/reports/export", {"report": "arrivals", "date": D(81)})
    assert st == 200 and out["exported"] == "sheets" and out["url"].startswith("https://")
    sent = providers.SHEETS_OUTBOX[-1]
    assert sent["rows"][0][0] == "Booking" and len(sent["rows"]) >= 2 and sent["tab"].startswith("Arrivals")
    # a misconfigured real provider falls back too, and never echoes the key
    os.environ["SHEETS_PROVIDER"] = "google"
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = '{"client_email": "x@y.iam.gserviceaccount.com", "private_key": "-----BEGIN PRIVATE KEY-----SECRETKEY"}'
    os.environ["GOOGLE_SHEETS_ID"] = "short"
    out = call("manager", "POST", "/api/reports/export", {"report": "arrivals", "date": D(81)})[1]
    assert out["exported"] == "csv" and "SECRETKEY" not in str(out) and "spreadsheet id" in out["message"]
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = "not json and not a file"
    os.environ["GOOGLE_SHEETS_ID"] = "a" * 44
    out = call("manager", "POST", "/api/reports/export", {"report": "arrivals", "date": D(81)})[1]
    assert out["exported"] == "csv" and "service account key" in out["message"]
    # auto-detect: both variables set means Google, none means nothing
    os.environ.pop("SHEETS_PROVIDER")
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = '{"client_email": "x@y.iam.gserviceaccount.com", "private_key": "k"}'
    assert isinstance(providers.sheets(), providers.GoogleSheets)
    os.environ.pop("GOOGLE_SHEETS_ID")
    assert providers.sheets() is None
    os.environ["SHEETS_PROVIDER"] = "bogus"
    try:
        providers.sheets()
    except providers.ProviderError:
        pass
    else:
        raise AssertionError("unknown provider accepted")
    for k in ("SHEETS_PROVIDER", "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SHEETS_ID"):
        os.environ.pop(k, None)
    assert shutil.which("openssl") is None or len(providers._rs256(OPENSSL_KEY(), b"data")) in (128, 256, 512)


def OPENSSL_KEY():
    import subprocess
    return subprocess.run(["openssl", "genrsa", "2048"], capture_output=True, text=True).stdout


def test_analytics_counts_without_personal_data():
    c = td.db()
    c.execute("DELETE FROM analytics_daily")
    base = dict(check_in=D(85), check_out=D(87), guests=2)
    g.reset_throttle()
    st, r = g.dispatch("GET", "/api/public/search", base, None, "5.5.5.5")
    assert st == 200 and r["properties"]
    g.reset_throttle()
    g.dispatch("GET", "/api/public/search", base | {"guests": 3}, None, "6.6.6.6")
    b = td.online(start=85, nights=2)  # hold, submit, booked
    by = {(r["kind"], r["key"]): r["n"] for r in c.execute("SELECT kind, key, n FROM analytics_daily")}
    assert by[("search", "")] == 2 and by[("search_ok", "")] == 2 and by[("search_dates", D(85))] == 2 and by[("search_nights", "2")] == 2 and by[("search_guests", "2")] == 1 and by[("search_guests", "3")] == 1
    assert by[("hold", "")] == 1 and by[("submit", "")] == 1 and by[("booked", "counter")] == 1, by
    # nothing that identifies anyone is stored
    cols = {r[1] for r in c.execute("PRAGMA table_info(analytics_daily)")}
    assert cols == {"day", "kind", "key", "n"}
    dump = str([tuple(r) for r in c.execute("SELECT * FROM analytics_daily")])
    assert not any(w in dump for w in SENSITIVE_WORDS) and b["mobile"] not in dump and b["guest_name"] not in dump
    fun = rows_of("funnel", **{"from": D(0), "to": D(0)})
    assert [r[0] for r in fun][:2] == ["Searched for rooms", "Saw rooms for their dates"] and fun[0][1] == 2 and fun[2][1] == 1 and fun[4][1] == 1
    assert fun[2][2] == 50.0 and fun[2][3] == 1, "of two searches one went on to choose a room, one dropped"
    s = rows_of("searches", **{"from": D(0), "to": D(0)})
    assert ["Check-in date", D(85), 2] in s and ["Nights", "2", 2] in s
    # sold-out nights are remembered as demand, and listed with the inventory view
    rooms = [r["id"] for r in g.free_rooms(g.connect(), g.today() + timedelta(days=90), g.today() + timedelta(days=91))]
    g.connect().close()
    c.executemany("INSERT INTO room_status_events (room_id, kind, reason, start_date, end_date, active, created_at) VALUES (?, 'internal_block', 'soldout-test', ?, ?, 1, 'now')", [(x, D(60), D(63)) for x in [r[0] for r in c.execute("SELECT id FROM rooms WHERE active = 1")]])
    g.reset_throttle()
    st, r = g.dispatch("GET", "/api/public/search", dict(check_in=D(61), check_out=D(62), guests=2), None, "7.7.7.7")
    assert st == 200 and not r["properties"]
    by = {(r["kind"], r["key"]): r["n"] for r in c.execute("SELECT kind, key, n FROM analytics_daily")}
    assert by[("search_soldout", "")] == 1 and by[("soldout_date", D(61))] == 1
    so = {r[0]: r for r in rows_of("soldout", **{"from": D(59), "to": D(64)})}
    assert set(so) == {D(60), D(61), D(62)} and so[D(61)][2] == 0 and so[D(61)][3] == "Sold out" and so[D(61)][4] == 1
    c.execute("DELETE FROM room_status_events WHERE reason = 'soldout-test'")
    # retention: old counters are removed by the periodic job
    c.execute("INSERT INTO analytics_daily VALUES (?, 'search', '', 5)", (D(-500),))
    cn = g.connect()
    assert g.analytics.purge(cn) == 1 and not c.execute("SELECT 1 FROM analytics_daily WHERE day = ?", (D(-500),)).fetchone()
    cn.close()
    assert call("frontdesk", "GET", "/api/reports/data", {"report": "funnel"})[0] == 403
    c.close()


def test_analytics_never_breaks_a_search():
    cn = g.connect()
    cn.execute("DROP TABLE analytics_daily")
    try:
        g.reset_throttle()
        st, r = g.dispatch("GET", "/api/public/search", dict(check_in=D(85), check_out=D(86), guests=2), None, "9.9.9.9")
        assert st == 200, r
    finally:
        g.migrate_v4(cn)
        cn.close()


if __name__ == "__main__":
    os.environ.pop("PAYMENT_PROVIDER", None)
    try:
        td.setup()
        for name, fn in list(globals().items()):
            if name.startswith("test_") and callable(fn) and not name.startswith("test_staff_desk"):
                try:
                    fn()
                except BaseException:
                    print("FAILED:", name, file=sys.stderr)
                    raise
        print("ok")
    finally:
        shutil.rmtree(td.TMP, ignore_errors=True)
