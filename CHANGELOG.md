# Changelog

## 2026-11-15 (booking detail dialog layout, staff app only)
Layout only: no API route, permission or business rule changed.
- **Reservations > booking dialog** (`openDetail` in `staff.html`): the title and status badges sit in a sticky header with a 44x44 "×" Close button (`aria-label` "Close booking", new key `staff.res.detail.close_aria`; the old bottom Close and its `staff.res.detail.close` key are gone). Esc still closes, and focus returns to the row link that opened it (not after an action, which opens its own dialog).
- **Actions in a sticky footer grid**: one full-width primary (Check in or Check out), then Edit / Reschedule / No-show in one equal-width row (columns match how many are allowed), then Cancel alone as an outlined danger button. Same status, date and role checks as before; an empty slot is not rendered, and a booking with no allowed actions shows no footer. At 560px and narrower everything stacks in one full-width column.
- The details body scrolls on its own; the amount is bold. Keyboard order: Close, primary, secondary, Cancel.
- **Rooms page dialogs**: the room drawer keeps its padding (it shares `.detail` with the booking dialog), and the footer of long form dialogs (such as New booking) stays pinned so Save is always in view.
- Checked in a real browser (headless Chrome, with the real markup, CSS and `openDetail`) for Confirmed, Checked-In, Checked-Out, Cancelled and a role without `book`. `test_i18n.py` passes; `test_mantralayam_staff.py` and `test_pay_notify.py` already failed before this change (an export audit-count assertion and `purge_ids`).

## 2026-11-15 (one Rooms page, staff app only)
The guest site and every booking rule are unchanged.
- **Rooms, Room status and the room board in Housekeeping are now one page, Rooms**, in the menu for every role. `#/status` and old bookmarks open it (`MOVED` in `staff.html`); the "Room status" menu entry is gone (`PAGES`). No existing route or role check changed.
- **One state per room** from the new `GET /api/rooms/board?property_id=&date=` (all roles, own properties): Available, Booked, Checked in, To clean, Being cleaned, Blocked (maintenance and out of service show their own word). A block hides cleaning, cleaning hides the guest, so a room never shows two labels. Cleaning states describe today only; another date shows Available, Booked, Checked in and Blocked.
- **Layout**: count cards that filter (default "Needs attention": to clean, blocked, urgent arrivals), toolbar (property, date, search, housekeeper, floor, Block a room), tiles grouped by building and floor in folding groups with a fixed-height scroll area, urgent rooms pinned on top, and a List view (remembered for the session).
- **Drawer** (a bottom sheet on a phone) holds the details and every action for the room's state and role: Book, Block room, Mark needs cleaning, Assign, Start cleaning, Mark ready, Reassign, Mark inspected, Add note, Edit, Clear status, Open the booking. The last few changes (who and when) are listed.
- **Bulk actions** (Select rooms): Assign to, Mark needs cleaning, Mark ready. **Undo**: Clear status, Mark ready and Needs cleaning reach the server about 5 seconds later, so Undo sends nothing; the change is sent at once if you leave the page. Keys: `/` search, `Esc` closes the drawer or the selection. Changes are announced to screen readers. The page refreshes every 60 seconds and keeps filters, open groups, scroll and the open room.
- **Housekeeping** keeps the team panel (housekeepers, stats, attendance, CSV) and a link "Open rooms to assign". Stats still count when a room is marked ready on the Rooms page.
- **New routes**: `POST /api/room_events/<id>/edit` (front desk and managers; same checks as a new block, audit `room.block_edit`) and `GET /api/rooms/<id>/history` (every role; only `room.*` entries, only user, action and time).
- **Fix**: "Mark needs cleaning" no longer refuses a room whose old block has already ended.
- Housekeeper records, assignments and notes are still kept in the browser (localStorage), so the housekeeper filter and "assigned to" differ between devices. Moving them to the database is a separate task.
- Tests: `test_rooms_page` in `test_mantralayam_staff.py` (state rule, roles, scope, edit, history, menu and the redirect text).

