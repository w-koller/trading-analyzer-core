/**
 * Same-origin reverse proxy: browser -> this handler -> FastAPI on localhost.
 *
 * ## Why this exists
 *
 * The browser used to call the backend directly at its LAN address with a
 * bearer token compiled into the bundle. Published to the internet that fails
 * three ways at once — an RFC1918 address is unroutable from outside, an
 * HTTPS page may not fetch plain HTTP, and the origin was not in CORS — and
 * it leaked the token to anyone who loaded the page. Routing through the
 * Next.js server collapses all four problems: one origin, one TLS cert, no
 * CORS, and no secret in the browser at all.
 *
 * ## What this handler is NOT
 *
 * It is not an authentication boundary. It deliberately does **not** inject
 * `X-API-Key` on the way through: if it did, every request that reached it
 * would be authenticated by that act alone, and the session check in FastAPI
 * would be decorative. It forwards cookies and lets the backend decide.
 *
 * ## Streaming
 *
 * `POST /chat/{code}/stream` is SSE and must arrive token by token. Verified
 * against the installed Next 15.2.8:
 *
 *   - `cache: "no-store"` on the outbound fetch is the load-bearing part.
 *     Next patches global fetch and, whenever a cache key is generated, does
 *     `await res.arrayBuffer()` on the response — which would turn an
 *     hour-long stream into a single buffered reply at the end of it.
 *   - Returning `res.body` unread lets Next pipe it straight to the socket,
 *     flushing per chunk.
 *   - `X-Accel-Buffering: no` is set by the backend and must survive to nginx,
 *     which is why response headers are copied rather than rebuilt.
 *
 * The request body is buffered with `arrayBuffer()` rather than streamed, so
 * `duplex: "half"` is not needed — every request here is small JSON.
 */

// Never prerender or cache: this is a proxy for live, per-user data.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const BACKEND =
  process.env.BACKEND_ORIGIN?.replace(/\/$/, "") || "http://127.0.0.1:8000";

/**
 * Hop-by-hop headers, which describe one TCP connection and must not be
 * relayed onto another (RFC 7230 s6.1).
 *
 * `content-length` and `content-encoding` are the subtle ones on the RESPONSE
 * side: undici transparently decompresses the body but leaves both headers in
 * place, so copying them through hands the browser a gzip-labelled plaintext
 * body of the wrong declared length. That renders as a truncated or corrupt
 * response, intermittently, which is a miserable thing to debug.
 */
const STRIP_REQUEST = new Set(["host", "connection", "content-length", "keep-alive"]);
const STRIP_RESPONSE = new Set([
  "content-encoding",
  "content-length",
  "transfer-encoding",
  "connection",
  "keep-alive",
]);

async function proxy(req: Request): Promise<Response> {
  const src = new URL(req.url);
  // The path is taken from the URL verbatim rather than rebuilt from
  // `params.path`. Next decodes those segments, so re-encoding them would
  // percent-escape characters that must survive: an alert fingerprint is
  // `rule:code:discriminator` and the backend matches it with a `:path`
  // converter, which lib/api.ts's encodePath is careful NOT to escape.
  // Passing the original bytes through cannot get that wrong.
  const suffix = src.pathname.replace(/^\/api/, "");
  const target = `${BACKEND}${suffix}${src.search}`;

  const headers = new Headers();
  req.headers.forEach((value, key) => {
    if (!STRIP_REQUEST.has(key.toLowerCase())) headers.set(key, value);
  });

  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  const body = hasBody ? await req.arrayBuffer() : undefined;

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: req.method,
      headers,
      body,
      redirect: "manual",
      // See the note above — without this the SSE stream is buffered whole.
      cache: "no-store",
    });
  } catch (err) {
    // 502, not 500: the proxy is fine, the thing behind it is not. The
    // dashboard's health banner distinguishes these, so the status matters.
    return Response.json(
      { detail: `Backend unreachable: ${(err as Error).message}` },
      { status: 502 },
    );
  }

  const out = new Headers();
  upstream.headers.forEach((value, key) => {
    if (!STRIP_RESPONSE.has(key.toLowerCase())) out.set(key, value);
  });
  // Nothing proxied here is ever cacheable, and NPM sits in front of this.
  // An upstream cache holding a /positions response would be a real leak.
  out.set("Cache-Control", "no-store, no-transform");

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: out,
  });
}

async function handler(req: Request) {
  return proxy(req);
}

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
export const HEAD = handler;
