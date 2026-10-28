# Mantralayam Guest Booking Workflow

## Objective
Let pilgrims search room availability, book rooms, look up and cancel their own bookings online, against the existing Mantralayam database, without ever double-booking a room.

## Status
| Part | State |
|------|-------|
| Guest website `web/guest.html` (+ `shared.css`, `tokens.css`) | done, served at `/` |
| Public API (`/api/public/*`) in `tools/mantralayam_rooms.py` | done, tested |
| Staff app and staff API (login, check-in/out, room status, properties, users, audit view, `/staff`) | **not rebuilt**: the original source was lost; only its data remains in the database |
| Real property photos | optional, not built (guest page shows "Photo coming soon") |
| Export report to Google Sheets | not built, needs `credentials.json` |

## Inputs and data
- Database: `.tmp/mantralayam.db` (override with env `MANTRALAYAM_DB`). Tables: `properties`, `room_types`, `rooms`, `bookings`, `room_status_events`, `settings`, `audit_log`, `users`, `roles`, `user_properties`. The tool never reseeds data; `migrate()` only adds missing columns or tables (currently `room_types.active`, `bookings.arrival_time`, `bookings.darshan_date` and the tables `holds`, `peak_periods` and `callback_requests`), once per process and database file.
- Settings (table `settings`, defaults in the tool): `guest_booking_enabled`, `guest_max_rooms` (3), `guest_advance_days` (90), `guest_max_nights` (15), `guest_cancel_cutoff_hours` (24), `checkin_time` (14:00), `checkout_time` (11:00).

## How to run
```
python tools/mantralayam_rooms.py serve [PORT]     # http://127.0.0.1:8001
python tools/run_all.py                            # guest (8001) and staff (8002) together
python tools/mantralayam_rooms.py backup           # verified DB copy in backups/, newest 14 kept; exit code 1 + message on failure (schedule daily, see below)
python tools/test_mantralayam_rooms.py             # prints "ok"; runs on a COPY of the database
```

## API (all JSON)
| Method and path | Purpose |
|---|---|
| GET `/health` | liveness and DB check, `{"ok": true}` (503 if the DB is missing) |
| GET `/images/<file>` | any png/jpg/webp/svg/ico in `web/images/` (no allowlist to edit; other types and path tricks give 404) |
| GET `/api/public/info` | properties, room types, settings, today, upcoming active `peak_periods` |
| GET `/api/public/search?check_in&check_out&guests[&property_id&room_type_id]` | free room types grouped by property, with rooms needed for the party |
| POST `/api/public/hold` `{property_id, room_type_id, rooms, check_in, check_out[, token]}` | holds the rooms for 10 minutes; returns `{token, expires_at, seconds, rooms}`. Passing the previous `token` replaces that hold |
| POST `/api/public/release` `{token}` | frees a hold at once |
| POST `/api/public/book` | creates a booking (1 to 3 rooms of one type); optional `hold_token` books the held rooms; a filled `website` field (honeypot) is refused; returns the booking view |
| POST `/api/public/booking/lookup` `{booking_id, phone}` | view a booking |
| POST `/api/public/booking/cancel` `{booking_id, phone, reason}` | cancel before the cutoff |

Errors return `{"error": "<message for the guest>"}` with 400, 403 (online booking off), 404, 409 (room taken, cannot cancel), 429 (rate limit) or 5xx. A 429 also carries `retry_after` (seconds) in the body and as the `Retry-After` header, and the message says how long to wait.

