"use client";

import Link from "next/link";
import { SourceIcon } from "@/components/news/source-icon";
import { Badge } from "@/components/ui/badge";
import { bareTicker, timeAgoLong } from "@/lib/format";
import type { NewsArticle } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * One headline, in the shape the reference screenshots use:
 * icon · "6 hours ago · Source" · the headline itself as the link.
 *
 * `rel="noopener noreferrer"` is not boilerplate here — every one of these
 * points at a third-party outlet.
 */
export function NewsItem({ article, className }: { article: NewsArticle; className?: string }) {
  const when = article.published_estimated
    ? "just seen"
    : timeAgoLong(article.published_at);

  return (
    <article className={cn("group py-2.5", className)}>
      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <SourceIcon icon={article.icon} category={article.category} />
        <span
          title={
            article.published_estimated
              ? "This feed publishes no usable date; showing when we first saw it."
              : article.published_at
          }
        >
          {when}
        </span>
        <span aria-hidden>·</span>
        <span className="truncate">{article.source_label}</span>
        {article.also_in.length > 0 && (
          <span className="truncate opacity-70" title={article.also_in.join(", ")}>
            +{article.also_in.length}
          </span>
        )}
      </div>

      {article.url ? (
        <a
          href={article.url}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-1 block text-sm font-medium leading-snug transition-colors group-hover:text-primary"
        >
          {article.title}
        </a>
      ) : (
        <p className="mt-1 text-sm font-medium leading-snug">{article.title}</p>
      )}

      {article.codes.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {article.codes.map((c) => (
            <Link key={c.code} href={`/ticker/${encodeURIComponent(c.code)}`}>
              <Badge
                variant="outline"
                className="hover:border-primary hover:text-primary"
                title={
                  c.match_basis === "feed_query"
                    ? "From this ticker's own feed"
                    : `Matched on the company name (${c.name ?? c.code})`
                }
              >
                {bareTicker(c.code)}
              </Badge>
            </Link>
          ))}
        </div>
      )}
    </article>
  );
}
