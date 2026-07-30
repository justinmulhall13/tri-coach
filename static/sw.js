// Tri Coach service worker — receives push nudges and shows notifications.
self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { title: "Tri Coach", body: event.data && event.data.text() }; }
  const title = data.title || "Tri Coach";
  const options = {
    body: data.body || "",
    tag: data.tag || "tricoach-nudge",
    icon: data.icon || "/static/icon.png",
    badge: "/static/icon.png",
    data: { url: "/" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const c of clients) { if ("focus" in c) return c.focus(); }
      if (self.clients.openWindow) return self.clients.openWindow("/");
    })
  );
});
