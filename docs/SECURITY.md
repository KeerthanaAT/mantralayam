# Security notes and incident runbook

Written after the security audit of November 2026. The controls below are tested in `tools/test_security.py`. Deployment steps are in `DEPLOYMENT.md`.

## What protects what

| Risk | Control |
|---|---|
| SMS-pumping (someone makes the site text many numbers) | Codes go to Indian (+91) numbers only (`SMS_ALLOWED_PREFIXES`); per address, per number and **site-wide** limits (`SMS_HOURLY_LIMIT`, `SMS_DAILY_LIMIT`) with a staff alert; optional CAPTCHA (turn it on) |
| Guessing booking IDs to read or cancel other guests' bookings | Booking ID + mobile number, wrong pairs counted **per mobile number across all addresses** (`LOOKUP_FAIL_LIMIT`); "My bookings" with an SMS code is the safer route |
| Blocking rooms with holds | 3 holds per visitor (an IPv6 visitor is one /64 block), 10-minute holds, and no new hold while `HOLD_GLOBAL_PERCENT` of all rooms are held; alert on hoarding |
| Slow-client attacks | 15-second socket timeout, 200-connection cap per server, body length checks, proxy timeouts (Caddy/nginx files) |
| Stolen database or backup | Only the last 4 characters of an ID are kept at booking; the full number is entered at the front desk and stored **encrypted** (scrypt-stretched key, `ID_ENCRYPTION_KEY`, 32+ characters, kept outside the database); backups are mode 600 in a mode 700 folder and must be **encrypted before they leave the server**; retention settings erase old data |
| Staff passwords | PBKDF2-HMAC-SHA256, 600,000 rounds (older hashes upgrade at next sign-in), 10+ characters, limits per username, per address and per username across addresses, alert on repeated failures, sessions end 12 hours after sign-in |
| Stolen staff password | Managers and super admins sign in with an authenticator-app code (`tools/totp.py`, RFC 6238), each code works once; the staff app is meant to sit behind a VPN or an address allowlist |
| A manager changing prices for other properties | Room types and tariffs are shared by all properties: super admin only |
| Script injection | The page builds its HTML with text nodes only (no `innerHTML`), and the CSP allows scripts only with a per-response nonce (no `unsafe-inline` for scripts) |
| Cross-site requests | POST must be JSON, carry this site's exact `Origin` (required with HTTPS), and signed-in POSTs carry the CSRF token |

## Known limits (decide or accept)

- **Booking IDs are sequential** (the first room's row number). The per-mobile failure limit makes scanning slow, but a random public reference would be better. This changes what guests see and quote, so it is left for a decision.
- Cancelling or changing by booking ID + mobile does not need an SMS code. Requiring one would match "My bookings" but adds a step for guests who phone the property.
- `style-src` still allows `unsafe-inline` (the page sets inline styles); script injection is blocked, style injection is not.
- The guest page keeps the booking ID and mobile number in `sessionStorage` after a booking so a page refresh still shows the confirmation. It is cleared when the tab closes.
- Staff sessions live in memory: restarting the staff app signs everyone out.
- The ID cipher is built from the standard library (HMAC-SHA256 counter mode, encrypt-then-MAC). It is sound, but a reviewed library (`cryptography`: AES-GCM or Fernet) would be preferable if installing one is acceptable.
- The Telugu, Kannada, Tamil and Hindi texts for the new messages are TODO (English is shown).

## Keys and secrets: where they live and how to rotate them

| Secret | Where | Rotating |
|---|---|---|
| `ID_ENCRYPTION_KEY` | `/etc/mantralayam/env` (root + service user only) | Stored IDs, staff two-factor secrets and pending payments are encrypted with it. To change it: keep the old value, add the new one, and re-encrypt (a `rekey` for a changed key is not built: ask before rotating). Never lose it |
| `APP_SECRET` | same | Changing it invalidates check-in QR codes already sent to guests (they can show the booking ID instead) |
| SMS / SMTP / Razorpay / CAPTCHA keys | same | Rotate at the provider, update the file, `systemctl restart mantralayam@rooms mantralayam@staff` |
| Staff passwords | database (hashed) | `python3 tools/mantralayam_staff.py set-password USER`, or Admin > Users |
| Staff two-factor secrets | database (encrypted) | `python3 tools/mantralayam_staff.py reset-totp USER` |

## Incident runbook

**1. Contain (first 30 minutes).** Write down the time and what you saw. Do not delete logs.
- Suspected staff account: Admin > Users > deactivate it (this ends its sessions), then reset its password and two-factor.
- Suspected leaked server secret: rotate it (table above) and restart both services.
- Active abuse of the code form: lower `SMS_HOURLY_LIMIT` / `SMS_DAILY_LIMIT`, turn on the CAPTCHA, or temporarily set `BOOKING_OTP_REQUIRED` back only if SMS is the problem. Block the address at the proxy.
- Suspected stolen backup or server disk: treat every guest's name, mobile number and booking history as exposed; the stored full ID numbers and photos are encrypted, and are exposed only if `ID_ENCRYPTION_KEY` was exposed too.

**2. Find out what happened.** Staff Audit log (filter "sensitive"), `journalctl -u mantralayam@rooms -u mantralayam@staff`, the proxy log, and the alerts on the dashboard. Look for `auth.login_failed` bursts, `id.reveal`, `report.export`, `guest.*` and `admin.*` entries.

**3. Tell the right people.** Under the Digital Personal Data Protection Act, 2023, a personal-data breach must be reported to the Data Protection Board of India and to each affected person, in the form and time the Rules prescribe. **The deadlines and the form are set in the Rules: confirm them with your legal adviser now and write them here**: _TODO (owner)_. Keep a record of: what data, how many people, when it happened and was found, what you did.

**4. Recover.** Restore from the last good backup if data was changed (`DEPLOYMENT.md`, step 10); change every secret that could have been seen; then review the controls above and write down what failed.

**5. Afterwards.** Add a regression test for the cause, update this page and the CHANGELOG.

## Requests from guests (DPDP)

- *Delete my data*: Admin (super admin) erase by mobile number, or `python3 tools/mantralayam_rooms.py erase <10-digit mobile> <reason>`. Bookings, dates and amounts stay without names, numbers or IDs (accounts need them). It refuses while the guest has a stay that is not over.
- *Delete my saved details*: the guest does it in "My bookings".
- *Retention*: set the periods in Admin > Settings. Until they are set, nothing is erased automatically.
- *Privacy text and contact*: Admin > Site info (`privacy_policy`, `privacy_contact`): still TODO until the owner supplies them.
