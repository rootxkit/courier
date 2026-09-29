"""Manage operator accounts from the command line. P6-08.

    python tools/operators.py create-admin <username> [--name "Display Name"]
    python tools/operators.py create <username> --role viewer|operator|admin
    python tools/operators.py list
    python tools/operators.py set-password <username>
    python tools/operators.py enable <username>

`create-admin` is how the first account exists: the API refuses anonymous
requests, including one to create an account. Passwords are read from the
terminal without echo, never from the command line, where they would end up
in shell history and process listings. `--password-stdin` reads one line from
standard input instead, for provisioning scripts that pipe it from a secret
store; it is never echoed either. Everything is recorded in `events`
as done by the system actor, since no operator is signed in.

In tools/ because it answers the person at the terminal - its output is the
deliverable - and only tools/ and tests may print (tests/test_layout.py).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from api.actors import SYSTEM
from api.auth import AuthError, OperatorStore, Role
from api.config import ApiSettings
from common import load_settings


def _store(engine: AsyncEngine, settings: ApiSettings) -> OperatorStore:
    return OperatorStore(
        engine=engine,
        session_ttl_s=settings.session_ttl_s,
        idle_timeout_s=settings.session_idle_timeout_s,
        max_failed_logins=settings.login_max_failures,
        lockout_s=settings.login_lockout_s,
    )


def _read_new_password(*, from_stdin: bool = False) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("New password (12+ characters): ")
    second = getpass.getpass("Repeat it: ")
    if first != second:
        raise SystemExit("The two passwords differ; nothing was changed.")
    return first


async def _id_of(engine: AsyncEngine, username: str) -> UUID:
    async with engine.connect() as connection:
        found = (
            await connection.execute(
                sa.text("SELECT id FROM operators WHERE username = :name"),
                {"name": username.strip().lower()},
            )
        ).scalar_one_or_none()
    if found is None:
        raise SystemExit(f"No operator {username!r}.")
    return UUID(str(found))


async def _run(args: argparse.Namespace) -> int:
    settings = load_settings(ApiSettings)
    engine = create_async_engine(str(settings.database_url))
    store = _store(engine, settings)
    try:
        if args.command in ("create-admin", "create"):
            role = Role.ADMIN if args.command == "create-admin" else Role(args.role)
            password = _read_new_password(from_stdin=args.password_stdin)
            created = await store.create_operator(
                username=args.username,
                display_name=args.name or args.username,
                role=role,
                password=password,
                actor=SYSTEM,
            )
            print(f"Created {role.value} {created['username']} ({created['id']}).")
        elif args.command == "list":
            for row in await store.list_operators():
                state = "disabled" if row["disabled_at"] else "active"
                print(f"{row['username']:<24} {row['role']:<9} {state}")
        elif args.command == "set-password":
            operator_id = await _id_of(engine, args.username)
            await store.set_password(
                operator_id,
                _read_new_password(from_stdin=args.password_stdin),
                actor=SYSTEM,
            )
            print("Password set; the operator's sessions were ended.")
        elif args.command == "enable":
            operator_id = await _id_of(engine, args.username)
            await store.enable(operator_id, actor=SYSTEM)
            print("Enabled, and any lockout cleared.")
        elif args.command == "disable":
            operator_id = await _id_of(engine, args.username)
            await store.disable(operator_id, actor=SYSTEM)
            print("Disabled; the operator's sessions were ended.")
    except AuthError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/operators.py")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="create an admin account")
    admin.add_argument("username")
    admin.add_argument("--name", help="display name")
    admin.add_argument("--password-stdin", action="store_true")
    create = commands.add_parser("create", help="create an account with a role")
    create.add_argument("username")
    create.add_argument("--role", required=True, choices=[r.value for r in Role])
    create.add_argument("--name", help="display name")
    create.add_argument("--password-stdin", action="store_true")
    commands.add_parser("list", help="list accounts")
    reset = commands.add_parser("set-password", help="set an account's password")
    reset.add_argument("username")
    reset.add_argument("--password-stdin", action="store_true")
    enable = commands.add_parser("enable", help="re-enable and unlock an account")
    enable.add_argument("username")
    disable = commands.add_parser("disable", help="disable an account")
    disable.add_argument("username")
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
