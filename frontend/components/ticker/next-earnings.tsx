"use client";

import { useQuery } from "@tanstack/react-query";
import { CalendarDays } from "lucide-react";
import Link from "next/link";
import { api } from "@/lib/api";

/**
 * "Next earnings" for one ticker, or nothing at all.
 *
 * Renders null when there is no upcoming report rather than an empty state:
 * the right rail is already three cards deep, and a permanent "no earnings
 * scheduled" row is furniture — invisible precisely when it changes.
 *
 * Reads the stored calendar, so it costs one cheap query and never waits on
 * OpenD.
 */
export function NextEarnings({ code }: { code: string }) {
  const { data } = useQuery({
    queryKey: ["earnings", "code", code],
    queryFn: () => api.earnings({ days: 30 }),
    staleTime: 5 * 60_000,
  });

  const event = data?.events.find((e) => e.code === code);
  if (!event) return null;

  const when =
    event.pub_type === "BEFORE"
      ? "before the open"
      : event.pub_type === "AFTER"
        ? "after the close"
        : event.pub_type === "REGULAR"
          ? "during the session"
          : "time not stated";

  const away =
    event.days_until === 0
      ? "today"
      : event.days_until === 1
        ? "tomorrow"
        : `in ${event.days_until} days`;

  return (
    <Link
      href="/earnings"
      className="flex items-center gap-2 rounded-md border bg-muted/30 px-3 py-2 text-xs transition-colors hover:border-primary"
    >
      <CalendarDays className="h-3.5 w-3.5 shrink-0 text-primary" />
      <span>
        <span className="font-medium">Reports {away}</span>
        <span className="text-muted-foreground">
          {" "}
          — {event.earnings_date}, {when}
        </span>
      </span>
    </Link>
  );
}