## 2026-11-14 (Housekeeping tab, staff app only)
The guest site and every booking rule are unchanged.
- **Housekeeping is back in the menu** (front desk, managers, super admin and housekeeping roles; `PAGES` in the staff module).
- **Team panel**: add, edit, delete (with confirmation) housekeepers with phone, shift, floor or block and status (Available / Busy / On Leave); search and status filter; per-person stats (cleaned today, this week, this month, pending, average minutes per room, attendance, rating, remarks, recent activity, 7-day chart); Mark attendance; CSV export; summary cards.
- **Room cards** show the task (Dirty, Cleaning in progress, Cleaned, Inspected), an urgent tag when the next check-in is within 2 hours, an assignee picker, Mark inspected and a note. Start cleaning and Mark ready also record the work for the assigned person. Check-out already flags a room as needing cleaning on the server.
- **Rooms section is now compact**: the five count cards are tabs (plus All; default To clean, gold border on the open one), rooms are small tiles (number, status dot, guest or housekeeper) grouped by building and floor in folding groups inside a scrolling area, with a room/guest search and a housekeeper filter. Ready and Occupied start folded. Selecting a tile opens a side drawer with the full card (assign, start, mark ready, inspect, note, needs cleaning, clear block, new Block room button). Tiles carry one quick action (Assign, Start, Ready) and a red mark when check-in is within 2 hours.
- Housekeeper records, assignments and history are stored in the browser's localStorage (`mantralayam.housekeeping.v1`), so they are per browser, not shared between devices. The first five housekeepers are sample data.

## 2026-11-10 (staff dashboard and Reservations)
Staff app only. The guest site and every booking rule are unchanged.

**Changed behaviour you will notice**
- **Dashboard** is rebuilt: Today cards (arrivals, departures, occupancy tonight, cash collected today), This-month cards for managers and super admin (net revenue, expenses, profit, outstanding dues; expenses and profit show 0 until an Expenses page exists), a 30-day revenue chart (plain SVG with a table view), booking sources, and room availability for a night or a date range. Money figures are for managers and super admin only; the server leaves them out for other roles.
- **Bookings is now Reservations**: status tabs with live counts (Checked In, Checked Out, All, Confirmed, Cancelled; No-show only under All), search, a check-in date filter, a Sort menu (kept for the browser session), 20 rows per page, and a detail view with check-in, check-out, no-show, edit, reschedule and cancel. On a phone each booking is a card.
- The menu no longer lists Front desk, Call-backs, Housekeeping, Payments, Messages, Notices, Feedback or Broadcasts (the pages and their routes still exist).

**Added**
- `GET /api/staff/dashboard`. `GET /api/bookings` takes `status`, `from`/`to`, `sort`, `page`, `page_size`, `counts`; with `page`, `page_size` or `counts` it returns `{items, total, page, page_size, pages, sort, counts}`, otherwise the old plain list. Sort values are a fixed whitelist; an unknown one is refused (400).
- `GET /api/bookings/csv`: the filtered list as CSV, audited as `booking.export`. New permission `booking.export` (managers and front desk; see Admin > Roles).
- The staff server serves `/i18n.js` and `/locales/en.json` (English only); new staff texts are `staff.*` keys in `web/locales/en.json`.

**Fixed**
- Availability for today now counts a room as occupied while a guest is still checked in past their check-out date.
- The dashboard's recent-bookings query filtered on `No-show`; the stored value is `No-Show`.

**Needs your action**: restart the staff server. `migrate()` now adds three empty text columns to `bookings` (`vehicle_number`, `guest_address`, `staff_notes`); no screen uses them yet. Translate the new `staff.*` texts only if the staff app will ever be shown in another language.

