# Deploying Mantralayam Rooms on a VPS (HTTPS, real domain)

This guide takes the app from your computer to a public site at `https://rooms.<your-domain>` (guests) and `https://staff.<your-domain>` (staff). It assumes a small Ubuntu 22.04/24.04 or Debian 12 VPS with Python 3.12 or newer, and one SQLite database file. Nothing here needs a third-party Python package.

Replace `example.org` everywhere with your domain.

## 0. What you need before you start

| Item | Notes |
|---|---|
| A domain name | Bought from any registrar. You only need to be able to edit its DNS records. |
| A VPS | 1 vCPU / 1 GB RAM is plenty. Note its public IPv4 (and IPv6 if it has one). |
| An SMS provider | Guests receive a code by SMS before a booking is confirmed (step 8). In India, bulk SMS usually needs sender-ID and template registration (DLT) with the provider: ask them early, it can take days. |
| An email account for alerts | Any SMTP account (step 8). Optional but recommended. |

## 1. Decide the two host names

- Guest site: `rooms.example.org`
- Staff app: `staff.example.org`

Two names keep the staff login on its own origin, with its own cookie.

## 2. DNS steps (do this first: it can take minutes to hours to spread)

1. Log in to your registrar's (or DNS host's) DNS page for `example.org`.
2. Add these records. Use the VPS addresses from your VPS provider's dashboard.

   | Type | Name | Value | TTL |
   |---|---|---|---|
   | `A` | `rooms` | VPS IPv4 address | 300 |
   | `A` | `staff` | VPS IPv4 address | 300 |
   | `AAAA` | `rooms` | VPS IPv6 address (only if the VPS has one and you configured it) | 300 |
   | `AAAA` | `staff` | VPS IPv6 address (same condition) | 300 |
   | `CAA` (optional) | `@` | `0 issue "letsencrypt.org"` | 3600 |

   A low TTL (300 seconds) now lets you fix a mistake quickly; raise it to 3600 later. If the registrar's form wants the full name, write `rooms.example.org`. Delete any old `A`/`AAAA`/`CNAME` record with the same name, and do not add an `AAAA` record you have not tested: a wrong one breaks HTTPS for everyone on IPv6.
3. If you want `www.rooms.example.org` to work too, add an `A` record for `www.rooms` and enable the `redir` block at the bottom of `deploy/Caddyfile`.
4. Check that the names point at your server (from your own computer):

   ```
   dig +short rooms.example.org
   dig +short staff.example.org
   ```

   (Windows: `nslookup rooms.example.org`.) Both must print the VPS address. If they print nothing or an old address, wait and retry; the certificate in step 6 only works once this is right.

## 3. Prepare the server

```
ssh root@<vps-ip>
adduser --disabled-password --gecos "" deploy && usermod -aG sudo deploy      # day-to-day admin user (add your SSH key to ~deploy/.ssh/authorized_keys)
apt update && apt -y upgrade
apt -y install python3 git ufw sqlite3 debian-keyring debian-archive-keyring apt-transport-https curl
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw enable      # only SSH, HTTP (certificate renewal + redirect) and HTTPS are open
python3 --version                                                             # must be 3.12 or newer
```

Create the service user and folders:

```
useradd --system --home /opt/mantralayam-rooms --shell /usr/sbin/nologin mantralayam
mkdir -p /opt/mantralayam-rooms /var/lib/mantralayam /var/backups/mantralayam /etc/mantralayam
chown mantralayam: /var/lib/mantralayam /var/backups/mantralayam
```

## 4. Install the app and the database

```
git clone <your repository url> /opt/mantralayam-rooms         # or copy the project folder with scp/rsync
chown -R root:root /opt/mantralayam-rooms                       # the service can read the code but not change it
```

Copy your database from your computer (stop the local servers first, or use a backup file):

```
scp backups/mantralayam-<latest>.db deploy@<vps-ip>:/tmp/mantralayam.db
sudo install -o mantralayam -g mantralayam -m 600 /tmp/mantralayam.db /var/lib/mantralayam/mantralayam.db && rm /tmp/mantralayam.db
```

Staff accounts live in the database. Set a strong password for each real user on the server (it asks twice and never echoes):

```
sudo -u mantralayam MANTRALAYAM_DB=/var/lib/mantralayam/mantralayam.db python3 /opt/mantralayam-rooms/tools/mantralayam_staff.py set-password admin
```

Do this for every account, and remove or deactivate any test accounts in Admin > Users.

## 5. Configure: `/etc/mantralayam/env`

