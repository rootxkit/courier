"""Upstream loss detection, checked against frames pymavlink built.

The first version of this analyser read the MAVLink sequence byte at offset 2
for every frame. That is correct for v1 and wrong for v2, where offset 2 is
`incompat_flags` — zero on every unsigned frame. It therefore compared a
constant against itself and reported "0 lost" on any capture whatsoever.

It reported the right answer for the wrong reason, which is the failure mode
CLAUDE.md's testing rule exists for. So these tests pin the offset against the
reference encoder, and every loss assertion has a matching no-loss assertion.
"""

from __future__ import annotations

import struct

import pytest
from pymavlink.dialects.v10 import ardupilotmega as mav1
from pymavlink.dialects.v20 import ardupilotmega as mav2

from tools.analyze_capture import (
    _SEQ_OFFSET_V1,
    _SEQ_OFFSET_V2,
    MAX_PLAUSIBLE_GAP,
    analyse,
    main,
    read_records,
    report,
    sequence_offset,
)

_RECORD_HEADER = struct.Struct("<QqH")


def v2_frame(seq: int, sysid: int = 1, compid: int = 1) -> bytes:
    link = mav2.MAVLink(None, srcSystem=sysid, srcComponent=compid)
    link.signing.sign_outgoing = False
    link.seq = seq
    return bytes(link.heartbeat_encode(2, 3, 0, 0, 4).pack(link))


def v1_frame(seq: int, sysid: int = 1, compid: int = 1) -> bytes:
    link = mav1.MAVLink(None, srcSystem=sysid, srcComponent=compid)
    link.seq = seq
    return bytes(link.heartbeat_encode(2, 3, 0, 0, 4).pack(link))


def records(*datagrams: bytes) -> list[tuple[int, int, bytes]]:
    return [
        (n, 1_700_000_000_000_000_000 + n * 10_000_000, d)
        for n, d in enumerate(datagrams)
    ]


# --- the offset that was wrong ----------------------------------------------


def test_the_sequence_offset_is_derived_not_remembered() -> None:
    """Pack two frames differing only in seq; the differing byte is the offset.

    Checksum bytes differ too, which is why the FIRST differing byte is taken.
    """
    for builder, expected in ((v2_frame, _SEQ_OFFSET_V2), (v1_frame, _SEQ_OFFSET_V1)):
        a, b = builder(17), builder(200)
        differing = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]

        assert differing[0] == expected, (
            f"sequence byte is at offset {differing[0]}, not {expected}"
        )


def test_v2_and_v1_offsets_differ() -> None:
    """The whole reason the first version was broken."""
    assert _SEQ_OFFSET_V2 != _SEQ_OFFSET_V1
    assert sequence_offset(v2_frame(0)) == _SEQ_OFFSET_V2
    assert sequence_offset(v1_frame(0)) == _SEQ_OFFSET_V1


def test_offset_two_of_a_v2_frame_is_not_the_sequence() -> None:
    """Pin the trap itself: incompat_flags is constant across sequences."""
    a, b = v2_frame(5), v2_frame(250)

    assert a[2] == b[2] == 0, "offset 2 varied; the trap has changed shape"


def test_unknown_magic_has_no_offset() -> None:
    assert sequence_offset(b"\x00\x01\x02") is None
    assert sequence_offset(b"") is None


# --- loss detection ---------------------------------------------------------


def test_a_clean_stream_reports_no_loss() -> None:
    sources, _ = analyse(records(*(v2_frame(n) for n in range(60))))

    stats = sources[(1, 1)]
    assert stats.frames == 60
    assert stats.lost == 0
    assert stats.gaps == []


def test_a_missing_frame_is_counted() -> None:
    """The presence half of the pair."""
    kept = [v2_frame(n) for n in range(10) if n != 4]

    sources, _ = analyse(records(*kept))

    stats = sources[(1, 1)]
    assert stats.lost == 1
    assert len(stats.gaps) == 1
    _when, before, after = stats.gaps[0]
    assert (before, after) == (3, 5)


