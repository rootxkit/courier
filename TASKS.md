# Work breakdown

Every task has an ID, a deliverable, and an acceptance criterion. A task is done
when the criterion is demonstrable, not when the code compiles. Reference the ID
in the commit message.

Status: `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked

---

## Phase 0 — Foundation and SITL (3-5 days)

Goal: a reproducible development environment where multiple simulated drones can
be launched and observed. No real hardware.

- [x] **P0-01** Monorepo skeleton: directories per `CLAUDE.md` layout, `README`,
      `.gitignore` (exclude `.env`, `*.bin`, `*.tlog`, `logs/`, SITL artifacts).
      *Done when:* `tree -L 2` matches the documented layout.

- [x] **P0-02** Git hygiene: `.githooks/commit-msg` strips AI attribution
      trailers, `.claude/settings.json` disables attribution, `make hooks`
      installs via `core.hooksPath`.
      *Done when:* a commit containing `Co-Authored-By: Claude` comes out clean.

- [x] **P0-03** `docker-compose.dev.yml`: PostgreSQL 16 + PostGIS 3.4,
      TimescaleDB, Redis 7, NATS. Healthchecks on all four. Named volumes.
      *Done when:* `make up` reaches healthy on all services from a cold start.

- [x] **P0-04** SITL launcher `sim/run_sitl.sh` — N instances, unique SYSID per
      instance, configurable home location, distinct UDP out ports.
      *Done when:* `make sim N=10` gives 10 vehicles with distinct SYSIDs.
      *Closed* on the first green `sitl` job (PR #5): three vehicles launched
      with distinct SYSIDs and the integration tests passed against them.
      *Fully closed* 2026-09-25: ten vehicles launched from WSL appeared in
      QGC on the Windows side as SYSIDs 201-210, in a row near Kukia Cemetery,
      Tbilisi. That is the visual confirmation CI could not give.

- [x] **P0-08** WSL2 environment for SITL per `docs/DEV_SETUP_WSL.md`:
      mirrored networking and ArduPilot SITL built locally.
      *Done when:* `make sim N=3` runs locally and all three vehicles appear in
      QGC on the Windows side.
      *Closed* 2026-09-25 with ten, not three.
      *Scope corrected while doing it:* the task said "repo on the WSL
      filesystem" and "Docker integration", and both were wrong. Only SITL
      lives in WSL. The repository, Gateway, relay agent, console, tests and
      Docker stack stay on Windows, because the relay ships to a pilot's bare
      Windows laptop and developing it anywhere else tests something we do not
      fly. `docs/DEV_SETUP_WSL.md` is rewritten around that.

- [x] **P0-05** Python tooling: `ruff`, `mypy` config, `pytest` with async
      support, shared `pyproject.toml` conventions.
      *Done when:* `make lint` and `make test` pass on an empty repo.

- [x] **P0-06** CI pipeline (GitHub Actions): lint, typecheck, unit tests on
      every push; SITL integration job on PR to `main`.
      *Done when:* CI is green on the P0 branch.
      *Closed* on PR #5, the first run where the `sitl` job built ArduPilot and
      ran the integration tests rather than failing in setup. The job is also
      dispatchable manually (`workflow_dispatch`), so the next branch can prove
      it green before opening a PR.

- [x] **P0-07** Structured logging and config loading shared library
      (`common/`): JSON logs, env-based config with validation, no bare prints.
      *Done when:* every service imports it and no service reads `os.environ`
      directly.

---

## Phase 1 — Telemetry pipeline (1-1.5 weeks)

Goal: telemetry from many vehicles reaches the database and a browser map.

- [~] **P1-00** Verify QGC forwarding behaviour empirically before building on
      it. Run `tools/mavlink_probe.py listen` and `roundtrip` against the real
      aircraft and fill in `docs/decisions/001-qgc-forwarding.md`.
      *Done when:* the decision record contains measured output, not
      assumptions. Everything downstream depends on this being accurate.
      *Partial:* findings 1 and 3 measured over USB direct. Outstanding —
      QGC version and firmware in the environment table; finding 2 is UNTESTED,
      re-run `roundtrip` with the ground station quiet; radio-port rates
      (`SR1_*`/`SR2_*`) must be measured separately before any multi-aircraft
      flight; `MISSION_ITEM_REACHED` to be confirmed on the first mission run.

- [~] **P1-01** Ground relay process: read UDP 14445, authenticate, forward to
      Gateway over TLS WebSocket, disk-backed queue that replays after an
      internet dropout. Runs as a Windows service or tray app on the ground PC.
      **The relay is lossless and dumb.** It forwards every datagram it
      receives, unmodified and unparsed. It does not filter, does not identify
      vehicles, and does not interpret MAVLink at all.
      Filtering here would be irreversible: the messages the hot path does not
      want — `ATTITUDE`, `VIBRATION`, `EKF_STATUS_REPORT`, `ESC_TELEMETRY` —
      are exactly the ones an incident investigation needs, and P10-03 flight
      replay cannot reconstruct what was never sent. The relay's uplink is
      internet, where ~2.8 KiB/s per aircraft is negligible; the bandwidth
      constraint is the radio link, which is a vehicle-side concern (P1-01b).
      The wire contract is [`docs/protocols/relay-v1.md`](docs/protocols/relay-v1.md).
      *Done when:* pulling the network cable for 2 minutes results in zero lost
      telemetry rows once it reconnects, and the relay conforms to relay-v1.
      *Partial:* Procedure B (stop and restart the receiver) passed on
      2026-09-22 over loopback — see `docs/runbooks/p1-01-test-records.md`. The
      sink resumed from its own disk and the relay refilled the gap exactly.
      Outstanding: Procedure A over a real LAN, which also covers TLS and the
      half-open-connection detection path that a connection refusal never
      exercises; and QGC-to-relay integrity, which the relay's own counters
      cannot see (`tools/analyze_capture.py`).

- [ ] **P1-01b** QGC setup documentation: forwarding configuration, stream rate
      tuning (`SR*_` parameters), multi-vehicle SYSID assignment, radio `NETID`
      separation. Written so a pilot can follow it without help.
      Includes the **radio-link stream-rate budget**. This is where bandwidth
      is actually scarce, and it is set in the vehicle's `SR1_*`/`SR2_*`
      parameters on the telemetry port — not in software downstream. ADR-001
      measured `SR0_*` over USB at 2.8 KiB/s for one aircraft; the radio port
      has its own rates and must be measured separately before any
      multi-aircraft flight.
      *Done when:* a second person sets up a ground station from the doc alone,
      and the documented budget is backed by a measurement of the radio port.

- [x] **P1-02** Gateway ingest: async UDP listener plus the relay-v1 WebSocket
      endpoint, MAVLink parse, vehicle identification. Handle `HEARTBEAT`,
      `GLOBAL_POSITION_INT`, `SYS_STATUS`, `BATTERY_STATUS`, `GPS_RAW_INT`,
      `VFR_HUD`, `STATUSTEXT`, `EKF_STATUS_REPORT`.
      **Classify sources by HEARTBEAT identity, per `(sysid, compid)`.** The
      relay forwards everything it hears, which includes QGC's own heartbeat,
      and a gimbal or companion computer may heartbeat under the vehicle's
      SYSID with a different component ID. Never register a ground station or a
      component as a vehicle. Classify on the HEARTBEAT `type` and `autopilot`
      fields — never on the SYSID number, since `GCS_SYSTEM_ID` is a user
      setting, and never on message volume, since a just-booted aircraft has
      sent one HEARTBEAT and nothing else, which is exactly when it must stay
      visible. Ambiguity resolves to vehicle. `tools/mavlink_probe.py` has a
      working implementation to lift.
      **Decide what enters the hot path.** `drone_state` takes the messages the
      pipeline needs; everything else goes to the raw archive for P10-03 replay
      and incident investigation. Nothing here may depend on a specific stream
      rate — QGC's own settings determine what `SR0_*` emits, and they change.
      **Distinguish "unreachable" from "losing data".** The Gateway declares
      a station unreachable after three missed `status` messages, about 3 s,
      while the relay does not give up on a half-open uplink for up to 25 s
      (`relay-v1.md` §8). For that window the two disagree, and the relay is
      alive and buffering correctly throughout it — nothing is deleted,
      because no acknowledgement can arrive through a dead link. The station
      state the Gateway publishes must carry that distinction: "unreachable,
      data buffered at the station" is not "data lost". Only a `gap`, an
      intake-drop delta, or a `uptime_s` reset means telemetry is actually
      gone.
      *Done when:* 10 SITL vehicles are parsed concurrently without drops,
      no non-vehicle endpoint is ever registered as a drone, and a station
      that goes unreachable is never reported as losing data unless a gap or
      a drop counter says so.
      *Closed* 2026-09-28 on three pieces of evidence:
      - **11 SITL vehicles at once.** On 2026-09-28 (the P1-13 run),
        254,315 records were stored with zero intake or cap drops and no
        conversion or write errors. Drain exceeded intake with a steady
        baseline.
      - **No non-vehicle registered.** `test_classify.py` covers this, and
        `test_binding.py` shows that a ground station never resolves to a
        drone even when bound.
      - **Unreachable is not data loss.** `test_station_state.py` and
        `test_publisher.py` keep the two states separate.

      One transient is expected, not a fault: each aircraft is logged once as
      unclaimed at startup, before its first HEARTBEAT.

- [x] **P1-03** Unit conversion at the parser boundary: 1e7 lat/lon scaling,
      mm→m altitude, cm/s→m/s velocity. Both AGL and AMSL preserved separately.
      *Done when:* property tests confirm round-trip accuracy and no field is
      stored in raw MAVLink units.
      *Closed* 2026-09-28. `test_conversion_roundtrip.py` sends every
      `GLOBAL_POSITION_INT` field across its whole wire range (the extremes
      plus 400 seeded draws each) through a real pymavlink frame and the
      ingest pipeline into a row. It then recovers the exact wire integer
      using factors defined independently of `gateway/units.py`. A paired test
      shows no row field equals its raw value.
      Battery, GPS and VFR fields are covered by the example tests in
      `test_conversion.py`, not by property tests.

- [x] **P1-04** TimescaleDB writer: batched inserts (flush on 100 rows or 500 ms),
      hypertable with 7-day chunks, 90-day retention policy.
      *Done when:* 10 drones at 4 Hz sustain writes with insert latency p99
      under 50 ms.
      *Closed* 2026-09-28. `BufferedStateWriter` inserts on 100 rows or
      500 ms, whichever comes first (`STATE_FLUSH_ROWS`,
      `STATE_FLUSH_INTERVAL_S`). The hypertable, 7-day chunks and retention
      were already in migration 0004.
      Measured with 11 bound SITL aircraft at 4 Hz for 180 s (the criterion is
      10):
      - 331 inserts of ~24 rows each, 44 rows/s;
      - insert latency **p99 19.1 ms**, p50 11.1 ms, one outlier at 100.7 ms;
      - no errors.
      Every insert is timed and logged, so the p99 is computed over the whole
      run, not per window.

- [x] **P1-05** Redis live state: `drone:{id}:state` with 15 s TTL. Expiry is
      the definition of "link lost".
      *Done when:* killing a SITL instance flips its status within 20 s.
      *Closed* 2026-09-28. An 11-aircraft SITL run was used, and one instance
      was killed. It flipped to link-lost in 14.9 s. The other ten stayed
      live throughout. See `docs/runbooks/p1-05-link-loss.md`.
      Expiry counts from **capture** time, capped at now plus the timeout:
      - A replayed backlog never makes a lost aircraft look live.
      - An older record never overwrites a newer one; the check is a
        compare-and-set in Redis.
      - A station clock running behind fails toward "link lost".
      Correcting station clocks is still spec §12 question 4.

- [x] **P1-06** NATS publication: `telemetry.{drone_id}`, `events.{type}`.
      *Done when:* a test subscriber receives every position update.
      *Closed* 2026-09-28. `test_publisher_nats.py` drives 1,000 position
      updates through the pipeline into a real NATS broker. A separate
      subscriber receives exactly 1,000, in order, with none duplicated. A
      paired test shows a HEARTBEAT alone publishes nothing.

- [x] **P1-07** Gateway authentication: **per-station bearer token** on the
      relay-v1 upgrade request, plus a server-side policy check binding
      `(station_id, sysid)`. Unauthenticated packets dropped and
      rate-limit-logged.
      Revised from "per-vehicle token" by `docs/protocols/relay-v1.md` §3. A
      ground station relays whatever its radio hears, so it cannot hold one
      credential per aircraft; it authenticates as itself. Which vehicles a
      station may carry is policy the server evaluates, not an assertion the
      station is trusted to make. The security property is unchanged — a
      spoofed SYSID is rejected — but it is enforced where the policy lives,
      and a compromised ground station cannot mint vehicles it was never
      assigned. Direct UDP sources (SITL, bench testing) keep their own path.
      *Done when:* a spoofed SYSID is rejected, and a valid station presenting
      a vehicle it is not assigned is rejected and logged.
      *Closed* 2026-09-28. The policy is `source_bindings` (spec §12 question
      6). An address bound only on another station resolves to
      `not assigned`. It is archived, not attributed, and recorded as a
      `rejected_source` event. It is published on its own subject, so the map
      never offers to register it. It is logged at most once per minute per
      address with the suppressed count. 401s are rate-limited the same way.
      Tested against PostgreSQL in both directions: rejected elsewhere,
      resolved on the assigned station, accepted on both during a handover,
      and unclaimed when bound nowhere.
      *Not covered:* a compromised station replaying a SYSID that *is* bound
      on it. That needs MAVLink 2 signing on the vehicle, outside Stage 0.
      Token storage and rotation remain spec §12 question 1.

- [x] **P1-08** WebSocket endpoint + minimal map page: MapLibre, one marker per
      drone, heading arrow, battery label.
      *Done when:* 10 markers move in the browser with under 500 ms end-to-end
      latency.
      *Closed* 2026-09-25: 11 drones listed, 10 markers placed from SITL, and
      `hexa-01` correctly listed as present but unplaced.
      *Known gap, not blocking:* the map has no base layer. The style is
      MapLibre's demo style, whose tiles stop at zoom 6 and contain only
      country outlines, so at city zoom there is nothing to draw. Choosing a
      tile provider is its own decision, with licensing attached; see P6-01.

- [x] **P1-09** Link-quality tracking: packet loss, round-trip latency,
      heartbeat gaps per vehicle.
      *Done when:* the metric degrades measurably under simulated packet loss.
      *Closed* 2026-09-28, for packet loss and heartbeat gaps. Both are
      measured per vehicle, over a 10 s window, published with every
      telemetry message and shown on the map.
      11 SITL aircraft were run through a forwarder that first dropped
      nothing, then 20.07% of datagrams. Measured loss (median of 11):
      - clean phase: 0.0%;
      - lossy phase: 20.16%.
      The longest heartbeat gap rose from 1.05 s to 3.98 s. See
      `docs/runbooks/p1-09-link-quality.md`.
      *Round-trip latency is moved to P3B-02.* It needs something sent and
      answered, and Stage 0 sends nothing.

- [x] **P1-10** Ingest capacity: measure intake and drain separately, find the
      bottleneck, and turn relay-v1 §10 into measured numbers.
      **Highest priority in Phase 1.** §10 claims a 30-minute outage "drains in
      seconds". That was bandwidth arithmetic only, and it is false: on
      2026-09-25, after a deliberate two-minute Gateway outage with 11 sources,
      the relay queue grew by roughly 790 records/s and never drained.
      Why it matters more than it looks. If drain rate is at or below intake
      rate, *any* outage becomes permanent lag rather than a recoverable
      backlog. Nothing reports a fault: the relay is buffering exactly as
      designed, `data_is_lost` is correctly false, the Gateway is healthy, the
      console is connected — and the fleet on screen is quietly getting older
      every second. It is the §9 failure mode one level up, where every
      component is honest and the system is still lying.
      Scope:
      - Measure intake and drain **as separate rates**, from a deliberate
        outage rather than a restart side-effect, at 1, 3 and 11 sources.
      - Locate the bottleneck by instrumentation, not by guessing. Candidates
        to measure, not to assume: the relay's SQLite commit rate, batch size
        and flush interval, the ack round trip, and the Gateway's
        persist-before-ack fsync.
      - Propose a requirement — drain rate at least N times intake at a stated
        fleet size per station — and say what the relay reports when it cannot
        meet it. **Stop with the proposal. Do not change the protocol.**
      *Done when:* intake and drain are recorded numbers at all three fleet
      sizes, the bottleneck is named with the measurement that identifies it,
      §10 is corrected, and a requirement is on the table for a ruling.
      *Closed* 2026-09-27. Drain stays near 300 records/s at 1, 3 and 11
      sources, against intake of 194, 416 and 1,281 records/s. The bottleneck
      is the Gateway resolving each MAVLink message's binding with its own
      query: 96% of the time spent storing a batch, 119,751 calls for 119,751
      records, measured by `gateway/stage_timing.py`. §10 is corrected. The
      requirement (drain ≥ 5× intake at design load, plus a `lagging` station
      state) is in `docs/decisions/002-drain-rate-requirement.md` **awaiting a
      ruling**. The fixes are P1-13 and P1-14.

- [x] **P1-11** Record `AUTOPILOT_VERSION` per vehicle, so firmware is fleet
      data rather than something read off a screen.
      The aircraft's firmware version is not in the raw archive: the message is
      only sent on request, Stage 0 is receive-only and cannot ask, and by the
      time the relay attaches QGC has already consumed the reply to its own
      request. So "the SITL that gates merges matches the aircraft" is
      currently unverifiable.
      A relay attached *before* QGC connects does see the reply, because QGC
      requests the version at every connect. That is Stage-0 compatible: the
      Gateway records what it observes and asks for nothing.
      Also serves P10-05 maintenance tracking, which needs firmware per
      airframe over time rather than a current value.
      *Done when:* connecting QGC to an aircraft results in a recorded flight
      software version for that `drone_id`, and a vehicle that never offers one
      is visibly unknown rather than silently absent.
      *Closed* 2026-09-28, for the Gateway's side. `drone_firmware` holds one
      row per change, and the console renders `fw unknown` until a version is
      recorded (unit-tested payload; the page was not watched live). Two SITL aircraft: the one asked for its version got one row
      (`4.8.0-dev`, `66c89850`), a second request added none, and the one
      never asked got none.
      MAVProxy does not request the version on connect, so a script stood in
      for QGC's request on the same path. QGC's own request has not been
      observed; the first P1-01 session with the real aircraft confirms it.
      `docs/runbooks/p1-11-firmware.md`.

- [ ] **P1-12** Self-hosted base map: a Georgia PMTiles extract served by the
      console itself.
      The P1-08 map has no base layer. The demo style's tiles stop at zoom 6 and
      contain only country outlines, so at city zoom there is nothing to draw —
      it was never a street map.
      Self-hosted rather than a tile provider: no API key, no per-request
      dependency on a third party, and it works with no internet at all, which
      is the case a pilot in the field is actually in.
      **Self-hosting does not remove the attribution obligation.** The data is
      OpenStreetMap, so the map must display "© OpenStreetMap contributors"
      wherever it is shown, including on the minimal P1-08 page and in
      `web-pilot/` afterwards. Record the extract's date and source, because an
      offline basemap has no way of telling anyone it is stale.
      *Done when:* the map renders streets over the operating area with no
      network access beyond the console itself, and attribution is visible.

- [x] **P1-13** Resolve source bindings per batch, not per message.
      P1-10 measured this as the Gateway's ceiling. `IngestPipeline` calls
      `BindingResolver.resolve` for every MAVLink message, and each call runs
      its own `source_bindings` SELECT - about 3.3 ms, so about 300 records/s
      whatever the fleet size. `resolve_batch` already exists for this case and
      is not called.
      **Resolution stays per record's capture time.** A backlog that straddles
      a binding change must still resolve each record against the binding in
      force when it was captured; batching changes how often the bindings are
      read, not which binding applies. Test that a batch crossing a rebinding
      yields two `drone_id`s.
      *Done when:* `tools/ingest_capacity.py` at 11 SITL sources reports drain
      above intake with a steady baseline, the stage timings show `resolve`
      calls per batch rather than per record, and the drain figure is recorded
      against ADR-002's requirement.
      *Closed* 2026-09-28. At 11 SITL sources with bound aircraft, drain went
      from 289 to 1,796 records/s against intake of 1,197. The baseline settled
      in 2 s, and the backlog from a 60 s outage cleared: 72,345 records down
      to 769. That is **1.5×, short of ADR-002's proposed 5×**. `resolve` now
      runs 1,353 times for 254,315 records. Batch time is now spread across
      stages: store 41%, resolve 36%, write 11%. See ADR-002, "After P1-13".

- [x] **P1-14** A `lagging` station state: the backlog is not clearing.
      Proposed in ADR-002. Today a station whose backlog grows looks healthy -
      buffering is correct, nothing is lost, the Gateway is up - and the
      console shows an ever-older fleet. Enter `lagging` when the age of the
      newest stored record (`now - recv_utc_ns`) exceeds a configured threshold
      and `status.queue_depth` is rising; publish it with `lag_s`. No protocol
      change: both signals already reach the Gateway.
      Like `unreachable`, it does not mean telemetry is lost, and the console
      must not say it does.
      *Done when:* a test drives a station into `lagging` and back out, and the
      state reaches the console. Blocked on the ADR-002 ruling for the
      threshold.
      *Closed* 2026-09-28. Threshold ruled as the Gateway's link timeout
      (15 s by default): past that age a record can no longer make a drone
      live, so it is when every aircraft on the station starts to read as
      link lost. "Rising" is depth above its value five `status` messages
      earlier. A relay-v1 session over a real socket goes `healthy` ->
      `lagging` -> `healthy` and each is published with `lag_s`; the same old
      record with a steady queue never lags. The console shows the state and
      how far behind. Not yet produced by a live overloaded Gateway.
      ADR-002's N >= 5 is still unruled and is not needed by this state.

---

## Phase 2 — Data model and order lifecycle (1-1.5 weeks)

Goal: orders exist, move through states, and are fully auditable.

- [ ] **P2-01** Alembic migrations for the full schema in
      `docs/ARCHITECTURE.md` §4, with GiST indexes on all geometry.
      *Done when:* `make migrate` runs clean up and down.

- [ ] **P2-02** Seed data: 3 bases, 10 drones, 3 pilots, realistic Tbilisi
      coordinates and airframe parameters.
      *Done when:* `make seed` produces a usable dataset.

- [ ] **P2-03** Order state machine with an explicit transition table; illegal
      transitions raise, every transition appends to `events`.
      *Done when:* a unit test proves every illegal transition is rejected.

- [ ] **P2-04** Order CRUD API: create, read, cancel, list with filters.
      OpenAPI schema generated.
      *Done when:* an order runs through all states via API calls and `events`
      holds the complete trail.

- [ ] **P2-05** Drone and pilot registry API, including status transitions
      (`IDLE`, `ASSIGNED`, `IN_FLIGHT`, `CHARGING`, `MAINTENANCE`, `OFFLINE`).
      **Registering or retiring a drone must project into the telemetry
      database's `known_drones`.** The Gateway never connects to the relational
      database, so `source_bindings.drone_id` points at that projection rather
      than at `drones`, and a binding to a drone the projection has not heard
      of is refused by a foreign key. Without this step someone inserts into
      `drones`, the binding is refused or the telemetry is marked unclaimed,
      and the cause is invisible from either side: the relational registry
      looks correct and the Gateway looks broken.
      `known_drones` is a projection, never an authority — see
      `docs/specs/p1-02-gateway-ingest.md` §7.
      *Done when:* status is derived from telemetry freshness, not set by hand,
      and registering a drone makes it bindable in the telemetry database
      without anyone touching that database by hand.

- [ ] **P2-06** Append-only audit log with a query API filtered by entity and
      time range.
      *Done when:* an auditor can reconstruct an order's full history.

- [ ] **P2-07** Pricing calculation: distance, weight, priority tier.
      *Done when:* quote endpoint returns price and ETA before order creation.

---

## Phase 3 — Mission planning and pilot handoff (1.5-2 weeks)

At this stage the server plans and validates; the pilot uploads through QGC.
Everything here is reusable unchanged once a command channel exists — only the
transport changes.

- [ ] **P3-01** Mission generator: pickup/dropoff coordinates → waypoint list
      (takeoff, climb to assigned altitude layer, cruise, loiter, land or
      hover-release, servo action, return leg). Pure function, no I/O.
      *Done when:* generated missions satisfy ArduPilot constraints and the
      generator is fully unit-tested without a vehicle.

- [ ] **P3-02** QGC `.plan` export: correct JSON structure (`fileType: "Plan"`,
      `mission`, `geoFence`, `rallyPoints`), schema-versioned.
      *Done when:* the exported file opens in QGC with no warnings and uploads
      to a SITL vehicle successfully.

- [ ] **P3-03** Geofence included in the export as a polygon matching the
      approved corridor, plus rally points at the nearest bases.
      *Done when:* a SITL vehicle flown outside the fence triggers FC-level RTL
      with no server involvement.

- [ ] **P3-04** Mission validation before release to the pilot: altitude band,
      leg lengths, turn angles, total energy, no-fly intersection, corridor
      reservation held.
      *Done when:* an invalid mission is rejected with a specific named reason,
      never a generic failure.

- [ ] **P3-05** Pilot handoff flow in the console: assigned mission appears,
      pilot downloads `.plan`, marks "uploaded", marks "launched". Each step
      timestamped in `events`.
      *Done when:* the full handoff is auditable end to end.

- [ ] **P3-06** Progress inference from telemetry: `MISSION_CURRENT`,
      `MISSION_ITEM_REACHED`, mode and arm state, proximity to waypoints. Drives
      the order state machine for reversible transitions only.
      *Done when:* a SITL flight advances the order through its states with no
      manual input, and the two irreversible steps (payload release, delivery
      complete) still require pilot confirmation.

- [ ] **P3-07** Deviation monitoring: compare actual track against the approved
      mission continuously. Alert on >50 m lateral deviation, altitude outside
      the approved band, or unexpected mode change.
      *Done when:* deliberately flying a SITL vehicle off-route raises an alert
      within 5 s.

- [ ] **P3-08** Mission reconciliation: detect when the vehicle is flying
      something other than what the server planned (pilot loaded the wrong file,
      or edited it in QGC).
      *Done when:* a modified mission is detected and flagged, not silently
      tracked as if it were the original.

- [ ] **P3-09** End-to-end manual-loop SITL run: order created → drone assigned →
      corridor reserved → `.plan` generated → loaded in QGC → flown → order
      completed.
      *Done when:* the loop completes 20 consecutive times and every run is
      fully reconstructable from `events` alone.

---

## Phase 3B — Direct command channel (DEFERRED)

Unblocked by swapping QGC forwarding for `mavlink-router` on the ground PC.
Still requires nothing on the aircraft. Do this when the manual handoff becomes
the bottleneck — not before.

- [ ] **P3B-01** `mavlink-router` configuration replacing QGC forwarding, with
      QGC still attached as one of the endpoints.
- [ ] **P3B-02** Bidirectional Gateway: per-vehicle command queue, send + await
      ACK, 3 retries with backoff, terminal failure as an alert. Never a silent
      success.
      Also measures round-trip latency per vehicle, moved here from P1-09:
      the first point at which the Gateway sends anything to time.
- [ ] **P3B-03** Idempotency keys on every command; duplicate submission is a
      no-op.
- [ ] **P3B-04** Mission upload via the MAVLink mission protocol with read-back
      verification.
- [ ] **P3B-05** Flight mode control and arm/disarm, with pre-arm failures
      surfaced as human-readable causes.
- [ ] **P3B-06** Payload actuation via `DO_SET_SERVO` confirmed by servo output
      telemetry.
- [ ] **P3B-07** Automatic execution of deconfliction resolutions (tightens the
      P5 alert threshold from 60 s back to 30 s).
- [ ] **P3B-08** Fully autonomous SITL delivery: one API call, no human in the
      loop, 20 consecutive successes.

---

## Phase 4 — Dispatch (1.5-2 weeks)

- [ ] **P4-01** Eligibility filters as a single testable function.
      *Done when:* each filter has a test that isolates it.

- [ ] **P4-02** PostGIS nearest-drone query using the KNN operator (`<->`) with
      a partial index on idle drones.
      *Done when:* `EXPLAIN ANALYZE` shows index usage and sub-10 ms at 100
      drones.

- [ ] **P4-03** Energy budget calculation per `ARCHITECTURE.md` §6, including
      the return-to-base leg and the 35% reserve.
      *Done when:* tests cover wind penalty, payload penalty, and the boundary
      case where a drone is rejected by 1 Wh.

- [ ] **P4-04** Wind data integration and `wind_factor` derivation from
      forecast at route altitude.
      *Done when:* headwind on the outbound leg measurably reduces eligibility.

- [ ] **P4-05** Scoring function with config-driven weights.
      *Done when:* weights are changeable without redeploy and the chosen drone
      changes accordingly.

- [ ] **P4-06** Batch assignment on a 5 s tick using
      `scipy.optimize.linear_sum_assignment`.
      *Done when:* a benchmark shows batch beating greedy on average wait time
      for a 30-order burst.

- [ ] **P4-07** Reassignment on failure: drone goes offline or battery drops
      mid-mission → order re-enters the pool, customer is notified. At this
      stage reassignment is a pilot-facing recommendation, not an automatic
      recall.
      *Done when:* killing a SITL drone mid-flight surfaces a reassignment
      proposal within 30 s.

- [ ] **P4-08** Dispatch simulation harness: 10 drones, 30 orders, measured
      average ETA, utilisation, and rejection reasons.
      *Done when:* the report is generated automatically and committed as a
      baseline.

---

## Phase 5 — Airspace and deconfliction (3-4 weeks) — CRITICAL PATH

This is the phase where a bug means physical damage. Budget the most time here.

- [ ] **P5-00** Terrain elevation source: ground elevation AMSL for a given
      position, so that AGL becomes derivable at all.
      **A prerequisite for P5-01 and P5-03, not an optional extra.** There is
      currently no source for height above ground anywhere in the system:
      `GLOBAL_POSITION_INT.relative_alt` is "Altitude above home", and
      `GPS_RAW_INT.alt` and `VFR_HUD.alt` are MSL. `ARCHITECTURE.md` §7's
      altitude layers are therefore written in AMSL against a reference
      elevation, and the terrain bound that makes them safe
      (`max_terrain_rise_m = lowest_layer_offset_m - minimum_clearance_m`)
      cannot be checked without this.
      Candidate sources, to be evaluated rather than assumed:
      - **A DEM** — SRTM (~30 m postings, void-filled variants vary) or
        Copernicus DEM (~30 m, generally better in mountainous terrain, which
        Georgia is). Queried by position, served locally; licence and
        coverage both need checking.
      - **ArduPilot's `TERRAIN_REPORT`** (message 180), observed at 3.00 Hz in
        ADR-001's capture. It carries terrain height from the flight
        controller's own onboard terrain database, which makes it the
        aircraft's own view rather than an independent one. **Investigate, do
        not assume:** that database has its own coverage, resolution and
        loading behaviour, it can be absent or stale, and a value that is
        missing in flight is worse than one that was never offered. Whether it
        agrees with a DEM is itself worth measuring.
        **Measured 2026-09-25, and it rules `TERRAIN_REPORT` out as a sole
        source.** Read back from the raw archive, per source, so the two are
        not confused:
        - **`hexa-01`, the aircraft we fly** (SYSID 1): 3,280 messages in the
          2026-09-24 hardware run and 381 more on 2026-09-25, every one of them
          `loaded=0`, `pending=0`, `terrain_height=0.0`, `current_height=0.0`.
          The message is emitted at 3 Hz whether or not any terrain data is
          aboard. Identified as the airframe rather than a simulator by
          `GIMBAL_DEVICE_ATTITUDE_STATUS`, `GIMBAL_MANAGER_STATUS`,
          `MCU_STATUS` and `RPM`, none of which any SITL instance sent, and by
          the Siyi A8 mount announcing itself in `STATUSTEXT`.
        - **SITL** (SYSIDs 201-210): `loaded=336`, `pending=0`, and plausible
          `terrain_height` of 584-656 m. SITL fetches terrain tiles, so it has
          the data the aircraft lacks.
        So the simulator would have concealed this: an `alt_agl_m` mapped from
        `current_height` looks correct in SITL and is a flat 0.0 m on the real
        aircraft — a drone reported as on the ground for an entire flight. The
        value is not missing, it is confidently wrong, which is the failure this
        task exists to prevent. `TERRAIN_REPORT` may still be useful as a
        cross-check where tiles *are* loaded; it cannot be the source.
      *Done when:* ground elevation can be queried for any point in the
      operating area, the two sources have been compared over that area, and
      the disagreement between them is a recorded number rather than an
      assumption. Any use of `TERRAIN_REPORT` must treat `loaded == 0` as "no
      answer", never as zero.

- [ ] **P5-01** Corridor generation: route → buffered polygon + altitude band +
      time window, stored as a reservation.
      *Done when:* corridors are visible as polygons on the pilot map.

- [ ] **P5-02** Strategic conflict query: spatial ∩ temporal ∩ altitude overlap.
      *Done when:* two crossing missions at the same altitude and time are
      rejected; the same routes 10 minutes apart are accepted.

- [ ] **P5-03** Semicircular altitude rule assignment by track angle.
      Bands are **AMSL**, offset from an operating area's reference elevation
      (`ARCHITECTURE.md` §7.1). AGL bands would not guarantee separation: two
      aircraft 15 m apart in AGL over terrain differing by 15 m are at the same
      height. Needs P5-00 to check the terrain bound.
      *Done when:* reciprocal routes are automatically assigned different bands.

- [ ] **P5-04** Conflict resolution ladder: altitude change → departure delay →
      reroute, attempted in that order.
      *Done when:* a scenario with 5 competing missions resolves all of them.

- [ ] **P5-05** No-fly and restricted zone enforcement at planning time.
      *Done when:* a route through a no-fly polygon is rejected with the zone
      named in the error.

- [ ] **P5-06** Neighbour lookup on each telemetry tick, 800 m radius.
      *Done when:* lookup stays under 5 ms at 100 airborne drones.

- [ ] **P5-07** CPA computation per `ARCHITECTURE.md` §7.2.
      *Done when:* unit tests cover head-on, crossing, overtaking, parallel, and
      the zero-relative-velocity degenerate case.

- [ ] **P5-08** Deterministic resolution by drone ID with commanded descent or
      loiter, logged on both vehicles.
      *Done when:* the same conflict evaluated twice produces the identical
      resolution.

- [ ] **P5-09** Resolution delivery as a pilot instruction: critical alert
      naming both aircraft, time to closest approach, and the prescribed action.
      Threshold widened to `t_cpa < 60 s` for human reaction time. Compliance
      tracked by watching the resulting telemetry.
      *Done when:* both pilots involved in a conflict receive compatible
      instructions derived from the same deterministic rule, and failure to
      comply within 20 s escalates.

- [ ] **P5-10** *(deferred to Stage 2)* Onboard peer broadcast (LoRa or
      ESP-NOW): 1 Hz position packet, acted on without server involvement.
      Requires an onboard computer — see P8-01.

- [ ] **P5-11** ArduPilot `AVOID_*` and `FENCE_*` parameter profile applied and
      verified at vehicle registration. At Stage 0 this is the only automatic
      avoidance that exists, so it carries more weight than it will later.
      *Done when:* parameter drift from the profile raises an alert.

- [ ] **P5-12** Scenario framework: YAML defining drones, orders, wind, and
      expected outcome; runs in CI.
      *Done when:* `make scenario FILE=crossing_10.yml` produces a pass/fail
      report.

- [ ] **P5-13** **Soak test.** 15 SITL drones, 50 orders, one city square, 2
      hours continuous, with scripted pilot compliance (instruction followed
      after a simulated 10-20 s human delay).
      *Done when:* zero separation violations under 30 m, and the log shows how
      many conflicts were detected, how each was resolved, and what the worst
      observed separation was.

- [ ] **P5-14** Pilot-delay sensitivity study: rerun the soak scenario with
      simulated reaction delays of 5, 15, 30, and 60 s.
      *Done when:* the report states the maximum tolerable human delay. This
      number determines whether Stage 0 can safely run more than two aircraft
      at once — it is a go/no-go input, not a nice-to-have.

---

## Phase 6 — Pilot console (2 weeks)

- [ ] **P6-01** Map view: all active drones, routes, corridors, zones, bases.
      Layer toggles.
- [ ] **P6-02** Per-drone detail panel: full telemetry, mission progress,
      battery trend, link quality.
- [ ] **P6-03** Alert system with severity levels, audible cue for critical,
      acknowledge flow.
      **Alert text must not imply loss that has not happened.** A station going
      unreachable means the ground station cannot be reached from here; the
      relay is almost certainly still receiving and buffering, and the record
      will be complete once it reconnects (`relay-v1.md` §8, P1-02). An alert
      reading "telemetry lost" there is false, and a pilot who learns the
      alerts overstate things will discount the one that does not. Reserve
      loss wording for a reported `gap` or a drop counter that moved.
- [ ] **P6-04** Takeover: switch to GUIDED/LOITER, virtual joystick, altitude
      and heading control. Confirmation required for any armed-state change.
- [ ] **P6-05** WebRTC video feed (deferred until onboard computer exists —
      depends on P8).
- [ ] **P6-06** Pilot assignment view: which pilot supervises which drones,
      enforced concurrency limit.
- [ ] **P6-07** Intervention audit: every pilot action recorded with ID and
      timestamp.
      *Phase done when:* a pilot can pause a SITL mission, fly manually, and
      resume, with the full sequence in the audit log.

---

## Phase 7 — Failsafe matrix (1.5 weeks, parallel with Phase 6)

Each row is a task and a SITL test. None may be skipped.

- [ ] **P7-01** Relay or server link loss >30 s → flight unaffected, tracking
      degrades gracefully, pilot and customer both informed.
- [ ] **P7-02** Telemetry/RC link loss → ArduPilot `FS_OPTIONS` behaviour
      verified.
- [ ] **P7-03** Battery below 25% → pilot alert with nearest base named and
      distance shown; order marked for reassignment.
- [ ] **P7-04** Battery below 15% → critical alert; ArduPilot battery failsafe
      parameters verified to act independently of the pilot.
- [ ] **P7-05** GPS fix loss → LOITER, alert, land after 20 s.
- [ ] **P7-06** Geofence breach → FC-level RTL.
- [ ] **P7-07** Wind above threshold → new departures blocked, airborne recalled.
- [ ] **P7-08** Server outage → flight entirely unaffected; verify no code path
      makes the aircraft depend on the server being reachable.
- [ ] **P7-11** Ground PC or QGC crash mid-flight → FC failsafe behaviour
      verified. This is the most serious Stage 0 failure mode; `FS_OPTIONS` must
      be correct and tested before any flight with a real payload.
- [ ] **P7-09** Payload release failure → do not proceed, return with payload,
      alert.
- [ ] **P7-10** EKF variance / compass error → abort to nearest base.
      *Phase done when:* every row has an automated SITL test in CI.

---

## Phase 8 — Payload and delivery confirmation (1.5-2 weeks)

- [ ] **P8-01** Onboard computer bring-up: RPi Zero 2 W + LTE HAT, WireGuard,
      agent as a systemd service, boots and connects unattended.
- [ ] **P8-02** Store-and-forward telemetry buffering across link dropouts.
- [ ] **P8-03** Release mechanism driver (servo latch or winch) with position
      feedback.
- [ ] **P8-04** Weight sensor on the latch confirming the payload actually left.
- [ ] **P8-05** Recipient PIN verification in the customer app.
- [ ] **P8-06** Photo capture at the drop point, uploaded and attached to the
      order.
- [ ] **P8-07** Precision landing markers (if base landing accuracy demands it).

---

## Phase 9 — Customer app (2-3 weeks)

- [ ] **P9-01** Auth: phone number + OTP.
- [ ] **P9-02** Order creation: map pin selection, address search, weight and
      dimensions.
- [ ] **P9-03** Quote screen: price and ETA before committing.
- [ ] **P9-04** Live tracking: drone position, progress, updated ETA.
- [ ] **P9-05** Delivery confirmation: PIN display, photo receipt.
- [ ] **P9-06** Order history and rating.
- [ ] **P9-07** Push notifications for each state transition.
- [ ] **P9-08** i18n: `ka` and `en` complete.

---

## Phase 10 — Operations (1.5 weeks)

- [ ] **P10-01** Prometheus metrics: fleet availability, mission success rate,
      average ETA, battery health, conflict rate.
- [ ] **P10-02** Grafana dashboards: fleet overview, per-drone health,
      dispatch performance.
- [ ] **P10-03** **Flight replay**: any past mission replayed on the map with
      telemetry scrubbing. Essential for incident review — do not defer this.
      **Render gaps explicitly.** Telemetry can be missing for reasons the
      system already knows about — a relay `gap` from queue cap, an intake
      drop, a station that went unreachable (`relay-v1.md` §11). Replay must
      show the track stopping and say why, never interpolate across a hole. A
      smooth line through missing data invents evidence, which in an accident
      investigation is worse than showing nothing.
- [ ] **P10-04** Automatic `.bin` dataflash log retrieval and archival after
      each flight.
- [ ] **P10-05** Maintenance tracking: flight hours, battery cycles, propeller
      and motor service intervals.
- [ ] **P10-06** Billing and invoicing.
- [ ] **P10-07** Admin panel: fleet management, zone editing, pricing config.

---

## Phase 11 — Field testing and regulation (starts during Phase 3, not at the end)

- [ ] **P11-01** Contact the civil aviation authority. Establish what BVLOS
      commercial delivery requires and whether 1:1 pilot-to-drone is mandated.
      **Do this before Phase 5 is written** — the answer changes the dispatch
      design and the unit economics.
- [ ] **P11-02** Remote ID requirements and hardware selection.
- [ ] **P11-03** Insurance and operational authorisation.
- [ ] **P11-04** Single drone, VLOS, empty field: 20 consecutive successful
      autonomous missions.
- [ ] **P11-05** Two drones, deliberately crossing routes, VLOS, deconfliction
      observed and logged.
- [ ] **P11-06** Three drones, real payload, real addresses, pilot supervision.
- [ ] **P11-07** Operations manual and pilot training material.

---

## Sequencing

```
Stage 0 — QGC forwarding, pilot in the loop, nothing on the aircraft
  Technical MVP     P0 → P1 → P2 → P3         4-5 weeks
  Working system    P4 → P5 → P6 → P7         8-10 weeks

Stage 1 — mavlink-router, server commands, still nothing on the aircraft
  Automation        P3B                       2 weeks

Stage 2 — onboard computer
  Product           P8 → P9 → P10             5-7 weeks

Regulation          P11 in parallel from P3 onward
```

Roughly 5-6 months for one developer. Phases 1, 2, 6 and 9 are conventional work
and go fast with Claude Code. Phase 5 is the critical path.

**What Stage 0 buys.** The server cannot touch the aircraft, so no server bug
can cause a crash. Dispatch, corridor reservation, conflict detection, deviation
monitoring and the pilot console are all fully exercised and fully testable
before anything gains the ability to send a command. When P3B lands, it swaps
the transport under code that has already been proven.

**What Stage 0 costs.** Single base, pilot tied to the ground station, 2-3
aircraft per radio net, and every deconfliction resolution routed through human
reaction time. P5-14 measures whether that last one is acceptable; if the
tolerable delay turns out to be short, P3B moves up the schedule.