## Rules
- A room is free for `[check_in, check_out)` if it and its property are active, no `Confirmed` or `Checked-In` booking overlaps, and no active `room_status_event` overlaps (open-ended events block indefinitely). Same-day turnover is allowed.
- Cleaning events (`cleaning_required`, `in_cleaning`) only block stays starting today or earlier. This is inferred, confirm with property staff.
- The availability check and the insert run in one `BEGIN IMMEDIATE` transaction.
- Multi-room bookings create one row per room, sharing `group_id`, which is the booking ID shown to the guest. Guests are spread evenly, at least one per room, none above room capacity.
- Online bookings are `source='online'`, `payment_method='pay_at_counter'`, `payment_status='due'`, amount = tariff x nights.
- Booking ID plus mobile number (last 10 digits compared) is the only credential for lookup and cancel.
- Cancelling is allowed while status is `Confirmed` and now is before check-in time minus the cutoff hours. Cancellation reason is mandatory.
- Every online create and cancel writes an `audit_log` row (`booking.online_create`, `booking.online_cancel`, user `guest`).
- In-memory rate limits per IP: 60 searches per minute, 10 hold requests and 10 booking attempts per 10 minutes, 20 lookups or cancels per 10 minutes; plus 5 bookings per 10 minutes per mobile number. They reset on restart.
- Holds: table `holds` (token, room, dates, `expires_at`, ip). `free_rooms()` treats an unexpired hold as unavailable to everyone except the holder's own token, so the booking transaction (`BEGIN IMMEDIATE`) still decides every race and double-booking stays impossible. Expired rows are ignored by the query and swept on the next hold; no background job. One IP may keep at most 3 live holds (429 after that). The guest page takes the hold when the confirm dialog opens, shows a countdown, re-holds when the room count changes, releases on close or page exit, and the booking consumes it. Staff walk-in booking is refused for a held room with a message saying it is held until HH:MM.
- Honeypot: the hidden, `inert` field `website` in the booking form. A bot that fills it gets a generic 400, counts double against its IP limit, and no booking is created.

## Edge cases
- Dates: valid ISO, not in the past, check-out after check-in, within the advance window and night limit.
- Phone 10 to 13 digits; ID type from a fixed list; email optional but validated.
- Two guests racing for the last room: the second gets 409 "no longer available" and the page re-runs the search.
- Wrong booking ID or wrong phone give the same 404 message, so IDs cannot be probed.

## Known limits
- Local server only (127.0.0.1, plain HTTP, no HTTPS); rate limits reset on restart.
- Server clock is used for "today" and cancel deadlines; it must be set to India time.
- Staff features are missing (see Status).

## Lessons learned
- 2026-10-06: `guest.html` linked only `shared.css`, so all `var(--...)` colors from `tokens.css` were undefined and the page was unstyled. Fixed by linking `tokens.css` first.
- 2026-10-06: `replaceChildren(array)` stringifies the array (`[object HTMLDivElement]`); spread it instead.
- Seed data: the "Non-AC Double" rooms list "AC" among their amenities. That is a data issue in `rooms.amenities`, not a code issue.
- 2026-10-28: images were served from a hard-coded allowlist, so a new banner 404'd until the list was edited. Now any image file in `web/images/` is served. The rooms test used to assert the real DB had zero online bookings; real ones exist, so it now checks the count is unchanged. The `.venv` points to a missing Python 3.14; use Python 3.12 (`py -3.12`).
- Peak periods (table `peak_periods`: name, start_date, end_date, optional max_nights, max_rooms, note, active). A period's end date is its last night, so a stay touches it when `start_date < check_out` and `end_date >= check_in`. `parse_stay()` tightens `max_nights` and `max_rooms` to the strictest overlapping active period (a period can only tighten the normal settings, never loosen them), so search, hold and book all enforce the same limits; refusals name the period, for example "During Brahmotsavam online bookings can be for at most 2 nights". The search response includes `peak` (the periods for the searched dates, with notes) and `info.peak_periods` lets the form show the note while the guest is still picking dates, in the results and in the confirm dialog. Existing bookings are never changed by a new period. Managed on the staff Admin page (super_admin only), see `workflows/mantralayam_staff.md`.
- 2026-10-28 lesson: `migrate()` runs once per process per database path. Replacing the database file under a running server (as a UI test harness does) leaves the new copy without the new tables until the server restarts.

## Backup and restore (added 2026-10-28)
`backup` uses SQLite's online backup API, so it is safe while both servers run. It writes `backups/mantralayam-YYYY-MM-DD-HHMM.db` (folder is gitignored; override with env `MANTRALAYAM_BACKUPS`), runs `PRAGMA integrity_check` on the copy, and only then deletes everything but the newest 14 (`mantralayam-????-??-??-????.db` only; other files are never touched). A failed run deletes its partial copy, rotates nothing, prints `Backup FAILED: <reason>` to stderr and exits 1. Every run appends `time<TAB>OK|FAILED<TAB>detail` to `backups/backup.log`; the staff Dashboard shows the last line as "Last backup: ... OK/FAILED" to super_admin only (nothing real shows until the first run). Two runs in the same minute replace each other.