```
sudo install -o root -g mantralayam -m 640 /dev/null /etc/mantralayam/env
sudo nano /etc/mantralayam/env
```

Minimum production settings (the complete list is in `.env.example` and at the end of this guide):

```
MANTRALAYAM_DB=/var/lib/mantralayam/mantralayam.db
MANTRALAYAM_BACKUPS=/var/backups/mantralayam
MANTRALAYAM_HTTPS=1                       # Secure cookies + HSTS, and the apps refuse to start with the mock SMS provider
TRUST_PROXY=1                             # read the visitor's address from the proxy (Caddy/nginx, on 127.0.0.1)
ALLOWED_HOSTS=rooms.example.org,staff.example.org
STAFF_URL=https://staff.example.org/staff
SMS_PROVIDER=http
SMS_HTTP_URL=https://<your SMS gateway's send endpoint>
SMS_HTTP_TOKEN=<your gateway token>
EMAIL_PROVIDER=smtp
SMTP_HOST=smtp.example.org
SMTP_PORT=587
SMTP_USER=alerts@example.org
SMTP_PASSWORD=<password>
SMTP_FROM=Mantralayam Rooms <alerts@example.org>
STAFF_ALERT_EMAILS=owner@example.org,manager@example.org
PUBLIC_URL=https://rooms.example.org      # used in feedback, unsubscribe and email-confirmation links in messages
ID_ENCRYPTION_KEY=<32+ random characters, for example from: python3 -c "import secrets; print(secrets.token_urlsafe(32))">   # REQUIRED: the apps refuse to start without it (or with a shorter one)
APP_SECRET=<another 32+ random characters>                # REQUIRED: signs the check-in QR codes; without it the secret would sit inside every backup
SMS_HOURLY_LIMIT=60                       # most code SMS the whole site may send per hour / per day: the cap on what an SMS-pumping attack can cost
SMS_DAILY_LIMIT=300
CAPTCHA_PROVIDER=turnstile                # strongly advised (step 8): the best defence against SMS pumping and room hoarding
CAPTCHA_SITE_KEY=<site key>
CAPTCHA_SECRET=<secret>
# Online payment (optional). Start with test keys (rzp_test_...) and switch to live keys only after a test booking and refund work:
PAYMENT_PROVIDER=razorpay
RAZORPAY_KEY_ID=rzp_test_xxxxxxxx
RAZORPAY_KEY_SECRET=<secret>
RAZORPAY_WEBHOOK_SECRET=<the secret you set for the webhook>
```

Back up `ID_ENCRYPTION_KEY` separately from the database backups: without it the stored guest IDs cannot be read. In the Razorpay dashboard add the webhook `https://rooms.example.org/api/public/payment/webhook` (the guest host) with the same secret and the events `payment.captured`, `order.paid`, `payment.failed`, `refund.processed`, `refund.failed`. Then, as a super admin, tick the payment options and set the advance and refund percentages in Admin > Settings. The 5-minute timer also expires unpaid payments, retries refunds, sends arrival reminders and erases IDs after the retention period.

Never commit this file, and do not paste real secrets into chat, tickets or the repository.

## 6. Run the apps and the reverse proxy

```
sudo cp deploy/mantralayam.service /etc/systemd/system/mantralayam@.service
sudo cp deploy/mantralayam-tick.service deploy/mantralayam-tick.timer deploy/mantralayam-backup.service deploy/mantralayam-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mantralayam@rooms mantralayam@staff mantralayam-tick.timer mantralayam-backup.timer
sudo systemctl status mantralayam@rooms mantralayam@staff           # both "active (running)"
curl -s http://127.0.0.1:8001/health                                # JSON, see step 7
```

Install Caddy (automatic HTTPS) from <https://caddyserver.com/docs/install#debian-ubuntu-raspbian>, then:

```
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo nano /etc/caddy/Caddyfile       # put in your two host names
caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

On the first request Caddy obtains the certificates by itself (it needs ports 80 and 443 open and the DNS from step 2). Prefer nginx? Use `deploy/nginx.conf` and `certbot --nginx` instead (instructions at the top of that file).

Take the first backup now and check it:

```
sudo systemctl start mantralayam-backup.service && ls -l /var/backups/mantralayam && sudo journalctl -u mantralayam-backup -n 5
```

## 6b. Staff access: VPN and two-factor sign-in

**Keep the staff app off the open internet.** Install Tailscale (or WireGuard) on the server and on the phones and computers of the people who work at the property, and let only the VPN's addresses reach `staff.example.org`:

- Caddy: in `deploy/Caddyfile`, the `staff.example.org` block has `@outside not remote_ip ...`. Replace the example ranges with your VPN range (Tailscale: `100.64.0.0/10`) and, if you like, the office's fixed address. nginx: the `allow`/`deny all` lines in `deploy/nginx.conf`.
- Optionally also close the staff name in DNS to the VPN's private address (Tailscale MagicDNS) so it does not resolve publicly at all.
- Test from a phone that is NOT on the VPN: `https://staff.example.org/` must answer 403.

