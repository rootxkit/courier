"""Register an aircraft and bind a MAVLink address to it.

    python tools/register_aircraft.py --label "hexa-01" \
        --station tbilisi-base-1 --sysid 1 --compid 1

**This is a placeholder that P2-05 replaces.** It exists so that the two rows
a working end-to-end run needs are created the same way twice, by something
reviewable, instead of by ad-hoc SQL nobody wrote down.

## What it does, and what P2-05 must do instead

Two rows:

1. `known_drones` - the identity the telemetry database may attribute
   telemetry to. This is a **projection** of the relational `drones` registry,
   which is authoritative. This script invents a `drone_id` because there is no
   registry yet to take one from.

2. `source_bindings` - `(station_id, sysid, compid) -> drone_id`, valid from a
   given instant. Overlaps are refused by a database exclusion constraint, so
   rebinding an address means closing the open binding first, which `--rebind`
   does.

**P2-05 owns both properly.** Registering a drone through the API must create
the relational row *and* project it here; retiring one must do the reverse.
When that exists, this script should be deleted rather than kept as a
convenience, because a second way to create bindings is a second way for the
two databases to disagree.

## Why `--from` defaults to now, and when not to use that

A binding is not retroactive: telemetry captured before `bound_from` resolves
to no drone and is archived as an unclaimed source. That is correct - records
from before anyone said what an address was do not belong to an airframe - but
it means that if an aircraft has already been transmitting, the records already
archived stay unclaimed unless `--from` is set back to cover them.

Setting `--from` earlier is safe only if the address was not previously bound
to a *different* airframe over that interval; the exclusion constraint will
refuse it if it was, which is the check doing its job.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from gateway.binding import BindingConflictError, BindingResolver
from gateway.parsing import SourceId


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="register_aircraft",
        description="Register an aircraft and bind a MAVLink address to it.",
    )
    parser.add_argument("--label", required=True, help="human name, e.g. hexa-01")
    parser.add_argument("--station", required=True, help="station_id")
    parser.add_argument("--sysid", type=int, required=True, help="MAVLink SYSID")
    parser.add_argument(
        "--compid", type=int, default=1, help="MAVLink component id (default: 1)"
    )
    parser.add_argument(
        "--drone-id",
        type=UUID,
        default=None,
        help="existing drone_id; a new one is generated if omitted",
    )
    parser.add_argument(
        "--serial",
        default=None,
        help=(
            "the serial its Remote ID module broadcasts (P1-15), so a broadcast "
            "by this aircraft is matched to it rather than shown as a second one"
        ),
    )
    parser.add_argument(
        "--from",
        dest="bound_from",
        default=None,
        help=(
            "ISO-8601 instant the binding starts (default: now). Earlier "
            "values claim already-archived telemetry; see the module docstring"
        ),
    )
    parser.add_argument(
        "--rebind",
        action="store_true",
        help="close the open binding for this address first",
    )
    parser.add_argument(
        "--retire",
        action="store_true",
        help=(
            "teardown: close the open binding and mark the drone retired. "
            "Nothing is deleted - a drone that has flown keeps its history, "
            "and its telemetry stays resolvable"
        ),
    )
    parser.add_argument(
        "--by",
        default=os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
        help="who is making this binding, recorded on the row",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    url = os.environ.get("TELEMETRY_DATABASE_URL")
    if not url:
        print(
            "TELEMETRY_DATABASE_URL is not set. Copy infra/.env.example to "
            ".env and export it, or pass it on the command line.",
            file=sys.stderr,
        )
        return 2

    bound_from = (
        datetime.fromisoformat(args.bound_from)
        if args.bound_from
        else datetime.now(tz=UTC)
    )
    if bound_from.tzinfo is None:
        print(
            "--from must carry a timezone, e.g. 2026-09-23T12:00:00+00:00",
            file=sys.stderr,
        )
        return 2

    drone_id = args.drone_id or uuid4()
    address = SourceId(sysid=args.sysid, compid=args.compid)

    engine = create_async_engine(url)
    resolver = BindingResolver(engine=engine)
    try:
        if args.retire:
            return await retire(resolver, engine, args, address, bound_from)
        await resolver.register_drone(drone_id, args.label, serial=args.serial)
        print(
            f"known_drones: {drone_id}  {args.label}"
            + (f"  serial {args.serial}" if args.serial else "")
        )

        if args.rebind:
            closed = await resolver.close_binding(args.station, address, at=bound_from)
            print(f"closed {closed} open binding(s) for {args.station} {address}")

        try:
            await resolver.bind(
                args.station,
                address,
                drone_id,
                bound_from=bound_from,
                created_by=args.by,
                note="tools/register_aircraft.py; P2-05 replaces this",
            )
        except BindingConflictError as error:
            print(f"refused: {error}", file=sys.stderr)
            print(
                "If this address was bound to another airframe, close that "
                "binding first with --rebind.",
                file=sys.stderr,
            )
            return 1

        print(
            f"source_bindings: {args.station} {address} -> {drone_id} "
            f"from {bound_from.isoformat()}"
        )

        async with engine.connect() as connection:
            total = await connection.scalar(
                sa.text("SELECT count(*) FROM source_bindings WHERE station_id = :s"),
                {"s": args.station},
            )
        print(f"{args.station} now has {total} binding(s)")
    finally:
        await engine.dispose()
    return 0


async def retire(
    resolver: BindingResolver,
    engine: Any,
    args: argparse.Namespace,
    address: SourceId,
    at: datetime,
) -> int:
    """Close the binding and mark the drone retired.

    Retired, not deleted. An aircraft that has flown keeps its identity so its
    telemetry stays resolvable - reading a two-year-old flight is exactly when
    the airframe is most likely to be long gone - and the foreign key would
    refuse the delete anyway.
    """
    closed = await resolver.close_binding(args.station, address, at=at)
    print(f"closed {closed} binding(s) for {args.station} {address}")

    async with engine.connect() as connection:
        found = (
            await connection.execute(
                sa.text(
                    "SELECT drone_id, label FROM known_drones WHERE label = :label"
                ),
                {"label": args.label},
            )
        ).all()

    for row in found:
        await resolver.register_drone(row.drone_id, row.label, retired_at=at)
        print(f"retired {row.label} ({row.drone_id})")

    if not found:
        print(f"no drone labelled {args.label!r} to retire", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
