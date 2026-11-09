// Sun-based dark mode: dark from sunset to sunrise at a given place. A standalone ES module with no imports and no
// DOM access except in watchSun(), so it can be copied into another app unchanged (Iris admin UI, Colossus).
// Times come from the NOAA solar equations (accurate to a minute or two at mid latitudes); sunrise and sunset are
// when the sun's upper edge crosses the horizon (zenith 90.833 degrees, refraction included).

export const HOUTEN = Object.freeze({ latitude: 52.0281, longitude: 5.1686 }); // Houten, The Netherlands

const RAD = Math.PI / 180;
const DAY_MS = 86400000;

/** Sunrise and sunset for the UTC calendar day of `date`, as Date objects. In polar day or polar night both are
 *  null and `polar` says which ("day": the sun stays up, "night": it stays down). */
export function sunTimes(date, latitude, longitude) {
  const day0 = Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate());
  const doy = Math.round((day0 - Date.UTC(date.getUTCFullYear(), 0, 1)) / DAY_MS) + 1;
  const leap = new Date(Date.UTC(date.getUTCFullYear(), 1, 29)).getUTCMonth() === 1;
  const g = (2 * Math.PI / (leap ? 366 : 365)) * (doy - 1); // fractional year at 12:00 UTC: (hour-12)/24 = 0
  const eqtime = 229.18 * (0.000075 + 0.001868 * Math.cos(g) - 0.032077 * Math.sin(g)
    - 0.014615 * Math.cos(2 * g) - 0.040849 * Math.sin(2 * g)); // minutes
  const decl = 0.006918 - 0.399912 * Math.cos(g) + 0.070257 * Math.sin(g) - 0.006758 * Math.cos(2 * g)
    + 0.000907 * Math.sin(2 * g) - 0.002697 * Math.cos(3 * g) + 0.00148 * Math.sin(3 * g); // radians
  const lat = latitude * RAD;
  const cosHa = Math.cos(90.833 * RAD) / (Math.cos(lat) * Math.cos(decl)) - Math.tan(lat) * Math.tan(decl);
  if (cosHa > 1) return { sunrise: null, sunset: null, polar: "night" };
  if (cosHa < -1) return { sunrise: null, sunset: null, polar: "day" };
  const ha = Math.acos(cosHa) / RAD; // degrees
  const at = (minutes) => new Date(day0 + Math.round(minutes * 60000));
  return { sunrise: at(720 - 4 * (longitude + ha) - eqtime), sunset: at(720 - 4 * (longitude - ha) - eqtime), polar: null };
}

/** Every sunrise and sunset from two days before `now` to two days after, in time order. */
function events(now, latitude, longitude) {
  const out = [];
  for (let d = -2; d <= 2; d++) {
    const t = sunTimes(new Date(now.getTime() + d * DAY_MS), latitude, longitude);
    if (t.sunrise) out.push({ at: t.sunrise, dark: false }, { at: t.sunset, dark: true });
  }
  return out.sort((a, b) => a.at - b.at);
}

/** Whether it is dark (between sunset and sunrise) at `now` for the given place. */
export function isDarkAt(now, latitude, longitude) {
  const past = events(now, latitude, longitude).filter((e) => e.at <= now);
  if (past.length) return past[past.length - 1].dark;
  return sunTimes(now, latitude, longitude).polar === "night";
}

/** The next sunrise or sunset after `now`, or null when none falls within two days (polar day or night). */
export function nextChange(now, latitude, longitude) {
  return events(now, latitude, longitude).find((e) => e.at > now)?.at ?? null;
}

/** Calls onChange(dark) now and again at every sunrise and sunset, until the returned stop() is called. Also
 *  re-checks when the page becomes visible again and every 10 minutes, since timers do not run while a device
 *  sleeps. Only calls onChange when the answer changes (and once at the start). */
export function watchSun({ latitude, longitude, onChange }) {
  let last = null, timer = null, stopped = false;
  const check = () => {
    if (stopped) return;
    clearTimeout(timer);
    const now = new Date();
    const dark = isDarkAt(now, latitude, longitude);
    if (dark !== last) { last = dark; onChange(dark); }
    const next = nextChange(now, latitude, longitude);
    const wait = next ? Math.min(next - now + 1000, 600000) : 600000;
    timer = setTimeout(check, Math.max(wait, 1000));
  };
  const onVisible = () => { if (document.visibilityState === "visible") check(); };
  document.addEventListener("visibilitychange", onVisible);
  check();
  return () => { stopped = true; clearTimeout(timer); document.removeEventListener("visibilitychange", onVisible); };
}

/** Checks a place: both numbers, latitude -90..90, longitude -180..180. */
export function validPlace(latitude, longitude) {
  return Number.isFinite(latitude) && Number.isFinite(longitude) && Math.abs(latitude) <= 90 && Math.abs(longitude) <= 180;
}
