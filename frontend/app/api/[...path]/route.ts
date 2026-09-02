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
 *   - The outbound call is undici's own `fetch`, NOT the global one, so Next's
 *     patched fetch is out of the path entirely. That patch does
 *     `await res.arrayBuffer()` whenever it generates a cache key, which would
 *     turn an hour-long stream into one buffered reply at the end of it; the
 *     `cache: "no-store"` that used to hold that off is no longer needed,
 *     because the thing it was defending against never sees this request.
 *   - Returning `res.body` unread lets Next pipe it straight to the socket,
 *     flushing per chunk.
 *   - `X-Accel-Buffering: no` is set by the backend and must survive to nginx,
 *     which is why response headers are copied rather than rebuilt.
 *
 * The request body is buffered with `arrayBuffer()` rather than streamed, so
 * `duplex: "half"` is not needed — every request here is small JSON.
 */
import { Agent, fetch as undiciFetch } from "undici";

// Never prerender or cache: this is a proxy for live, per-user data.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const BACKEND =
  process.env.BACKEND_ORIGIN?.replace(/\/$/, "") || "http://127.0.0.1:8000";

/**
 * ## Why this proxy has no 300-second ceiling
 *
 * `POST /scan/run` blocks for the whole scan — 60-120s per ticker, so over an
 * hour for a full watchlist (decisions #22), which is why `lib/api.ts` passes
 * `null` for the browser-side timeout. That only removes the ceiling on the
 * browser -> Next leg. Node's fetch is undici, whose DEFAULT `headersTimeout`
 * is 300s, so the Next -> backend leg had one of its own: the proxy gave up
 * at five minutes and answered 502 while the backend carried on and finished
 * the work. It went unnoticed because the scan dialog infers progress from
 * `trade_setups` rows carrying the running `scanner_run_id` rather than from
 * this response, so the scan looks fine while its own request has already
 * failed.
 *
 * Both timeouts are disabled rather than raised. Picking a number means
 * guessing how slow the slowest cold model load can be, and guessing low
 * reintroduces exactly this on a worse day. Nothing is lost by removing the
 * ceiling here: the backend has its own timeouts, `lib/api.ts` sets the
 * per-call ones the UI wants, and the client can abort.
 *
 * ## Why `undici.fetch` and not `fetch(..., { dispatcher })`
 *
 * The cloud portal's copy of this proxy passes the dispatcher to the GLOBAL
 * fetch. **That form is inert on this box**, measured on Node 18.20.4 against
 * a server that holds its headers for 6s: with `headersTimeout: 2000`, global
 * fetch returned 200 at 6.0s (the option was ignored) while undici's own
 * fetch threw `UND_ERR_HEADERS_TIMEOUT` at 2.5s. `setGlobalDispatcher` is no
 * help either — it sets the npm package's global agent, and Node's built-in
 * fetch has its own internal copy of undici that never consults it. Node 18
 * simply does not read `dispatcher` off a RequestInit. So the dispatcher has
 * to be handed to the fetch that belongs to the same undici as the Agent,
 * which means importing both.
 */
const dispatcher = new Agent({ headersTimeout: 0, bodyTimeout: 0 });

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
    // undici's Response is structurally the same object the Web API defines
    // and its `body` is a global ReadableStream, which is what `new Response`
    // below wants; the two type declarations are just not related by
    // inheritance.
    upstream = (await undiciFetch(target, {
      method: req.method,
      headers,
      body,
      redirect: "manual",
      dispatcher,
    })) as unknown as Response;
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
