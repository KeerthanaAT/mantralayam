"""External-service providers behind small interfaces, each with a mock mode so everything runs locally without credentials.

SMS      SMS_PROVIDER=mock (default) | http      mock prints the message to the console (development only); http posts JSON to your SMS gateway
WhatsApp WHATSAPP_PROVIDER= (off, default) | mock | http    the same message as the SMS, to the same number; http posts JSON to your WhatsApp gateway
Email    EMAIL_PROVIDER=mock (default) | smtp    mock prints to the console and appends to .tmp/outbox.log; messages may carry an HTML part, inline images and attachments
Sheets   SHEETS_PROVIDER= (auto: google when GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEETS_ID are set, else none) | mock | google    report export; none = the page downloads a CSV instead
Payments PAYMENT_PROVIDER= (off, default) | mock | razorpay    mock simulates checkout on the local guest site; razorpay uses your Razorpay keys (use rzp_test_ keys for test mode)
CAPTCHA  CAPTCHA_PROVIDER= (off, default) | mock | turnstile      the optional second check on public forms, after the honeypot

All settings come from environment variables (see .env.example). Nothing here reads or logs a secret.
Messages that carry an OTP are only ever printed by the mock SMS provider; real providers never log message text.
"""
import base64
import hmac
import json
import os
import re
import secrets
import smtplib
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

OUTBOX = []  # messages the mock providers "sent" in this process, newest last; tests read it. Capped.
OUTBOX_MAX = 200
ROOT = Path(__file__).resolve().parent.parent


class ProviderError(Exception):
    pass


