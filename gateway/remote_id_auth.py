"""Receiver authentication for the Remote ID ingest. P1-15.

The broadcast itself can never be authenticated: anyone can transmit one.
What can be is the receiver that says it heard it. Without that, anything
that can reach the ingest's port can put an aircraft on the map and in the
airspace monitor, so an unauthenticated ingest stays on loopback
(`gateway/config.py` refuses anything else).

## The datagram

A receiver signs the JSON report it would otherwise send, and appends the
signature on a line of its own:

    <report JSON>\\nsig=<hex HMAC-SHA256 of the report JSON bytes>

The report carries two extra fields: `sent_at_ms` (the receiver's clock,
milliseconds since the epoch) and `nonce` (any string, unique per datagram).
The signature covers the exact bytes of the report, so no JSON
canonicalisation is involved, and an ESP32 can produce it with its SDK's
HMAC and a hex encoder.

## What is refused

- A datagram with no signature, when keys are configured.
- An unknown receiver, or a signature that does not match its key.
- A report whose `sent_at_ms` is more than `max_skew_s` from the ingest's
  clock: a captured datagram replayed later.
- A nonce seen again from the same receiver within that window: a captured
  datagram replayed at once.

Keys are 32 random bytes per receiver, in a file of `receiver_id: base64`
lines like the Gateway's station tokens (`tools/remote_id_keys.py` makes
one). The file is read at start-up; revoking a receiver means removing its
line and restarting.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

SIGNATURE_MARKER = b"\nsig="
KEY_BYTES = 32


class AuthenticationError(ValueError):
    """A datagram whose receiver could not be established."""


def sign(report: bytes, key: bytes) -> bytes:
    """The datagram a receiver sends for `report`."""
    return (
        report
        + SIGNATURE_MARKER
        + hmac.new(key, report, hashlib.sha256).hexdigest().encode()
    )


def split(datagram: bytes) -> tuple[bytes, bytes | None]:
    """The report and its signature (None when unsigned)."""
    report, marker, signature = datagram.rpartition(SIGNATURE_MARKER)
    if not marker:
        return datagram, None
    return report, signature


def load_keys(path: Path) -> dict[str, bytes]:
    keys: dict[str, bytes] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        receiver_id, separator, encoded = stripped.partition(":")
        receiver_id = receiver_id.strip()
        if not separator or not receiver_id:
            raise ValueError(f"{path}:{number}: expected 'receiver_id: base64 key'")
        try:
            key = base64.b64decode(encoded.strip(), validate=True)
        except binascii.Error as error:
            raise ValueError(f"{path}:{number}: the key is not base64") from error
        if len(key) < KEY_BYTES:
            raise ValueError(
                f"{path}:{number}: the key is {len(key)} bytes; at least {KEY_BYTES}"
            )
        if receiver_id in keys:
            raise ValueError(f"{path}:{number}: {receiver_id} appears twice")
        keys[receiver_id] = key
    if not keys:
        raise ValueError(f"{path} defines no receivers")
    return keys


@dataclass
class ReceiverAuthenticator:
    keys: dict[str, bytes]
    max_skew_s: float = 30.0
    # (receiver, nonce) pairs seen within the window, oldest first.
    _seen: set[tuple[str, str]] = field(default_factory=set, init=False)
    _order: deque[tuple[float, tuple[str, str]]] = field(
        default_factory=deque, init=False
    )

    def check(self, datagram: bytes, *, now_s: float) -> bytes:
        """The report, if a known receiver signed it just now; else raises."""
        report, signature = split(datagram)
        if signature is None:
            raise AuthenticationError("not signed")
        try:
            fields = json.loads(report)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AuthenticationError("the signed report is not JSON") from error
        if not isinstance(fields, dict):
            raise AuthenticationError("the signed report is not a JSON object")
        receiver_id = fields.get("receiver_id")
        if not isinstance(receiver_id, str) or receiver_id not in self.keys:
            raise AuthenticationError(f"unknown receiver {receiver_id!r}")
        key = self.keys[receiver_id]
        expected = hmac.new(key, report, hashlib.sha256).hexdigest().encode()
        if not hmac.compare_digest(expected, signature.strip().lower()):
            raise AuthenticationError(f"bad signature from {receiver_id}")
        sent_at_ms = fields.get("sent_at_ms")
        nonce = fields.get("nonce")
        if isinstance(sent_at_ms, bool) or not isinstance(sent_at_ms, int):
            raise AuthenticationError("sent_at_ms is not an integer")
        if not isinstance(nonce, str) or not nonce:
            raise AuthenticationError("no nonce")
        skew_s = abs(sent_at_ms / 1000.0 - now_s)
        if skew_s > self.max_skew_s:
            raise AuthenticationError(
                f"sent {skew_s:.0f} s from now, more than {self.max_skew_s:.0f} s"
            )
        self._forget(now_s)
        seen = (receiver_id, nonce)
        if seen in self._seen:
            raise AuthenticationError(f"nonce repeated by {receiver_id}")
        self._seen.add(seen)
        self._order.append((now_s, seen))
        return report

    def _forget(self, now_s: float) -> None:
        # Twice the window: a nonce is refused for as long as its timestamp
        # could still be accepted, whichever side of now it was.
        while self._order and now_s - self._order[0][0] > 2 * self.max_skew_s:
            _, old = self._order.popleft()
            self._seen.discard(old)
