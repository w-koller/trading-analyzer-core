"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity } from "lucide-react";
import { NAV_ITEMS } from "@/components/layout/nav-items";
import { ThemeToggle } from "@/components/theme/theme-toggle";
import { ScanRunnerDialog } from "@/components/scan/scan-runner-dialog";
import { ScannerStatus } from "@/components/scan/scanner-status";
import { ModelSelector } from "@/components/settings/model-selector";
import { NotificationToggle } from "@/components/settings/notification-toggle";
import { SignOutButton } from "@/components/settings/sign-out-button";
import { cn } from "@/lib/utils";

/** Desktop left rail. Hidden below md, where MobileNav takes over. */
export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="fixed inset-y-0 left-0 z-30 hidden w-56 flex-col bg-sidebar text-sidebar-foreground md:flex">
      <div className="flex items-center gap-2 px-5 py-5">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-sidebar-accent">
          <Activity className="h-4 w-4" />
        </div>
        <div className="leading-tight">
          <p className="text-sm font-semibold tracking-tight">Trading Analyzer</p>
          <p className="text-[10px] uppercase tracking-wider text-sidebar-muted">Advisory only</p>
        </div>
      </div>

      <nav className="flex-1 space-y-1 px-3 py-2">
        {NAV_ITEMS.map((item) => {
          const active = pathname === item.href;
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "relative flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                active
                  ? "bg-sidebar-accent text-sidebar-foreground"
                  : "text-sidebar-muted hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
              )}
            >
              {active && (
                <span className="absolute inset-y-1.5 -left-3 w-1 rounded-r-full bg-primary" aria-hidden />
              )}
              <Icon className="h-4 w-4 shrink-0" />
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="space-y-2 border-t border-sidebar-border p-3">
        <p className="px-1 text-[10px] leading-snug text-sidebar-muted">
          Never places orders. Suggestions are for manual review.
        </p>
        <ScannerStatus className="bg-sidebar-accent/40 border-sidebar-border text-sidebar-foreground" />
        <ModelSelector className="bg-sidebar-accent/40 border-sidebar-border text-sidebar-foreground" />
        <ScanRunnerDialog />
        <div className="flex items-center justify-between px-1">
          <span className="text-[11px] text-sidebar-muted">Theme</span>
          <ThemeToggle />
        </div>
        <NotificationToggle className="text-sidebar-muted [&_button:not(:disabled)]:hover:bg-sidebar-accent/60" />
        <SignOutButton className="text-sidebar-muted hover:bg-sidebar-accent/60 hover:text-sidebar-foreground" />
      </div>
    </aside>
  );
}
