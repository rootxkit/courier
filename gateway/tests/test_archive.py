"""The raw archive: hourly zstd segments under station and epoch.

Truncation gets a test because zstd was measured, not assumed, to return a
short read silently rather than raising. An archive that quietly shortens an
hour is worse than one that refuses to open it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gateway.archive import (
    ArchiveError,
    RawArchive,
    segment_hour,
    segment_relative_path,
)
from gateway.relay_records import Record

EPOCH = "9f2c1b7d4e6a58039ab1c2d3e4f50617"
STATION = "tbilisi-base-1"

# 2026-09-23T14:30:00Z, comfortably inside one hour.
BASE_NS = int(datetime(2026, 9, 23, 14, 30, tzinfo=UTC).timestamp()) * 1_000_000_000


def archive(tmp_path: Path) -> RawArchive:
    return RawArchive(root=tmp_path / "archive")


def records(first_seq: int, count: int, *, base_ns: int = BASE_NS) -> list[Record]:
    return [
        Record(
            seq=first_seq + n,
            recv_utc_ns=base_ns + n * 1_000_000,
            datagram=bytes([(first_seq + n) % 256]) * 24,
        )
        for n in range(count)
    ]


# --- hour arithmetic -------------------------------------------------------


def test_the_hour_is_floored_in_utc() -> None:
    assert segment_hour(BASE_NS) == datetime(2026, 9, 23, 14, 0, tzinfo=UTC)


def test_a_negative_timestamp_floors_downwards() -> None:
    """A station whose clock predates 1970 sends a negative `recv_utc_ns`.

    Truncating towards zero would file it in the following hour - a plausible
    hour, silently wrong, which is the shape of every wire-format bug here.
    """
    one_second_before_epoch = -1_000_000_000
    assert segment_hour(one_second_before_epoch) == datetime(
        1969, 12, 31, 23, 0, tzinfo=UTC
    )


def test_the_path_is_partitioned_by_station_and_epoch() -> None:
    path = segment_relative_path(STATION, EPOCH, segment_hour(BASE_NS))
    assert path == f"{STATION}/{EPOCH}/2026/09/23/14.zst"


# --- round trip ------------------------------------------------------------


def test_records_come_back_exactly_as_they_went_in(tmp_path: Path) -> None:
    store = archive(tmp_path)
    sent = records(0, 50)

    writes = store.append(STATION, EPOCH, sent)

    assert len(writes) == 1
    assert store.read_segment(writes[0].relative_path) == sent


def test_appends_accumulate_in_one_segment(tmp_path: Path) -> None:
    """Each batch is its own zstd frame; reading decodes across all of them."""
    store = archive(tmp_path)
    store.append(STATION, EPOCH, records(0, 10))
    store.append(STATION, EPOCH, records(10, 10))
    writes = store.append(STATION, EPOCH, records(20, 10))

    read_back = store.read_segment(writes[0].relative_path)

    assert [record.seq for record in read_back] == list(range(30))


def test_a_batch_spanning_an_hour_boundary_is_split(tmp_path: Path) -> None:
    """A segment claiming 14:00-15:00 must not contain a 15:00 record.

    The index over these segments exists to answer "what covers this time
    range", and a segment that lies about its range makes it useless.
    """
    store = archive(tmp_path)
    boundary_ns = int(datetime(2026, 9, 23, 14, 59, 59, tzinfo=UTC).timestamp())
    sent = [
        Record(seq=0, recv_utc_ns=boundary_ns * 1_000_000_000, datagram=b"a"),
        Record(seq=1, recv_utc_ns=(boundary_ns + 1) * 1_000_000_000, datagram=b"b"),
    ]

    writes = store.append(STATION, EPOCH, sent)

    assert len(writes) == 2
    assert writes[0].relative_path.endswith("14.zst")
    assert writes[1].relative_path.endswith("15.zst")
    assert (writes[0].first_seq, writes[0].last_seq) == (0, 0)
    assert (writes[1].first_seq, writes[1].last_seq) == (1, 1)


def test_an_empty_batch_writes_nothing(tmp_path: Path) -> None:
    assert archive(tmp_path).append(STATION, EPOCH, []) == []


def test_the_write_reports_what_the_index_needs(tmp_path: Path) -> None:
    store = archive(tmp_path)
    sent = records(100, 5)

    write = store.append(STATION, EPOCH, sent)[0]

    assert write.record_count == 5
    assert (write.first_seq, write.last_seq) == (100, 104)
    assert write.first_recv_utc_ns == sent[0].recv_utc_ns
    assert write.last_recv_utc_ns == sent[-1].recv_utc_ns
    assert write.compressed_bytes > 0
    assert write.uncompressed_bytes > write.compressed_bytes


def test_a_segment_spanning_a_gap_reads_back(tmp_path: Path) -> None:
    """Sequence numbers jump across a recorded gap.

    Refusing to read the hour either side of a hole would make the archive
    useless exactly when it is needed.
    """
    store = archive(tmp_path)
    store.append(STATION, EPOCH, records(0, 5))
    writes = store.append(STATION, EPOCH, records(900, 5))

    read_back = store.read_segment(writes[0].relative_path)

    assert [record.seq for record in read_back] == [
        0,
        1,
        2,
        3,
        4,
        900,
        901,
        902,
        903,
        904,
    ]


# --- durability and damage -------------------------------------------------


def test_the_data_is_on_disk_when_append_returns(tmp_path: Path) -> None:
    """The ack that follows is a promise the relay acts on by deleting.

    Read through a second `RawArchive` instance so nothing in-process can be
    supplying the answer from memory.
    """
    store = archive(tmp_path)
    write = store.append(STATION, EPOCH, records(0, 20))[0]

    reopened = RawArchive(root=tmp_path / "archive")

    assert (tmp_path / "archive" / write.relative_path).stat().st_size > 0
    assert len(reopened.read_segment(write.relative_path)) == 20


def test_a_truncated_segment_is_detected_against_the_index(tmp_path: Path) -> None:
    """zstd returns a short read silently, so the index is what catches it.

    Measured, not assumed: decompressing a truncated frame yields the bytes it
    managed rather than raising.
    """
    store = archive(tmp_path)
    write = store.append(STATION, EPOCH, records(0, 50))[0]
    path = tmp_path / "archive" / write.relative_path

    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) - 12])

    with pytest.raises(ArchiveError, match="truncated or the index is wrong"):
        store.read_segment(
            write.relative_path,
            expected_uncompressed_bytes=write.uncompressed_bytes,
        )


def test_an_intact_segment_passes_the_same_check(tmp_path: Path) -> None:
    """The paired presence test.

    Without it, a check that always raised would pass the test above.
    """
    store = archive(tmp_path)
    write = store.append(STATION, EPOCH, records(0, 50))[0]

    read_back = store.read_segment(
        write.relative_path,
        expected_uncompressed_bytes=write.uncompressed_bytes,
    )

    assert len(read_back) == 50


def test_a_missing_segment_is_an_archive_error(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError, match="could not read"):
        archive(tmp_path).read_segment("nowhere/at/all/00.zst")


# --- names that would escape the root --------------------------------------


@pytest.mark.parametrize(
    "station_id",
    ["../escape", "/absolute", "C:/drive", "", "has space", "."],
)
def test_a_station_id_that_is_not_a_safe_name_is_refused(
    tmp_path: Path, station_id: str
) -> None:
    """Rejected, not sanitised.

    A station id needing sanitisation is a configuration error, and rewriting
    it quietly would file that station's data where nobody looks for it.
    """
    with pytest.raises(ArchiveError, match="not usable as a directory name"):
        archive(tmp_path).append(station_id, EPOCH, records(0, 1))


def test_an_epoch_that_is_not_a_safe_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError, match="not usable as a directory name"):
        archive(tmp_path).append(STATION, "../../etc", records(0, 1))


def test_a_legitimate_station_id_is_accepted(tmp_path: Path) -> None:
    """Paired with the rejection tests: the filter must not reject everything."""
    write = archive(tmp_path).append("tbilisi-base-1.a_2", EPOCH, records(0, 1))[0]
    assert write.relative_path.startswith("tbilisi-base-1.a_2/")
