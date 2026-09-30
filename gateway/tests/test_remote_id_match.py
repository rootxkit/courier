"""One of our aircraft broadcasting Remote ID is one track. P1-15."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest

from gateway import odid, remote_id_match
from gateway.remote_id import RemoteIdTracker
from gateway.remote_id_match import (
    FleetSerials,
    LinkFreshness,
    Registered,
    as_registered,
)
from gateway.tests.rid_frames import FlatGeoid, basic, frame, location, pack

OURS = Registered(drone_id=UUID(int=42), label="hexa-01")


def observation(
    ua_id: str = "SN-OURS-1", id_type: int = odid.IdType.SERIAL_NUMBER
) -> dict[str, Any]:
    found = RemoteIdTracker(geoid=FlatGeoid()).take(
        frame(pack(basic(ua_id, id_type), location())), now_s=0.0
    )
    assert found is not None
    return found


def test_a_registered_serial_number_matches() -> None:
    fleet = FleetSerials(by_serial={"SN-OURS-1": OURS})
    assert fleet.match(observation()) == OURS
    assert fleet.match(observation("SN-STRANGER")) is None


def test_a_registration_id_equal_to_our_serial_does_not_match() -> None:
    """A registration can move between airframes; a serial cannot."""
    fleet = FleetSerials(by_serial={"SN-OURS-1": OURS})
    assert fleet.match(observation(id_type=odid.IdType.CAA_REGISTRATION_ID)) is None


def telemetry(drone_id: UUID, **extra: object) -> bytes:
    return json.dumps({"drone_id": str(drone_id), **extra}).encode()


def test_mavlink_telemetry_makes_a_link_live_for_a_while() -> None:
    links = LinkFreshness(live_for_s=5.0)
    assert not links.live(OURS.drone_id, now_s=0.0)

    links.on_telemetry(telemetry(OURS.drone_id), now_s=10.0)

    assert links.live(OURS.drone_id, now_s=15.0)
    assert not links.live(OURS.drone_id, now_s=15.1)


def test_a_remote_id_observation_does_not_count_as_a_live_link() -> None:
    """Else the ingest's own publication would keep withholding itself."""
    links = LinkFreshness()
    links.on_telemetry(telemetry(OURS.drone_id, source="remote_id"), now_s=0.0)
    assert not links.live(OURS.drone_id, now_s=0.0)


def test_an_unreadable_message_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    said: list[str] = []
    monkeypatch.setattr(
        remote_id_match._log, "warning", lambda message, *a, **k: said.append(message)
    )
    LinkFreshness().on_telemetry(b"not json", now_s=0.0)
    LinkFreshness().on_telemetry(b'{"no": "id"}', now_s=0.0)
    assert len(said) == 2


def test_as_registered_takes_our_identity_and_stays_a_broadcast() -> None:
    published = as_registered(observation(), OURS)

    assert (published["drone_id"], published["label"]) == (
        str(OURS.drone_id),
        "hexa-01",
    )
    assert published["source"] == "remote_id"
    assert published["authenticated"] is False
    assert published["remote_id"]["matched"] is True
    assert published["remote_id"]["ua_id"] == "SN-OURS-1"
