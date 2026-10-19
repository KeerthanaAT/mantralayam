# Mantralayam Staff App Workflow

## Objective
Give property staff a sign-in app to run daily operations: see today's arrivals and departures, check guests in and out, take walk-in bookings, cancel or no-show bookings, manage room status (blocked, maintenance, cleaning) and review the audit trail. It shares the database with the guest site, so staff and guests can never double-book a room.

## Status
| Part | State |
|------|-------|
| Staff server `tools/mantralayam_staff.py` (port 8002) | done, tested |
| Staff UI `web/staff.html` + `web/staff.css` | done, checked in a real browser at 1280px and 390px |
| Admin screens: properties, room types, rooms, users, settings | **not built** (edit the database or add later) |
| Guest site (port 8001) | locked and untouched, see `workflows/mantralayam_rooms.md` |

## Isolation from the guest site
- Separate server and port: guest `serve` is 8001, staff `serve` is 8002. Separate URLs, cookies and API prefixes (`/api/staff` style routes live only on 8002).
- New files only: `tools/mantralayam_staff.py`, `tools/test_mantralayam_staff.py`, `web/staff.html`, `web/staff.css`. The guest files (`guest.html`, `shared.css`, `tokens.css`, `mantralayam_rooms.py`) are read-only inputs: the staff server serves the two CSS files and imports helper functions (database connection, availability query, settings, transactions) from `mantralayam_rooms.py`.
- Restore point before this work: git commit `1f8020f` "Guest website approved baseline".

## How to run
```
python tools/mantralayam_staff.py serve [PORT]            # http://127.0.0.1:8002
python tools/mantralayam_staff.py set-password USERNAME   # prompts for a new password (min 8 characters)
python tools/test_mantralayam_staff.py                    # prints "ok"; runs on a COPY of the database
```
Both servers can run at once. Same database file: `.tmp/mantralayam.db` (override with `MANTRALAYAM_DB`).

## Roles (checked on the server for every request)
| Role | Sees | Can do |
|------|------|--------|
| super_admin | all properties, audit incl. login events | everything below |
| property_manager | assigned properties, audit for them | everything below except property-less audit entries |
| front_desk | assigned properties | walk-in booking, check in/out, no-show, cancel, block rooms or report issues, clear any room status, cleaning steps |
| housekeeping | assigned properties | start cleaning, mark clean (cleaning entries only) |
| viewer | assigned properties | read only (no audit) |

Property access comes from `user_properties` (super admin sees all active properties). Asking for another property returns 403.

## Rules
- Check-in: booking must be `Confirmed`, today must be within check-in to check-out, the room must have no active status event and no other guest checked in.
- Check-out: sets `Checked-Out` and automatically creates a `cleaning_required` status for the room, which blocks new stays that start today until it is marked clean.
- No-show: `Confirmed` and the check-in date has arrived. Cancel: `Confirmed` only, reason required; guests already in the room must be checked out instead.
- Walk-in booking: check-in not in the past, guests within room capacity, room free for the whole stay using the same availability rule as the guest site; stored with `source='front_desk'`.
- Room status: blocked and maintenance need an end date, out of service may be open-ended; refused if a live booking overlaps the period.
- Every action writes an `audit_log` row (`booking.create/checkin/checkout/noshow/cancel`, `room.block/release/cleaning_start/cleaning_done`, `auth.login/login_failed/logout`).
- Staff only see the last 4 characters of a guest's ID proof number.

## Security
- Sign-in: PBKDF2-HMAC-SHA256 (240,000 rounds for new hashes, stored as `<rounds>$<salt>` in the `salt` column). 5 failed attempts per username and IP lock sign-in for 10 minutes. Unknown usernames take the same time as wrong passwords.
- Session: random token in an `HttpOnly`, `SameSite=Strict` cookie, 8 hour sliding expiry, held in server memory (restart signs everyone out). POST requests must be `application/json`, which a cross-site form cannot send.
- Deactivated users lose their session on the next request.
- Local only: plain HTTP on 127.0.0.1. Add HTTPS (and the cookie `Secure` flag) before exposing it beyond localhost.

## Known issue: existing staff passwords
The five accounts already in the database (`admin`, `manager`, `frontdesk`, `housekeeping`, `viewer`) were created by the lost original backend. Its exact password-hashing settings could not be recovered, so signing in with their old passwords may fail. The app tries PBKDF2-SHA256 with 200,000 rounds (override with env `STAFF_LEGACY_ROUNDS`). If sign-in fails, set a new password per account with `set-password`, which rewrites that user's hash and writes an audit entry. Do this only for accounts you own.

## Edge cases handled
- Two staff acting on the same room at once: every write runs in one `BEGIN IMMEDIATE` transaction.
- Stale or expired session: the page returns to the sign-in screen.
- Online (guest) group bookings appear as one row per room; staff actions apply to that room's row only.

## Lessons learned
- 2026-10-06: `replaceChildren(array)` prints `[object HTMLElement]` and `replaceChildren(null)` prints "null"; spread arrays and filter nulls.
- 2026-10-06: a `display: grid` page container makes wide tables stretch the whole page on phones; give grid items `min-width: 0` so the table scrolls inside its wrapper.
- Mobile emulation widens the layout viewport instead of scrolling, so `scrollWidth - innerWidth` reports no overflow; measure element positions instead.
