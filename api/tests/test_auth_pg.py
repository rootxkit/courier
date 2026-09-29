"""Operator accounts and sessions against the real relational database. P6-08.

Sessions, lockout and revocation are claims about rows, so they are checked
against PostgreSQL. A clock is injected so expiry and idle timeout are
tested by moving time, not by waiting. Every account has a unique name,
because the database is shared across the session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from api.actors import SYSTEM
from api.app import create_api_app
from api.auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthError,
    OperatorStore,
    Role,
    ScryptCost,
)
from api.registry import FleetRegistry
from gateway.binding import BindingResolver

pytestmark = pytest.mark.postgres

PASSWORD = "a long enough password"
FEED_SECRET = b"auth-pg-test-feed-secret-0123456789ab"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(relational_engine: AsyncEngine, clock: Clock) -> OperatorStore:
    return OperatorStore(
        engine=relational_engine,
        session_ttl_s=3600,
        idle_timeout_s=600,
        max_failed_logins=3,
        lockout_s=900,
        cost=ScryptCost(n_log2=10),
        clock=clock,
    )


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


async def make(store: OperatorStore, role: Role = Role.VIEWER) -> dict[str, Any]:
    return await store.create_operator(
        username=unique("op"), display_name="Op", role=role, password=PASSWORD
    )


async def events_for(engine: AsyncEngine, operator_id: UUID) -> list[tuple[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT event_type, payload FROM events "
                "WHERE entity_type = 'operator' AND entity_id = :id ORDER BY id"
            ),
            {"id": str(operator_id)},
        )
        return [(row.event_type, row.payload) for row in rows]


# --- accounts -------------------------------------------------------------------


async def test_an_account_is_created_and_audited(
    store: OperatorStore, relational_engine: AsyncEngine
) -> None:
    created = await make(store, Role.OPERATOR)

    assert created["role"] == "operator"
    assert [e for e, _ in await events_for(relational_engine, created["id"])] == [
        "operator_created"
    ]


async def test_the_password_is_not_stored(
    store: OperatorStore, relational_engine: AsyncEngine
) -> None:
    created = await make(store)
    async with relational_engine.connect() as connection:
        stored = (
            await connection.execute(
                sa.text("SELECT password_hash FROM operators WHERE id = :id"),
                {"id": created["id"]},
            )
        ).scalar_one()

    assert PASSWORD not in stored
    assert stored.startswith("scrypt$")


async def test_a_username_is_taken_regardless_of_case(store: OperatorStore) -> None:
    name = unique("case")
    await store.create_operator(
        username=name, display_name="", role=Role.VIEWER, password=PASSWORD
    )

    with pytest.raises(AuthError) as refused:
        await store.create_operator(
            username=name.upper(), display_name="", role=Role.VIEWER, password=PASSWORD
        )

    assert refused.value.kind == "conflict"


# --- sign-in --------------------------------------------------------------------------


async def test_the_right_password_opens_a_session(
    store: OperatorStore, relational_engine: AsyncEngine
) -> None:
    created = await make(store, Role.ADMIN)

    login = await store.login(created["username"], PASSWORD, remote_addr="10.0.0.9")
    operator = await store.session(login.token)

    assert operator is not None
    assert operator.id == created["id"] and operator.role is Role.ADMIN
    assert (await events_for(relational_engine, created["id"]))[-1][0] == "login"


async def test_the_session_token_is_not_stored(
    store: OperatorStore, relational_engine: AsyncEngine
) -> None:
    login = await store.login((await make(store))["username"], PASSWORD)
    async with relational_engine.connect() as connection:
        found = (
            await connection.execute(
                sa.text(
                    "SELECT count(*) FROM operator_sessions "
                    "WHERE encode(token_sha256, 'escape') LIKE :t"
                ),
                {"t": f"%{login.token}%"},
            )
        ).scalar_one()

    assert found == 0


async def test_a_wrong_password_is_refused_and_audited(
    store: OperatorStore, relational_engine: AsyncEngine
) -> None:
    created = await make(store)

    with pytest.raises(AuthError) as refused:
        await store.login(created["username"], "not the password at all")

    assert refused.value.kind == "invalid_credentials"
    event, payload = (await events_for(relational_engine, created["id"]))[-1]
    assert event == "login_failed" and payload["reason"] == "wrong_password"


async def test_an_unknown_name_is_refused_the_same_way(
    store: OperatorStore, relational_engine: AsyncEngine
) -> None:
    name = unique("nobody")

    with pytest.raises(AuthError) as refused:
        await store.login(name, PASSWORD)

    assert refused.value.kind == "invalid_credentials"
    async with relational_engine.connect() as connection:
        payload = (
            await connection.execute(
                sa.text(
                    "SELECT payload FROM events WHERE event_type = 'login_failed' "
                    "AND payload->>'username' = :name"
                ),
                {"name": name},
            )
        ).scalar_one()
    assert payload["reason"] == "unknown_username"


async def test_repeated_failures_lock_the_account_even_against_the_right_password(
    store: OperatorStore,
) -> None:
    created = await make(store)
    for _ in range(3):
        with pytest.raises(AuthError):
            await store.login(created["username"], "wrong wrong wrong")

    with pytest.raises(AuthError):
        await store.login(created["username"], PASSWORD)


async def test_fewer_failures_than_the_limit_do_not_lock(store: OperatorStore) -> None:
    """The paired absence: two failures, then the right password works."""
    created = await make(store)
    for _ in range(2):
        with pytest.raises(AuthError):
            await store.login(created["username"], "wrong wrong wrong")

    login = await store.login(created["username"], PASSWORD)

    assert await store.session(login.token) is not None


async def test_the_lock_lifts_after_the_lockout(
    store: OperatorStore, clock: Clock
) -> None:
    created = await make(store)
    for _ in range(3):
        with pytest.raises(AuthError):
            await store.login(created["username"], "wrong wrong wrong")

    clock.advance(901)
    login = await store.login(created["username"], PASSWORD)

    assert await store.session(login.token) is not None


# --- sessions ---------------------------------------------------------------------


async def test_a_session_ends_after_its_lifetime(
    store: OperatorStore, clock: Clock
) -> None:
    login = await store.login((await make(store))["username"], PASSWORD)
    for _ in range(6):
        clock.advance(599)
        assert await store.session(login.token) is not None

    clock.advance(599)

    assert await store.session(login.token) is None


async def test_a_session_ends_when_left_idle(
    store: OperatorStore, clock: Clock
) -> None:
    login = await store.login((await make(store))["username"], PASSWORD)

    clock.advance(601)

    assert await store.session(login.token) is None


async def test_logout_ends_the_session(store: OperatorStore) -> None:
    login = await store.login((await make(store))["username"], PASSWORD)

    assert await store.logout(login.token) is True
    assert await store.session(login.token) is None


@pytest.mark.parametrize("change", ["disable", "set_password", "set_role"])
async def test_an_account_change_ends_its_sessions(
    store: OperatorStore, change: str
) -> None:
    admin = await make(store, Role.ADMIN)
    target = await make(store)
    login = await store.login(target["username"], PASSWORD)
    actor = await store.session((await store.login(admin["username"], PASSWORD)).token)
    assert actor is not None

    if change == "disable":
        await store.disable(target["id"], actor=actor.actor)
    elif change == "set_password":
        await store.set_password(
            target["id"], "another long password", actor=actor.actor
        )
    else:
        await store.set_role(target["id"], Role.OPERATOR, actor=actor.actor)

    assert await store.session(login.token) is None


async def test_a_disabled_account_cannot_sign_in_and_enabling_restores_it(
    store: OperatorStore,
) -> None:
    target = await make(store)
    await store.disable(target["id"], actor=SYSTEM)

    with pytest.raises(AuthError):
        await store.login(target["username"], PASSWORD)

    await store.enable(target["id"], actor=SYSTEM)
    assert (await store.login(target["username"], PASSWORD)).token


# --- through HTTP, with the real store --------------------------------------------


@pytest.fixture
async def http(
    store: OperatorStore, relational_engine: AsyncEngine, engine: AsyncEngine
) -> AsyncIterator[AsyncClient]:
    registry = FleetRegistry(
        engine=relational_engine,
        projection=BindingResolver(engine=engine),
        live=_NoLive(),
    )
    app = create_api_app(
        registry,
        auth=store,
        feed_secret=FEED_SECRET,
        feed_ticket_ttl_s=60,
        cookie_secure=False,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


class _NoLive:
    async def get(self, drone_id: UUID) -> dict[str, Any] | None:
        return None


async def test_a_change_through_the_api_names_the_operator_who_made_it(
    store: OperatorStore, http: AsyncClient, relational_engine: AsyncEngine
) -> None:
    """The point of named accounts: the audit log says who."""
    admin = await make(store, Role.ADMIN)
    signed_in = await http.post(
        "/auth/login", json={"username": admin["username"], "password": PASSWORD}
    )
    assert signed_in.status_code == 200
    assert "token" not in signed_in.json()
    assert http.cookies.get(SESSION_COOKIE)

    created = await http.post(
        "/bases",
        json={"name": unique("base"), "lat_deg": 41.7, "lon_deg": 44.8},
        headers={CSRF_HEADER: "1"},
    )

    assert created.status_code == 201, created.text
    async with relational_engine.connect() as connection:
        row = (
            await connection.execute(
                sa.text(
                    "SELECT actor_type, actor_id FROM events "
                    "WHERE entity_type = 'base' AND entity_id = :id"
                ),
                {"id": created.json()["id"]},
            )
        ).one()
    assert (row.actor_type, row.actor_id) == ("operator", str(admin["id"]))


async def test_after_logout_the_cookie_opens_nothing(
    store: OperatorStore, http: AsyncClient
) -> None:
    viewer = await make(store)
    await http.post(
        "/auth/login", json={"username": viewer["username"], "password": PASSWORD}
    )
    token = http.cookies.get(SESSION_COOKIE)
    assert (await http.get("/auth/me")).status_code == 200

    await http.post("/auth/logout")
    http.cookies.set(SESSION_COOKIE, str(token))

    assert (await http.get("/auth/me")).status_code == 401


async def test_a_wrong_password_over_http_is_401_and_sets_nothing(
    store: OperatorStore, http: AsyncClient
) -> None:
    viewer = await make(store)

    response = await http.post(
        "/auth/login",
        json={"username": viewer["username"], "password": "nope nope nope"},
    )

    assert response.status_code == 401
    assert SESSION_COOKIE not in response.cookies
