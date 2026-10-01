# Terrain

Nothing in the telemetry says how high an aircraft is above the ground.
`GLOBAL_POSITION_INT.relative_alt` is height above home, and ArduPilot's
`TERRAIN_REPORT` reported 0.0 m through every real flight we recorded,
because the aircraft has no terrain data aboard (TASKS.md, P5-00). P5-00
computes it instead:

```
height above ground = alt_amsl_m - ground elevation (DEM, EGM2008)
```

```
Copernicus DEM (AWS) ──tools.terrain_fetch──▶ local/terrain/*.pgm + index.json
                                                   │
                                  common/terrain.py (read, bilinear)
                                                   │
                         GET /terrain ──▶ console: "Ground elevation", "Above ground"
```

It works anywhere Copernicus has data, which is every land surface on
Earth. Operating somewhere other than Georgia means fetching that area's
tiles, not changing code.

## Fetch the tiles (once per operating area)

```
python -m tools.terrain_fetch --bbox 39.9,41.0,46.8,43.6 --out local/terrain
python -m tools.terrain_fetch --bbox ... --out local/terrain --dry-run   # list only
```

`--bbox` is `min_lon,min_lat,max_lon,max_lat`. For each 1 x 1 degree cell,
the tool uses GLO-30 (30 m) where the public release has it and GLO-90
(90 m) where it does not. A cell with neither is recorded as sea (height 0).
The fetch is resumable: re-running it keeps tiles already converted, and
`--refetch` replaces them. The standard library is enough; nothing needs
GDAL.

Georgia (the box above), fetched 2026-09-30:

| | Cells |
|---|---|
| GLO-30 | 19 |
| GLO-90 | 4: N41 E043 to E046, the Tbilisi strip. Copernicus withholds these from the public GLO-30 release. |
| sea | 1: N42 E039 |

About 480 MB. The tiles are not committed. `local/` is gitignored, and every
machine fetches its own.

Tiles in a directory are read on first use and kept in memory.

## Licence

Copernicus DEM is free to use, including commercially, but anything that
shows or redistributes the adapted data has to carry this attribution
(`SOURCE.json` holds the exact text):

> produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus
> Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European
> Union and ESA; all rights reserved

(WorldDEM-90 for GLO-90 cells.) This also applies to a presentation that
shows the console's elevation figures.

## Turn it on

In `infra/.env`:

```
TERRAIN_DIR=local/terrain
```

Restart the API. Without `TERRAIN_DIR`, `GET /terrain` answers 503 and the
console shows the elevation as unknown, never as 0.

`GET /terrain?lat_deg=41.7151&lon_deg=44.8271` (viewer role) returns
`elevation_m`, `dataset`, `spacing_m` and `vertical_datum`:

| Answer | Meaning |
|---|---|
| 404 | This area was not fetched, or the samples there are no data. The height is unknown, not zero. |
| 422 | The coordinates are out of range. |

## What the number is, and is not

- **The surface, not bare ground.** Copernicus is a surface model: roofs in
  a city, canopy over a forest. Height above it errs low, which is the safe
  direction for clearance.
- **Orthometric, on EGM2008.** Aircraft AMSL from MAVLink is compared
  directly.
- **A few metres, and interpolated.** Accuracy is a few metres, bilinear
  between samples 30 or 90 m apart, so steep slopes add error. The console
  prints the dataset beside the number.

## How far it agrees with ArduPilot

`tools.terrain_compare` reads every `TERRAIN_REPORT` in the raw archive and
compares it with the DEM at the same position:

```
python -m tools.terrain_compare --archive local/e2e/archive --terrain local/terrain --json local/terrain/compare-2026-09-30.json
```

Measured 2026-09-30 from SITL flights that went through the full pipeline
(relay, Gateway, raw archive). The table gives DEM minus flight controller,
per 1 x 1 degree cell:

| Cell | Terrain | DEM | Flight | Reports | Median | Stdev | p95 abs | Max abs |
|---|---|---|---|---|---|---|---|---|
| N42E042, Samtredia | plain | GLO-30 | 60 m, 1 km square | 2,091 | +1.5 m | 1.8 m | 4.3 m | 6.4 m |
| N42E044, Kazbegi | valley and slopes, ground 1,674-1,842 m | GLO-30 | 250 m, 1.5 km N and 0.7 km W | 3,027 | +0.4 m | 3.5 m | 6.9 m | 14.4 m |
| N41E044, Tbilisi | city, hills | GLO-90 | hovers and short legs, SYSID 201 | 15,693 | -2.9 m | 13.4 m | 27.1 m | 27.1 m |
| - | - | - | hexa-01, the real aircraft | 11,520 | not compared: all `loaded=0` | | | |

Stationary SITL aircraft in Tbilisi (SYSIDs 204-211) show a constant
-2.8 to -6.6 m.

Where the flight controller's terrain came from:

- **Samtredia and Kazbegi** used ArduPilot's own terrain files, put in
  `terrain/` beside the repository (the SITL working directory) from
  `https://terrain.ardupilot.org/tilesdat3/<cell>.DAT.gz`, 100 m grid. These
  are the files a real aircraft's SD card would hold.
- **Tbilisi** used a file a ground station had filled block by block during
  earlier runs. Only 9 blocks were filled. In the one block checked against
  `tilesdat3`, it differs by -22 to +15 m, with a mean of -0.9 m.

What this shows:

- **With GLO-30, the two sources agree to a few metres.** On the plain
  95 % of reports agree within 4.3 m; in the mountains, within 6.9 m.
  Neither source is the truth. ArduPilot's terrain is SRTM-derived bare
  ground on a 100 m grid; Copernicus is the surface (roofs, trees).
  Slopes widen the spread: the maximum is 14 m in the mountains against
  6 m on the plain.
- **Tbilisi's 27 m is the worst case, and it is not one cause.** Two
  differences compound there: GLO-90 (90 m spacing) where the other cells
  have 30 m, and a different flight-controller source. It is not evidence
  that cities are worse.
- **None of this helps the real aircraft.** It had no terrain loaded, so
  the DEM is the only height-above-ground there is. Loading terrain onto it
  would give the cross-check the SITL runs had.

To repeat a run:

1. Put the cell's `tilesdat3` file in `terrain/`.
2. Start SITL with `SITL_HOME` in that cell.
3. Fly the aircraft over ground that varies.
4. Run `tools.terrain_compare` over the archive. It keeps each cell apart
   even when one SYSID flew several.

`SITL_HOME` was 450 m for years; the ground there is 605 m. The DEM exposed
the gap, and the default is now 605 (`sim/sitl.env.example`, CI).

## Check it in the console

`local\capacity\run\52-terrain-demo.bat` starts SITL hovering 30 m above
home. Select the aircraft:

| Aircraft | Ground elevation | Above ground (approx.) |
|---|---|---|
| SITL-01 | 605 m · COP-DEM GLO-90 | 30 m, the same as "Above home" |