## 2026-11-04 (security audit fixes)
Fixes for the November 2026 security audit (findings F1 to F22; notes, limits and the incident runbook are in `docs/SECURITY.md`; tests in `tools/test_security.py`).

**Changed behaviour you will notice**
- Guests give only the **last 4 characters** of their ID number when booking (the field now says so). The full number is entered, encrypted, by the front desk at check-in. Walk-in bookings keep only the last 4 as well. English text only; te/kn/ta/hi are TODO for the new label.
- Managers and super admins sign in with an **authenticator-app code** (new Settings > Account security page). On a public server (`MANTRALAYAM_HTTPS=1`) it is required before they can do anything else.
- Room types and tariffs can be changed by the **super admin only** (they are shared by all properties). Editing a guest's name or mobile across their bookings is for managers (was front desk).
- Over HTTPS every POST needs this site's exact `Origin`. Passwords: 10+ characters, 600,000 PBKDF2 rounds (older hashes are upgraded at the next sign-in), sessions end 12 hours after sign-in. Codes go to Indian (+91) numbers only.
- `/health` no longer shows the backup age. The servers refuse to start over HTTPS without `ALLOWED_HOSTS`, `APP_SECRET` and a 32+ character `ID_ENCRYPTION_KEY`.

**Added**
- SMS budget `SMS_HOURLY_LIMIT` / `SMS_DAILY_LIMIT` (defaults 60 / 300) with a staff alert; `SMS_ALLOWED_PREFIXES`.
- Wrong booking ID + mobile pairs are counted per mobile number across all addresses (`LOOKUP_FAIL_LIMIT`); a site-wide cap on rooms on hold (`HOLD_GLOBAL_PERCENT`); IPv6 visitors are counted per /64.
- Sign-in limits per address and per username across addresses, with an alert; 15-second socket timeout, 200-connection cap and body-length checks on both servers; access logs without query strings.
- Scripts carry a per-response nonce, so the CSP has no `unsafe-inline` for scripts.
- Vault format version 2 (scrypt key stretching; version 1 values still open). Commands: `mantralayam_rooms.py scrub-ids` (move old plain-text ID numbers into the vault), `rekey`, `erase <mobile> <reason>`; `mantralayam_staff.py reset-totp USER`. Settings `guest_retention_days` and an erase-on-request route (`/api/admin/guest_erase`).
- Backups are mode 600 in a mode 700 folder; systemd units are hardened (`UMask=0077` and more); Caddy/nginx files: timeouts, per-path upload limits, a VPN/IP allowlist for the staff site, query-free logs.

**Fixed**
- `/images/..\..\x.png` (Windows) read any image file on disk; image names are now a single plain file name.
- The mock payment provider's signing key is random per process; mock payments are refused on a public server.

**Needs your action**: see `docs/DEPLOYMENT.md` steps 6b and 11b (VPN, two-factor set-up, `scrub-ids`, `rekey`, encrypted offsite backups, retention periods).

## 2026-11-02 (live audit)
Audited every feature by running it (see TEST_REPORT.md; `python tools/audit_run.py` repeats it). Fixed: room photos and admin upload (were missing), larger-text and high-contrast modes (were missing, then mis-scaled), the revenue and method reports leaving out refunded payments that never became a booking, WCAG label-in-name and list-structure problems, checkbox tap targets under 44 px, the header overflowing with the new buttons, and a 404 for /favicon.ico on every page. New: `MANTRALAYAM_UPLOADS` (where photos are stored, default `uploads/`, keep it in your backups), `payments.property_id` column. Brand link and Book buttons lost their long aria-labels (the visible text now names them).

