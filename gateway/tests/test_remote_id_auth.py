"""Receiver authentication: signed by a known receiver, recently, once. P1-15.

Every refusal is paired with the acceptance it differs from by one thing.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from gateway.remote_id_auth import (
    AuthenticationError,
    ReceiverAuthenticator,
    load_keys,
    sign,
    split,
)

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
NOW_S = 1_790_000_000.0


def report(**changes: Any) -> bytes:
    fields = {
        "receiver_id": "rx-1",
        "transmitter": "AA:BB:CC:00:00:01",
        "payload_hex": "00",
        "sent_at_ms": int(NOW_S * 1000),
        "nonce": "n-1",
        **changes,
    }
    return json.dumps(fields).encode()


def authenticator() -> ReceiverAuthenticator:
    return ReceiverAuthenticator(keys={"rx-1": KEY}, max_skew_s=30.0)


def test_a_signed_report_from_a_known_receiver_is_accepted_as_sent() -> None:
    body = report()
    assert authenticator().check(sign(body, KEY), now_s=NOW_S) == body


def test_the_signature_is_hmac_sha256_of_the_report_bytes_in_hex() -> None:
    """Pinned independently of `sign`, as a receiver's firmware would do it."""
    import hashlib
    import hmac

    body = report()
    expected = hmac.new(KEY, body, hashlib.sha256).hexdigest()
    assert sign(body, KEY) == body + b"\nsig=" + expected.encode()


@pytest.mark.parametrize(
    ("datagram", "complaint"),
    [
        (report(), "not signed"),
        (sign(report(receiver_id="rx-2"), KEY), "unknown receiver"),
        (sign(report(), OTHER_KEY), "bad signature"),
        (sign(report(), KEY).replace(b'"00"', b'"01"'), "bad signature"),
        (sign(report(sent_at_ms=int((NOW_S - 31) * 1000)), KEY), "more than 30"),
        (sign(report(sent_at_ms=int((NOW_S + 31) * 1000)), KEY), "more than 30"),
        (sign(report(sent_at_ms="now"), KEY), "not an integer"),
        (sign(report(nonce=""), KEY), "no nonce"),
        (sign(b"[1]", KEY), "not a JSON object"),
    ],
    ids=[
        "unsigned",
        "unknown-receiver",
        "wrong-key",
        "tampered",
        "stale",
        "future",
        "time-not-int",
        "no-nonce",
        "not-object",
    ],
)
def test_refusals(datagram: bytes, complaint: str) -> None:
    with pytest.raises(AuthenticationError, match=complaint):
        authenticator().check(datagram, now_s=NOW_S)


def test_just_inside_the_window_is_accepted() -> None:
    auth = authenticator()
    for n, offset in enumerate((-29.9, 29.9)):
        datagram = sign(
            report(sent_at_ms=int((NOW_S + offset) * 1000), nonce=f"edge-{n}"), KEY
        )
        assert auth.check(datagram, now_s=NOW_S)


def test_a_replayed_datagram_is_refused_and_a_new_nonce_is_not() -> None:
    auth = authenticator()
    datagram = sign(report(), KEY)
    auth.check(datagram, now_s=NOW_S)

    with pytest.raises(AuthenticationError, match="nonce repeated"):
        auth.check(datagram, now_s=NOW_S + 1)
    assert auth.check(sign(report(nonce="n-2"), KEY), now_s=NOW_S + 1)


def test_nonces_are_forgotten_once_their_time_could_not_pass_anyway() -> None:
    auth = authenticator()
    auth.check(sign(report(), KEY), now_s=NOW_S)
    auth.check(
        sign(report(nonce="later", sent_at_ms=int((NOW_S + 61) * 1000)), KEY),
        now_s=NOW_S + 61,
    )

    assert auth._seen == {("rx-1", "later")}


def test_split_finds_the_last_signature_line() -> None:
    assert split(b"{}") == (b"{}", None)
    assert split(b"{}\nsig=ab") == (b"{}", b"ab")


def key_line(receiver: str, key: bytes) -> str:
    return f"{receiver}: {base64.b64encode(key).decode()}\n"


def test_keys_load_from_a_file_of_lines(tmp_path: Path) -> None:
    path = tmp_path / "keys"
    path.write_text(
        "# receivers\n\n" + key_line("rx-1", KEY) + key_line("rx-2", OTHER_KEY)
    )
    assert load_keys(path) == {"rx-1": KEY, "rx-2": OTHER_KEY}


@pytest.mark.parametrize(
    ("content", "complaint"),
    [
        ("", "no receivers"),
        ("rx-1 " + base64.b64encode(KEY).decode(), "expected"),
        ("rx-1: not base64!", "not base64"),
        (key_line("rx-1", b"short"), "at least 32"),
        (key_line("rx-1", KEY) + key_line("rx-1", OTHER_KEY), "twice"),
    ],
    ids=["empty", "no-colon", "not-base64", "short", "duplicate"],
)
def test_bad_key_files_are_refused(
    tmp_path: Path, content: str, complaint: str
) -> None:
    path = tmp_path / "keys"
    path.write_text(content)
    with pytest.raises(ValueError, match=complaint):
        load_keys(path)
