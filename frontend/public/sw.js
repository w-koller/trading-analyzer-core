/**
 * Service worker — notifications only.
 *
 * There is deliberately NO offline caching here. This dashboard shows live
 * positions, prices and theses; a cached shell serving yesterday's numbers
 * without saying so is worse than a page that fails honestly, and this whole
 * codebase already treats "stale data presented as current" as the thing to
 * design against (rule #7, decisions #32). The service worker exists solely
 * so Web Push has something to deliver to, because the Push API requires one.
 *
 * Keep it that way. If offline support is ever wanted, it needs an explicit
 * staleness indicator in the UI first.
 */

self.addEventListener("install", () => {
  // Take over immediately rather than waiting for every tab to close —
  // otherwise a push subscription made now is handled by nothing until the
  // user quits the app entirely.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    // A push with a body we cannot parse still means SOMETHING happened, and
    // silently dropping it is the wrong failure for an alert channel.
    payload = { title: "Trading Analyzer", body: "You have a new alert." };
  }

  const title = payload.title || "Trading Analyzer";
  const critical = payload.severity === "critical";

  event.waitUntil(
    self.registration.showNotification(title, {
      body: payload.body || "",
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      // The alert fingerprint. Same fact re-delivered (a phone that was
      // offline can be handed it twice) replaces the old notification instead
      // of stacking a duplicate.
      tag: payload.tag || "trading-analyzer",
      renotify: true,
      // A breached stop is worth interrupting for; a warn-level one is not
      // worth pulling someone out of a meeting.
      requireInteraction: critical,
      data: { url: payload.url || "/" },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";

  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clients) => {
        // Reuse an open window if there is one — launching a second copy of an
        // installed PWA is disorienting, and the user usually already has the
        // dashboard open on the phone.
        for (const client of clients) {
          if ("focus" in client) {
            client.navigate(target);
            return client.focus();
          }
        }
        return self.clients.openWindow(target);
      }),
  );
});
