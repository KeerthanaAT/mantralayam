# Mantralayam Staff App Workflow

## Objective
Give property staff a sign-in app to run daily operations: see today's arrivals and departures, check guests in and out, take walk-in bookings, cancel or no-show bookings, manage room status (blocked, maintenance, cleaning) and review the audit trail. It shares the database with the guest site, so staff and guests can never double-book a room.

## Status
| Part | State |
|------|-------|
| Staff server `tools/mantralayam_staff.py` (port 8002) | done, tested |
| Staff UI `web/staff.html` + `web/staff.css` | done, checked in a real browser at 1280px and 390px |
| Admin screens: properties, room types, rooms, users, settings (page `Admin`, super_admin only) | done, tested, checked at 1280px and 390px |
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

## Menu (hamburger drawer) and pages, added 2026-10-20
The top nav is replaced by a hamburger button (top-left, 44px+) that opens a left drawer (20rem on desktop, 80% wide on phones). It shows the user's name and role, one icon + label per page, the current page highlighted, and Log out. It closes on overlay click, the X (the burger itself), Esc or picking an item; the page behind cannot scroll; Tab stays inside while open.
The server decides the menu: `GET /api/meta` returns `pages` (from `PAGES` in `mantralayam_staff.py`), and every route enforces the same roles. Typing a hidden page's URL falls back to the Dashboard.

