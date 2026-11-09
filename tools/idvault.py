"""Authenticated encryption for ID documents and pending-payment details, using only the standard library.

Construction (encrypt-then-MAC): two keys are derived from the master secret; the keystream is HMAC-SHA256(enc_key, nonce || counter) in counter mode
(a PRF used as a stream cipher); the tag is HMAC-SHA256(mac_key, version || nonce || aad || ciphertext), checked in constant time before anything is decrypted.
Every call uses a fresh random 16-byte nonce. `aad` binds a record to its owner (for example the booking id), so a blob copied to another row will not open.

Versions: 1 derived the two keys with one HMAC round (fast to guess against a weak secret). 2 (what seal() writes now) stretches the master secret with scrypt
(n=2**15, r=8, p=1), so guessing a weak secret from a stolen database costs real memory and time. Version 1 blobs still open; `python tools/mantralayam_rooms.py rekey`
rewrites them as version 2. Use a random master secret of 32+ characters either way.

Where a vetted library (such as `cryptography`'s Fernet) can be installed, prefer it; this module exists so the project needs no third-party package.
Keep the master secret out of the database in production: set ID_ENCRYPTION_KEY (see docs/DEPLOYMENT.md). Losing it makes stored IDs unreadable.
"""
import hashlib
import hmac
import secrets
import struct
from functools import lru_cache

V1, V2 = b"\x01", b"\x02"
VERSION = V2
SCRYPT_SALT = b"mantralayam|vault|v2"


class VaultError(Exception):
    pass


@lru_cache(maxsize=4)
def _keys(master, version=V2):
    m = master.encode() if isinstance(master, str) else bytes(master)
    if len(m) < 16:
        raise VaultError("The encryption key is too short (use at least 16 characters).")
    if version == V1:
        return hmac.new(m, b"mantralayam|enc", "sha256").digest(), hmac.new(m, b"mantralayam|mac", "sha256").digest()
    k = hashlib.scrypt(m, salt=SCRYPT_SALT, n=2 ** 15, r=8, p=1, maxmem=96 * 1024 * 1024, dklen=64)  # computed once per process, then cached
    return k[:32], k[32:]


def _stream(key, nonce, n):
    out, i = bytearray(), 0
    while len(out) < n:
        out += hmac.new(key, nonce + struct.pack(">Q", i), "sha256").digest()
        i += 1
    return bytes(out[:n])


def seal(master, plain, aad=b""):
    enc, mac = _keys(master, V2)
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(plain, _stream(enc, nonce, len(plain))))
    return V2 + nonce + ct + hmac.new(mac, V2 + nonce + aad + ct, "sha256").digest()


def version_of(blob):
    return bytes(blob[:1]) if blob else b""


def open_(master, blob, aad=b""):
    if not blob or len(blob) < 1 + 16 + 32 or blob[:1] not in (V1, V2):
        raise VaultError("The stored value is damaged or in an unknown format.")
    v = bytes(blob[:1])
    enc, mac = _keys(master, v)
    nonce, ct, tag = blob[1:17], blob[17:-32], blob[-32:]
    if not hmac.compare_digest(tag, hmac.new(mac, v + nonce + aad + ct, "sha256").digest()):
        raise VaultError("The stored value could not be verified (wrong key, or it was altered).")
    return bytes(a ^ b for a, b in zip(ct, _stream(enc, nonce, len(ct))))