def load_dotenv(path=ROOT / ".env"):
    """Reads KEY=VALUE lines from the project's .env into the environment (variables already set win; # starts a comment). No third-party package needed."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for line in lines:
        k, sep, v = line.partition("=")
        k = k.strip()
        if sep and k and not k.startswith("#"):
            os.environ.setdefault(k, v.strip().strip("\"'"))


load_dotenv()


def _keep(kind, to, text):
    OUTBOX.append({"kind": kind, "to": to, "text": text})
    del OUTBOX[:-OUTBOX_MAX]


def env(name, default=""):
    return os.environ.get(name, default).strip()


# ---------- SMS ----------
class MockSms:
    def send(self, phone, text):
        _keep("sms", phone, text)
        print(f"[mock SMS to {phone}] {text}", flush=True)


class HttpSms:
    """Generic gateway adapter: POST {"to": ..., "message": ..., "sender": ...} as JSON with a bearer token. Adapt the payload to your provider here."""

    def send(self, phone, text):
        url, token = env("SMS_HTTP_URL"), env("SMS_HTTP_TOKEN")
        if not url.startswith("https://"):
            raise ProviderError("SMS_HTTP_URL must be an https:// address")
        req = urllib.request.Request(url, json.dumps({"to": phone, "message": text, "sender": env("SMS_SENDER_ID")}).encode(),
                                     {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except OSError as e:
            raise ProviderError(f"SMS gateway failed: {type(e).__name__}") from e


def sms():
    kind = env("SMS_PROVIDER", "mock")
    if kind == "http":
        return HttpSms()
    if kind != "mock":
        raise ProviderError(f"unknown SMS_PROVIDER {kind!r}")
    return MockSms()


# ---------- WhatsApp (same message as the SMS; off unless WHATSAPP_PROVIDER is set) ----------
class MockWhatsApp:
    def send(self, phone, text):
        _keep("whatsapp", phone, text)
        print(f"[mock WhatsApp to {phone}] {text}", flush=True)


class HttpWhatsApp:
    """Generic gateway adapter: POST {"to", "message", "sender"} as JSON with a bearer token. Meta's WhatsApp Cloud API needs approved message templates:
    adapt the payload here (or put a small relay in front) for it."""

    def send(self, phone, text):
        url, token = env("WHATSAPP_HTTP_URL"), env("WHATSAPP_HTTP_TOKEN")
        if not url.startswith("https://"):
            raise ProviderError("WHATSAPP_HTTP_URL must be an https:// address")
        req = urllib.request.Request(url, json.dumps({"to": phone, "message": text, "sender": env("WHATSAPP_SENDER")}).encode(),
                                     {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except OSError as e:
            raise ProviderError(f"WhatsApp gateway failed: {type(e).__name__}") from e


def whatsapp():
    """The WhatsApp provider, or None when WHATSAPP_PROVIDER is empty (the channel is then skipped, not failed)."""
    kind = env("WHATSAPP_PROVIDER")
    if not kind:
        return None
    if kind == "http":
        return HttpWhatsApp()
    if kind != "mock":
        raise ProviderError(f"unknown WHATSAPP_PROVIDER {kind!r}")
    return MockWhatsApp()


# ---------- email ----------
# send(to, subject, text, html=None, inline=(), attachments=()): inline = [(content_id, mime, bytes)] shown inside the HTML part (src="cid:..."), attachments = [(filename, mime, bytes)]
class MockEmail:
    def send(self, to, subject, text, html=None, inline=(), attachments=()):
        OUTBOX.append({"kind": "email", "to": to, "text": f"{subject}\n{text}", "subject": subject, "html": html, "inline": [c[0] for c in inline], "inline_data": [(c[0], c[1], c[2]) for c in inline],
                       "attachments": [(a[0], a[1], a[2]) for a in attachments]})
        del OUTBOX[:-OUTBOX_MAX]
        print(f"[mock email to {to}] {subject}" + (f" (+{len(attachments)} attachment(s))" if attachments else ""), flush=True)
        try:
            (ROOT / ".tmp").mkdir(exist_ok=True)
            with open(ROOT / ".tmp" / "outbox.log", "a", encoding="utf-8") as f:
                f.write(f"To: {to}\nSubject: {subject}\n{text}\n" + "".join(f"[attachment {a[0]}, {len(a[2])} bytes]\n" for a in attachments) + "---\n")
        except OSError:
            pass


class SmtpEmail:
    def send(self, to, subject, text, html=None, inline=(), attachments=()):
        host, port = env("SMTP_HOST"), int(env("SMTP_PORT", "587"))
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = env("SMTP_FROM") or env("SMTP_USER"), to, subject
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
            for cid, mime, data in inline:
                maintype, _, subtype = mime.partition("/")
                msg.get_payload()[1].add_related(data, maintype, subtype, cid=f"<{cid}>")
        for name, mime, data in attachments:
            maintype, _, subtype = mime.partition("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
        try:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls(context=ssl.create_default_context())
                if env("SMTP_USER"):
                    s.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
                s.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            raise ProviderError(f"SMTP failed: {type(e).__name__}") from e


def email():
    kind = env("EMAIL_PROVIDER", "mock")
    if kind == "smtp":
        return SmtpEmail()
    if kind != "mock":
        raise ProviderError(f"unknown EMAIL_PROVIDER {kind!r}")
    return MockEmail()


# ---------- payments (Razorpay behind an interface; the mock needs no account) ----------
# Interface: key_id, create_order(amount_paise, receipt, notes) -> {"id"}, verify_checkout(order_id, payment_id, signature) -> bool, verify_webhook(raw_body, signature) -> bool,
# order_payments(order_id) -> [{"id", "status", "amount", "method"}], refund(payment_id, amount_paise, receipt) -> {"id", "status": "pending"|"processed"|"failed"}
RAZORPAY_API = "https://api.razorpay.com/v1"
MOCK_PAYMENT_SECRET = secrets.token_hex(16)  # only the mock uses it; new for every process, so no fixed value can be used to forge a payment. The mock is refused on a public server (serve_checks)


def _hmac_hex(secret, msg):
    return hmac.new(secret.encode(), msg if isinstance(msg, bytes) else msg.encode(), "sha256").hexdigest()


class MockPayments:
    name, key_id = "mock", "mock_key"

    def create_order(self, amount_paise, receipt, notes=None):
        return {"id": "order_mock_" + secrets.token_hex(8), "amount": amount_paise}

    def sign(self, order_id, payment_id):
        return _hmac_hex(MOCK_PAYMENT_SECRET, f"{order_id}|{payment_id}")

    def verify_checkout(self, order_id, payment_id, signature):
        return hmac.compare_digest(self.sign(order_id, payment_id), str(signature or ""))

    def webhook_signature(self, raw):
        return _hmac_hex(MOCK_PAYMENT_SECRET, raw)

    def verify_webhook(self, raw, signature):
        return hmac.compare_digest(self.webhook_signature(raw), str(signature or ""))

    def order_payments(self, order_id):
        return []

    def refund(self, payment_id, amount_paise, receipt):
        return {"id": "rfnd_mock_" + secrets.token_hex(6), "status": "processed"}


class RazorpayPayments:
    name = "razorpay"

    def __init__(self):
        self.key_id, self.secret = env("RAZORPAY_KEY_ID"), env("RAZORPAY_KEY_SECRET")
        if not (self.key_id and self.secret):
            raise ProviderError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set for PAYMENT_PROVIDER=razorpay")

    def _call(self, method, path, body=None, headers=None):
        auth = base64.b64encode(f"{self.key_id}:{self.secret}".encode()).decode()
        req = urllib.request.Request(RAZORPAY_API + path, json.dumps(body).encode() if body is not None else None,
                                     {"Authorization": "Basic " + auth, "Content-Type": "application/json", **(headers or {})}, method=method)
        try:
            return json.load(urllib.request.urlopen(req, timeout=15))
        except urllib.error.HTTPError as e:
            raise ProviderError(f"Razorpay answered {e.code}") from e  # the body can echo details; only the status is kept
        except (OSError, ValueError) as e:
            raise ProviderError(f"Razorpay request failed: {type(e).__name__}") from e

    def create_order(self, amount_paise, receipt, notes=None):
        return self._call("POST", "/orders", {"amount": amount_paise, "currency": "INR", "receipt": receipt[:40], "notes": notes or {}})

    def verify_checkout(self, order_id, payment_id, signature):
        return hmac.compare_digest(_hmac_hex(self.secret, f"{order_id}|{payment_id}"), str(signature or ""))

    def verify_webhook(self, raw, signature):
        secret = env("RAZORPAY_WEBHOOK_SECRET")
        return bool(secret) and hmac.compare_digest(_hmac_hex(secret, raw), str(signature or ""))

    def order_payments(self, order_id):
        return [{"id": p["id"], "status": p["status"], "amount": p["amount"], "method": p.get("method")} for p in self._call("GET", f"/orders/{order_id}/payments").get("items", [])]

    def refund(self, payment_id, amount_paise, receipt):
        r = self._call("POST", f"/payments/{payment_id}/refund", {"amount": amount_paise, "speed": "normal", "receipt": receipt[:40]}, {"X-Refund-Idempotency": receipt[:40]})
        return {"id": r["id"], "status": "processed" if r.get("status") == "processed" else "failed" if r.get("status") == "failed" else "pending"}


def payments():
    """The payment provider, or None when PAYMENT_PROVIDER is empty (online payment is then off and the site says nothing about it)."""
    kind = env("PAYMENT_PROVIDER")
    if not kind:
        return None
    if kind == "mock":
        return MockPayments()
    if kind == "razorpay":
        return RazorpayPayments()
    raise ProviderError(f"unknown PAYMENT_PROVIDER {kind!r}")


# ---------- Google Sheets (report export through a service account) ----------
# Interface: write(tab, rows) -> url of the sheet, rows = list of lists (the first is the header). Mock: kept in SHEETS_OUTBOX.
SHEETS_OUTBOX = []
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # fixed here: never taken from the key file
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
_TOKEN = {"value": "", "until": 0.0}


class MockSheets:
    def write(self, tab, rows):
        SHEETS_OUTBOX.append({"tab": tab, "rows": rows})
        del SHEETS_OUTBOX[:-50]
        return "https://sheets.mock/" + re.sub(r"\W+", "-", tab)


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _rs256(key_pem, data):
    """RSA-SHA256 signature. Uses the `cryptography` package when it is installed, else the `openssl` command line tool; the standard library has no RSA."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        return serialization.load_pem_private_key(key_pem.encode(), None).sign(data, padding.PKCS1v15(), hashes.SHA256())
    except ImportError:
        pass
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:  # created with owner-only access by the OS, removed straight away
            path = f.name
            f.write(key_pem)
        done = subprocess.run(["openssl", "dgst", "-sha256", "-sign", path], input=data, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        raise ProviderError("signing needs the 'cryptography' package or the openssl tool") from e
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    if done.returncode != 0:
        raise ProviderError("the service account key could not be used to sign")
    return done.stdout


class GoogleSheets:
    def __init__(self):
        raw = env("GOOGLE_SERVICE_ACCOUNT_JSON")
        try:
            info = json.loads(raw) if raw.lstrip().startswith("{") else json.loads(open(raw, encoding="utf-8").read())
            self.email, self.key = info["client_email"], info["private_key"]
        except (OSError, ValueError, KeyError) as e:
            raise ProviderError("GOOGLE_SERVICE_ACCOUNT_JSON is not a service account key (JSON text or a file path)") from e
        self.sheet = env("GOOGLE_SHEETS_ID")
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", self.sheet):
            raise ProviderError("GOOGLE_SHEETS_ID does not look like a spreadsheet id")

    def _token(self):
        if _TOKEN["value"] and _TOKEN["until"] > time.time() + 60:
            return _TOKEN["value"]
        now = int(time.time())
        head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claim = _b64url(json.dumps({"iss": self.email, "scope": "https://www.googleapis.com/auth/spreadsheets", "aud": GOOGLE_TOKEN_URL, "iat": now, "exp": now + 3000}).encode())
        jwt = f"{head}.{claim}.{_b64url(_rs256(self.key, f'{head}.{claim}'.encode()))}"
        data = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt}).encode()
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(GOOGLE_TOKEN_URL, data), timeout=15))
        except (OSError, ValueError) as e:
            raise ProviderError(f"Google sign-in failed: {type(e).__name__}") from e
        _TOKEN["value"], _TOKEN["until"] = r["access_token"], time.time() + int(r.get("expires_in", 3000))
        return _TOKEN["value"]

    def _call(self, method, path, body=None):
        req = urllib.request.Request(f"{SHEETS_API}/{self.sheet}{path}", json.dumps(body).encode() if body is not None else None,
                                     {"Authorization": "Bearer " + self._token(), "Content-Type": "application/json"}, method=method)
        try:
            return json.load(urllib.request.urlopen(req, timeout=20))
        except urllib.error.HTTPError as e:
            raise ProviderError(f"Google answered {e.code}") from e
        except (OSError, ValueError) as e:
            raise ProviderError(f"Google request failed: {type(e).__name__}") from e

    def write(self, tab, rows):
        tab = re.sub(r"[^\w .-]", "", tab)[:90].strip() or "Report"
        try:
            self._call("POST", ":batchUpdate", {"requests": [{"addSheet": {"properties": {"title": tab}}}]})
        except ProviderError:
            pass  # the tab exists already
        q = urllib.parse.quote(f"'{tab}'!A1")
        self._call("POST", f"/values/{q}:clear", {})
        self._call("PUT", f"/values/{q}?valueInputOption=RAW", {"values": rows})  # RAW: nothing is read as a formula
        return f"https://docs.google.com/spreadsheets/d/{self.sheet}"


