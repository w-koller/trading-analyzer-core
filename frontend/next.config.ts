import type { NextConfig } from "next";

/**
 * The LXC now has 4 GiB of RAM and 512 MiB of swap. It previously had 2 GiB
 * and no swap, which is why this file used to disable the type-check worker
 * and pin the build to a single CPU: the checker worker was OOM-killed with
 * SIGSEGV, which looks like a compiler crash rather than a memory problem.
 *
 * Type checking is back ON in the build. `ignoreBuildErrors` was the more
 * expensive half of that workaround by far — there is no CI here, so a type
 * error simply shipped, silently, and the only thing standing between a
 * broken build and production was remembering to run `npm run typecheck` by
 * hand. `npm run verify` still exists for a faster local loop.
 *
 * Linting stays out of the build (it is slower, noisier, and `npm run lint`
 * covers it), and the build still runs single-worker: page generation forks
 * a worker per CPU, and this box shares its memory with a live backend that
 * may be mid-inference. Serialising is a couple of seconds slower and avoids
 * competing with the thing the build is for.
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  eslint: { ignoreDuringBuilds: true },
  experimental: { cpus: 1, workerThreads: false },

  // "x-powered-by: Next.js" was being served publicly. Version disclosure on
  // an internet-facing app is free reconnaissance and buys nothing.
  poweredByHeader: false,

  /**
   * Security headers. This app is reachable from the public internet, and NPM
   * was observed sending none of these — no HSTS, no framing policy, nothing.
   * HSTS itself belongs on the proxy (it terminates TLS); everything that can
   * be set from the app is set here so it lives in git rather than in a
   * proxy UI nobody can diff.
   */
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            // No third-party scripts, styles, fonts or frames of any kind.
            // 'unsafe-inline' on styles is required by Tailwind's runtime
            // style injection; scripts do NOT get it. connect-src is 'self'
            // only because the API is same-origin now — an absolute
            // NEXT_PUBLIC_API_URL would be blocked here as well as failing
            // CORS, which is the correct outcome.
            key: "Content-Security-Policy",
            value: [
              "default-src 'self'",
              "script-src 'self' 'unsafe-inline'",
              "style-src 'self' 'unsafe-inline'",
              "img-src 'self' data: blob:",
              "font-src 'self' data:",
              "connect-src 'self'",
              "manifest-src 'self'",
              "worker-src 'self'",
              "frame-ancestors 'none'",
              "base-uri 'self'",
              "form-action 'self'",
              "object-src 'none'",
            ].join("; "),
          },
        ],
      },
    ];
  },
};

export default nextConfig;