## 2026-11-02 (reports and analytics; sevas, festivals, notices, feedback, broadcasts, PWA)
**Reports (Staff > Reports, managers)**
- Daily arrivals and departures, seva schedule, occupancy by day, revenue (booked value, received online and at the counter, refunded), money by payment method, payment reconciliation. Each table downloads as CSV (spreadsheet formulas are defused) or goes to Google Sheets through a service account set by env vars; without them the CSV downloads instead. Exports are audited.
- First-party analytics: counters of searches, results, sold-out nights, room holds, form submissions and bookings. No IP, cookie, visit id or user agent is stored and nothing goes to a third party. Booking funnel with drop-offs, popular dates, nights and party sizes, sold-out dates with the searches turned away, online payment outcomes. Counters older than 400 days are deleted.

**Guest features**
- Sevas booked with the room, and packages such as room + seva (fixed days, discount): priced into the quote, capacity per day checked in the booking transaction, refunded and cancelled with the stay.
- Festival and panchanga calendar entered by staff; a festival can create the peak period for its dates, so price and limits follow it. Festival list on the home page and a Festivals page.
- Weather and river-level notices set by staff: banner on the site, in the confirmation email, the booking page and receipt; warnings also in the SMS.
- Post-stay feedback: a signed link sent after check-out, one rating per stay, low ratings alert the staff; Staff > Feedback with averages, filters, review notes and CSV.
- Festival updates: opt-in (consent box, SMS code for SMS and WhatsApp, email link for email), broadcasts from Staff > Broadcasts in the subscriber's language with an unsubscribe link in every message.
- Installable app: manifest, icons, service worker with an offline shell, install button, offline banner with the properties' phone numbers, quick re-book shortcut.

**Schema (additive):** `analytics_daily`, `sevas`, `packages`, `package_items`, `seva_bookings`, `festivals`, `panchanga`, `notices`, `feedback`, `subscribers`, `broadcasts`, `broadcast_log`. New setting `feedback_days`. Restart both servers.

**You must configure:** `PUBLIC_URL` (links in messages); for Sheets `GOOGLE_SERVICE_ACCOUNT_JSON` and `GOOGLE_SHEETS_ID` (share the sheet with the service account; the server needs the `cryptography` package or `openssl`); in Admin the sevas, packages, festivals and panchanga you want (nothing is pre-filled or calculated); in Admin > Settings the feedback delay. The new texts in te, kn, ta, hi are `TODO`; seva, festival and notice names are shown as entered.

**Decisions to check**
- Sevas are not taxed and promo codes do not apply to them; tax rules for sevas are for you to decide.
- A booking with sevas cannot have its dates changed online or by staff (cancel and rebook), because the seva dates follow the stay.
- Broadcasts are sent while the manager waits (at most 1000 people, 3 a day). A queue would be needed beyond that. WhatsApp marketing messages usually need approved templates at your gateway.
- Feedback and low-rating alerts apply to online bookings only.
- Panchanga is whatever staff enter; there is no calculation and no source is assumed.
- Not tried against the live Google Sheets API (no credentials here); the mock and the fallback are tested.

## 2026-11-02 (notifications, online payments, staff desk)
**Notifications**
- SMS, WhatsApp (off unless configured) and email providers behind interfaces with mock modes (`tools/providers.py`). Confirmation, change, cancellation and an arrival reminder (run by the 5-minute `tick`) are sent in the guest's language. The email carries a printable confirmation with a QR code for check-in and a PDF attachment; the PDF is also downloadable (booking page and My bookings). Stdlib-only QR (`tools/qr.py`, checked with an independent decoder) and PDF (`tools/pdfdoc.py`) writers.
- `notification_log` records channel, masked recipient, delivery status and error class, never the message; failed sends are retried 3 times; Staff > Messages shows it.

**Payments (Razorpay, test mode ready)**
- UPI, cards and net banking through Razorpay Checkout (no card data touches the server). Options full, advance (configurable %) or counter. Checkout success and webhook both confirm a payment idempotently; signatures are verified; the booking is only created when paid; unpaid or abandoned payments release the rooms (15-minute hold); a payment that arrives after the rooms went is refunded in full.
- Refunds on cancellation follow the refund percentage with status tracking (requested, pending, processed, failed, manual), provider webhooks, retries and a staff alert. Payment records (online and counter) reconcile against bookings (Staff > Payments). The secure-payment note shows only when online payment is on. Tests for the pricing, advance and refund arithmetic.