**Two-factor sign-in.** With `MANTRALAYAM_HTTPS=1`, managers and super admins must set up an authenticator app (Google Authenticator, Microsoft Authenticator, Aegis, FreeOTP) the first time they sign in after this update: Settings > Account security > Get a key. After that every sign-in asks for the 6-digit code. Set `TOTP_REQUIRED=0` only to postpone it. If someone loses their phone: Admin > Users > edit > reset (API field `reset_totp`), or on the server `python3 tools/mantralayam_staff.py reset-totp USERNAME`. Do the set-up for the super admin first, in a quiet moment, and keep a second super admin account for emergencies.

## 7. Check that it is right

```
curl -I https://rooms.example.org/                       # 200, with strict-transport-security, content-security-policy, x-frame-options
curl -s https://rooms.example.org/health                 # {"status": "ok", ...}
curl -I http://rooms.example.org/                        # 308 redirect to https
```

- Open both sites in a browser and sign in to the staff app: the padlock must be closed, and the cookie must show `Secure` and `HttpOnly` in the browser's developer tools (Application > Cookies).
- Make a test booking from your phone: the SMS code must arrive (step 8) and the booking must be confirmed. Cancel it afterwards.
- From a device that is not on the VPN, `curl -I https://staff.example.org/` must give 403.
- `sudo -u mantralayam python3 tools/mantralayam_rooms.py serve` with a missing or short `ID_ENCRYPTION_KEY`, no `APP_SECRET` or no `ALLOWED_HOSTS` must refuse to start and say why.
- Run an external check such as <https://www.ssllabs.com/ssltest/> and <https://securityheaders.com/>.
- HSTS tells browsers to use HTTPS only for a year. Do not add the domain to the browser preload list until you are sure every subdomain will always have HTTPS.

## 8. SMS codes, alert emails and the optional CAPTCHA

**SMS (required for booking).** Guests must verify their mobile number before a booking is confirmed, and "My bookings" signs in with a code. `SMS_PROVIDER=http` posts `{"to": "<mobile>", "message": "<text>", "sender": "<SMS_SENDER_ID>"}` as JSON with `Authorization: Bearer <SMS_HTTP_TOKEN>` to `SMS_HTTP_URL`. If your gateway expects a different shape, adapt `HttpSms.send` in `tools/providers.py` (it is about ten lines); the rest of the app only calls `sms().send(phone, text)`. The message texts are `sms.otp` and `sms.waitlist` in `web/locales/*.json`; if your provider needs registered templates, register those texts exactly (the placeholders are `{code}`, `{minutes}`, `{property}`, `{check_in}`, `{check_out}`, `{link}`). Until SMS works you can set `BOOKING_OTP_REQUIRED=0` to let bookings through without a code; switch it back on as soon as SMS works, because it is the main protection against fake bookings. In development, `SMS_PROVIDER=mock` prints the code to the console.

**Email alerts.** `STAFF_ALERT_EMAILS` receive one email per new alert (a failed or missing backup, many rooms on hold, many holds lapsing). With no address configured, alerts still appear as a red banner on the staff dashboard (managers and the super admin).

**CAPTCHA (optional, off by default).** Cloudflare Turnstile is free: create a widget at <https://dash.cloudflare.com/?to=/:account/turnstile> for your guest host name, then set `CAPTCHA_PROVIDER=turnstile`, `CAPTCHA_SITE_KEY=<site key>` and `CAPTCHA_SECRET=<secret key>` and restart. It then guards the OTP request, the Call me back and call-back forms, and the waiting list, in addition to the hidden honeypot field. If Turnstile cannot be reached, those forms are refused (they fail closed); the phone number on the Contact page still works. `CAPTCHA_PROVIDER=mock` shows a tick box for testing.

## 9. Monitoring

**Uptime monitor.** Point any uptime service (UptimeRobot, Better Stack, Pingdom, Healthchecks, or your own) at:

