"use strict";

async function report(data, kind) {
  if (!data || !data.r || !data.k) return;
  try {
    await fetch(data.r, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ d: data.id, k: data.k, e: kind }),
      keepalive: true,
    });
  } catch (e) { /* the service treats a missing report as "not shown yet" */ }
}

self.addEventListener("push", (event) => {
  let m;
  try { m = event.data.json(); } catch (e) { return; }
  const data = { u: m.u, r: m.r, k: m.k, id: m.id };
  event.waitUntil(
    self.registration.showNotification(m.t || "Notification", {
      body: m.b, tag: m.id, requireInteraction: !!m.ri, data,
    }).then(() => report(data, "shown"))
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  event.waitUntil(Promise.all([
    report(data, "clicked"),
    data.u ? self.clients.openWindow(data.u) : Promise.resolve(),
  ]));
});
