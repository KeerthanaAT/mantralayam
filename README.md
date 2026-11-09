# Mantralayam Rooms

Room booking for the Mantralayam guest houses: a guest website and a staff app, both Python standard library only, sharing one SQLite database.

## Getting started
1. Install Python 3.12 or newer. There is nothing else to install (standard library only).
2. Clone the repo: `git clone https://github.com/KeerthanaAT/mantralayam` and `cd mantralayam`.
3. Optional: copy `.env.example` to `.env` and fill in what you need. Without it, everything runs locally with mock SMS, email and payments.
4. Start both servers: `python tools/run_all.py`.
5. Open the guest site at http://127.0.0.1:8001 and the staff app at http://127.0.0.1:8002/staff. Press Ctrl+C to stop.

The database is created in `.tmp/mantralayam.db` on first start. In development, SMS codes are printed in the terminal.

## Run
```
python tools/run_all.py                    # guest site http://127.0.0.1:8001, staff app http://127.0.0.1:8002/staff
python tools/mantralayam_rooms.py backup   # verified database copy in backups/
python tools/mantralayam_rooms.py tick     # alerts + waiting-list messages (`run_all.py` does this every 5 minutes; use a timer in production)
python tools/test_mantralayam_rooms.py     # each test script prints "ok"
python tools/test_mantralayam_staff.py
python tools/test_mantralayam_features.py   # production hardening, booking flow, OTP
python tools/test_i18n.py
python tools/test_pay_notify.py            # notifications, PDF/QR, payments, refunds (mock provider)
python tools/test_staff_desk.py            # roles, QR check-in, ID vault, housekeeping, blocked rooms, call-backs
python tools/test_reports.py               # reports, CSV and Google Sheets export, analytics
python tools/test_audit_fixes.py           # regression tests for the audit's findings
python tools/audit_run.py                  # live audit: real servers, browser, writes TEST_REPORT_LIVE.md (see TEST_REPORT.md)
python tools/test_extras.py                # sevas, packages, festivals, notices, feedback, broadcasts, PWA
python tools/test_security.py              # security audit regression tests (see docs/SECURITY.md)
python tools/i18n_sync.py                  # after editing web/locales/en.json
```
The tests run on a copy of `.tmp/mantralayam.db`. Optional settings are listed in `.env.example`.

## Where things are
- `CLAUDE.md`: architecture summary and the standing project rules.
- `workflows/`: API, rules and edge cases for the guest and staff apps.
- `web/locales/`: guest-page translations (English is the source; `TODO` means not translated yet).
- Admin > Site info (staff app, super admin): the facts shown to guests, such as timings and policies.
- `docs/DEPLOYMENT.md`: put it on a VPS with HTTPS on a real domain (DNS steps, Caddy or nginx, systemd, uptime monitor). The config files are in `deploy/`.
- `tools/providers.py`: SMS, email and CAPTCHA providers, each with a mock mode (SMS codes are printed to the console in development).

Notifications and payments (see `workflows/mantralayam_rooms.md`): booking confirmations, changes, cancellations and arrival reminders go by SMS, WhatsApp (when configured) and email with a QR code and a PDF; every send is logged (Staff > Messages, no message text kept). Online payment (full, advance or counter) runs through Razorpay, or the local mock; refunds follow the refund percentage in Admin > Settings. Staff get a Front desk screen (scan the QR, record the ID, take payment, check in), a Housekeeping board, a Calendar with blocked rooms, Payments (reconciliation, refunds) and call-back follow-up notes.

Reports and analytics: Staff > Reports (managers) has daily arrivals, departures and sevas, occupancy, revenue and payment reconciliation, and first-party analytics (searches, booking funnel, sold-out dates; counters only, nothing identifies a visitor). Every table downloads as CSV or goes to Google Sheets through a service account (env vars, see `.env.example`); without them the CSV downloads instead.

Guest extras: sevas and room + seva packages (priced into the booking), a festival and panchanga calendar that can drive peak-date pricing, weather and river-level notices (also in confirmations), post-stay feedback with an admin view, opt-in festival broadcasts (SMS, WhatsApp, email, with unsubscribe), and an installable app with an offline shell and a quick re-book shortcut.

Signing in as a guest, booking and the waiting list need an SMS provider in production: set `SMS_PROVIDER` (see `.env.example`).

See `CHANGELOG.md` for what changed and what must be configured.