def test_a_run_of_missing_frames_is_counted() -> None:
    kept = [v2_frame(n) for n in range(20) if n not in (5, 6, 7)]

    sources, _ = analyse(records(*kept))

    assert sources[(1, 1)].lost == 3


def test_loss_is_detected_in_v1_too() -> None:
    kept = [v1_frame(n) for n in range(10) if n != 4]

    sources, _ = analyse(records(*kept))

    assert sources[(1, 1)].lost == 1


# --- the 8-bit wrap ---------------------------------------------------------


def test_the_sequence_wrap_is_not_mistaken_for_loss() -> None:
    """254, 255, 0, 1 is continuous, not a 253-frame hole."""
    sources, _ = analyse(records(*(v2_frame(n % 256) for n in range(250, 262))))

    stats = sources[(1, 1)]
    assert stats.lost == 0
    assert stats.gaps == []


def test_loss_across_the_wrap_boundary_is_counted() -> None:
    """254 -> 1 means 255 and 0 are missing: two frames, not 253."""
    sources, _ = analyse(records(v2_frame(254), v2_frame(1)))

    assert sources[(1, 1)].lost == 2


def test_many_wraps_stay_clean() -> None:
    """The real capture wrapped about 224 times."""
    sources, _ = analyse(records(*(v2_frame(n % 256) for n in range(2000))))

    assert sources[(1, 1)].lost == 0


# --- things that are not loss -----------------------------------------------


def test_an_implausibly_large_jump_is_not_reported_as_loss() -> None:
    """A stream that stopped and restarted is not 200 lost frames."""
    sources, _ = analyse(records(v2_frame(10), v2_frame(10 + MAX_PLAUSIBLE_GAP + 5)))

    stats = sources[(1, 1)]
    assert stats.lost == 0
    assert stats.suspicious_jumps == 1


def test_a_repeated_sequence_is_flagged_not_counted_as_loss() -> None:
    sources, _ = analyse(records(v2_frame(7), v2_frame(7)))

    stats = sources[(1, 1)]
    assert stats.lost == 0
    assert stats.suspicious_jumps == 1


# --- per-source tracking ----------------------------------------------------


def test_sources_are_tracked_separately() -> None:
    """QGC and the aircraft have independent sequence counters.

    Merging them would manufacture gaps out of ordinary interleaving — which
    is how a clean capture could be reported as catastrophic loss.
    """
    interleaved: list[bytes] = []
    for n in range(20):
        interleaved.append(v2_frame(n, sysid=1, compid=1))
        interleaved.append(v2_frame(200 + n, sysid=255, compid=190))

    sources, _ = analyse(records(*interleaved))

    assert sources[(1, 1)].lost == 0
    assert sources[(255, 190)].lost == 0
    assert sources[(1, 1)].frames == 20
    assert sources[(255, 190)].frames == 20


def test_a_component_under_the_same_sysid_is_its_own_source() -> None:
    """A gimbal on SYSID 1 has a different sequence counter from the autopilot."""
    frames = [v2_frame(n, sysid=1, compid=1) for n in range(10)]
    frames += [v2_frame(n, sysid=1, compid=154) for n in range(100, 110)]

    sources, _ = analyse(records(*frames))

    assert set(sources) == {(1, 1), (1, 154)}
    assert all(stats.lost == 0 for stats in sources.values())


# --- datagram packing -------------------------------------------------------


def test_multiple_frames_in_one_datagram_are_all_counted() -> None:
    """QGC may coalesce; the analyser must not count datagrams as frames."""
    coalesced = b"".join(v2_frame(n) for n in range(5))

    sources, datagram_times = analyse(records(coalesced))

    assert sources[(1, 1)].frames == 5
    assert len(datagram_times) == 1
    assert datagram_times[0][1] == 5


# --- reading the capture file -----------------------------------------------


