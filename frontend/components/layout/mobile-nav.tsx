"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity } from "lucide-react";
import { NAV_ITEMS } from "@/components/layout/nav-items";
import { ThemeToggle } from "@/components/theme/theme-toggle";
import { SignOutButton } from "@/components/settings/sign-out-button";
import { ScanRunnerDialog } from "@/components/scan/scan-runner-dialog";
import { cn } from "@/lib/utils";

/**
 * Phone navigation: a compact top bar plus a fixed bottom tab bar.
 *
 * A bottom bar rather than a hamburger because these six destinations are
 * the whole app — hiding them behind a menu would add a tap to every
 * navigation for no gain. NOTE: the column count below is hardcoded and must
 * be changed whenever NAV_ITEMS grows; it is `grid-cols-6` now.
 *
 * Six cells is ~53px each at 320px, which is why the label dropped to 9px
 * and each one truncates inside its own cell. Without the truncate a long
 * label widens its column and the icons stop lining up.
 */
export function MobileNav() {
  const pathname = usePathname();

  return (
    <>
      <header className="sticky top-0 z-30 flex items-center justify-between border-b bg-background/95 px-4 py-3 backdrop-blur md:hidden">
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-md bg-primary text-primary-foreground">
            <Activity className="h-3.5 w-3.5" />
          </div>
          <span className="text-sm font-semibold tracking-tight">Trading Analyzer</span>
        </div>
        <div className="flex items-center gap-2">
          <ScanRunnerDialog compact />
          <ThemeToggle />
          {/* Sign-out lives up here rather than in the tab bar below: that
              grid is grid-cols-6 and hardcoded, and a seventh cell at 320px
              would leave ~45px each and make every label unreadable. */}
          <SignOutButton className="w-auto text-muted-foreground hover:text-foreground" />
        </div>
      </header>

      <nav className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-6 border-t bg-background/95 backdrop-blur md:hidden">
        {NAV_ITEMS.map((item) => {
          const active = pathname === item.href;
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex flex-col items-center gap-1 px-0.5 py-2.5 text-[9px] font-medium leading-none transition-colors",
                active ? "text-primary" : "text-muted-foreground",
              )}
            >
              <Icon className="h-4 w-4 shrink-0" />
              <span className="w-full truncate text-center">{item.label}</span>
            </Link>
          );
        })}
      </nav>
    </>
  );
}
