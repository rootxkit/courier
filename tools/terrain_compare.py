"""Compare the DEM with ArduPilot's own terrain, from the raw archive. P5-00.

    python -m tools.terrain_compare --archive local/e2e/archive --terrain local/terrain

P5-00 is done when the disagreement between the two sources is a measured
number rather than an assumption. This reads every `TERRAIN_REPORT` in the
archive (what a flight controller says the ground under it is, from its own
terrain database), looks up the DEM at the same position, and reports the
difference per aircraft and per DEM dataset.

A report with `loaded == 0` is not an answer: the flight controller has no
terrain data and says 0.0 m. Those are counted and left out, never compared
(the real aircraft sent nothing else, 2026-09-24/25).

The two are not the same quantity, so the number is a disagreement, not an
error of either: ArduPilot's terrain is SRTM-derived ground, on its own
vertical reference; Copernicus is the surface (roofs, canopy) on EGM2008.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from common.terrain import Terrain
from gateway.archive import RawArchive
from gateway.parsing import parse_datagram


@dataclass
class Tally:
    differences_m: list[float] = field(default_factory=list)
    not_loaded: int = 0
    outside_dem: int = 0

    def summary(self) -> dict[str, float | int | None]:
        d = self.differences_m
        magnitudes = sorted(abs(x) for x in d)
        return {
            "compared": len(d),
            "not_loaded": self.not_loaded,
            "outside_dem": self.outside_dem,
            "mean_dem_minus_fc_m": round(statistics.fmean(d), 2) if d else None,
            "median_dem_minus_fc_m": round(statistics.median(d), 2) if d else None,
            "stdev_m": round(statistics.stdev(d), 2) if len(d) > 1 else None,
            "p95_abs_m": (
                round(magnitudes[min(len(d) - 1, math.ceil(0.95 * len(d)) - 1)], 2)
                if d
                else None
            ),
            "max_abs_m": round(magnitudes[-1], 2) if d else None,
        }


def compare(archive_root: Path, terrain: Terrain) -> dict[str, Tally]:
    """Tallies keyed by "sysid <n> / <dataset>" (dataset "-" if not compared)."""
    archive = RawArchive(root=archive_root)
    tallies: dict[str, Tally] = defaultdict(Tally)
    for segment in sorted(archive_root.rglob("*.zst")):
        relative = segment.relative_to(archive_root).as_posix()
        for record in archive.read_segment(relative):
            for message in parse_datagram(record.datagram).messages:
                if message.name != "TERRAIN_REPORT":
                    continue
                report = message.payload
                sysid = message.source.sysid
                if int(report.loaded) == 0:
                    tallies[f"sysid {sysid} / -"].not_loaded += 1
                    continue
                lat, lon = report.lat / 1e7, report.lon / 1e7
                dem = terrain.elevation(lat, lon)
                if dem is None:
                    tallies[f"sysid {sysid} / -"].outside_dem += 1
                    continue
                tallies[f"sysid {sysid} / {dem.dataset}"].differences_m.append(
                    dem.elevation_m - float(report.terrain_height)
                )
    return dict(tallies)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.terrain_compare")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--terrain", type=Path, required=True)
    parser.add_argument("--json", type=Path, help="also write the summary here")
    args = parser.parse_args(argv)

    tallies = compare(args.archive, Terrain(args.terrain))
    summary = {key: tally.summary() for key, tally in sorted(tallies.items())}
    for key, row in summary.items():
        print(key, row)
    if not summary:
        print("no TERRAIN_REPORT messages in the archive")
    if args.json:
        args.json.write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
