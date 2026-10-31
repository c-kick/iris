"use strict";
(function () {
  const status = document.getElementById("status");
  const button = document.getElementById("enable");
  const code = new URLSearchParams(location.search).get("c") || "";
  const say = (text) => { status.textContent = text; };

  function key(b64) {
    const pad = "=".repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
  }

  async function enable() {
    button.disabled = true;
    try {
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        throw new Error("This browser does not support push notifications.");
      }
      if ((await Notification.requestPermission()) !== "granted") {
        throw new Error("Notifications were not allowed. Allow them for this site and try again.");
      }
      const reg = await navigator.serviceWorker.register("/push/sw.js", { scope: "/push/" });
      await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: key(document.body.dataset.key),
      });
      const res = await fetch("/push/enroll", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, subscription: sub.toJSON(), user_agent: navigator.userAgent }),
      });
      if (!res.ok) {
        await sub.unsubscribe();
        throw new Error(res.status === 410 ? "This link was already used or has expired." : "Enrolling failed (" + res.status + ").");
      }
      say("Done. This browser now receives your notifications.");
    } catch (e) {
      say(e.message || String(e));
      button.disabled = false;
    }
  }
  button.addEventListener("click", enable);
})();
