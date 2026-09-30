"""Receiver keys for the Remote ID ingest. P1-15.

    python -m tools.remote_id_keys new rx-tbilisi-1 --file local/remote-id-receivers.keys

Adds a line `rx-tbilisi-1: <base64 key>` to the file (created if missing)
and prints the key once, for the receiver's own configuration. The ingest
reads the file named by `REMOTE_ID_RECEIVER_KEYS` at start-up
(`gateway/remote_id_auth.py`). To revoke a receiver, delete its line and
restart the ingest.

The file holds secrets: keep it out of the repository (`local/` is ignored)
and readable only by the service.
"""

from __future__ import annotations

import argparse
import base64
import secrets
import sys
from pathlib import Path

from gateway.remote_id_auth import KEY_BYTES, load_keys


def new_key(path: Path, receiver_id: str) -> str:
    if (
        ":" in receiver_id
        or not receiver_id.strip()
        or receiver_id != receiver_id.strip()
    ):
        raise ValueError("a receiver id must be non-empty, with no ':' or edge spaces")
    if (
        path.exists()
        and path.read_text(encoding="utf-8").strip()
        and receiver_id in load_keys(path)
    ):
        raise ValueError(f"{receiver_id} already has a key in {path}")
    key = base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{receiver_id}: {key}\n")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.remote_id_keys")
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help="make a key for a receiver")
    new.add_argument("receiver_id")
    new.add_argument("--file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        key = new_key(args.file, args.receiver_id)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"{args.receiver_id}: {key}")
    print(f"added to {args.file}; restart the Remote ID ingest to use it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
