"""Flight replay: where the track is cut, and what it says about why. P10-03.

Pure: no database. The rule under test is TASKS.md P10-03's - a hole in the
record is shown as a hole and never drawn across - so every test that asserts
a track is *not* cut is paired with one that makes the same input cut it
(CLAUDE.md, "test presence, not only absence").
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from api.replay import (
    Evidence,
    EvidenceKind,
    HoleCause,
    Replay,
    Sample,
    build_replay,
    gap_evidence,
    link_state_evidence,
)

T0 = datetime(2026, 9, 28, 20, 26, tzinfo=UTC)
STATION = "station-a"
OTHER = "station-b"
THRESHOLD_S = 3.0
SLACK_S = 5.0


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def sample(
    seconds: float, *, station: str = STATION, positioned: bool = True
) -> Sample:
    return Sample(
        ts=at(seconds),
        station_id=station,
        lat_deg=41.7 + seconds * 1e-5 if positioned else None,
        lon_deg=44.8 if positioned else None,
    )


def every(start_s: float, end_s: float, step_s: float = 0.25) -> list[Sample]:
    count = round((end_s - start_s) / step_s)
    return [sample(start_s + i * step_s) for i in range(count + 1)]


def replay(samples: list[Sample], evidence: list[Evidence] | None = None) -> Replay:
    return build_replay(
        sorted(samples, key=lambda s: (s.ts, s.station_id)),
        evidence or [],
        gap_threshold_s=THRESHOLD_S,
        evidence_slack_s=SLACK_S,
    )


def exact_gap(
    start_s: float, end_s: float, *, station: str = STATION, missing: int = 3
) -> Evidence:
    return Evidence(
        kind=EvidenceKind.RELAY_GAP,
        station_id=station,
        start=at(start_s),
        end=at(end_s),
        exact=True,
        detail={"missing_count": missing},
    )


# --- silence ------------------------------------------------------------------


def test_a_continuous_record_is_one_segment_with_no_holes() -> None:
    result = replay(every(0, 10))

    assert result.holes == []
    assert result.segments == [(0, 40)]


def test_silence_longer_than_the_threshold_cuts_the_track() -> None:
    """The paired presence: the same record with ten seconds taken out."""
    result = replay(every(0, 10) + every(20, 30))

    assert len(result.holes) == 1
    hole = result.holes[0]
    assert hole.cause is HoleCause.NO_TELEMETRY
    assert (hole.after_ts, hole.before_ts) == (at(10), at(20))
    assert hole.duration_s == 10.0
    # Nothing is drawn between the two runs.
    assert result.segments == [(0, 40), (41, 81)]


def test_silence_at_exactly_the_threshold_is_not_a_hole() -> None:
    result = replay([sample(0), sample(THRESHOLD_S)])

    assert result.holes == []
    assert result.segments == [(0, 1)]


def test_a_hole_with_nothing_logged_near_it_says_so() -> None:
    """ "No recorded cause" is the finding, not a blank to be filled."""
    far_away = Evidence(
        kind=EvidenceKind.STATION_UNREACHABLE,
        station_id=STATION,
        start=at(100),
        end=at(110),
        exact=True,
    )
    result = replay(every(0, 10) + every(20, 30), [far_away])

    as_dict = result.holes[0].as_dict()
    assert as_dict["explained"] is False
    assert as_dict["reasons"] == []
    assert as_dict["data_lost"] is False


def test_a_hole_is_explained_by_a_station_that_was_unreachable() -> None:
    unreachable = Evidence(
        kind=EvidenceKind.STATION_UNREACHABLE,
        station_id=STATION,
        start=at(11),
        end=at(19),
        exact=True,
    )
    result = replay(every(0, 10) + every(20, 30), [unreachable])

    as_dict = result.holes[0].as_dict()
    assert as_dict["explained"] is True
    assert [r["kind"] for r in as_dict["reasons"]] == ["station_unreachable"]
    # §9: unreachable is buffering, not loss. The replay must not upgrade it.
    assert as_dict["data_lost"] is False


def test_a_hole_with_an_intake_drop_beside_it_is_data_lost() -> None:
    """The paired presence for `data_lost`: same hole, a real loss nearby."""
    drop = Evidence(
        kind=EvidenceKind.INTAKE_DROP,
        station_id=STATION,
        start=at(20) - timedelta(seconds=SLACK_S),
        end=at(20),
        exact=False,
        detail={"datagram_count": 7},
    )
    result = replay(every(0, 10) + every(20, 30), [drop])

    as_dict = result.holes[0].as_dict()
    assert as_dict["data_lost"] is True
    assert as_dict["reasons"][0]["detail"] == {"datagram_count": 7}


def test_evidence_just_beyond_the_slack_is_not_attached() -> None:
    just_outside = Evidence(
        kind=EvidenceKind.STATION_RADIO_SILENT,
        station_id=STATION,
        start=at(20 + SLACK_S + 0.5),
        end=at(40),
        exact=True,
    )
    just_inside = Evidence(
        kind=EvidenceKind.STATION_RADIO_SILENT,
        station_id=STATION,
        start=at(20 + SLACK_S - 0.5),
        end=at(40),
        exact=True,
    )

    assert replay(every(0, 10) + every(20, 30), [just_outside]).holes[0].reasons == []
    assert (
        len(replay(every(0, 10) + every(20, 30), [just_inside]).holes[0].reasons) == 1
    )


# --- relay gaps -------------------------------------------------------------------


def test_a_short_relay_gap_cuts_the_track_below_the_silence_threshold() -> None:
    """Three records lost in under a second: no silence rule would see it,
    and it is still three records nobody will ever see."""
    samples = [sample(0), sample(0.25), sample(0.5), sample(1.5), sample(1.75)]
    gap = exact_gap(0.5, 1.5)

    result = replay(samples, [gap])

    assert gap.track_effect == "cuts"
    assert len(result.holes) == 1
    hole = result.holes[0]
    assert (hole.after_index, hole.before_index) == (2, 3)
    assert [r.kind for r in hole.reasons] == [EvidenceKind.RELAY_GAP]
    assert hole.as_dict()["data_lost"] is True
    assert result.segments == [(0, 2), (3, 4)]


def test_a_relay_gap_heard_over_another_station_does_not_cut_the_track() -> None:
    """The paired absence: the same gap, with the other link hearing the
    aircraft through it. What one station lost, the other delivered."""
    samples = [
        sample(0),
        sample(0.25),
        sample(0.5),
        sample(1.0, station=OTHER),
        sample(1.5),
        sample(1.75),
    ]
    gap = exact_gap(0.5, 1.5)

    result = replay(samples, [gap])

    assert gap.track_effect == "heard_elsewhere"
    assert result.holes == []
    assert result.segments == [(0, 5)]
    # Still listed: the Gateway logged it, and the replay does not drop it.
    assert result.evidence == [gap]


def test_a_relay_gap_before_the_first_sample_lies_outside_the_track() -> None:
    gap = exact_gap(-5, -1)

    result = replay(every(0, 2), [gap])

    assert gap.track_effect == "outside_track"
    assert result.holes == []


def test_an_inexact_relay_gap_never_cuts_the_track() -> None:
    """Placed at the time it was recorded, which for a backlog can be long
    after it happened: cutting the track there would invent a hole."""
    samples = [sample(0), sample(0.25), sample(0.5), sample(1.5), sample(1.75)]
    gap = exact_gap(0.5, 1.5)
    gap.exact = False

    result = replay(samples, [gap])

    assert gap.track_effect == ""
    assert result.holes == []


def test_an_inexact_relay_gap_is_listed_as_a_possible_cause_of_silence() -> None:
    gap = exact_gap(12, 12)
    gap.exact = False

    result = replay(every(0, 10) + every(20, 30), [gap])

    assert result.holes[0].reasons == [gap]


def test_an_exact_gap_heard_elsewhere_is_not_offered_as_a_cause() -> None:
    """It was demonstrably not the cause of anything on this track."""
    covered = exact_gap(11, 12, station=OTHER)
    samples = every(0, 10) + every(20, 30)
    samples.append(sample(11.5, station=STATION))

    result = replay(samples, [covered])

    assert covered.track_effect == "heard_elsewhere"
    assert all(covered not in hole.reasons for hole in result.holes)


# --- position -------------------------------------------------------------------


def test_telemetry_without_a_position_stops_the_line() -> None:
    samples = [
        *every(0, 1),
        sample(1.25, positioned=False),
        sample(1.5, positioned=False),
        *every(1.75, 2.5),
    ]

    result = replay(samples)

    assert [hole.cause for hole in result.holes] == [HoleCause.NO_POSITION]
    hole = result.holes[0]
    assert (hole.after_index, hole.before_index) == (4, 7)
    assert hole.as_dict()["explained"] is True
    assert result.segments == [(0, 4), (7, 10)]


def test_positionless_samples_across_a_silence_are_one_hole_not_two() -> None:
    samples = [*every(0, 1), sample(1.25, positioned=False), *every(10, 11)]

    result = replay(samples)

    assert [hole.cause for hole in result.holes] == [HoleCause.NO_TELEMETRY]
    assert result.segments == [(0, 4), (6, 10)]


def test_a_single_positioned_sample_is_a_segment_of_its_own() -> None:
    result = replay([sample(0), sample(10), sample(20)])

    assert result.segments == [(0, 0), (1, 1), (2, 2)]
    assert len(result.holes) == 2


def test_no_samples_is_an_empty_replay() -> None:
    result = replay([], [exact_gap(0, 1)])

    assert result.segments == []
    assert result.holes == []


# --- link state ----------------------------------------------------------------


def test_link_states_become_intervals_until_the_next_transition() -> None:
    transitions = [
        (STATION, at(0), "healthy"),
        (STATION, at(10), "unreachable"),
        (STATION, at(20), "healthy"),
        (OTHER, at(5), "radio_silent"),
    ]

    found = link_state_evidence(transitions, window_end=at(60))

    by_kind = {item.kind: item for item in found}
    assert set(by_kind) == {
        EvidenceKind.STATION_UNREACHABLE,
        EvidenceKind.STATION_RADIO_SILENT,
    }
    unreachable = by_kind[EvidenceKind.STATION_UNREACHABLE]
    assert (unreachable.start, unreachable.end) == (at(10), at(20))
    assert unreachable.detail["ongoing"] is False
    # The last transition holds to the end of the window.
    silent = by_kind[EvidenceKind.STATION_RADIO_SILENT]
    assert (silent.start, silent.end, silent.detail["ongoing"]) == (at(5), at(60), True)


def test_healthy_and_data_lost_states_are_not_evidence() -> None:
    """Healthy explains nothing; data_lost is carried by the loss events
    themselves, which say what was lost."""
    transitions = [(STATION, at(0), "healthy"), (STATION, at(5), "data_lost")]

    assert link_state_evidence(transitions, window_end=at(60)) == []


def test_link_states_arriving_out_of_order_are_sorted() -> None:
    transitions = [(STATION, at(20), "healthy"), (STATION, at(10), "lagging")]

    (lagging,) = link_state_evidence(transitions, window_end=at(60))

    assert (lagging.start, lagging.end) == (at(10), at(20))


# --- gaps in time -------------------------------------------------------------------


def ns(when: datetime) -> int:
    return int(when.timestamp() * 1_000_000_000)


def test_a_gap_with_both_neighbours_indexed_is_exact() -> None:
    gap = gap_evidence(
        station_id=STATION,
        from_seq=100,
        to_seq=103,
        reason="queue_cap",
        recorded_at=at(500),
        before_ns=ns(at(10)),
        after_ns=ns(at(11)),
    )

    assert gap.exact is True
    assert (gap.start, gap.end) == (at(10), at(11))
    # to_seq is exclusive (relay-v1 §11): 100, 101, 102.
    assert gap.detail["missing_count"] == 3


def test_a_gap_without_its_neighbours_is_placed_where_it_was_recorded() -> None:
    gap = gap_evidence(
        station_id=STATION,
        from_seq=0,
        to_seq=5,
        reason="queue_cap",
        recorded_at=at(500),
        before_ns=None,
        after_ns=None,
    )

    assert gap.exact is False
    assert (gap.start, gap.end) == (at(500), at(500))


def test_a_gap_with_one_neighbour_is_inexact_and_ordered() -> None:
    gap = gap_evidence(
        station_id=STATION,
        from_seq=0,
        to_seq=5,
        reason="queue_cap",
        recorded_at=at(5),
        before_ns=None,
        after_ns=ns(at(10)),
    )

    assert gap.exact is False
    assert gap.start <= gap.end
