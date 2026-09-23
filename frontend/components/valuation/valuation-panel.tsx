"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { PillGroup } from "@/components/ui/pill-group";
import { api, type Mover } from "@/lib/api";
import { DCFForm } from "./dcf-form";
import { GGMForm } from "./ggm-form";
import { NAVForm } from "./nav-form";

type ModelKind = "dcf" | "ggm" | "nav";

const MODEL_OPTIONS: { value: ModelKind; label: string }[] = [
  { value: "dcf", label: "DCF" },
  { value: "ggm", label: "Dividend Discount" },
  { value: "nav", label: "NAV" },
];

/**
 * The valuation tab's root. `?model=` is its own URL param, independent of
 * the ticker page's own `?view=` — both must survive a switch of the other,
 * same reasoning and same read/write shape as `setups/page.tsx`'s `?view=`
 * beside `?market=`.
 *
 * Fundamentals are fetched once here and handed to whichever model form is
 * active, so switching tabs never re-fetches.
 */
export function ValuationPanel({ code, quote }: { code: string; quote?: Mover }) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const modelParam = params.get("model");
  const model: ModelKind = modelParam === "ggm" || modelParam === "nav" ? modelParam : "dcf";

  const selectModel = (next: ModelKind) => {
    const q = new URLSearchParams(params.toString());
    if (next === "dcf") q.delete("model");
    else q.set("model", next);
    const query = q.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  };

  const fundamentals = useQuery({
    queryKey: ["fundamentals", code],
    queryFn: () => api.fundamentals(code),
  });

  const marketPrice = quote?.last_price ?? 0;

  return (
    <div className="space-y-4">
      <PillGroup
        options={MODEL_OPTIONS}
        value={model}
        onChange={selectModel}
        ariaLabel="Valuation model"
      />
      {model === "dcf" && (
        <DCFForm code={code} marketPrice={marketPrice} fundamentals={fundamentals.data} />
      )}
      {model === "ggm" && (
        <GGMForm code={code} marketPrice={marketPrice} fundamentals={fundamentals.data} />
      )}
      {model === "nav" && (
        <NAVForm code={code} marketPrice={marketPrice} fundamentals={fundamentals.data} />
      )}
    </div>
  );
}
