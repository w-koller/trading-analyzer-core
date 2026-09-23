"use client";

import * as React from "react";
import { useMutation } from "@tanstack/react-query";
import { Loader2, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  api,
  type ValuationComputed,
  type ValuationInterpretRequest,
  type ValuationModelKind,
} from "@/lib/api";

function buildRequest(
  modelKind: ValuationModelKind,
  inputs: Record<string, number>,
  computed: ValuationComputed,
): ValuationInterpretRequest {
  switch (modelKind) {
    case "dcf":
      return { model_kind: "dcf", dcf_inputs: inputs, computed };
    case "ggm":
      return { model_kind: "ggm", ggm_inputs: inputs, computed };
    case "nav":
      return { model_kind: "nav", nav_inputs: inputs, computed };
  }
}

/**
 * The "brief me" button. Same `useMutation` + `Loader2` shape the ticker
 * page's Scan button already uses — no client timeout, a local generation
 * legitimately runs 30-120s.
 *
 * The explanation is cleared the moment `inputs`/`computed` change after it
 * was generated: nothing here caches a result against inputs it no longer
 * describes, since the whole point of a slider is that the last explanation
 * may no longer match the current numbers.
 */
export function InterpretPanel({
  code,
  modelKind,
  inputs,
  computed,
  disabled,
}: {
  code: string;
  modelKind: ValuationModelKind;
  inputs: Record<string, number>;
  computed: ValuationComputed | null;
  disabled?: boolean;
}) {
  const interpret = useMutation({
    mutationFn: () => {
      if (!computed) throw new Error("Nothing computed yet.");
      return api.interpretValuation(code, buildRequest(modelKind, inputs, computed));
    },
  });

  const key = `${JSON.stringify(inputs)}|${computed ? JSON.stringify(computed) : ""}`;
  const lastKeyRef = React.useRef<string | null>(null);
  React.useEffect(() => {
    if (lastKeyRef.current !== null && lastKeyRef.current !== key) {
      interpret.reset();
    }
    lastKeyRef.current = key;
    // interpret is a fresh mutation object each render; only `key` should
    // drive this effect, or it would fire (and reset) on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return (
    <div className="space-y-2">
      <Button
        size="sm"
        variant="outline"
        onClick={() => interpret.mutate()}
        disabled={disabled || !computed || interpret.isPending}
      >
        {interpret.isPending ? (
          <>
            <Loader2 className="h-4 w-4 animate-spin" /> Interpreting…
          </>
        ) : (
          <>
            <Sparkles className="h-4 w-4" /> Interpret this valuation
          </>
        )}
      </Button>

      {interpret.isPending && (
        <p className="text-xs text-muted-foreground">
          Asking the local model to explain this result — 30-120 seconds.
        </p>
      )}

      {interpret.isError && (
        <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {interpret.error instanceof Error
            ? interpret.error.message
            : "Could not reach the model."}{" "}
          <button type="button" className="underline" onClick={() => interpret.mutate()}>
            Try again
          </button>
        </p>
      )}

      {interpret.data && (
        <div className="space-y-2 rounded-md border bg-card p-3 text-xs leading-relaxed">
          <p>{interpret.data.summary}</p>
          <p className="text-muted-foreground">{interpret.data.sensitivity_note}</p>
        </div>
      )}
    </div>
  );
}
