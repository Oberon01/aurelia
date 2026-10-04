// Aurelia's service worker: it shows the notifications the server pushes, and opens Aurelia when one is
// tapped. It caches nothing, so the app is always the live view (never a stale copy).
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = { title: "Aurelia", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "Aurelia", {
    body: d.body || "", tag: d.tag || undefined, renotify: !!d.tag,
    icon: "/icons/icon-192.png", badge: "/icons/icon-192.png", data: { url: d.url || "/view" },
  }));
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "/view", self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(wins => {
    for (const w of wins) {               // an open window: bring it up and open what the notification is about there
      if (w.url.startsWith(self.location.origin + "/view")) { w.postMessage({ open: url }); return w.focus(); }
    }
    return self.clients.openWindow(url);
  }));
});
