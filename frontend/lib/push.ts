/**
 * Web Push registration, browser side.
 *
 * The awkward part of this API is that there are FOUR independent states that
 * all look like "notifications are off", and conflating them produces a
 * toggle that lies:
 *
 *   unsupported   no service worker or no PushManager (older browser, or any
 *                 iOS browser before 16.4 / not installed to the home screen)
 *   unconfigured  the server has no VAPID keys, so subscribing cannot work
 *   denied        the user refused permission — and the browser will NOT ask
 *                 again, so a toggle that just retries does nothing forever
 *   default       never asked; a prompt will appear
 *
 * They are distinguished across three places rather than by one call, and it
 * is worth knowing which is which before changing any of them:
 *
 *   unsupported   `isSupported()` here, checked before anything else runs
 *   unconfigured  the server's own `/push/status`, read by the toggle — only
 *                 the backend knows whether it holds VAPID keys
 *   denied /      inside `subscribe()`, from the permission the browser
 *   default       returns. Both throw, with DIFFERENT messages, because
 *                 "blocked, go to site settings" and "you dismissed it, try
 *                 again" call for different actions from the user.
 *
 * Everything here is browser-side. The server is told about a subscription
 * only after the browser has produced one.
 */

import { api } from "@/lib/api";

export function isSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

/** base64url -> Uint8Array, which is what applicationServerKey requires. */
function urlBase64ToUint8Array(base64: string): Uint8Array {
  const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), "=");
  const raw = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

export async function registerServiceWorker(): Promise<ServiceWorkerRegistration> {
  return navigator.serviceWorker.register("/sw.js", { scope: "/" });
}

export async function currentSubscription(): Promise<PushSubscription | null> {
  if (!isSupported()) return null;
  const reg = await navigator.serviceWorker.getRegistration("/");
  if (!reg) return null;
  return reg.pushManager.getSubscription();
}

/**
 * Subscribe this device and register it with the backend.
 *
 * The server is told only AFTER the browser hands back a subscription, so a
 * refused permission never leaves a dead row in push_subscriptions.
 */
export async function subscribe(publicKey: string): Promise<void> {
  const reg = await registerServiceWorker();
  await navigator.serviceWorker.ready;

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error(
      permission === "denied"
        ? "Notifications are blocked for this site. Re-enable them in your browser's site settings."
        : "Notification permission was dismissed.",
    );
  }

  const sub =
    (await reg.pushManager.getSubscription()) ??
    (await reg.pushManager.subscribe({
      // Required on Chrome: it refuses a subscription that could be used to
      // send silent, contentless pushes.
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(publicKey),
    }));

  await api.pushSubscribe(sub.toJSON() as unknown as Record<string, unknown>);
}

export async function unsubscribe(): Promise<void> {
  const sub = await currentSubscription();
  if (!sub) return;
  // Tell the server first. If the order were reversed and the page closed in
  // between, the row would linger and the backend would keep pushing to an
  // endpoint the browser has already discarded.
  await api.pushUnsubscribe(sub.endpoint);
  await sub.unsubscribe();
}