def sheets():
    """The Sheets provider, or None when it is not configured (the caller then offers the CSV)."""
    kind = env("SHEETS_PROVIDER")
    if kind == "mock":
        return MockSheets()
    if kind == "google" or (not kind and env("GOOGLE_SERVICE_ACCOUNT_JSON") and env("GOOGLE_SHEETS_ID")):
        return GoogleSheets()
    if kind:
        raise ProviderError(f"unknown SHEETS_PROVIDER {kind!r}")
    return None


# ---------- CAPTCHA ----------
MOCK_CAPTCHA_PASS = "mock-pass"
TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def captcha_config():
    """What the page needs to show the widget, or None when CAPTCHA is off."""
    kind = env("CAPTCHA_PROVIDER")
    if not kind:
        return None
    if kind not in ("mock", "turnstile"):
        raise ProviderError(f"unknown CAPTCHA_PROVIDER {kind!r}")
    return {"provider": kind, "site_key": env("CAPTCHA_SITE_KEY") if kind == "turnstile" else ""}


def captcha_ok(token, ip):
    """True when CAPTCHA is off or the token passes. Fails closed: a provider outage rejects the form (the guest can use Call me back or phone)."""
    cfg = captcha_config()
    if not cfg:
        return True
    token = str(token or "")[:2048]
    if not token:
        return False
    if cfg["provider"] == "mock":
        return token == MOCK_CAPTCHA_PASS
    data = urllib.parse.urlencode({"secret": env("CAPTCHA_SECRET"), "response": token, "remoteip": ip}).encode()
    try:
        return bool(json.load(urllib.request.urlopen(urllib.request.Request(TURNSTILE_URL, data), timeout=8)).get("success"))
    except (OSError, ValueError):
        return False


# ---------- heartbeat (dead-man's switch for the periodic job) ----------
def heartbeat():
    """Pings HEARTBEAT_URL (for example a Healthchecks.io check) so a monitor notices when the 5-minute job stops running. Best effort; no URL, no ping."""
    url = env("HEARTBEAT_URL")
    if not url.startswith("https://"):
        return False
    try:
        urllib.request.urlopen(url, timeout=8).read()
        return True
    except OSError:
        return False
