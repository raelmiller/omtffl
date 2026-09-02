"""Web push: encrypting a notification and getting it to a push service.

Written against RFC 8291 (message encryption) and RFC 8292 (VAPID) rather
than taken from a library. The obvious library, `pywebpush`, pulls in
`http-ece`, whose setup.py does not build against a current setuptools — and
a dependency that fails to build is a deploy that fails at the worst moment.
Everything here rests on `cryptography`, which ships wheels.

That trade is only defensible because the result is checked rather than
trusted: the test suite encrypts with fixed keys and decrypts with an
independent implementation of the same RFCs, so a misreading of the spec
shows up as a failure rather than as notifications that silently never
arrive.

What this file will not tell you is whether a real push service accepts the
result. Nothing here has ever spoken to one — the sandbox this was written in
cannot reach fcm.googleapis.com or web.push.apple.com — so the first real
send is the first proof. `/account` has a button for exactly that.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

# Soft, deliberately. `cryptography` is a compiled wheel, and a compiled wheel
# is the dependency most likely to be missing or half-installed on some host —
# it already was on the machine this was written on. Notifications are the
# least important thing the app does, and must not be able to stop it serving
# a table. Absent, push reports itself unavailable and nothing else notices.
try:
    from cryptography.hazmat.primitives import hashes, hmac, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    AVAILABLE = True
    UNAVAILABLE_BECAUSE = None
except (KeyboardInterrupt, SystemExit):     # never swallow these
    raise
except BaseException as exc:                # ImportError, or a broken build
    # BaseException rather than Exception, because the broken-build case does
    # not raise an ordinary one. A half-installed `cryptography` fails inside
    # its Rust extension and comes out as pyo3's PanicException, which derives
    # from BaseException — so the guard written for exactly this let it
    # through, and importing anything that touches push took the app with it.
    AVAILABLE = False
    UNAVAILABLE_BECAUSE = f"{type(exc).__name__}: {exc}"

# One record, which for a notification is always the whole message: the
# largest payload a push service is obliged to carry is 4096 bytes, and
# nothing here is close to it.
RECORD_SIZE = 4096
TTL = 12 * 60 * 60          # how long a service should hold an undelivered one
TIMEOUT = 10


def b64(raw: bytes) -> str:
    """base64url, unpadded — what every field in these RFCs uses."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def unb64(text: str) -> bytes:
    """The other way. Browsers strip the padding; Python insists on it."""
    text = text.strip().replace("-", "-").replace("_", "_")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    h = hmac.HMAC(key, hashes.SHA256())
    h.update(data)
    return h.finalize()


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    """Extract then expand, with the one-block expand these RFCs always use.

    Every output here is 32 bytes or fewer, so the expand loop never runs a
    second time and writing it out in full would only add a branch nothing
    takes.
    """
    return _hmac(_hmac(salt, ikm), info + b"\x01")[:length]


def derive(ua_public: bytes, auth_secret: bytes, as_private, salt: bytes):
    """The content encryption key and nonce for one message. RFC 8291 §3.4.

    The two-stage derivation is the part worth reading twice: the ECDH secret
    is first combined with the subscription's `auth` secret — which the push
    service never sees — so that a service relaying the message cannot read
    it even though it handles both public keys.
    """
    as_public = as_private.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    shared = as_private.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), ua_public))

    # The receiver's key comes first in key_info, always, whichever end is
    # deriving. Getting that order wrong yields a key that works against your
    # own decrypt and against nobody else's.
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = _hkdf(auth_secret, shared, key_info, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    return cek, nonce, as_public


def encrypt(payload: bytes, ua_public: bytes, auth_secret: bytes,
            as_private=None, salt=None) -> bytes:
    """One aes128gcm record, header and all. RFC 8188 §2, RFC 8291 §4.

    `as_private` and `salt` are arguments only so the tests can pin them to
    the values an independent implementation is given; in use both are fresh
    per message and must be.
    """
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    cek, nonce, as_public = derive(ua_public, auth_secret, as_private, salt)

    # 0x02 is the delimiter marking the last record. A single record is still
    # the last one, and omitting this is decrypted as a truncated stream.
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return (salt + struct.pack("!I", RECORD_SIZE)
            + bytes([len(as_public)]) + as_public + body)


def decrypt(block: bytes, ua_private, auth_secret: bytes) -> bytes:
    """The inverse, which exists so the tests can prove a round trip.

    Nothing in the app calls it: the app only ever sends. It is here rather
    than in the test file because it is the same spec read the same way, and
    splitting the two halves apart is how they drift.
    """
    salt, _rs, idlen = block[:16], block[16:20], block[20]
    as_public = block[21:21 + idlen]
    body = block[21 + idlen:]

    shared = ua_private.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), as_public))
    ua_public = ua_private.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    return AESGCM(cek).decrypt(nonce, body, None).rstrip(b"\x02")


