#!/usr/bin/env bash
# P1-12: fetch the console's self-hosted base map.
#
#   infra/basemap/fetch_basemap.sh OUT_DIR BBOX [BUILD]
#
#   OUT_DIR  where the console reads it (BASEMAP_DIR, default local/basemap)
#   BBOX     min_lon,min_lat,max_lon,max_lat of the operating area. Never
#            defaulted: coordinates are configuration, not code (CLAUDE.md).
#   BUILD    a Protomaps daily build, e.g. 20260928. Default: the newest.
#
# Needs `pmtiles` (github.com/protomaps/go-pmtiles), `git` and `curl`.
#
# Produces:
#   OUT_DIR/basemap.pmtiles   the OpenStreetMap extract, one file, read by the
#                             browser with HTTP range requests
#   OUT_DIR/fonts/            glyph PBFs (Noto Sans; carries Georgian)
#   OUT_DIR/sprites/v4/       icon sheets for the light and dark flavours
#   OUT_DIR/SOURCE.json       where and when it came from. An offline map has
#                             no other way to say it is stale.
#
# Nothing here is committed: the extract is hundreds of MB, and it is data.

set -euo pipefail

if [[ $# -lt 2 ]]; then
  sed -n '2,20p' "$0" >&2
  exit 2
fi

out_dir=$1
bbox=$2
build=${3:-}

for tool in pmtiles git curl; do
  command -v "$tool" >/dev/null || { echo "fetch_basemap: $tool not found" >&2; exit 1; }
done

if [[ -z "$build" ]]; then
  build=$(curl -fsS https://build-metadata.protomaps.dev/builds.json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)[-1]["key"].removesuffix(".pmtiles"))')
fi

mkdir -p "$out_dir"
echo "fetch_basemap: extracting ${bbox} from build ${build}"
pmtiles extract "https://build.protomaps.com/${build}.pmtiles" \
  "$out_dir/basemap.pmtiles.part" --bbox="$bbox"
mv "$out_dir/basemap.pmtiles.part" "$out_dir/basemap.pmtiles"

assets=$(mktemp -d)
trap 'rm -rf "$assets"' EXIT
git clone -q --depth 1 --filter=blob:none --sparse \
  https://github.com/protomaps/basemaps-assets.git "$assets"
git -C "$assets" sparse-checkout set \
  "fonts/Noto Sans Regular" "fonts/Noto Sans Medium" "fonts/Noto Sans Italic" sprites/v4
assets_commit=$(git -C "$assets" rev-parse HEAD)
rm -rf "$out_dir/fonts" "$out_dir/sprites"
mkdir -p "$out_dir/fonts" "$out_dir/sprites"
cp -r "$assets/fonts/." "$out_dir/fonts/"
cp -r "$assets/sprites/v4" "$out_dir/sprites/"

osm_as_of=$(pmtiles show "$out_dir/basemap.pmtiles" \
  | sed -n 's/^planetiler:osm:osmosisreplicationtime //p')

cat > "$out_dir/SOURCE.json" <<JSON
{
  "source": "https://build.protomaps.com/${build}.pmtiles",
  "build": "${build}",
  "bbox": "${bbox}",
  "osm_data_as_of": "${osm_as_of}",
  "assets": "https://github.com/protomaps/basemaps-assets@${assets_commit}",
  "fetched_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "licence": "Map data (c) OpenStreetMap contributors, ODbL. Fonts: SIL OFL."
}
JSON

echo "fetch_basemap: done"
du -sh "$out_dir"
cat "$out_dir/SOURCE.json"