**Staff**
- Roles and permissions matrix (front desk, manager, super admin, housekeeping, viewer), enforced server-side, shown in Admin > Roles; audit filter for sensitive actions; cancelling a paid booking needs a manager.
- Front desk screen: QR scan or booking ID, ID capture (encrypted with `ID_ENCRYPTION_KEY`, photo optional, manager-only reveal with a logged reason, retention setting), counter payments, check-in.
- Housekeeping board (blocked, to clean, cleaning, ready, occupied), blocked-room calendar, database-level overlap guard against double booking.
- Call-backs: new, contacted, follow-up (date), closed (note required), reopening, notes with time and staff name.

**Schema (additive, `migrate()`):** `payments`, `refunds`, `webhook_events`, `notification_log`, `id_records`, `callback_notes`, `app_secrets`; `bookings.lang`; `callback_requests` rebuilt (rows kept) for the follow_up status and `follow_up_on`; overlap triggers on `bookings`. New settings: `pay_options`, `reminder_days`, `reminder_hour`, `id_retention_days`, `checkin_requires_id`. Restart both servers.

**You must configure:** `ID_ENCRYPTION_KEY` (required with HTTPS; keep a copy); for payments `PAYMENT_PROVIDER=razorpay`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` and the webhook in the Razorpay dashboard; `WHATSAPP_PROVIDER` and a gateway if wanted; in Admin > Settings the payment options, advance %, refund %, reminder days and the ID retention days (empty on purpose until you decide). The new texts in te, kn, ta, hi are `TODO`.

**Decisions to check**
- Staff-created walk-in bookings are not messaged (only online bookings, and staff changes to them).
- The ID number given at online booking stays on the booking until the front desk records the verified ID (then only the last four remain) or the retention period erases it.
- The ID vault uses a standard-library construction (HMAC-SHA256 counter-mode keystream, encrypt-then-MAC). Swap in a vetted library (for example `cryptography`) if you accept a dependency.
- A refund of money taken at the counter is manual; no refund percentage is assumed until you set one.
- A booking moved by staff after an online payment keeps its payments; reconciliation flags any difference.
- Not tried against live Razorpay (no keys here): the mock follows the same interface. Test with `rzp_test_` keys before going live.

## 2026-11-01 (production readiness, booking flow, phone OTP)
**Production hardening**
- HTTPS on a real domain: `deploy/Caddyfile` (automatic TLS) or `deploy/nginx.conf` (certbot), systemd units and timers in `deploy/`, and a step-by-step guide with the DNS records in `docs/DEPLOYMENT.md`. `MANTRALAYAM_HTTPS=1` gives Secure cookies and HSTS, `TRUST_PROXY=1` reads the visitor's address from the proxy (from the right, only from a trusted proxy), `ALLOWED_HOSTS` restricts the Host header. The apps still listen on 127.0.0.1 only.
- Rate limits are now stored in the database (`rate_hits`): they survive restarts and are shared by both servers, including the staff login lock-out. New limits for OTP codes, promo-code guessing and the waiting list.
- `GET /health` (and `HEAD`, on both servers) checks the database and the age of the newest backup and answers 200 or 503 (`failed`, `stale` after `HEALTH_BACKUP_MAX_HOURS` = 30 by default, or `none`). The monitor set-up is in the deployment guide.
- Alerts: a failed, stale or missing backup, many rooms on hold at once, and many holds lapsing unbooked open an alert that is shown as a red banner on the staff dashboard (super admins and managers) and emailed once to `STAFF_ALERT_EMAILS`. `python tools/mantralayam_rooms.py tick` (every 5 minutes) evaluates them and runs the waiting list.
- Security baseline: CSRF token on every signed-in POST (staff and guest), Origin check, JSON-only POSTs, input-shape validation on every request, X-Frame-Options, Permissions-Policy, COOP, stricter CSP, and an optional CAPTCHA (`CAPTCHA_PROVIDER=turnstile|mock`) on the OTP request, call-back forms and the waiting list, besides the honeypot.
- `tools/providers.py`: SMS, email and CAPTCHA behind small interfaces with mock modes; `.env` is now read at start.

**Guest booking flow**
- Availability calendar with colour, symbol and text per day, nightly prices and peak dates; peak periods can now raise the price (`surcharge_percent`).
- Price breakdown before confirming (room charge by night, extra beds, children, discount, tax, advance, balance), from `POST /api/public/quote`, and stored with the booking. Tax, advance, extra-bed and child rules are settings (Admin > Settings) and are empty (not charged) until you set them.
- Senior-citizen-friendly (new per-room flag) and ground-floor filters; the booking lands on a matching room.
- Booking for someone else: separate guest and booker names and an arrival contact.
- Promo, donor and automatic pilgrim group-rate codes (Admin > Promo codes) with usage limits, per-mobile limits, expiry, minimums and "not on peak dates".
- Date change for an existing booking with repricing and an availability check (guest site and My bookings).
- Waiting list ("Notify me") for sold-out dates; guests are told by SMS (and email) when a cancellation, a released block or a date change frees a stay.

**Phone OTP and My bookings**
- A booking needs an SMS-verified mobile number (5-minute codes, 5 tries, resend limits). My bookings (`#/account`) signs in with a code to view, change dates, edit details, cancel, download a printable receipt, and manage opt-in saved details (name, email, ID type; the ID number is never saved).

