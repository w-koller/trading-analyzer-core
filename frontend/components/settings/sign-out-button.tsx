"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { LogOut } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Sign out.
 *
 * Lives in the sidebar and NOT in the bottom nav on purpose: mobile-nav.tsx
 * is a hardcoded grid-cols-6 and six cells is already ~53px each at 320px
 * with a 9px truncated label. A seventh would make every label unreadable to
 * add a control nobody presses daily.
 *
 * The query cache is cleared before navigating. Without that, the next
 * person to sign in on this device paints the previous session's holdings
 * and theses from cache for a moment before the refetch lands.
 */
export function SignOutButton({ className }: { className?: string }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [busy, setBusy] = React.useState(false);

  async function signOut() {
    setBusy(true);
    try {
      await api.logout();
    } catch {
      // Deliberately ignored. The cookie is cleared server-side on success,
      // and if the request failed the redirect still gets the user to a login
      // screen — where a stale cookie is rejected anyway. Blocking sign-out
      // on a network error is the wrong call: "let me out" should always work.
    } finally {
      queryClient.clear();
      router.replace("/login");
      router.refresh();
    }
  }

  return (
    <button
      type="button"
      onClick={signOut}
      disabled={busy}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-1 py-1.5 text-[11px] transition-colors disabled:opacity-50",
        className,
      )}
    >
      <LogOut className="h-3.5 w-3.5 shrink-0" aria-hidden />
      {busy ? "Signing out…" : "Sign out"}
    </button>
  );
}
