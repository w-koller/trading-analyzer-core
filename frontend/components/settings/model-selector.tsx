"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, RotateCcw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { api, type ModelSettings } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Which model writes the theses.
 *
 * A native <select> rather than a styled primitive: nothing else in this
 * codebase uses Radix Select, and a picker over a handful of strings does not
 * justify introducing one.
 *
 * The list comes from Ollama itself, so it can only ever offer models that
 * are actually pulled — the failure this avoids is a name that looks right,
 * is accepted, and then kills every thesis 90 seconds at a time inside the
 * next scan.
 *
 * Every colour here is `sidebar-*`, never the page-level tokens. This lives on
 * the sidebar, which is dark purple in BOTH themes — `bg-background` is white
 * in light mode and `text-muted-foreground` is a mid grey meant for a white
 * card, so the page tokens produce an invisible control on exactly one theme.
 */
export function ModelSelector({ className }: { className?: string }) {
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery<ModelSettings>({
    queryKey: ["model-settings"],
    queryFn: () => api.modelSettings(),
    refetchInterval: 60_000,
  });

  const change = useMutation({
    mutationFn: (model: string) => api.setModel(model),
    onSuccess: (next) => {
      queryClient.setQueryData(["model-settings"], next);
      // /health reports the active model too.
      queryClient.invalidateQueries({ queryKey: ["health"] });
    },
  });

  const reset = useMutation({
    mutationFn: () => api.resetModel(),
    onSuccess: (next) => {
      queryClient.setQueryData(["model-settings"], next);
      queryClient.invalidateQueries({ queryKey: ["health"] });
    },
  });

  if (isLoading || !data) return null;

  const busy = change.isPending || reset.isPending;
  const err = change.error ?? reset.error;

  return (
    <div className={cn("rounded-md border bg-muted/30 p-2.5", className)}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-medium text-sidebar-muted">Thesis model</span>
        <div className="flex items-center gap-1">
          <Badge
            variant={data.source === "persisted" ? "held" : "outline"}
            className={
              data.source === "persisted"
                ? undefined
                : "border-sidebar-input-border text-sidebar-muted"
            }
          >
            {data.source === "persisted" ? "custom" : "default"}
          </Badge>
          {data.source === "persisted" && (
            <button
              type="button"
              onClick={() => reset.mutate()}
              disabled={busy}
              title={`Reset to the configured default (${data.env_default})`}
              className="rounded p-1 text-sidebar-muted transition-colors hover:text-sidebar-foreground disabled:opacity-50"
            >
              <RotateCcw className="h-3 w-3" />
            </button>
          )}
        </div>
      </div>

      {data.reachable ? (
        <select
          value={data.active}
          disabled={busy}
          onChange={(e) => change.mutate(e.target.value)}
          className="mt-1.5 w-full rounded-md border border-sidebar-input-border bg-sidebar-input px-2 py-1 text-xs text-sidebar-foreground outline-none transition-colors focus:border-primary disabled:opacity-50"
        >
          {/* The active model may not be in the list if it was pulled away
              underneath us — show it anyway rather than silently selecting
              something the user never chose. */}
          {/* The popup is drawn by the browser, and Firefox on Linux paints it
              with the select's own colours rather than the OS palette — so the
              options need an explicit pair of their own, or the list renders
              light-on-light against the sidebar-tinted trigger. */}
          {!data.available.includes(data.active) && (
            <option className="bg-background text-foreground" value={data.active}>
              {data.active} (not currently served)
            </option>
          )}
          {data.available.map((m) => (
            <option className="bg-background text-foreground" key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      ) : (
        <p className="mt-1.5 text-[11px] text-sidebar-warning">
          Model host unreachable — cannot list or change models.
        </p>
      )}

      {busy && (
        <p className="mt-1.5 flex items-center gap-1 text-[11px] text-sidebar-muted">
          <Loader2 className="h-3 w-3 animate-spin" /> Applying…
        </p>
      )}

      {data.warning && !busy && (
        <p className="mt-1.5 text-[11px] text-sidebar-warning">{data.warning}</p>
      )}

      {err && (
        <p className="mt-1.5 text-[11px] text-sidebar-danger">
          {err instanceof Error ? err.message : String(err)}
        </p>
      )}

      {!busy && !data.warning && (
        <p className="mt-1.5 text-[11px] leading-snug text-sidebar-muted">
          Applies from the next thesis, never mid-scan. A reasoning model
          spends most of its 90-120s per ticker thinking, and this task is
          mostly &ldquo;read these numbers, return strict JSON&rdquo; — but a
          faster model is only better if it still passes the validator. Change
          it attended and watch the first scan.
        </p>
      )}
    </div>
  );
}
