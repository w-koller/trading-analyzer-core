"use client";

/**
 * The "Load more" control shared by the paginated lists.
 *
 * Both callers page by raising a `limit` and refetching, so the button knows
 * nothing about pagination itself — it renders only when the page came back
 * full, which is the signal that there may be more behind it. A short page
 * means the end of the data, and a button offering more that returns nothing
 * is worse than no button.
 */
export function LoadMoreButton({
  loaded,
  limit,
  onLoadMore,
  label = "Load more",
}: {
  /** How many rows the current page actually returned. */
  loaded: number;
  /** The limit that page was requested with. */
  limit: number;
  /** Raise the limit. The caller owns the step size — a news page and a
   *  thesis grid fit different numbers of rows on a screen. */
  onLoadMore: () => void;
  label?: string;
}) {
  if (loaded < limit) return null;
  return (
    <button
      onClick={onLoadMore}
      className="mx-auto mt-4 block rounded-md border px-4 py-2 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
    >
      {label}
    </button>
  );
}
