"""P1-09: link quality from sequence numbers and heartbeat gaps.

The criterion is that the metric degrades measurably under simulated packet
loss. The pipeline tests below do exactly that with real frames. A single
pymavlink sender packs a stream, so its sequence numbers are the ones a real
aircraft would send. Datagrams are then dropped on the way in, and the loss
must come out as the share that was dropped. The paired no-loss run must come
out as zero, not merely "low".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gateway.classify import HEARTBEAT_ID
from gateway.link_quality import LinkQualityTracker
from gateway.parsing import ParsedMessage, SourceId
from gateway.tests.test_pipeline import DRONE, EPOCH, build, link, record

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
ADDRESS = SourceId(sysid=1, compid=1)


class Frame:
    """Stands in for a pymavlink message: only get_seq() is read."""

    def __init__(self, seq: int) -> None:
        self.seq = seq

    def get_seq(self) -> int:
        return self.seq


def message(seq: int, *, heartbeat: bool = False) -> ParsedMessage:
    return ParsedMessage(
        source=ADDRESS,
        message_id=HEARTBEAT_ID if heartbeat else 33,
        name="HEARTBEAT" if heartbeat else "GLOBAL_POSITION_INT",
        payload=Frame(seq % 256),
    )


def feed(tracker: LinkQualityTracker, seqs: list[int], step_s: float = 0.1) -> None:
    for n, seq in enumerate(seqs):
        tracker.observe(message(seq), T0 + timedelta(seconds=n * step_s))


# --- the tracker -----------------------------------------------------------


def test_nothing_observed_is_not_a_perfect_link() -> None:
    assert LinkQualityTracker().snapshot().loss_pct is None


def test_a_complete_sequence_has_no_loss() -> None:
    tracker = LinkQualityTracker()
    feed(tracker, list(range(50)))

    assert tracker.snapshot().loss_pct == 0.0


def test_every_fifth_frame_missing_is_twenty_percent() -> None:
    tracker = LinkQualityTracker()
    feed(tracker, [n for n in range(100) if n % 5 != 4])

    snapshot = tracker.snapshot()
    assert snapshot.lost == 19  # the 100th frame, seq 99, is never followed
    assert snapshot.loss_pct == pytest.approx(100 * 19 / (80 + 19))


def test_the_sequence_wrapping_at_256_is_not_loss() -> None:
    tracker = LinkQualityTracker()
    feed(tracker, list(range(250, 262)))

    assert tracker.snapshot().lost == 0


def test_a_sender_restart_is_not_counted_as_200_lost_frames() -> None:
    """A reboot mid-flight must not read as a catastrophic link."""
    tracker = LinkQualityTracker()
    feed(tracker, [40, 41, 42, 0, 1, 2])

    assert tracker.snapshot().lost == 0


def test_loss_older_than_the_window_is_forgotten() -> None:
    tracker = LinkQualityTracker(window_s=5.0)
    feed(tracker, [0, 5, 10], step_s=1.0)  # 8 lost, at t=1 s and t=2 s
    assert tracker.snapshot().lost == 8

    tracker.observe(message(11), T0 + timedelta(seconds=30))

    assert tracker.snapshot().lost == 0


def test_the_longest_heartbeat_gap_is_reported() -> None:
    tracker = LinkQualityTracker()
    for n, second in enumerate([0.0, 1.0, 2.0, 5.5, 6.5]):
        tracker.observe(message(n, heartbeat=True), T0 + timedelta(seconds=second))

    assert tracker.snapshot().heartbeat_gap_max_s == pytest.approx(3.5)


# --- through the pipeline, with real frames and simulated loss -------------


def stream(count: int) -> list[bytes]:
    """One sender, so the sequence numbers are the ones an aircraft sends:
    a HEARTBEAT, then positions, every frame from the same MAVLink object.

    pymavlink advances the sequence number in `send()`, not in `pack()`, so
    packing alone gives every frame seq 0 - and a stream with no sequence has
    no loss to find. The first version of this test did exactly that, and the
    zero-loss test beside it passed for the wrong reason. The sequence is
    advanced here as `send()` would, and `sequence_of` checks it is real.
    """
    sender = link()
    encoded = [sender.heartbeat_encode(2, 3, 1, 4, 4)]  # quadrotor, ArduPilot
    encoded += [
        sender.global_position_int_encode(
            0, 417151000, 448271000, 450000, 60000, 0, 0, 0, 9000
        )
        for _ in range(count)
    ]
    frames = []
    for message_to_send in encoded:
        frames.append(bytes(message_to_send.pack(sender)))
        sender.seq = (sender.seq + 1) % 256
    return frames


def sequence_of(frames: list[bytes]) -> list[int]:
    """The sequence numbers the frames actually carry, decoded by pymavlink."""
    decoder = link()
    return [decoder.decode(bytearray(frame)).get_seq() for frame in frames]


async def published_link(frames: list[bytes]) -> dict[str, Any]:
    pipeline, _, _, publisher = build()
    batch = [
        record(n, frame, offset_ns=n * 25_000_000) for n, frame in enumerate(frames)
    ]
    await pipeline.process(EPOCH, batch)
    return publisher.links[DRONE]


def test_the_stream_carries_a_real_sequence() -> None:
    """Guards the two tests below: without it, zero loss proves nothing."""
    assert sequence_of(stream(10)) == list(range(11))


async def test_without_loss_the_published_link_says_zero() -> None:
    link_quality = await published_link(stream(100))

    assert link_quality["received"] == 101
    assert link_quality["lost"] == 0
    assert link_quality["loss_pct"] == 0.0


async def test_simulated_packet_loss_shows_in_the_published_link() -> None:
    """The P1-09 criterion. A fifth of the positions are dropped between the
    sender and the Gateway, and the published loss must say so."""
    frames = stream(100)
    kept = [frames[0]] + [frame for n, frame in enumerate(frames[1:]) if n % 5 != 2]

    link_quality = await published_link(kept)

    assert link_quality["lost"] == 20
    assert link_quality["loss_pct"] == pytest.approx(100 * 20 / 101, abs=0.01)
