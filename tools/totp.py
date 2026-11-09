"""Time-based one-time passwords (RFC 6238: HMAC-SHA1, 6 digits, 30 second steps) for staff two-factor sign-in, standard library only.
Works with Google Authenticator, Microsoft Authenticator, Aegis, FreeOTP and any other authenticator app."""
import base64
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP, DIGITS, WINDOW = 30, 6, 1  # a code is accepted one step early or late, for clock drift


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code_at(secret, step):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", step), "sha1").digest()
    off = mac[-1] & 15
    return f"{(struct.unpack('>I', mac[off:off + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS:0{DIGITS}d}"


def verify(secret, code, after=0, now=None):
    """The time step the code belongs to, or None. `after`: the last step already used; a code from that step or earlier is refused (one use only)."""
    code = str(code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    now_step = int((time.time() if now is None else now) // STEP)
    hit = None
    for step in range(now_step - WINDOW, now_step + WINDOW + 1):  # check every step: no early exit, so timing does not show which one matched
        if hmac.compare_digest(code_at(secret, step), code) and step > after:
            hit = step
    return hit


def uri(secret, account, issuer="Mantralayam Rooms"):
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP}"
