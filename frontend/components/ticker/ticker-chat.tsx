"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, MessageSquare, Send, Square } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  streamTickerChat,
  type ChatEvent,
  type ChatMeta,
  type ChatTurn,
} from "@/lib/chat-stream";
import { cn } from "@/lib/utils";

/**
 * Ask the local model about this ticker.
 *
 * Plain React state rather than TanStack Query: this is a stream, not a
 * cache. There is nothing to revalidate, nothing to share between components,
 * and a query key would imply an answer is reusable when it is a one-off.
 *
 * Nothing is persisted, here or on the server. A refresh clears the
 * transcript, which is the right trade for a panel you consult and move on
 * from — `trading.db` is the corpus future advice is retrieved from, and free
 * prose sitting beside validated theses invites being read as evidence.
 *
 * The advisory-only line under the input is static and always rendered. It is
 * the one piece of framing model output cannot remove, which is why it is not
 * generated and not conditional.
 */

/**
 * Phrased away from action on purpose. These are the highest-traffic path
 * into the model, so the default question should never be one that asks for
 * a recommendation — the guardrail that never has to fire is the best kind.
 */
const PRESETS = [
  "What's the bull case here?",
  "What's the bear case?",
  "What would invalidate this thesis?",
  "Explain the options walls in plain English.",
  "How does the recent news line up with the technicals?",
  "What are the key levels, and why those?",
] as const;

const HELD_PRESET = "How does this thesis relate to my position?";

type Message = {
  role: "user" | "assistant";
  content: string;
  reasoning?: string;
  truncated?: boolean;
};

