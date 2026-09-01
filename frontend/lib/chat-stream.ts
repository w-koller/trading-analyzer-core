/**
 * Reads the ticker-chat SSE stream.
 *
 * Deliberately not built on `EventSource`, which fails here on two
 * independent counts: it cannot POST, and it cannot set headers — and
 * `X-API-Key` is required on every guarded route (decisions #34). `fetch`
 * plus a ReadableStream is the only option, so the SSE framing is parsed by
 * hand. That is ~40 lines and consistent with this repo hand-rolling its
 * indicators, dedup and sentence counting rather than taking a dependency.
 *
 * It also cannot go through `api.ts`'s `request()`, which calls `res.json()`
 * and therefore waits for the whole body — the exact behaviour streaming
 * exists to avoid.
 *
 * The one rule that must not be got wrong: the decode buffer is carried
 * ACROSS reads and split on a blank line, never per-chunk on "\n". A network
 * chunk boundary lands mid-frame routinely, and splitting per read corrupts
 * silently — you get half a JSON payload, a parse error swallowed by a
 * try/catch, and a word missing from the middle of a sentence.
 */

import { API_URL, CREDENTIALS } from "@/lib/api";

export type ChatTurn = { role: "user" | "assistant"; content: string };

export type ChatMeta = {
  model: string;
  setup_id: number;
  setup_age_hours: number | null;
  is_delayed_data: boolean;
  data_as_of: string;
  held: boolean;
  news_items: number;
  has_walls: boolean;
};

export type ChatEvent =
  | { type: "meta"; data: ChatMeta }
  | { type: "token"; data: { text: string } }
  | { type: "reasoning"; data: { text: string } }
  | { type: "done"; data: { finish_reason: string; elapsed_seconds: number } }
  | { type: "error"; data: { detail: string; retryable?: boolean } };

/** No event at all for this long means something is wrong; the server
 *  heartbeats every 15s, so 60 is four missed beats rather than a guess. */
const IDLE_TIMEOUT_MS = 60_000;

export class ChatStreamError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = "ChatStreamError";
  }
}

export async function streamTickerChat(
  code: string,
  body: { message: string; history: ChatTurn[] },
  onEvent: (e: ChatEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch(
    `${API_URL}/chat/${encodeURIComponent(code)}/stream`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // The session cookie authenticates this stream too. It is not sent
      // unless the request is same-origin, which it is because API_URL is
      // relative — see the note on API_URL in lib/api.ts.
      credentials: CREDENTIALS,
      body: JSON.stringify(body),
      cache: "no-store",
      // No AbortSignal.timeout here. A blanket fetch timeout would kill the
      // stream mid-answer — the same trap that would break the scan button.
      // Liveness is the idle timer below instead.
      signal,
    },
  );

  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j?.detail) detail = String(j.detail);
    } catch {
      /* a non-JSON error body is still an error; keep the status text */
    }
    throw new ChatStreamError(detail, res.status);
  }
  if (!res.body) throw new ChatStreamError("The response carried no body.");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let idle: ReturnType<typeof setTimeout> | undefined;

  const armIdle = (abort: () => void) => {
    if (idle) clearTimeout(idle);
    idle = setTimeout(abort, IDLE_TIMEOUT_MS);
  };

  const controller = new AbortController();
  const bail = () => controller.abort();
  armIdle(bail);
  signal.addEventListener("abort", bail);

  try {
    for (;;) {
      if (controller.signal.aborted && !signal.aborted) {
        throw new ChatStreamError(
          "The model stopped responding — no data for 60 seconds.",
        );
      }
      const { done, value } = await reader.read();
      if (done) break;

      armIdle(bail);
      buffer += decoder.decode(value, { stream: true });

      // Frames are separated by a blank line. Anything after the last one is
      // an incomplete frame and stays in the buffer for the next read.
      let split: number;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        const parsed = parseFrame(frame);
        if (parsed) onEvent(parsed);
      }
    }
  } finally {
    if (idle) clearTimeout(idle);
    signal.removeEventListener("abort", bail);
    // Releasing the lock lets the body be cancelled, which closes the
    // connection — which is what tells the backend to stop generating.
    reader.cancel().catch(() => {});
  }
}

function parseFrame(frame: string): ChatEvent | null {
  let event = "";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    // ": ping" and any other comment. Every SSE parser ignores these; they
    // exist to prove the connection is alive while the model loads.
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!event || dataLines.length === 0) return null;
  try {
    const data = JSON.parse(dataLines.join("\n"));
    return { type: event, data } as ChatEvent;
  } catch {
    // A malformed frame is dropped rather than thrown: one bad frame should
    // not lose an answer that is otherwise arriving fine.
    return null;
  }
}
