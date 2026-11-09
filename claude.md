# Agent Instructions

You're working inside the **WAT framework** (Workflows, Agents, Tools). This architecture separates concerns so that probabilistic AI handles reasoning while deterministic code handles execution. That separation is what makes this system reliable.

## The WAT Architecture

**Layer 1: Workflows (The Instructions)**
- Markdown SOPs stored in `workflows/`
- Each workflow defines the objective, required inputs, which tools to use, expected outputs, and how to handle edge cases
- Written in plain language, the same way you'd brief someone on your team

**Layer 2: Agents (The Decision-Maker)**
- This is your role. You're responsible for intelligent coordination.
- Read the relevant workflow, run tools in the correct sequence, handle failures gracefully, and ask clarifying questions when needed
- You connect intent to execution without trying to do everything yourself
- Example: If you need to pull data from a website, don't attempt it directly. Read `workflows/scrape_website.md`, figure out the required inputs, then execute `tools/scrape_single_site.py`

**Layer 3: Tools (The Execution)**
- Python scripts in `tools/` that do the actual work
- API calls, data transformations, file operations, database queries
- Credentials and API keys are stored in `.env`
- These scripts are consistent, testable, and fast

**Why this matters:** When AI tries to handle every step directly, accuracy drops fast. If each step is 90% accurate, you're down to 59% success after just five steps. By offloading execution to deterministic scripts, you stay focused on orchestration and decision-making where you excel.

## How to Operate

**1. Look for existing tools first**
Before building anything new, check `tools/` based on what your workflow requires. Only create new scripts when nothing exists for that task.

**2. Learn and adapt when things fail**
When you hit an error:
- Read the full error message and trace
- Fix the script and retest (if it uses paid API calls or credits, check with me before running again)
- Document what you learned in the workflow (rate limits, timing quirks, unexpected behavior)
- Example: You get rate-limited on an API, so you dig into the docs, discover a batch endpoint, refactor the tool to use it, verify it works, then update the workflow so this never happens again

**3. Keep workflows current**
Workflows should evolve as you learn. When you find better methods, discover constraints, or encounter recurring issues, update the workflow. That said, don't create or overwrite workflows without asking unless I explicitly tell you to. These are your instructions and need to be preserved and refined, not tossed after one use.

## The Self-Improvement Loop

Every failure is a chance to make the system stronger:
1. Identify what broke
2. Fix the tool
3. Verify the fix works
4. Update the workflow with the new approach
5. Move on with a more robust system

This loop is how the framework improves over time.

## File Structure

**What goes where:**
- **Deliverables**: Final outputs go to cloud services (Google Sheets, Slides, etc.) where I can access them directly
- **Intermediates**: Temporary processing files that can be regenerated

**Directory layout:**
```
.tmp/           # Temporary files (scraped data, intermediate exports). Regenerated as needed.
tools/          # Python scripts for deterministic execution
workflows/      # Markdown SOPs defining what to do and how
.env            # API keys and environment variables (NEVER store secrets anywhere else)
credentials.json, token.json  # Google OAuth (gitignored)
```

**Core principle:** Local files are just for processing. Anything I need to see or use lives in cloud services. Everything in `.tmp/` is disposable.

## Bottom Line

You sit between what I want (workflows) and what actually gets done (tools). Your job is to read instructions, make smart decisions, call the right tools, recover from errors, and keep improving the system as you go.

Stay pragmatic. Stay reliable. Keep learning.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

## Project summary: Mantralayam Rooms

**Stack.** Python standard library only (`http.server`, `sqlite3`), no framework or build step. Two servers share one SQLite file (`.tmp/mantralayam.db`, override with `MANTRALAYAM_DB`): the guest site and public API (`tools/mantralayam_rooms.py`, port 8001) and the staff app (`tools/mantralayam_staff.py`, port 8002, imports the guest module as `g`). Front ends are single HTML files (`web/guest.html`, `web/staff.html`) with plain JS and shared CSS (`tokens.css`, `shared.css`). Font: Lora. Run both with `python tools/run_all.py`. Both listen on 127.0.0.1 only; HTTPS comes from a reverse proxy (`deploy/Caddyfile`, `docs/DEPLOYMENT.md`). Provider interfaces (SMS, email, CAPTCHA) are in `tools/providers.py`, which also reads `.env`. The `.venv` points at a missing Python 3.14; use any working Python 3.12+.

