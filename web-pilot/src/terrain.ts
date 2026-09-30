// P5-00. Ground (surface) elevation under an aircraft, from GET /terrain.
// Asked again when the aircraft has moved ~10 m, at most every 5 s, so
// a panel open on a 4 Hz feed is not four requests a second.
import { useEffect, useRef, useState } from "react";
import { type GetResponse, apiGet } from "./api/client";

export type TerrainAt = GetResponse<"/terrain">;

// "unknown": the API answered that it has no elevation here (404) or no
// terrain at all (503). Different from "loading", and never shown as zero.
export type TerrainState = TerrainAt | "loading" | "unknown";

const MIN_INTERVAL_MS = 5000;

export function useTerrain(lat: number | null, lon: number | null): TerrainState {
  const [state, setState] = useState<TerrainState>("loading");
  // Bumped to re-check a position that was skipped for being too soon.
  const [recheck, setRecheck] = useState(0);
  const last = useRef<{ key: string; at: number } | null>(null);
  // ~11 m in latitude; the DEM is sampled every 30-90 m.
  const key = lat === null || lon === null ? "" : `${lat.toFixed(4)},${lon.toFixed(4)}`;

  useEffect(() => {
    if (!key) {
      setState("unknown");
      return;
    }
    // From the key, not the live position: a new message a few centimetres on
    // must not cancel a request in flight and start another.
    const [atLat, atLon] = key.split(",");
    const now = Date.now();
    if (last.current && last.current.key === key) return;
    if (last.current && now - last.current.at < MIN_INTERVAL_MS) {
      const timer = window.setTimeout(
        () => setRecheck((n) => n + 1),
        MIN_INTERVAL_MS - (now - last.current.at),
      );
      return () => window.clearTimeout(timer);
    }
    last.current = { key, at: now };
    let cancelled = false;
    let settled = false;
    apiGet("/terrain", `?lat_deg=${atLat}&lon_deg=${atLon}`)
      .then((found) => {
        settled = true;
        if (!cancelled) setState(found);
      })
      .catch(() => {
        settled = true;
        if (!cancelled) setState("unknown");
      });
    return () => {
      cancelled = true;
      // An answer abandoned before it arrived was never had: ask again next
      // time rather than wait forever on a position marked as asked.
      if (!settled) last.current = null;
    };
  }, [key, recheck]);

  return state;
}