```
https://rooms.example.org/health        check every 5 minutes, GET or HEAD
https://staff.example.org/health        optional, the same check from the staff server
```

- **Healthy:** HTTP `200` and body `{"status":"ok", ...}`.
- **Unhealthy:** HTTP `503` and `"status":"fail"`. The `checks` object says which part failed: `database` (cannot read the database) or `backup` (`failed`, `stale`: older than `HEALTH_BACKUP_MAX_HOURS`, default 30, or `none`: never ran). Nothing else is revealed: no paths or error text.
- Alert on any status other than 200, and on timeouts longer than about 10 seconds. If the service supports it, also match the keyword `"status":"ok"`.
- Add your phone or email as the alert contact, and test it by stopping the service once: `sudo systemctl stop mantralayam@rooms`, wait for the alert, `sudo systemctl start mantralayam@rooms`.
- For development only, `HEALTH_BACKUP_MAX_HOURS=0` turns the backup part of the check off.

**The 5-minute job.** `mantralayam-tick.timer` runs alerts and waiting-list messages. To be told when it stops, create a "heartbeat" check (for example at healthchecks.io, period 5 minutes, grace 10), and put its ping address in `HEARTBEAT_URL`; every successful run pings it.

**Logs.** `journalctl -u mantralayam@rooms -f` (guest), `journalctl -u mantralayam@staff -f`, `journalctl -u caddy -f`. OTP codes and ID numbers are never logged (the mock SMS provider prints codes, and it refuses to run when `MANTRALAYAM_HTTPS=1`).

## 10. Backups off the server

A backup file holds every guest's name and mobile number, so the backup folder is private to the service user (mode 700, files 600) and **a copy that leaves the server must be encrypted first**. For example with [age](https://github.com/FiloSottile/age): `age -r <public key> -o mantralayam-2026-11-04.db.age mantralayam-2026-11-04.db`, keeping the private key somewhere safe and apart from the server (and apart from `ID_ENCRYPTION_KEY`).

The daily backup protects against corruption and mistakes, not against losing the server. Copy `/var/backups/mantralayam` (encrypted, as above) somewhere else every day, for example with `rclone` or `rsync` to another machine or a cloud bucket, from a cron job or a second systemd timer. Test a restore once: stop both services, copy a backup over `/var/lib/mantralayam/mantralayam.db`, start them.

## 11. Updating

```
cd /opt/mantralayam-rooms && sudo git pull
python3 tools/test_i18n.py                     # optional checks; the other test scripts need a local database copy
sudo systemctl restart mantralayam@rooms mantralayam@staff
```

New columns and tables are added automatically on first start (existing data is kept). Take a manual backup before an update: `sudo systemctl start mantralayam-backup.service`.

## 11b. One-time steps after the security update (existing data)

Take a backup first (`sudo systemctl start mantralayam-backup.service`), then, with the same environment as the service (`sudo -u mantralayam env $(cat /etc/mantralayam/env | xargs) ...`):

```
python3 tools/mantralayam_rooms.py scrub-ids   # full ID numbers that old bookings still hold in plain text are moved into the encrypted store and masked to ****1234
python3 tools/mantralayam_rooms.py rekey       # old stored IDs, photos and pending payments are rewritten with the stronger key stretching (same ID_ENCRYPTION_KEY)
```

Both can be repeated safely. Delete the old backups made before `scrub-ids` once the new ones are verified: they still hold full ID numbers.
Decide, with your privacy adviser, the retention periods in Admin > Settings (ID documents, and guest names and mobiles). Nothing is erased until they are set. Erase one guest on request in Admin (API `/api/admin/guest_erase`) or with `python3 tools/mantralayam_rooms.py erase <10-digit mobile> <reason>`.