| Page | Roles | Notes |
|------|-------|-------|
| Dashboard | all | the old "Today" view |
| Bookings | all except housekeeping | adds **Edit** (guest name, phone, guests, remarks) and **Reschedule** (dates and/or room, re-checks availability, never double-books) |
| Calendar | all except housekeeping | day / week / month, rooms x dates; click a booking to open it, click a free cell to book |
| Rooms, Room status | all | unchanged |
| Guests | all except housekeeping | derived from bookings, grouped by the last 10 digits of the phone number; there is no guests table. "Add guest" opens a new booking; "Edit guest" updates name and phone on all of that guest's bookings |
| Rooms & rates | managers, super admin | add, edit, delete room types (delete refused while rooms use the type) |
| Reports | managers, super admin | by check-in date; revenue = booking amount excluding cancelled and no-show |
| Notifications | all | new and cancelled bookings (last 30 days) and rooms needing cleaning; housekeeping sees cleaning only. "Read" marker per user stored in `settings` as `notif_seen_<user id>` |
| Audit log | managers, super admin | unchanged |
| Settings | all | own name and password for everyone (password change ends the user's other sessions); business profile (property address, contact, phone, description, facilities) for managers; check-in/out times for super admin only |

Dropped from the original menu request because the data has no such concept: Services (replaced by Rooms & rates) and Staff schedule.

## Lessons learned (menu work)
- 2026-10-20: `tools/test_mantralayam_rooms.py` fails against today's live database (it expects zero online bookings). `test_mantralayam_staff.py` copies the live DB too, so old blocks on rooms can leak into it; the cleaning-event check now filters to cleaning kinds.
- 2026-10-20: a `position: sticky` header always forms its own stacking context; to keep the burger visible above the drawer the header itself sits above the drawer (z-index 50 vs 40).

## Admin screens (super_admin only)
Page `Admin` (`#/admin`), routes under `/api/admin/*`, every route role-checked on the server (managers get 403) and audited (`admin.property_create/edit`, `admin.room_create/edit`, `admin.user_create/edit`, `admin.<thing>_activate/deactivate`, `admin.settings`, `rate.create/edit`).
- Properties, room types, rooms and users can be created, edited and **deactivated, never deleted**. Reactivating is always allowed.
- Deactivating a property, room type or room is refused (409) while it has a Confirmed booking that has not ended or a Checked-In booking. A room's type also cannot change while it has such bookings, and a room cannot move to another property.
- Inactive room types and properties disappear from the guest site and cannot be chosen for new rooms or user assignments.
- Users: username unique (case-insensitive), password 8 to 200 characters, role from the `roles` table, at least one active property unless `super_admin` (who sees all). A password reset signs that user out everywhere. You cannot demote or deactivate yourself.
- Settings keys: `guest_booking_enabled`, `guest_max_rooms` (1-20), `guest_advance_days` (1-730), `guest_max_nights` (1-60), `guest_cancel_cutoff_hours` (0-168), `checkin_time`, `checkout_time`. Changes are audited as old to new.
- `room_types.active` did not exist in the original database; `migrate()` in `mantralayam_rooms.py` adds it (default 1) the first time any server connects. `migrate()` also creates `holds` and `peak_periods`.
- **Peak periods** tab: create, edit, deactivate (audited as `admin.peak_period_create/edit/activate/deactivate`). Fields: name, first and last night, optional most nights and most rooms per online booking (empty means the normal limit, a value above the normal limit changes nothing), note shown to guests. Dates are validated server-side (end not before start, at most 366 days). Overlapping periods are allowed; the strictest limit applies and all notes are shown. super_admin only, like the rest of the Admin page.
- Lesson 2026-10-28: `can` in `/api/meta` now has an `admin` key; tests that compare the whole dict must include it.

## Backup status on the Dashboard (added 2026-10-28)
`GET /api/dashboard` includes `backup` (`{ts, ok, detail}` from the newest line of `backups/backup.log`, or `null` before the first run) for super_admin only; other roles do not get the key. The Dashboard shows "Last backup: <date time> OK", or in red "FAILED (<reason>)". Scheduling and restore steps are in `workflows/mantralayam_rooms.md`.

## Date flexibility in the walk-in dialog (added 2026-10-28)
`GET /api/availability?property_id&check_in&check_out&guests` (front desk, managers, super admin; 403 for others) returns `{free, alternatives}`: `free` counts rooms in the user's properties that sleep the party and are free for the whole stay (holds count as taken); when it is 0, `alternatives` holds up to 3 nearby stays from the shared `alternatives()` function in `mantralayam_rooms.py`, limited to the user's properties (`property_id` of another property gives 403), one room per party (`max_rooms=1`). The walk-in dialog calls it (300 ms after any change of property, dates or guests) and, when `free` is 0, shows "No room for this party is free on these dates" with a button per alternative that sets the dates. Note: the shared function applies the guest rules (`guest_advance_days`, `guest_max_nights`), so staff suggestions never go past them even though a walk-in booking itself is allowed to.

## Call-backs page (added 2026-10-28)
Page `Call-backs` (`#/callbacks`) for front desk, managers, super admin and viewers (housekeeping does not get it). `GET /api/callbacks[?status=new|contacted|closed&property_id=]` lists newest first (up to 300). A user sees requests for their own properties **plus requests with no property**; picking a property filter shows only that property's requests, and another user's property gives 403. `POST /api/callbacks/<id>/status {status: contacted|closed}` (front desk, managers, super admin; viewers get 403) records `handled_by` and `handled_at` and writes `callback.contacted` or `callback.closed` to the audit log (old status to new, with name and phone in the reason). A request in another property is 404; `closed` is final and repeating a status is 409. The **Book rooms** button opens the walk-in dialog with the dates (moved up to today if already past), name and phone filled in and the request's property selected; guests is left at 1 because a group usually needs several rooms booked one by one. Text is shown as plain text, so any HTML typed by a guest displays as typed.

## Arrival details (added 2026-10-28)
The walk-in dialog has optional "Expected arrival time" and "Darshan date" next to "Remarks"; `POST /api/bookings` validates them with the same `arrival_fields()` as the guest site (see `workflows/mantralayam_rooms.md`). Every booking returned by the staff API (`BOOKING_SQL`) carries `arrival_time` and `darshan_date`. The booking rows on the Dashboard (arrivals, late arrivals, due out, in house) and the Bookings page show, under the stay dates, "Arrives about HH:MM", "Darshan <date>" and "Remarks: ...". `POST /api/bookings/<id>/edit` changes the two fields only when the request contains them (empty clears one); no screen uses the edit endpoint yet.

## Alerts banner, CSRF and health (added 2026-11-01)
- `GET /api/dashboard` adds `alerts` (open alerts: failed, stale or missing backup; many rooms on hold; many holds lapsing) for super admins and property managers; the Today page shows them as a red banner above everything else. Opening the dashboard also re-checks the conditions. New alerts are emailed once to `STAFF_ALERT_EMAILS` (the banner says "Not emailed" when no address is configured). They clear by themselves when the condition ends.
- The login reply and `GET /api/meta` carry `csrf`; the page sends it as `X-CSRF-Token` on every POST. The handler refuses a signed-in POST without the matching token (403) and any POST whose `Origin` is another site. The session cookie is `Secure` when `MANTRALAYAM_HTTPS=1`.
- `GET /health` exists on this server too (same body as the guest server's). Login failures (5 per 10 minutes per address and username) are counted in the database and survive a restart.

## Promo codes, pricing rules, peak prices, senior-friendly rooms (added 2026-11-01)
- Admin > Promo codes (`POST /api/admin/promos`, activate/deactivate via `/api/admin/promo_codes/<id>/active`, super admin only, audited `admin.promo_create`, `admin.promo_edit`, `admin.promo_code_activate`/`_deactivate`): code or automatic group rate, percent or flat discount with an optional cap, minimum rooms/guests/nights, valid dates, total and per-mobile usage limits, building and room type, "not on peak dates". The list shows how many bookings used each code.
- Admin > Settings: tax %, advance %, extra bed price and most per room, child charge, and whether children count towards capacity (`PRICING_RANGES`; empty means not charged; numbers with at most 2 decimals).
- Admin > Peak periods: "Price increase on these nights, %" (`surcharge_percent`).
- Admin > Rooms: "Senior-citizen friendly" checkbox (`rooms.senior_friendly`); ground floor is floor 0.
- Booking tables show who booked for a guest, the arrival contact, children, extra beds and the discount used.
- Cancelling, marking no-show, rescheduling or releasing a block calls `waitlist_tick_safe()`, which tells waiting guests that a stay is bookable again.


## Front desk, ID vault, housekeeping, call-backs (added 2026-11-02)
- **Permissions** (`PERMS` in `mantralayam_staff.py`, shown in Admin > Roles, enforced on every route): front desk works the counter (bookings, check-in, ID capture, counter payments, blocks, call-backs); a manager also reveals IDs (reason required, logged), sees Payments, Messages and the audit log, handles refunds and cancels paid bookings; the super admin also changes settings and users; housekeeping only changes room cleaning; viewers read.
- **Front desk screen**: scan the guest's QR (camera `BarcodeDetector`, or a handheld scanner typing into the box) or type the booking ID (`GET /api/checkin/lookup`; a typed ID is marked less certain). Record the ID type and number for all rooms of the booking (encrypted with `ID_ENCRYPTION_KEY`; the booking keeps only the last four characters), optionally a photo (JPEG/PNG/WebP, shrunk by the page, at most 2.5 MB, encrypted). Check-in requires the ID (`checkin_requires_id`). `id_retention_days` erases IDs and numbers after the stay (the 5-minute job); empty = kept until set.
- **Housekeeping board** (`GET /api/housekeeping`): blocked, to clean, being cleaned, ready, occupied; a room with a guest arriving today that is not ready is flagged. Check-out creates the cleaning task; "Mark ready" closes it.
- **Calendar**: bookings and blocked rooms by day; click a free day to block, a blocked day to clear. A block cannot cover a booking and a booking cannot be made on a blocked room.
- **Call-backs**: new -> contacted -> follow-up (date) <-> contacted -> closed (note required); a closed request can be reopened; every change and note keeps the time and the staff member's name; due follow-ups appear in notifications.
- **Cancelling a paid booking** needs a manager; it records the refund by policy and tells the guest.
- Audit log: a "Sensitive actions only" filter (IDs, money, cancellations, blocks, users, settings).

## Reports, notices, feedback, broadcasts, admin lists (added 2026-11-02)
- **Reports** (managers): arrivals and departures for a day, seva schedule, occupancy by day (rooms that could be sold = rooms minus blocked), revenue (booked value by check-in date beside money received and refunded by the day it moved), money by method, payment reconciliation, booking funnel, searches, sold-out dates, online payment outcomes. `GET /api/reports/data`, `/api/reports/csv` (formula-looking cells are prefixed with an apostrophe), `POST /api/reports/export` (Google Sheets, or `{exported: "csv"}` so the page downloads the CSV). Every export is audited because tables hold names and numbers.
- **Admin** (super admin): Sevas, Packages, Festivals (with the peak-pricing link), Panchanga (with line import). **Notices** (front desk and up), **Feedback** (managers: averages, filter, review note), **Broadcasts** (managers: composer, history, masked subscriber list, removal on request).
