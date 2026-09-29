"""Write the API's OpenAPI schema to web-pilot/openapi.json. P6-01.

    python tools/export_openapi.py            # write it
    python tools/export_openapi.py --check    # exit 1 if it is out of date

The console's TypeScript types are generated from this file, never written
by hand (CLAUDE.md). It is committed, so the console builds without a running
API, and `api/tests/test_openapi_export.py` fails when the API changes and
the file was not regenerated.

The app is built with placeholders for everything it would reach at run
time: a schema describes routes, it does not call them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from api.app import create_api_app
from api.registry import FleetRegistry
from api.replay import ReplayStore

TARGET = Path(__file__).resolve().parent.parent / "web-pilot" / "openapi.json"


class _NoAccounts:
    async def session(self, token: str) -> None:
        return None


def schema() -> str:
    unused = cast(Any, None)
    replay = ReplayStore(
        telemetry=unused,
        relational=None,
        gap_threshold_s=1.0,
        evidence_slack_s=1.0,
        flight_split_s=1.0,
        max_samples=1,
    )
    app = create_api_app(
        cast(FleetRegistry, unused),
        auth=cast(Any, _NoAccounts()),
        feed_secret=b"x" * 32,
        feed_ticket_ttl_s=1.0,
        replay=replay,
    )
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/export_openapi.py")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    current = schema()
    if args.check:
        stale = not TARGET.exists() or TARGET.read_text(encoding="utf-8") != current
        if stale:
            print(f"{TARGET} is out of date: run python tools/export_openapi.py")
            return 1
        print("openapi.json is current")
        return 0
    TARGET.write_text(current, encoding="utf-8")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