## 12. Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MANTRALAYAM_DB` | `.tmp/mantralayam.db` | Database file |
| `MANTRALAYAM_BACKUPS` | `backups` | Backup folder |
| `MANTRALAYAM_HTTPS` | off | `1`: Secure cookies, HSTS; refuse to start with the mock SMS provider |
| `TRUST_PROXY` | off | `1`: take the visitor's address (and https) from the proxy's `X-Forwarded-*` headers |
| `TRUSTED_PROXIES` | `127.0.0.1,::1` | Which connecting addresses are trusted proxies |
| `ALLOWED_HOSTS` | any | Comma-separated host names the apps answer to. Required with HTTPS |
| `STAFF_URL` | `http://127.0.0.1:8002/staff` | The staff login address shown in the guest site's menu and footer |
| `BOOKING_OTP_REQUIRED` | `1` | `0`: bookings do not need an SMS code |
| `SMS_PROVIDER` | `mock` | `mock` (prints to the console) or `http` |
| `SMS_HTTP_URL`, `SMS_HTTP_TOKEN`, `SMS_SENDER_ID` | | The SMS gateway |
| `EMAIL_PROVIDER` | `mock` | `mock` (console and `.tmp/outbox.log`) or `smtp` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | | The mail account |
| `STAFF_ALERT_EMAILS` | | Who receives alert emails |
| `CAPTCHA_PROVIDER`, `CAPTCHA_SITE_KEY`, `CAPTCHA_SECRET` | off | Optional CAPTCHA on public forms |
| `HEALTH_BACKUP_MAX_HOURS` | `30` | A backup older than this makes `/health` fail (`0`: off) |
| `HOLD_ALERT_PERCENT` | `25` | Alert when this share of rooms is on hold at once |
| `HOLD_LAPSE_ALERT` | `20` | Alert when this many holds lapse unbooked within an hour |
| `HEARTBEAT_URL` | | Pinged after each successful 5-minute job |
| `STAFF_LEGACY_ROUNDS` | `200000` | Older password hashes only |
| `PAYMENT_PROVIDER` | off | `razorpay` (or `mock` for local tests; refused with HTTPS) |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` | | Razorpay keys and webhook secret |
| `WHATSAPP_PROVIDER`, `WHATSAPP_HTTP_URL`, `WHATSAPP_HTTP_TOKEN`, `WHATSAPP_SENDER` | off | WhatsApp gateway |
| `ID_ENCRYPTION_KEY` | | Required with HTTPS, 32+ random characters: encrypts stored IDs, staff two-factor secrets and pending payment details |
| `APP_SECRET` | database | Signs check-in and feedback links. Required with HTTPS (otherwise the secret is inside every backup) |
| `SMS_HOURLY_LIMIT`, `SMS_DAILY_LIMIT` | `60`, `300` | Most code SMS the whole site sends per hour and per day. Past it nothing is sent and the staff are alerted |
| `SMS_ALLOWED_PREFIXES` | `+91` | Countries whose numbers may receive a code |
| `TOTP_REQUIRED` | `1` with HTTPS, else `0` | Managers and super admins must use two-factor sign-in |
| `LOOKUP_FAIL_LIMIT` | `8` | Wrong booking ID + mobile pairs per mobile number per hour |
| `HOLD_GLOBAL_PERCENT` | `50` | No new room hold while this share of all rooms is on hold |
| `PUBLIC_URL` | `http://127.0.0.1:8001` | The guest site's address in message links (feedback, unsubscribe, email confirmation) |
| `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_SHEETS_ID`, `SHEETS_PROVIDER` | off | Report export to Google Sheets. Share the sheet with the service account's email. Signing the token needs the `cryptography` package or the `openssl` tool on the server; otherwise the page downloads a CSV |

## 13. Troubleshooting

- **Certificate not issued.** `journalctl -u caddy`. Usually the DNS records are not live yet (step 2), port 80 or 443 is blocked (`ufw status`, and the VPS provider's own firewall), or an `AAAA` record points nowhere.
- **Everyone is "too many requests".** `TRUST_PROXY=1` is missing, so all visitors share the proxy's address.
- **Staff cannot sign in over HTTPS but can on localhost.** The browser does not send a `Secure` cookie over plain HTTP: use the https address; check `MANTRALAYAM_HTTPS=1` only on the server behind the proxy.
- **"This request came from another site" (403).** The page and the API are on different host names; keep each app on its own name and use only that name.
- **Booking asks for a code but no SMS arrives.** Check `journalctl -u mantralayam@rooms` for `OTP SMS failed`, the gateway URL, token and your sender/template registration.
- **`/health` says `backup: none` or `stale`.** The daily timer has not run or failed: `systemctl status mantralayam-backup.timer`, `journalctl -u mantralayam-backup`.

## 14. Installable app, messages and exports
- The guest site is an installable app (manifest and service worker at `/manifest.webmanifest` and `/sw.js`): it must be served over HTTPS, which the proxy already does. The worker keeps only static files (page, styles, texts, images); the API is never cached.
- Festival broadcasts, feedback links and the unsubscribe page need `PUBLIC_URL` and a working SMS provider (WhatsApp and email optional). Broadcasts are sent while you wait, to at most 1000 people, 3 a day.
- Analytics are counters in `analytics_daily` (no IP, cookie or user agent); the 5-minute job deletes counters older than 400 days.
