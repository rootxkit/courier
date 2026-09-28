# Flight replay

`http://127.0.0.1:8010/replay`, served by the core API (`python -m api`).
Pick an aircraft, pick a flight (an armed span) or type a UTC window, and
scrub or play it on the map.

## The rule

**A hole in the record is shown as a hole.** The track is drawn as separate
segments and nothing is drawn between them. Both ends of every hole are
marked with a hollow ring, and the hole is listed with the reason the
Gateway's own log gives for it, or with **no recorded cause** when it gives
none. Inside a hole the cursor stays, hollow, at the last place the aircraft
was known to be, and the status line says why there is no data.

The track is cut when:

| Cut | Condition |
|---|---|
| no telemetry | two consecutive samples further apart than `REPLAY_GAP_THRESHOLD_S` (3 s) |
| relay gap | a relay `gap` whose exact bounds fall between two samples, however short, unless another station heard the aircraft inside it |
| no position | telemetry arrived without a position fix |

Ring colours: red, data is lost (a relay gap, intake drop, relay restart or
replaced queue); blue, a cause was logged that is not loss (station
unreachable, radio silent, lagging); grey, no recorded cause; purple, no
position.

## What each cause means

| Shown as | Source | Meaning |
|---|---|---|
| relay dropped N records (queue cap) | `relay_epoch_gaps`, bounds from `archive_segments` | lost for good; exact |
| … time approximate | same, neighbours not indexed | lost; placed where the Gateway recorded it, never cuts the track |
| datagrams dropped before sequencing | `ingest_events` `loss.intake_drop` | lost; time known to within `REPLAY_EVIDENCE_SLACK_S` |
| relay restarted | `loss.relay_restart` | what the relay held in memory is lost; extent unknown |
| relay queue replaced | `epoch_closed` | undelivered records of the old queue are lost |
| station unreachable | `link_state` | **not** loss (relay-v1 §9): the station buffers. If the hole is still there, what it buffered never arrived |
| station radio silent | `link_state` | the station heard no aircraft at all |
| station lagging | `link_state` | the station was delivering late |

A station's departure is logged as `unreachable` from 2026-09-29 (this
change). Before that the log's last word on a relay that left mid-flight was
`healthy`, so holes from older sessions that ended that way show **no
recorded cause**. That is correct: the cause was not recorded.

Loss evidence that did not cut this track (heard over another station, or
outside the track) is listed separately, so nothing the Gateway logged about
the window is silently dropped.

Airspace alerts come from `events` in the relational database. If that
database cannot be read, the page says alerts are **unavailable**, not that
there were none.

## Clocks

Sample times and relay-gap bounds are the station's clock (`recv_utc_ns`);
link states and alerts are the Gateway's and the airspace service's. On one
machine they agree. Across machines they agree to within clock sync, which
is why causes are matched to holes with a slack rather than exactly.

## Settings

| Variable | Default | |
|---|---|---|
| `REPLAY_GAP_THRESHOLD_S` | 3.0 | silence longer than this is a hole |
| `REPLAY_EVIDENCE_SLACK_S` | 5.0 | how far apart a hole and its logged cause may be |
| `REPLAY_FLIGHT_SPLIT_S` | 120.0 | armed silence longer than this is two flights |
| `REPLAY_MAX_SAMPLES` | 100000 | a larger window is refused, not thinned |

## API

- `GET /replay/drones`: every aircraft in the telemetry registry, retired
  included.
- `GET /replay/drones/{id}/flights?since&until`: armed spans, newest first.
- `GET /replay/drones/{id}?start&end`: samples, `segments` (the only things
  a client may draw as lines), `holes` with `reasons`, `evidence`, `alerts`.
  Times must carry a zone.

The API has no operator authentication yet and binds to loopback.

## Result, 2026-09-29

On the device, against the airspace run of 2026-09-28 (SITL-01, SITL-02):

- Both flights listed as armed spans: SITL-01 20:26:34-20:30:30 (945 armed
  samples, 4 Hz), SITL-02 20:26:34-20:30:30 (942).
- Each track has exactly one hole, **no position**, 26.3 s at the start:
  104 samples arrived before the EKF had an origin. The line starts where
  the position does, and the one early fix before it stands alone.
- No telemetry holes and no loss evidence in the window: the station's log
  shows `healthy` from 20:25:50 and `radio_silent` only after the aircraft
  stopped transmitting at 20:30:33.
- All eight airspace alert transitions per aircraft came back from `events`
  in order, and the cursor shows the alert active at its time.
- The replay settled the airspace monitor's open question about the early
  clear on the return; see `p5-airspace-monitor.md`.
- Stopping the relay logged `unreachable` at the moment it left
  (21:21:15.486); before this change the log's last word would have been
  `healthy`. On its return the station went to `data_lost` (21:21:20.964).
  The relay was killed rather than stopped, so losing what it held in
  memory is expected, but the `loss.*` row that says which loss it was was
  not read: that cause is unconfirmed.

Not yet exercised live: a hole in a flight with a logged cause (a relay gap,
or a station unreachable mid-flight). The database tests cover each cause
against the real schema; a live one comes with P1-01 Procedure A (pulling
the network during a flight).

The page draws its own layers on the base map, and the first version lost
them: MapLibre's `setStyle` diffs by default, the diff removed the replay's
layers, and no `style.load` fired to restore them. The base map arriving
after page load left an empty map. It now reloads the style in full.