def test_read_records_round_trips(tmp_path: object) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    path = tmp_path / "x.records"
    payloads = [v2_frame(n) for n in range(4)]
    blob = b"".join(
        _RECORD_HEADER.pack(n, 1_000 + n, len(p)) + p for n, p in enumerate(payloads)
    )
    path.write_bytes(blob)

    out = read_records(path)

    assert [seq for seq, _, _ in out] == [0, 1, 2, 3]
    assert [d for _, _, d in out] == payloads


def test_a_truncated_tail_is_ignored_not_guessed(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    """A process killed mid-write leaves a partial record; earlier ones stand."""
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    path = tmp_path / "x.records"
    good = v2_frame(1)
    blob = _RECORD_HEADER.pack(0, 1_000, len(good)) + good
    blob += _RECORD_HEADER.pack(1, 2_000, 99) + b"short"
    path.write_bytes(blob)

    out = read_records(path)

    assert len(out) == 1


# --- the report and the CLI -------------------------------------------------


def write_capture(directory: object, frames: list[bytes]) -> object:
    """Write frames into a sink-shaped capture directory, one per datagram."""
    from pathlib import Path

    assert isinstance(directory, Path)
    station = directory / "station-a"
    station.mkdir(parents=True, exist_ok=True)
    path = station / "epoch.records"
    blob = b"".join(
        _RECORD_HEADER.pack(n, 1_700_000_000_000_000_000 + n * 12_000_000, len(f)) + f
        for n, f in enumerate(frames)
    )
    path.write_bytes(blob)
    return directory


def test_report_says_clean_for_a_clean_capture(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    sources, times = analyse(records(*(v2_frame(n % 256) for n in range(120))))

    clean = report(sources, times, bucket_seconds=10, mark_from=None, mark_to=None)

    out = capsys.readouterr().out
    assert clean is True
    assert "UPSTREAM: CLEAN" in out
    assert out.isascii(), "the report is pasted into a test record"


def test_report_says_lost_when_frames_are_missing(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    """The presence half. A clean-only test would not notice a broken detector."""
    kept = [v2_frame(n) for n in range(40) if n not in (11, 12)]
    sources, times = analyse(records(*kept))

    clean = report(sources, times, bucket_seconds=10, mark_from=None, mark_to=None)

    out = capsys.readouterr().out
    assert clean is False
    assert "2 FRAME(S) LOST" in out
    assert "seq 10 -> 13" in out


def test_report_handles_an_empty_capture(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean = report({}, [], bucket_seconds=10, mark_from=None, mark_to=None)

    assert clean is False
    assert "NO RECORDS" in capsys.readouterr().out


def test_the_outage_window_is_marked(capsys: pytest.CaptureFixture[str]) -> None:
    sources, times = analyse(records(*(v2_frame(n % 256) for n in range(200))))
    from tools.analyze_capture import _parse_mark, _seconds_of_day

    # Mark from the first record's second so the first bucket falls inside.
    start = _seconds_of_day(times[0][0])

    report(
        sources,
        times,
        bucket_seconds=10,
        mark_from=start,
        mark_to=start + 30,
    )

    assert "<-- outage" in capsys.readouterr().out
    assert _parse_mark("01:02:03") == 3723
    assert _parse_mark(None) is None


def test_main_exits_zero_on_a_clean_capture(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    write_capture(tmp_path, [v2_frame(n % 256) for n in range(60)])

    assert main([str(tmp_path)]) == 0
    assert "UPSTREAM: CLEAN" in capsys.readouterr().out


def test_main_exits_one_when_frames_were_lost(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-zero exit so a CI step or a script can gate on it."""
    write_capture(tmp_path, [v2_frame(n) for n in range(30) if n != 8])

    assert main([str(tmp_path)]) == 1
    assert "LOST" in capsys.readouterr().out


def test_main_accepts_a_single_records_file(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    write_capture(tmp_path, [v2_frame(n) for n in range(20)])

    assert main([str(tmp_path / "station-a" / "epoch.records")]) == 0


def test_main_reports_a_missing_directory(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)

    assert main([str(tmp_path / "absent")]) == 2
    assert "no .records files" in capsys.readouterr().err
