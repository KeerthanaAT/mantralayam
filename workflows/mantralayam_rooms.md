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
- Database: `.tmp/mantralayam.db` (override with env `MANTRALAYAM_DB`). Tables: `properties`, `room_types`, `rooms`, `bookings`, `room_status_events`, `settings`, `audit_log`, `users`, `roles`, `user_properties`. The tool never reseeds data; `migrate()` only adds missing columns or tables (currently `room_types.active`), once per process and database file.
- Settings (table `settings`, defaults in the tool): `guest_booking_enabled`, `guest_max_rooms` (3), `guest_advance_days` (90), `guest_max_nights` (15), `guest_cancel_cutoff_hours` (24), `checkin_time` (14:00), `checkout_time` (11:00).

## How to run
```
python tools/mantralayam_rooms.py serve [PORT]     # http://127.0.0.1:8001
python tools/run_all.py                            # guest (8001) and staff (8002) together
python tools/mantralayam_rooms.py backup           # timestamped DB copy in .tmp/backups, newest 14 kept (schedule this daily)
python tools/test_mantralayam_rooms.py             # prints "ok"; runs on a COPY of the database
```

## API (all JSON)
| Method and path | Purpose |
|---|---|
| GET `/health` | liveness and DB check, `{"ok": true}` (503 if the DB is missing) |
| GET `/images/<file>` | any png/jpg/webp/svg/ico in `web/images/` (no allowlist to edit; other types and path tricks give 404) |
| GET `/api/public/info` | properties, room types, settings, today |
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