**Data model (SQLite).** `properties` (guest houses), `room_types` (name, capacity, tariff), `rooms` (per property, number, floor, type, capacity), `bookings` (one row per room; rooms of one booking share `group_id`, which is the booking ID guests see; guest and ID details, dates, status, source, payment fields, `arrival_time`, `darshan_date`), `room_status_events` (blocks, maintenance, cleaning), `holds`, `peak_periods`, `callback_requests`, `settings` (key/value; includes the optional `refund_percent` and `refund_days`), `site_config` (key, lang, value), `faqs` and `faq_text` (question and answer per language), `users`, `roles`, `user_properties`, `audit_log`. Dates are ISO text. `migrate()` in the guest module adds missing columns and tables, once per process and database.

**Routes.** Guest, all JSON, dispatched through `ROUTES` and `dispatch()`: `GET /health`, `/api/public/info`, `/api/public/search`; `POST /api/public/hold`, `release`, `book`, `callback`, `booking/lookup`, `booking/cancel`. Static: `/`, `/shared.css`, `/tokens.css`, `/i18n.js`, `/locales/<lang>.json`, `/images/<file>`. Staff: session-cookie login, then role-checked `/api/...` routes (dashboard, rooms, bookings with check-in/out/edit/reschedule, calendar, guests, callbacks, reports, notifications, rates, audit, and `/api/admin/*` for super admin).

**Bookings.** `book()` validates the stay against settings and peak periods, then checks free rooms and inserts inside one `BEGIN IMMEDIATE` transaction (`write()`), so a double booking is impossible. A room is free when no `Confirmed` or `Checked-In` booking overlaps, no status event blocks it, and no other guest holds it. Online bookings are `source='online'`, pay at the counter, amount = tariff x nights. A guest views or cancels with booking ID plus mobile number; cancelling is allowed until the cutoff (default 24 hours before check-in) and needs a reason. Every online create and cancel writes `audit_log`.

**Holds.** When the guest opens the confirm dialog, `hold()` reserves the chosen rooms for 10 minutes (`holds` table). A hold blocks everyone except its own token; `book()` consumes it. A re-hold replaces the old token, `release` frees it, expired rows are swept on the next hold, one IP may keep at most 3. The booking transaction still decides every race.

