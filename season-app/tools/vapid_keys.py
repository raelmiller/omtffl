#!/usr/bin/env python3
"""Generate the VAPID key pair push notifications are signed with.

    python3 tools/vapid_keys.py

Set the private half as VAPID_PRIVATE_KEY in Railway. It has to stay the
same for the life of the league: every subscription a browser makes is bound
to the public half, so changing the key silently unsubscribes everyone and
the only symptom is notifications quietly stopping.

Set VAPID_SUBJECT too — a mailto: or https: URL a push service can use to
reach whoever runs this. Some services refuse a token without one.
"""
import base64
import sys

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError:
    # The one dependency, and the one thing likely to be missing on a laptop
    # that has never run this app. A traceback here reads as "the script is
    # broken" rather than "you need one package".
    sys.exit("This needs the `cryptography` package:\n\n"
             "    pip install cryptography\n\n"
             "(or `pip3 install cryptography`, or `py -m pip install "
             "cryptography` on Windows) and then run this again.")


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


key = ec.generate_private_key(ec.SECP256R1())
private = b64(key.private_numbers().private_value.to_bytes(32, "big"))
public = b64(key.public_key().public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))

print("VAPID_PRIVATE_KEY=" + private)
print()
print("# The public half is derived from the private one, so it does not need")
print("# setting — this is only here to check against what the app reports.")
print("# public: " + public)
