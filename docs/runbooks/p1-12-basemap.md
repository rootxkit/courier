# P1-12 self-hosted base map

The console draws an OpenStreetMap base map from one PMTiles file that it
serves itself. Nothing on the page is fetched from the internet: a pilot in
the field often has none.

## Installing it on a machine

```
infra/basemap/fetch_basemap.sh local/basemap 39.9,41.0,46.8,43.6
```

Needs `pmtiles` (github.com/protomaps/go-pmtiles), `git` and `curl`. The bbox
is required and never defaulted: it is configuration (CLAUDE.md). The console
reads `BASEMAP_DIR` (default `local/basemap`) at startup, so restart it after
fetching. Without a base map the page still works over a blank background and
says so.

What lands in the directory:

| File | What |
|---|---|
| `basemap.pmtiles` | the extract, read by the browser in byte ranges |
| `fonts/` | Noto Sans glyphs; they carry Georgian (U+10A0-10FF, U+1C90-1CBF) |
| `sprites/v4/` | icons for the light and dark flavours |
| `SOURCE.json` | build, bbox, OSM data date, asset commit, fetch time |

## Result, 2026-09-28

Protomaps daily build 20260928, OSM data as of 2026-09-28 04:00 UTC, bbox
39.9,41.0,46.8,43.6 (Georgia), zoom 0-15: 333 MB. Zoom 15 is the highest the
build has.

Watched in the development machine's browser with the console running:

- Tbilisi at zoom 14 draws streets, buildings, parks and the river, with
  labels in Georgian script;
- every request went to 127.0.0.1 (page, vendored libraries, `basemap.pmtiles`
  as 206 range responses, fonts, sprites);
- the attribution line reads "© OpenStreetMap contributors · 2026-09-28 ·
  Protomaps".

In `ka` every name label shows OSM's `name`, which in Georgia is the Georgian
name. The style library has no Georgian and the extract has no `name:ka`; in
`en` it shows the English name with the local one beneath.

## Refreshing

An offline map does not know it is stale. The date in the attribution line is
the data date; re-run the script to refresh, and compare `SOURCE.json`.
