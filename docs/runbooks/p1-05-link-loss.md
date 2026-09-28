# P1-05 link-loss check: kill one SITL aircraft, time its flip

P1-05's criterion: *killing a SITL instance flips its status within 20 s.*
Status is the existence of `drone:{drone_id}:state` in Redis. The key expires
`LINK_TIMEOUT_S` (15 s) after the aircraft's newest record was **captured**,
so no process decides link loss; expiry is the decision
(`gateway/live_state.py`).

## Result, 2026-09-28

| | |
|---|---|
| Fleet | 11 ArduCopter SITL, SYSID 201-211, headless, bound for the run |
| Path | SITL -> relay (UDP 14445) -> Gateway -> Redis, no QGC |
| Killed | instance 0 (SYSID 201, `SITL-01`): its arducopter and MAVProxy process groups |
| Flip | **14.9 s** after the kill (criterion: under 20 s) |
| Other 10 | live throughout, polled every 0.25 s for 20 s after the flip |

All 11 were live within 2.1 s of the Gateway and relay starting.

14.9 s is the 15 s timeout counted from the last record SITL-01 sent. The
transport added no measurable delay. That matches expiry counted from capture
time: a record already in flight at the kill still counts, but only from when
it was captured.

## Procedure

Run on the development machine, with the dev stack up (`make up`).

1. Set `SITL_QGC_PORT=14445` in `sim/sitl.env`, so SITL feeds the relay
   directly. Restore it afterwards.
2. Bind the fleet: `tools/register_aircraft.py` for `SITL-01`..`SITL-11` ->
   SYSID 201-211 on `tbilisi-base-1`. Reuse each existing `drone_id`, so no
   identity is invented twice.
3. Start SITL headless. WSLg's display is not always there, and without one
   `sim_vehicle.py` needs no xterm. CI launches SITL the same way:

   ```
   wsl -d Ubuntu-24.04 -- bash -lc "cd /mnt/d/Projects/courier && env -u DISPLAY -u WAYLAND_DISPLAY ./sim/run_sitl.sh -n 11"
   ```

4. Start the Gateway, then a relay with a fresh queue pointed at it.
5. Wait until all 11 `drone:*:state` keys exist.
6. Kill instance 0 by its process groups, which are the first two lines of
   `sim/out/sitl.pids`: `kill -TERM -- -<pgid>`.
7. Poll until SITL-01's key is gone. Check that no other key disappeared.
8. Stop everything. Retire the bindings with `register_aircraft.py --retire`.

Write the kill as a script file and run that. A `$(...)` passed through
`wsl ... bash -lc` from Windows is mangled on the way. The first attempt
killed nothing and reported it clearly, which is how this was found.
