#!/usr/bin/env bash
# P1-15, P5-00: fetch a geoid grid, from HAE (ellipsoid) to AMSL heights.
#
#   infra/geoid/fetch_geoid.sh [MODEL] [OUT_DIR]
#
#   MODEL    egm2008-2_5 (default) or egm96-15
#   OUT_DIR  default local/geoid. GEOID_PATH then points at
#            OUT_DIR/MODEL.pgm.
#
# Remote ID broadcasts height above the WGS-84 ellipsoid; the system uses
# height above mean sea level. The grid is the difference (common/geoid.py).
# EGM2008 is the default because the terrain (Copernicus DEM, P5-00) is
# published on EGM2008: one geoid for both keeps heights comparable.
#
# Source: GeographicLib's distribution of the US National
# Geospatial-Intelligence Agency's models (public domain). Each archive's
# SHA-256 is pinned below, so a changed download fails instead of quietly
# moving every Remote ID aircraft up or down.
#
# Needs `curl`, `tar` with bzip2, and `sha256sum`. Nothing here is committed.

set -euo pipefail

model=${1:-egm2008-2_5}
out_dir=${2:-local/geoid}
case "$model" in
  egm96-15) expected_sha256=8b1ebad1ebae0a045502d0edb9cc51553da1d3914f01e07470c11b3bed75048e ;;
  egm2008-2_5) expected_sha256=d602e13446a4a4a23f39aecfe6a2a0760a1bc6c1b497482c2ebc9f7d513be699 ;;
  *) echo "fetch_geoid: unknown model '$model' (egm2008-2_5 or egm96-15)" >&2; exit 2 ;;
esac
url=https://downloads.sourceforge.net/project/geographiclib/geoids-distrib/${model}.tar.bz2

for tool in curl tar sha256sum; do
  command -v "$tool" >/dev/null || { echo "fetch_geoid: $tool not found" >&2; exit 1; }
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL -o "$work/${model}.tar.bz2" "$url"
actual=$(sha256sum "$work/${model}.tar.bz2" | cut -d' ' -f1)
if [[ "$actual" != "$expected_sha256" ]]; then
  echo "fetch_geoid: SHA-256 $actual, expected $expected_sha256" >&2
  exit 1
fi
tar -xjf "$work/${model}.tar.bz2" -C "$work"
pgm=$(find "$work" -name "${model}.pgm" -print -quit)
[[ -n "$pgm" ]] || { echo "fetch_geoid: ${model}.pgm not in the archive" >&2; exit 1; }
mkdir -p "$out_dir"
cp "$pgm" "$out_dir/${model}.pgm.part"
mv "$out_dir/${model}.pgm.part" "$out_dir/${model}.pgm"
printf '{"model": "%s", "source": "%s", "sha256": "%s", "fetched_at": "%s"}\n' \
  "$model" "$url" "$actual" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$out_dir/SOURCE-${model}.json"
echo "fetch_geoid: wrote $out_dir/${model}.pgm"
