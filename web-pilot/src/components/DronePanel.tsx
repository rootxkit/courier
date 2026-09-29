// P6-02. Everything the feed carries about one aircraft. Mission progress is
// not here: there are no missions yet (P3).
import { DASH, ageSeconds, num, shortId } from "../format";
import { useT } from "../i18n";
import type { Aircraft, Alert } from "../types";
import { Sparkline } from "./Sparkline";

interface Props {
  droneId: string;
  aircraft: Aircraft | undefined;
  alerts: Alert[];
  now: number;
  onClose: () => void;
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </>
  );
}

export function DronePanel({ droneId, aircraft, alerts, now, onClose }: Props) {
  const t = useT();
  const d = aircraft?.data;
  return (
    <aside className="detail">
      <header className="detail-head">
        <div>
          <h2>{d?.label ?? t("unnamed")}</h2>
          <div className="muted mono small">{droneId}</div>
        </div>
        <button type="button" className="icon" onClick={onClose} aria-label={t("close")}>
          ×
        </button>
      </header>
      {!d || !aircraft ? (
        <p className="muted pad">{t("no_position")}</p>
      ) : (
        <>
          {alerts.length > 0 && (
            <ul className="detail-alerts">
              {alerts.map((alert) => (
                <li key={alert.key} className={`alert-${alert.severity}`}>
                  {t(alert.severity)} ·{" "}
                  {alert.kind === "conflict"
                    ? t("conflict")
                    : t("in_zone", { zone: alert.detail.zone_name ?? DASH })}
                </li>
              ))}
            </ul>
          )}
          <section>
            <h3>{t("battery_trend")}</h3>
            <Sparkline
              points={aircraft.history.map((h) => ({ t: h.t, value: h.batt }))}
              min={0}
              max={100}
              label={t("battery_trend")}
            />
          </section>
          <dl className="fields">
            <Field
              label={t("last_seen")}
              value={t("seconds_ago", { n: ageSeconds(aircraft.receivedAt, now) })}
            />
            <Field label={t("mode")} value={d.mode ?? DASH} />
            <Field
              label={t("arming")}
              value={d.armed === null ? t("unknown") : d.armed ? t("armed") : t("disarmed")}
            />
            <Field label={t("battery")} value={num(d.batt_pct, 0, "%")} />
            <Field label={t("voltage")} value={num(d.batt_voltage_v, 2, "V")} />
            <Field label={t("consumed")} value={num(d.batt_consumed_wh, 1, "Wh")} />
            <Field
              label={t("position")}
              value={
                d.lat_deg === null || d.lon_deg === null
                  ? t("no_position")
                  : `${d.lat_deg.toFixed(6)}, ${d.lon_deg.toFixed(6)}`
              }
            />
            <Field label={t("altitude_amsl")} value={num(d.alt_amsl_m, 1, "m")} />
            <Field label={t("altitude_home")} value={num(d.alt_above_home_m, 1, "m")} />
            <Field label={t("heading")} value={num(d.heading_deg, 0, "°")} />
            <Field label={t("speed")} value={num(d.groundspeed_ms, 1, "m/s")} />
            <Field label={t("climb")} value={num(d.climb_ms, 1, "m/s")} />
            <Field
              label={t("velocity")}
              value={[d.vx_ms, d.vy_ms, d.vz_ms].map((v) => num(v, 1)).join(" / ") + " m/s"}
            />
            <Field
              label={t("gps")}
              value={t("gps_value", { fix: d.gps_fix_type ?? DASH, sats: d.sat_count ?? DASH })}
            />
            <Field label={t("station")} value={d.station_id} />
            <Field label={t("loss")} value={d.link ? num(d.link.loss_pct, 1, "%") : DASH} />
            <Field
              label={t("heartbeat_gap")}
              value={d.link ? num(d.link.heartbeat_gap_max_s, 1, "s") : DASH}
            />
            <Field
              label={t("firmware")}
              value={
                d.firmware
                  ? `${d.firmware.version ?? DASH}${d.firmware.git_hash ? ` (${d.firmware.git_hash})` : ""}`
                  : DASH
              }
            />
          </dl>
          <section>
            <h3>{t("altitude_home")}</h3>
            <Sparkline
              points={aircraft.history.map((h) => ({ t: h.t, value: h.alt }))}
              min={Math.min(0, ...aircraft.history.map((h) => h.alt ?? 0))}
              max={Math.max(10, ...aircraft.history.map((h) => h.alt ?? 0))}
              label={t("altitude_home")}
            />
          </section>
        </>
      )}
      <a className="button" href={`/replay?drone=${encodeURIComponent(droneId)}`}>
        {t("replay_this")} ({shortId(droneId)})
      </a>
    </aside>
  );
}
