"use client";

import * as React from "react";

/**
 * Matches a CSS media query, SSR-safe.
 *
 * Starts false on the server and syncs on mount, so a `(pointer: coarse)`
 * check never causes a hydration mismatch.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = React.useState(false);

  React.useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}

/** True on touch-first devices, where there is no hover to reveal a tooltip. */
export function useIsTouch(): boolean {
  return useMediaQuery("(pointer: coarse)");
}
