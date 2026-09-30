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
GEOID_PATH=local/geoid/egm2008-2_5.pgm     # Remote ID; see p1-15-remote-id.md
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
  directly. Remote ID gives height above the ellipsoid, and
  `common/geoid.py` converts it on the same EGM2008 grid. EGM96 would put
  Georgia off by -2.3 to +4.7 m (Tbilisi 15.92 m on EGM2008 against
  14.71 m on EGM96), which is why EGM2008 is the default.
- **A few metres, and interpolated.** Accuracy is a few metres, bilinear
  between samples 30 or 90 m apart, so steep slopes add error. The console
  prints the dataset beside the number.

## How far it agrees with ArduPilot

`tools.terrain_compare` reads every `TERRAIN_REPORT` in the raw archive and
compares it with the DEM at the same position:

```
python -m tools.terrain_compare --archive local/e2e/archive --terrain local/terrain --json local/terrain/compare-2026-09-30.json
```

Measured 2026-09-30, DEM minus flight controller:

| Source | Reports | Median | Stdev | Max abs |
|---|---|---|---|---|
| hexa-01, the real aircraft (SYSID 1) | 11,520 | not compared: all `loaded=0` | | |
| SITL, stationary (204-211) | 3,270-5,152 each | -2.8 to -6.6 m, constant per position | 0 | 2.8-6.6 m |
| SITL, flying (201, 202) | 14,770 / 10,062 | -2.9 / -4.8 m | 13.7 m | 27.1 m |
| SITL 203 | 6,003 | -5.8 m | 2.3 m | 24.2 m |

What this shows:

- **On flat ground, a steady offset of 3-7 m.** ArduPilot's terrain is
  SRTM-derived bare ground on its own vertical reference. The difference is
  expected, not an error in either source.
- **In motion, up to 27 m.** The two surfaces are sampled at different
  resolutions and times on a slope, so they diverge there.
- **One area only.** All of it is the Tbilisi GLO-90 cell, because that is
  where SITL flies. No GLO-30 cell and no mountains have been compared yet.
  SITL started elsewhere (`SITL_HOME`) would measure them.

`SITL_HOME` was 450 m for years; the ground there is 605 m. The DEM exposed
the gap, and the default is now 605 (`sim/sitl.env.example`, CI).

## Check it in the console

`local\capacity\run\52-terrain-demo.bat` starts SITL hovering 30 m above
home, plus a Remote ID aircraft flying east at 635 m AMSL. Select each
aircraft:

| Aircraft | Ground elevation | Above ground (approx.) |
|---|---|---|
| SITL-01 | 605 m · COP-DEM GLO-90 | 30 m, the same as "Above home" |
| SIM-RID-0001 | rises to about 660 m | goes negative |

SIM-RID-0001 goes negative because the ground rises 55 m within half a
kilometre east of home, and the simulator flies a fixed altitude. The
console shows that negative number as it is. Alerting on it is P5-19.
