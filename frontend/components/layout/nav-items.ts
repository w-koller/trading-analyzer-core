import {
  BarChart3,
  CalendarDays,
  LayoutDashboard,
  ListChecks,
  Newspaper,
  Wallet,
} from "lucide-react";

/**
 * The bottom mobile bar hardcodes its column count to match this list —
 * see `mobile-nav.tsx`. Adding an item here without changing it there
 * silently wraps the bar onto two rows.
 */
export const NAV_ITEMS = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/news", label: "News", icon: Newspaper },
  { href: "/watchlist", label: "Watchlist", icon: ListChecks },
  { href: "/earnings", label: "Earnings", icon: CalendarDays },
  { href: "/setups", label: "Theses", icon: BarChart3 },
  { href: "/positions", label: "Holdings", icon: Wallet },
] as const;
