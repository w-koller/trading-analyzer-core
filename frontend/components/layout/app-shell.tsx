import * as React from "react";
import { Sidebar } from "@/components/layout/sidebar";
import { MobileNav } from "@/components/layout/mobile-nav";
import { HealthBanner } from "@/components/health-banner";

/**
 * The persistent frame. Sidebar on desktop, top+bottom bars on mobile.
 * `pb-20` on the main region keeps content clear of the fixed mobile tab bar.
 *
 * The health banner lives here, not on the dashboard: an outage has to be
 * visible from whichever page the user happens to be on, or they sit reading
 * numbers that quietly stopped updating.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen">
      <Sidebar />
      <MobileNav />
      <main className="pb-20 md:pb-0 md:pl-56">
        <div className="mx-auto w-full max-w-[1600px] px-4 py-4 sm:px-6 sm:py-6">
          <HealthBanner />
          {children}
        </div>
      </main>
    </div>
  );
}