export function TickerChat({ code, className }: { code: string; className?: string }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [meta, setMeta] = useState<ChatMeta | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showThinking, setShowThinking] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  // Abort on unmount. Without it, navigating away leaves Ollama generating
  // for a page that no longer exists, holding one of only two GPU slots.
  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages, streaming]);

  const ask = useCallback(
    async (question: string) => {
      const q = question.trim();
      if (!q || streaming) return;

      setError(null);
      setInput("");
      // The history sent is the transcript BEFORE this turn, and it carries
      // no `reasoning`: a chain-of-thought is not the answer, and replaying
      // it as one would have the model treat its own scratchpad as fact.
      const history: ChatTurn[] = messages.map((m) => ({
        role: m.role,
        content: m.content,
      }));

      setMessages((prev) => [
        ...prev,
        { role: "user", content: q },
        { role: "assistant", content: "" },
      ]);
      setStreaming(true);

      const controller = new AbortController();
      abortRef.current = controller;

      const patchLast = (fn: (m: Message) => Message) =>
        setMessages((prev) => {
          const next = [...prev];
          next[next.length - 1] = fn(next[next.length - 1]);
          return next;
        });

      try {
        await streamTickerChat(
          code,
          { message: q, history },
          (e: ChatEvent) => {
            switch (e.type) {
              case "meta":
                setMeta(e.data);
                break;
              case "token":
                patchLast((m) => ({ ...m, content: m.content + e.data.text }));
                break;
              case "reasoning":
                patchLast((m) => ({
                  ...m,
                  reasoning: (m.reasoning ?? "") + e.data.text,
                }));
                break;
              case "done":
                // 'length' means the answer was cut off at the token cap. Say
                // so — a truncated answer that looks complete is worse than a
                // short one, because the missing half is invisible.
                patchLast((m) => ({
                  ...m,
                  truncated: e.data.finish_reason === "length",
                }));
                break;
              case "error":
                setError(e.data.detail);
                break;
            }
          },
          controller.signal,
        );
      } catch (err) {
        if (!controller.signal.aborted) {
          setError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        setStreaming(false);
        abortRef.current = null;
        // Drop an assistant turn that never produced text, so a failed or
        // stopped question does not leave an empty bubble that looks like
        // the model answering with silence.
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          return last && last.role === "assistant" && !last.content.trim()
            ? prev.slice(0, -1)
            : prev;
        });
      }
    },
    [code, messages, streaming],
  );

  const presets = meta?.held ? [...PRESETS, HELD_PRESET] : [...PRESETS];

  return (
    <Card className={cn(className)}>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="flex items-center gap-2">
          <MessageSquare className="h-4 w-4 text-primary" />
          Ask about {code}
        </CardTitle>
        {meta && (
          <div className="flex items-center gap-1.5">
            <Badge variant="outline">{meta.model}</Badge>
            {meta.setup_age_hours !== null && (
              <Badge variant={meta.setup_age_hours > 24 ? "delayed" : "outline"}>
                thesis {formatAge(meta.setup_age_hours)}
              </Badge>
            )}
          </div>
        )}
      </CardHeader>

      <CardContent>
        {messages.length === 0 && (
          /* The empty state names its own limits. This is what stops "what is
             it trading at right now" being asked and the answer believed. */
          <p className="mb-3 text-xs leading-relaxed text-muted-foreground">
            Answers come from the stored thesis for {code} — its indicators,
            option walls and recent headlines, plus your position if you hold
            it. Everything was calculated in Python before the model saw it.
            It has no live price and cannot place an order.
          </p>
        )}

        {messages.length > 0 && (
          <div className="mb-3 space-y-3">
            {messages.map((m, i) => (
              <div key={i}>
                {m.role === "user" ? (
                  <p className="rounded-md bg-muted px-3 py-2 text-sm font-medium">
                    {m.content}
                  </p>
                ) : (
                  <div className="px-1">
                    {m.reasoning && (
                      <details
                        open={showThinking}
                        onToggle={(e) => setShowThinking(e.currentTarget.open)}
                        className="mb-1.5"
                      >
                        <summary className="cursor-pointer text-[11px] text-muted-foreground hover:text-foreground">
                          Show the model&rsquo;s thinking
                        </summary>
                        <p className="mt-1 whitespace-pre-wrap border-l-2 border-border pl-2 text-[11px] leading-relaxed text-muted-foreground">
                          {m.reasoning}
                        </p>
                      </details>
                    )}
                    <p className="whitespace-pre-wrap text-sm leading-relaxed">
                      {m.content}
                      {streaming && i === messages.length - 1 && (
                        <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse-soft bg-primary align-middle" />
                      )}
                    </p>
                    {m.truncated && (
                      <p className="mt-1 text-[11px] text-delayed">
                        Cut off at the length limit — ask for the rest, or a
                        narrower question.
                      </p>
                    )}
                  </div>
                )}
              </div>
            ))}
            <div ref={endRef} />
          </div>
        )}

        {streaming && !messages[messages.length - 1]?.content && (
          <p className="mb-3 flex items-center gap-1.5 text-xs text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" />
            Thinking — a cold model can take up to a minute to start.
          </p>
        )}

        {error && (
          <p className="mb-3 rounded-md border border-bear/40 bg-bear-muted px-3 py-2 text-xs text-bear">
            {error}
          </p>
        )}

        <div className="mb-2 flex flex-wrap gap-1.5">
          {presets.map((p) => (
            <button
              key={p}
              type="button"
              disabled={streaming}
              onClick={() => ask(p)}
              className="rounded-full border border-border px-2.5 py-1 text-[11px] text-muted-foreground transition-colors hover:border-primary hover:text-foreground disabled:opacity-50"
            >
              {p}
            </button>
          ))}
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            ask(input);
          }}
          className="flex items-center gap-2"
        >
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={streaming}
            placeholder={`Ask anything about ${code}…`}
            maxLength={2000}
            className="h-9 flex-1 rounded-md border border-input bg-background px-3 text-sm outline-none transition-colors placeholder:text-muted-foreground focus:border-primary disabled:opacity-50"
          />
          {streaming ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => abortRef.current?.abort()}
              title="Stop generating and free the model"
            >
              <Square className="h-3.5 w-3.5" /> Stop
            </Button>
          ) : (
            <Button type="submit" size="sm" disabled={!input.trim()}>
              <Send className="h-3.5 w-3.5" /> Ask
            </Button>
          )}
        </form>

        <p className="mt-2 text-[11px] leading-snug text-muted-foreground">
          Advisory only. This tool has no order path — nothing here is an
          instruction to trade, and the model cannot see a live price.
        </p>
      </CardContent>
    </Card>
  );
}

function formatAge(hours: number): string {
  if (hours < 1) return "just now";
  if (hours < 48) return `${Math.round(hours)}h old`;
  return `${Math.round(hours / 24)}d old`;
}
