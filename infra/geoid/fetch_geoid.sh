#!/usr/bin/env bash
# P1-15: fetch the EGM96 geoid grid the Remote ID ingest needs.
#
#   infra/geoid/fetch_geoid.sh [OUT_DIR]
#
#   OUT_DIR  default local/geoid. GEOID_PATH then points at
#            OUT_DIR/egm96-15.pgm.
#
# Remote ID broadcasts height above the WGS-84 ellipsoid; the system uses
# height above mean sea level. The grid is the difference (gateway/geoid.py).
#
# Source: GeographicLib's distribution of EGM96 on a 15-minute grid. EGM96
# itself is the US National Geospatial-Intelligence Agency's model, public
# domain. The archive's SHA-256 is pinned below, so a changed download fails
# instead of quietly moving every Remote ID aircraft up or down.
#
# Needs `curl`, `tar` with bzip2, and `sha256sum`. Nothing here is committed.

set -euo pipefail

out_dir=${1:-local/geoid}
url=https://downloads.sourceforge.net/project/geographiclib/geoids-distrib/egm96-15.tar.bz2
expected_sha256=8b1ebad1ebae0a045502d0edb9cc51553da1d3914f01e07470c11b3bed75048e

for tool in curl tar sha256sum; do
  command -v "$tool" >/dev/null || { echo "fetch_geoid: $tool not found" >&2; exit 1; }
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL -o "$work/egm96-15.tar.bz2" "$url"
actual=$(sha256sum "$work/egm96-15.tar.bz2" | cut -d' ' -f1)
if [[ "$actual" != "$expected_sha256" ]]; then
  echo "fetch_geoid: SHA-256 $actual, expected $expected_sha256" >&2
  exit 1
fi
tar -xjf "$work/egm96-15.tar.bz2" -C "$work"
pgm=$(find "$work" -name egm96-15.pgm -print -quit)
[[ -n "$pgm" ]] || { echo "fetch_geoid: egm96-15.pgm not in the archive" >&2; exit 1; }
mkdir -p "$out_dir"
cp "$pgm" "$out_dir/egm96-15.pgm.part"
mv "$out_dir/egm96-15.pgm.part" "$out_dir/egm96-15.pgm"
printf '{"source": "%s", "sha256": "%s", "fetched_at": "%s"}\n' \
  "$url" "$actual" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$out_dir/SOURCE.json"
echo "fetch_geoid: wrote $out_dir/egm96-15.pgm"