**Call-backs.** `callback()` stores a `callback_requests` row (status `new`, `contacted`, `closed`) behind a honeypot and throttles. Two kinds share the queue: the long form for a big group or a sold-out search (people and dates), and the floating **Call me back** button (name, mobile, best time `call_time`, and the visitor's language `lang`; people and dates are empty). Staff work the queue in the Call-backs page, which shows best time and language.

**Rate limiting.** Stored in the database (`rate_hits`), so limits survive restarts and cover both servers: `throttle()` / `rate()` open their own connection, so never call them inside `write()`. 60 searches per minute, 10 holds, 10 bookings and 20 lookups, cancels or date changes per 10 minutes per IP, plus 5 bookings and 3 call-backs per 10 minutes per mobile number, and the OTP, promo-guess and waiting-list limits. A 429 carries `retry_after` and the `Retry-After` header. Forms also have a hidden honeypot field and an optional CAPTCHA (`CAPTCHA_PROVIDER`). Staff login failures are throttled separately (also in the database).

**Backups.** `python tools/mantralayam_rooms.py backup` uses the SQLite online backup API, runs `PRAGMA integrity_check` on the copy, only then replaces `backups/mantralayam-YYYY-MM-DD-HHMM.db`, keeps the newest 14, and logs to `backups/backup.log`. Exit code 1 on failure. The staff dashboard shows the last result. Schedule it daily.

**Production hardening.** `MANTRALAYAM_HTTPS=1` gives Secure cookies and HSTS, `TRUST_PROXY=1` reads the visitor's address from the proxy, `ALLOWED_HOSTS` limits the Host header. Every POST needs JSON, a same-site `Origin`, and (signed in) the `X-CSRF-Token` derived from the session; `clean_body()` checks the shape of all input. `GET /health` returns 200 or 503 (database and backup recency). `alerts` rows (failed/stale backup, rooms held too long, many lapsed holds) show as a banner on the staff dashboard and are emailed once to `STAFF_ALERT_EMAILS`. `python tools/mantralayam_rooms.py tick` runs the alerts and the waiting list every 5 minutes.

**Booking flow.** `GET /api/public/calendar` (colour-coded availability, per-night prices, peak-aware), `POST /api/public/quote` (price breakdown: room charge by nightly price, extra beds, children, one discount, tax, advance; all rules are settings and empty means not charged), senior-friendly (`rooms.senior_friendly`) and ground-floor filters, booking on behalf (`booker_name`, arrival contact), promo/donor codes and automatic group rates (`promo_codes`, one discount per booking, usage limits, expiry, peak exclusion), guest date change with repricing (`/api/public/booking/change`), waiting list (`waitlist`, `waitlist_tick()`). `book()` stores the breakdown (`amount` is the grand total, `price_json` on the first row).

**Phone OTP and accounts.** Booking needs an SMS-verified mobile number (`otp_proof`, one use, consumed in the booking transaction; `BOOKING_OTP_REQUIRED=0` only until SMS works). The mock SMS provider prints codes to the console in development only; the guest app refuses to start with it when `MANTRALAYAM_HTTPS=1`. "My bookings" (`#/account`) signs in with a code (cookie `guest_session`, `ME_ROUTES`, CSRF header) to view, change dates, edit details, cancel, download a printable receipt, and manage opt-in saved details (name, email, ID type; the ID number is never saved). Server-side texts (SMS, receipt) come from `web/locales` (`sms.*`, `receipt.*`).

**Site config and i18n.** `SITE_CONFIG` (guest module) lists the editable facts (contacts, policies, know-before-you-go, directions and map, pilgrim timings and nearby places, privacy text); `site_check()` validates phone, WhatsApp, email and link items, and map embeds may only come from Google Maps or OpenStreetMap. Values live in `site_config`; a missing or `TODO` value is not shown to guests. Super admins edit them in Admin > Site info, and FAQs in Admin > FAQ (call-back notes seed inactive drafts with a `TODO` answer when the FAQ tables are first created). The refund percentage and days are set in Admin > Settings and stay empty until decided; the cancellation cutoff is the same setting the server enforces. `GET /api/public/info?lang=xx` returns the set values, the active FAQs and the refund settings, with English fallback per key. The guest content pages (`#/policy`, `#/know`, `#/directions`, `#/pilgrim`, `#/contact`, `#/faq`, `#/privacy`) only render what that response holds; an unset item is simply absent. UI text lives in `web/locales/<lang>.json`; `web/i18n.js` provides `t()` and `data-i18n` attributes. Run `python tools/i18n_sync.py` after changing `en.json`; `sms.*` and `receipt.*` keys are used by the server, not the page. In `guest.html`, never name a variable `t` (the room type is `rt`). The visitor's language is remembered in `localStorage` (`lang`). Guest API errors are English text; the page matches each one to an `err.*` template in the locale files (`errText()`), so every new guest-facing `UserError` needs an `err.*` key (`test_i18n.py` fails otherwise). Mobile rules for the guest site: tap targets at least 44px, no sideways scroll at 320px, optional form fields folded into `<details>`.

**i18n status.** All guest-page text and the API's guest-facing errors go through `web/locales`. Dates, times and money use the visitor's locale (`en-IN`, `te-IN`, `kn-IN`, `ta-IN`, `hi-IN`). The Telugu, Kannada, Tamil and Hindi texts were drafted by Claude and have not been reviewed by native speakers: have each checked before relying on them. The staff app is English only. Fonts: Lora plus Noto Serif per Indic script from Google Fonts; `CSP` in the guest module allows `fonts.googleapis.com` and `fonts.gstatic.com` for that (self-hosting the font files would remove the dependency).

## Standing rules

- Never hard-code secrets. Use environment variables and keep `.env.example` up to date.
- External services (Razorpay, SMS/WhatsApp, SMTP, Google Sheets, maps) go behind a provider interface with a mock/sandbox mode, so everything runs locally without credentials.
- All schema changes use migrations (extend `migrate()`) and keep existing data intact.
- Build mobile-first and accessible: labels, contrast, keyboard use.
- All guest-facing text goes through the i18n layer (English, Telugu, Kannada, Tamil, Hindi). English is the source; mark missing translations `TODO`.
- Never invent facts such as temple timings, distances, phone numbers or policy numbers. Put them in the site config (Admin > Site info) as clearly marked `TODO` placeholders.
- Write tests for money, booking-state and permission logic. Never log OTPs, card data or ID numbers.
- After each task: run the app and the tests, update the README and CHANGELOG, and summarize what changed and what the user must configure.
