"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, BellOff } from "lucide-react";
import { api } from "@/lib/api";
import * as push from "@/lib/push";
import { cn } from "@/lib/utils";

/**
 * Enable/disable push notifications for THIS device.
 *
 * Per-device, not per-account, and the copy says so — a subscription belongs
 * to one browser on one phone, so enabling it on a laptop does nothing for
 * the phone in your pocket and a toggle that implied otherwise would be lying.
 *
 * Permission is requested on press, never on mount. Chrome demotes sites that
 * prompt unprompted, and a permission dialog that appears before the user has
 * asked for anything is the fastest way to get "Block" pressed — which is
 * permanent, and cannot be re-prompted.
 *
 * Lives in the sidebar and NOT the bottom nav: mobile-nav.tsx is a hardcoded
 * grid-cols-6 and a seventh cell at 320px would leave ~45px each.
 */
export function NotificationToggle({ className }: { className?: string }) {
  const queryClient = useQueryClient();
  const [subscribed, setSubscribed] = React.useState<boolean | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const supported = push.isSupported();

  const { data: status } = useQuery({
    queryKey: ["push-status"],
    queryFn: () => api.pushStatus(),
    enabled: supported,
  });

  React.useEffect(() => {
    if (!supported) return;
    push.currentSubscription().then((s) => setSubscribed(Boolean(s)));
  }, [supported]);

  const toggle = useMutation({
    mutationFn: async () => {
      setError(null);
      if (subscribed) {
        await push.unsubscribe();
        return false;
      }
      if (!status?.public_key) throw new Error("Push is not configured on the server.");
      await push.subscribe(status.public_key);
      return true;
    },
    onSuccess: (nowSubscribed) => {
      setSubscribed(nowSubscribed);
      queryClient.invalidateQueries({ queryKey: ["push-status"] });
    },
    onError: (e: Error) => setError(e.message),
  });

  const test = useMutation({
    mutationFn: () => api.pushTest(),
    onError: (e: Error) => setError(e.message),
  });

  // Say which of the several "off" states this is, rather than showing a
  // toggle that would silently do nothing.
  if (!supported) {
    return (
      <p className={cn("px-1 text-[10px] leading-snug", className)}>
        This browser cannot receive notifications.
      </p>
    );
  }
  if (status && !status.configured) {
    return (
      <p className={cn("px-1 text-[10px] leading-snug", className)}>
        Notifications unavailable — no VAPID keys on the server.
      </p>
    );
  }
  const blocked =
    typeof Notification !== "undefined" && Notification.permission === "denied";

  return (
    <div className={cn("space-y-1.5", className)}>
      <button
        type="button"
        onClick={() => toggle.mutate()}
        disabled={toggle.isPending || blocked}
        className="flex w-full items-center gap-2 rounded-md px-1 py-1.5 text-[11px] transition-colors disabled:opacity-50"
      >
        {subscribed ? (
          <Bell className="h-3.5 w-3.5 shrink-0" aria-hidden />
        ) : (
          <BellOff className="h-3.5 w-3.5 shrink-0" aria-hidden />
        )}
        {toggle.isPending
          ? "Working…"
          : subscribed
            ? "Alerts on (this device)"
            : "Enable alerts on this device"}
      </button>

      {subscribed && (
        <button
          type="button"
          onClick={() => test.mutate()}
          disabled={test.isPending}
          className="px-1 text-[10px] underline underline-offset-2 opacity-70 disabled:opacity-40"
        >
          {test.isPending
            ? "Sending…"
            : test.isSuccess && !test.data?.ok
              ? "Test failed — check the phone is online"
              : test.isSuccess
                ? "Test sent"
                : "Send a test notification"}
        </button>
      )}

      {blocked && (
        <p className="px-1 text-[10px] leading-snug opacity-70">
          Blocked in your browser&apos;s site settings — it will not ask again.
        </p>
      )}
      {error && <p className="px-1 text-[10px] leading-snug opacity-80">{error}</p>}
    </div>
  );
}
