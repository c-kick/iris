// Render at 128 px so the browser can scale the icon for its tab strip.
function numberedIcon(doc, count) {
  const canvas = doc.createElement("canvas");
  canvas.width = canvas.height = 128;
  const c = canvas.getContext("2d");
  if (!c) throw new Error("Canvas unavailable");
  c.strokeStyle = "#087f79"; c.lineWidth = 9; c.lineCap = "round";
  for (const [x1, y1, x2, y2] of [[91, 64, 107, 64], [64, 91, 64, 107], [37, 64, 21, 64], [64, 37, 64, 21]]) {
    c.beginPath(); c.moveTo(x1, y1); c.lineTo(x2, y2); c.stroke();
  }
  c.fillStyle = "#087f79";
  for (const [x, y] of [[113, 64], [64, 113], [15, 64], [64, 15]]) {
    c.beginPath(); c.arc(x, y, 7, 0, Math.PI * 2); c.fill();
  }
  c.beginPath(); c.arc(64, 64, 13, 0, Math.PI * 2); c.fill();
  const label = count > 99 ? "99+" : String(count);
  const padding = 16;
  let size = 85;
  let metrics;
  do {
    c.font = `bold ${size}px system-ui`;
    metrics = c.measureText(label);
    if (metrics.width <= 124 - 2 * padding && metrics.actualBoundingBoxAscent + metrics.actualBoundingBoxDescent <= 124 - 2 * padding) break;
    size -= 1;
  } while (size > 12);
  const ascent = metrics.actualBoundingBoxAscent;
  const descent = metrics.actualBoundingBoxDescent;
  const width = Math.max(8, Math.ceil(metrics.width) + 2 * padding);
  const height = Math.max(8, Math.ceil(ascent + descent) + 2 * padding);
  const x = 126 - width;
  c.fillStyle = "#173c3a"; c.strokeStyle = "#092323"; c.lineWidth = 4;
  c.beginPath(); c.roundRect(x, 2, width, height, Math.min(24, height / 2)); c.fill(); c.stroke();
  c.fillStyle = "#ffffff"; c.textAlign = "center"; c.textBaseline = "alphabetic";
  c.fillText(label, x + width / 2, 2 + (height - ascent - descent) / 2 + ascent);
  return canvas.toDataURL("image/png");
}

// The tab mirrors unread chats and outstanding notices, even away from the Chats page.
export function createTabAlerts({ loadFolders, onFolders, chatVisible, doc = document, interval = 5000 }) {
  const icons = [...doc.querySelectorAll('link[rel="icon"]')];
  const title = doc.title;
  const alertIcon = doc.createElement("link");
  alertIcon.rel = "icon";
  alertIcon.type = "image/svg+xml";
  alertIcon.href = "/admin/static/brand/iris-favicon-unread.svg";
  let count = 0;
  let timer = null;
  let running = false;
  let generation = 0;
  let revision = 0;
  let pending = null;

  function update(folders) {
    revision += 1;
    // A notice may remain after its message was read. Count it once for that folder;
    // a message with a bell and an @ must not be counted three times.
    const next = folders.reduce((n, f) => n + Math.max(f.unread || 0, f.alert || f.owner_mention || f.pending_confirm_requests ? 1 : 0), 0);
    if (next === count) return;
    count = next;
    doc.title = count ? `(${count}) ${title}` : title;
    if (count) {
      try { alertIcon.href = numberedIcon(doc, count); alertIcon.type = "image/png"; }
      catch { alertIcon.href = "/admin/static/brand/iris-favicon-unread.svg"; alertIcon.type = "image/svg+xml"; }
      icons.forEach((icon) => icon.remove());
      if (!alertIcon.isConnected) doc.head.append(alertIcon);
    } else {
      alertIcon.remove();
      icons.forEach((icon) => doc.head.append(icon));
    }
  }

  async function poll() {
    if (!running || pending || chatVisible()) return;
    const at = generation;
    const before = revision;
    const request = loadFolders();
    pending = request;
    try {
      const folders = await request;
      // A foreground refresh or sign-out may have happened while this was in flight.
      if (running && generation === at && revision === before) {
        update(folders);
        onFolders(folders);
      }
    } catch { /* transient failure: retain the last known badge and retry */ }
    finally { if (pending === request) pending = null; }
  }

  function start() {
    if (running) return;
    running = true;
    timer = setInterval(poll, interval); // keep polling hidden tabs; browsers may throttle the timer
    doc.addEventListener("visibilitychange", poll);
    doc.defaultView.addEventListener("focus", poll);
    poll();
  }

  function stop() {
    running = false;
    generation += 1;
    pending = null;
    clearInterval(timer);
    doc.removeEventListener("visibilitychange", poll);
    doc.defaultView.removeEventListener("focus", poll);
    update([]);
  }

  return { update, start, stop };
}
