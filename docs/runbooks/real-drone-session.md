# One session with the real aircraft: everything that is waiting on it

Four tasks cannot close without the real aircraft and QGC: P1-00, P1-01,
P1-01b and the last part of P1-11. This runbook puts their measurements in
one sitting, in the order that keeps each one valid. About an hour.

**Propellers off, the whole session.** Nothing here flies. USB power is enough
for parts 1-3; the radio part needs the flight battery or a bench supply.

Each part names the document its result goes into. A part that cannot be
completed is recorded as not done, with why; it is never filled in from
memory or from a similar-looking run.

## 0. Before the aircraft is connected

- Dev stack up (`make up`), `.env` present, the aircraft registered and bound
  once (`local-end-to-end.md`, "One-time: register the aircraft"). With P2-05
  it can be registered through the API instead: `POST /drones`, then bind its
  SYSID with `tools/register_aircraft.py --drone-id <id>`.
- Note the QGC version: Help -> About. ADR-001's environment table still says
  TO FILL; the executable's version resource reads `0.0.0.0`, so a person has
  to read it off the screen.
- QGC forwarding: Application Settings -> General -> MAVLink -> Enable MAVLink
  forwarding, host `127.0.0.1:14445`.

## 1. P1-00, finding 2: is the forwarded channel bidirectional?

The probe needs port 14445 to itself, so this runs **before** the relay is
started.

1. Connect the aircraft over USB. Let QGC finish its parameter download.
2. Close every other ground station. Leave QGC idle on the flight view for two
   minutes: ADR-001's first attempt was void because QGC was still requesting
   parameters on its own.
3. `python tools/mavlink_probe.py roundtrip --param SYSID_THISMAV`

Record the output verbatim in ADR-001 finding 2. INCONCLUSIVE again is a
result, not a failure: note how long QGC was idle and move on. Nothing built
depends on the answer (ADR-001, "This does not block anything").

## 2. P1-11: does QGC request the version on connect?

1. Disconnect the aircraft in QGC (or unplug USB).
2. Start the Gateway, then the relay (`local-end-to-end.md`, "Start order").
   The relay must be listening **before** QGC connects: the reply is sent once,
   to whoever is forwarding at that moment.
3. Reconnect the aircraft.
4. Expect a `firmware recorded` line in the Gateway log, and a row in
   `drone_firmware` for this drone. The console shows `fw <version> (<hash>)`.

If no row appears, check the archive for `AUTOPILOT_VERSION` before concluding
anything (`p1-11-firmware.md` shows how the SITL run was read). No such message
in the archive means QGC did not ask, and the runbook's premise was wrong:
record that in `p1-11-firmware.md`. The aircraft then stays `fw unknown`, which
is the visible failure P1-11 asks for.

Also fill ADR-001's firmware row from this reading.

## 3. P1-01: QGC-to-relay integrity over USB

With the relay running and the aircraft on USB for at least ten minutes:

    python tools/analyze_capture.py <sink or capture directory>

USB has no radio to lose frames, so a hole in the MAVLink sequence here is loss
between QGC and the relay (`p1-01-hardware-test.md`, "Then check upstream
integrity"). Record it in `p1-01-test-records.md`.

**Procedure A (cable pulled for 2 minutes) needs a second machine** so that
the uplink crosses a real network and TLS is exercised. It waits for the
staging server, or for a second computer on the LAN; it is not done in this
session unless one is available.

## 4. P1-01b: the radio port's stream rates

The budget that matters is the radio's, and ADR-001 only measured USB (`SR0_*`).

1. Switch the aircraft to the telemetry radio. QGC connects through it.
2. Read and write down the `SR1_*` (or `SR2_*`, whichever serial port the radio
   is on) parameters, and the radio's air data rate and `NETID`.
3. With the relay stopped again: `python tools/mavlink_probe.py listen
   --seconds 60 --json docs/decisions/001-listen-radio.json`
4. The report's bytes per second is the measured radio budget for one aircraft.

This is the measurement P1-01b's "Done when" requires. The document itself is
drafted in `docs/pilot/ground-station-setup.md`; the numbers from this part
replace its TO MEASURE markers.

## 5. After

- Stop relay, Gateway and console; leave the aircraft bound (it is the real
  one).
- Commit the filled documents; each task closes in TASKS.md only on what was
  observed in this session.