**Schema (all additive, by `migrate()`):** tables `rate_hits`, `alerts`, `promo_codes`, `otp_codes`, `guest_sessions`, `guest_profiles`, `waitlist`; columns on `rooms` (`senior_friendly`), `peak_periods` (`surcharge_percent`) and `bookings` (`children`, `extra_beds`, `tax_amount`, `discount_amount`, `promo_code`, `advance_due`, `booker_name`, `arrival_contact_name`, `arrival_contact_phone`, `price_json`). Existing data is kept. Restart both servers after pulling this change.

**Decisions to check**
- "Unpaid or expiring holds" was read as the 10-minute room holds (an unpaid reservation): the alert fires when many rooms are held at once, or many holds lapse unbooked. Nothing is paid online yet.
- The advance in the price breakdown is informational: the property collects it. Online payment (Razorpay) is still not built.
- The ID number is deliberately not saved for repeat visitors.
- Staff rescheduling still sets amount = tariff x nights and clears the online breakdown.
- Receipts are printable HTML (save as PDF from the print dialog), and say they are not a tax invoice.

**To configure (nothing was invented):** in production `SMS_PROVIDER=http` with your gateway (and your SMS template/sender registration), `SMTP_*` and `STAFF_ALERT_EMAILS`, `STAFF_URL`, `ALLOWED_HOSTS`; schedule the daily backup and the 5-minute `tick` (timers are provided); set the real tax %, advance %, extra-bed and child prices in Admin > Settings (check the GST treatment of your tariffs); flag senior-friendly rooms in Admin > Rooms; create promo codes and any group rate; register the DNS records. Texts in Telugu, Kannada, Tamil and Hindi for the new features are `TODO` (English shows) and need native-speaker translation; the SMS and receipt texts are `sms.*` and `receipt.*` in `web/locales`.

**Tests:** new `tools/test_mantralayam_features.py` (production hardening, calendar, pricing, filters, promo, date change, waiting list, OTP, My bookings, an HTTP-level check of headers and cookies); new staff tests for promo admin, pricing settings, banner and the waiting-list hook; the browser flow was also run end to end in headless Chrome.

