import { AppShell } from "@/components/layout/app-shell";

/**
 * Layout for every signed-in page. The (app) group does not appear in URLs,
 * so "/", "/news", "/ticker/US.PLTR" and the rest are unchanged.
 *
 * force-dynamic because these pages sit behind a session. Next was serving
 * the root from its full route cache (`x-nextjs-cache: HIT`,
 * `s-maxage=31536000`), which is harmless for a shell whose data is fetched
 * client-side but is not a property to leave to chance once there is a
 * signed-in state to get frozen into it.
 */
export const dynamic = "force-dynamic";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