Schedule it daily at 02:00 (run once in Command Prompt; use the full path of `python.exe`, here Python 3.12):
```
schtasks /Create /SC DAILY /ST 02:00 /TN "Mantralayam Backup" /TR "\"C:\Users\keerthana\AppData\Local\Programs\Python\Python312\python.exe\" \"C:\Users\keerthana\Desktop\claude\tools\mantralayam_rooms.py\" backup"
schtasks /Run /TN "Mantralayam Backup"
schtasks /Query /TN "Mantralayam Backup" /V /FO LIST
```
`/Run` tries it now (then check `backups\backup.log`); in the query output "Last Result: 0" means the last run succeeded. The task runs only while the PC is on and the user is logged in (add `/RU SYSTEM` from an administrator prompt to avoid that). If the PC was off at 02:00 the dashboard line goes stale, so check its date.

Restore: stop both servers (Ctrl+C in the `run_all.py` window); keep the current file (`copy .tmp\mantralayam.db .tmp\mantralayam.before-restore.db`); delete `.tmp\mantralayam.db-wal` and `-shm` if present; copy the chosen `backups\mantralayam-....db` over `.tmp\mantralayam.db`; start the servers. Bookings made after that backup are lost, so tell staff what time the data goes back to. Staff accounts come back as of the backup too.

## Date flexibility on sold-out searches (added 2026-10-28)
`alternatives(conn, ci, co, guests, property_ids=None, type_id=None, max_rooms=None)` in `mantralayam_rooms.py` is the one shared function. For a sold-out request it tries check-in dates 1 to 14 days either side (same number of nights), nearest first (earlier before later on a tie), and returns the first 3 that are bookable: each candidate passes `check_stay()` (the date rules split out of `parse_stay`: not in the past, `guest_advance_days`, `guest_max_nights` including peak-period limits) and `free_rooms()` has rooms that cover the party within the room limit (`max_rooms`, default the online limit). Each result is `{check_in, check_out, nights, days_away}`. Up to 28 small queries per call.
- `/api/public/search` always includes `alternatives` (empty unless the search found nothing, then up to 3; the search's property and room-type filters are kept). `guest.html` shows buttons "2 nights free from 5 Nov" that fill the dates and search again.
- Not offered: a party that needs more than the online room limit (no date helps), stays longer than the night limit, dates past the advance window, anything in the past.
- Staff use the same function through `GET /api/availability` (see `workflows/mantralayam_staff.md`).

## Call-back requests (added 2026-10-28)
Table `callback_requests` (created by `migrate()`): `id, property_id (nullable), name, phone, people, check_in, check_out, note, status new|contacted|closed, created_at, handled_by, handled_at`.
- `POST /api/public/callback {name, phone, people, check_in, check_out[, note, property_id]}` returns `{"ok": true}`. Every field is validated on the server: name 1-100 characters, phone 10-13 digits, people 1-500, dates valid, not in the past, check-out after check-in, check-in within 366 days and at most 60 nights, note up to 500 characters, property (optional) must be active. The hidden `website` field is a honeypot (filled: generic 400, counts double against the IP, nothing stored). Rate limit uses the same `throttle()` helper as booking but its own counters: 5 requests per 10 minutes per IP and 3 per mobile number (429 with `retry_after` and the `Retry-After` header). Each request is audited as `callback.request` (user `guest`).
- `/api/public/search` adds `callback`: `"group"` (rooms are free but the party needs more rooms than may be booked online), `"soldout"` (nothing free and no alternative dates) or `null`. `guest.html` shows the "Request a call-back" form under the results for the first two, with the search's dates and party size filled in and the search's property (if one was chosen) attached.
- Known limit: someone rotating IP addresses and phone numbers could still fill the staff queue; staff can close junk requests. Nothing is emailed or texted, staff see requests only on the Call-backs page.

## Arrival details on bookings (added 2026-10-28)
Two optional columns on `bookings`, added by `migrate()` (existing rows stay NULL): `arrival_time` (text `HH:MM`, 00:00 to 23:59) and `darshan_date` (ISO date, today or later; it does not have to fall inside the stay). They are separate from the existing 500-character note (`bookings.remarks`, "Special requests" for guests), which is unchanged; there is no second note field. `arrival_fields()` in `mantralayam_rooms.py` validates both for the guest and staff paths (bad time: "Expected arrival time must look like 14:30."; past or invalid date: refused; empty means not given). The guest booking form has "Expected arrival time" and "Darshan date"; the confirm dialog summary echoes them as they are typed, and the booking view (confirmation page and My booking) shows them next to the requests. A group booking stores them on every room row. All text from guests is put on the page as text (the pages never use `innerHTML`; a test checks this), so `<b>` in a note shows as typed.