# ── Identifying ourselves to the push service ──────────────────────────────
def private_key():
    """The server's VAPID key, or None if push is not configured.

    Held in the environment rather than the database. It has to survive a
    redeploy — every subscription is bound to the public half, so a key that
    changes silently unsubscribes the whole league — and it is the one secret
    here that is not derived from something a manager holds.
    """
    raw = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not raw or not AVAILABLE:
        return None
    try:
        return ec.derive_private_key(
            int.from_bytes(unb64(raw), "big"), ec.SECP256R1())
    except Exception:
        return None


def public_key() -> str | None:
    """The half the browser subscribes with, base64url, uncompressed point."""
    key = private_key()
    if key is None:
        return None
    return b64(key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint))


def configured() -> bool:
    return private_key() is not None


def _jwt(audience: str, subject: str) -> str:
    """A VAPID token for one push service. RFC 8292 §2.

    ES256 signatures arrive from `cryptography` as DER and have to go out as
    a raw r‖s pair. A DER signature is accepted by nothing and rejected with
    a 401 that says only "invalid JWT".
    """
    key = private_key()
    header = b64(json.dumps({"typ": "JWT", "alg": "ES256"},
                            separators=(",", ":")).encode())
    claims = b64(json.dumps({"aud": audience,
                             "exp": int(time.time()) + 12 * 60 * 60,
                             "sub": subject},
                            separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = asym_utils.decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{claims}.{b64(raw)}"


def headers(endpoint: str, subject=None) -> dict:
    """What a push service needs to believe the message is from us."""
    origin = urlparse(endpoint)
    subject = subject or os.environ.get("VAPID_SUBJECT") or "mailto:admin@omtffl"
    token = _jwt(f"{origin.scheme}://{origin.netloc}", subject)
    return {
        "Authorization": f"vapid t={token}, k={public_key()}",
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(TTL),
        "Urgency": "normal",
    }


def send(subscription: dict, message: dict, subject=None) -> tuple[int, str]:
    """Deliver one notification. Returns (status, detail).

    404 and 410 mean the subscription is dead — the app uninstalled, the
    browser data cleared — and the caller is expected to drop it. Everything
    else is reported and nothing is thrown: a push service having a bad
    minute must never take a page down with it.
    """
    if not configured():
        return 0, "no VAPID key configured"
    try:
        block = encrypt(json.dumps(message).encode(),
                        unb64(subscription["p256dh"]),
                        unb64(subscription["auth"]))
        request = urllib.request.Request(
            subscription["endpoint"], data=block, method="POST",
            headers=headers(subscription["endpoint"], subject))
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, "sent"
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read()[:200].decode(errors="replace") or exc.reason)
    except Exception as exc:                       # DNS, TLS, timeouts
        return 0, f"{type(exc).__name__}: {exc}"


def status() -> dict:
    """Why push is or isn't working, for /health and the admin page."""
    return {
        "available": AVAILABLE,
        "configured": configured(),
        "public_key": public_key(),
        "why": (UNAVAILABLE_BECAUSE if not AVAILABLE else
                None if configured() else
                "set VAPID_PRIVATE_KEY — generate one with "
                "python3 tools/vapid_keys.py"),
    }