## 2026-11-01 (language, accessibility, Call me back, mobile pass)
- **Fonts now actually load.** The Content-Security-Policy blocked the Google Fonts stylesheet and font files, so Lora and the Noto fonts were never applied. `style-src` and `font-src` now allow `fonts.googleapis.com` and `fonts.gstatic.com`.
- Language switcher (English, Telugu, Kannada, Tamil, Hindi) in the menu. Every guest-page string and every guest-facing API error is translated through `web/locales` (290 keys); dates, times and money follow the language. The first visit uses the browser's language; the choice is remembered. The four Indian-language texts are Claude drafts and need review by native speakers.
- The larger-text and high-contrast buttons and the bar above the header were added and then removed at the owner's request; the language switcher is back in the menu.
- **Call me back:** a floating button on every page and a card on the home page open a short form (name, mobile, best time to call). It uses the existing call-back queue; staff see the best time and the visitor's language. `callback_requests` was rebuilt by `migrate()` so people and dates may be empty (every existing row is kept) and gained `call_time` and `lang`.
- Mobile pass on the guest site: tap targets at least 44px, no sideways scroll at 320, 360 and 390px (checked in headless Edge), full-screen dialogs on phones, optional fields (email, arrival time, darshan date, requests, and the building and room-type filters) folded away.
- To do by you: have the Telugu, Kannada, Tamil and Hindi texts reviewed; restart the guest and staff servers (they are still running old code); decide whether to self-host the fonts.

## 2026-11-01 (content pages)
- New guest pages, all read from the database and translatable: Cancellation and refunds, Know before you go, Directions and map, Pilgrim information, Contact and emergency, FAQ, Privacy policy. They are in a "Plan your visit" menu in the header and in the footer.
- Booking step: an "official booking site" notice and a cancellation and refund summary with the exact cancel-by time, computed from the same cutoff the server enforces.
- Refund percentage and refund days are new optional settings (Admin > Settings, empty until decided). Booking views and online cancellations carry them, and `refund_for()` computes the refund (half up to the paisa) for any amount marked paid. Nothing is charged online today, so the amount is 0 until a payment method exists.
- Site info now covers 27 items, validated by kind (phone, WhatsApp, email, https links, map embeds from Google Maps or OpenStreetMap only). New FAQ tables `faqs` and `faq_text` (added by `migrate()`), Admin > FAQ, and `POST /api/admin/faqs`. Call-back notes seed inactive FAQ drafts when the FAQ tables are first created (this database has none).
- Not done: the new pages' text can be translated in `web/locales` and Admin, but every non-English value is still `TODO`.
- To configure: fill in Admin > Site info (map embed, contacts, timings, policies, privacy text, official site address), add FAQs, and decide the refund percentage and days. Nothing is invented: until you do, each page shows only what is set, or "This page is being prepared".

## 2026-11-01
- Font: the whole site now uses Lora (`web/tokens.css`, `web/shared.css`, `web/kolam-header.html`).
- i18n layer for the guest page: `web/i18n.js`, `web/locales/{en,te,kn,ta,hi}.json`, language selector in the header, `tools/i18n_sync.py`, `tools/test_i18n.py`. English is the source; other languages show `TODO` until translated and fall back to English.
- Site config: new `site_config` table (added by `migrate()`, existing data untouched), `site` in `GET /api/public/info?lang=xx`, and Admin > Site info in the staff app (`POST /api/admin/site_config`, super admin only, audited). Items with no value or a `TODO` value are never shown to guests.
- `.env.example`, `README.md`, and the project summary and standing rules in `CLAUDE.md`.
- Still English only: the views built in `web/guest.html` scripts and the server's error messages (see the i18n backlog in `CLAUDE.md`).
- To configure: fill in Admin > Site info; add real translations to `web/locales/*.json` (replace the `TODO` values). Nothing here needs credentials.
