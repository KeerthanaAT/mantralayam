# Hotel Reservation Workflow

## Objective
Manage hotel room reservations: initialise the system, check availability, create, view and cancel bookings, without double-booking any room.

## Assumptions (no requirements doc existed; correct these if wrong)
- Single hotel, rooms of types `single`, `double`, `suite` with a nightly rate.
- Storage is a local SQLite file at `.tmp/hotel.db` (processing data, disposable per claude.md). Export to a cloud deliverable (e.g. Google Sheets) is a later step, once credentials exist.
- A stay is `check_in` (inclusive) to `check_out` (exclusive); dates are `YYYY-MM-DD`.
- Cancelled bookings free the room.

## Tools (in `tools/`)
| Step | Tool | Status |
|------|------|--------|
| 1. Init database and seed rooms | `tools/reservation_db.py init` | done |
| 2. Search available rooms | `tools/check_availability.py` | planned |
| 3. Create a reservation | `tools/create_reservation.py` | planned |
| 4. View / cancel reservations | `tools/manage_reservation.py` | planned |
| 5. Export report to Google Sheets | `tools/export_to_sheets.py` | planned, needs `credentials.json` |

## Required inputs
- Init: none.
- Availability: `check_in`, `check_out`, optional `room_type`, optional `guests`.
- Reservation: guest name, contact, room id, `check_in`, `check_out`.
- Cancel/view: reservation id.

## Expected outputs
- Tools print JSON to stdout so the agent can parse results; non-zero exit code on error with a message on stderr.

## Edge cases
- `check_out` must be after `check_in`; reject past dates.
- Overlap rule: two stays conflict if `new_in < existing_out and new_out > existing_in` (same-day turnover is allowed).
- Booking happens inside one transaction, so the availability check and insert cannot race.

## Lessons learned
(Add rate limits, quirks and fixes here as they are discovered.)
