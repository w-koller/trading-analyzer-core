"""The shared scaffolding every test script in this directory used to re-declare.

There is no pytest here, deliberately: each test file is a standalone script,
run as `.venv/bin/python -m tests.test_x`, that prints one line per check and
exits non-zero if any of them failed. That shape is worth keeping — it needs no
dependency, it reads top-to-bottom as prose, and a failure says exactly which
assertion broke without a traceback to decode.

What was not worth keeping is that 22 of those files each carried their own
copy of the `failures` list, the `check()` function and the exit epilogue.
Three genuinely different comparison styles had grown up across them, so this
module provides three functions rather than flattening them into one:

    check        a boolean condition, with an optional detail string
    check_eq     exact equality, reporting got vs want
    check_close  float comparison with an explicit tolerance

`failures` is module state, shared by every function here, and `report()` is
what reads it. That means a test file imports these four names and declares no
scaffolding of its own.

Inputs and outputs are per-function below. Every `check*` returns the boolean
it decided, so a caller can branch on a check without re-evaluating it, and
`report()` returns a process exit code (0 or 1) as well as — by default —
raising SystemExit with it.
"""

from __future__ import annotations

import math
from typing import Any

# Every check appends here; report() is the only reader. Module-level rather
# than passed around, because a test script is a single linear program and
# threading an accumulator through 200 top-level calls would be worse than the
# duplication this replaces.
failures: list[str] = []


def check(label: str, cond: Any, detail: str = "") -> bool:
    """Assert a condition. Prints PASS/FAIL, records the label if it failed.

    Args:
        label:  what is being asserted, in prose. This is what a failure
                report names, so it should read on its own.
        cond:   the condition. Truthiness is used, not `is True`, because
                callers legitimately pass `x in y`, a count, or an object.
        detail: optional context appended to the line — a measured value, a
                reason. Shown on both PASS and FAIL, since knowing the number
                a passing check actually saw is often the point.

    Returns:
        The boolean the check decided.
    """
    ok = bool(cond)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)
    return ok


def check_eq(label: str, got: Any, want: Any, *, quiet: bool = False) -> bool:
    """Assert `got == want`, reporting both when they differ.

    Args:
        label: what is being asserted.
        got:   the value produced.
        want:  the value expected.
        quiet: when True, print nothing at all and fold the got/want detail
               into the recorded failure instead. Used by the market_hours
               suite, whose 30 checks are a dense table where per-check PASS
               lines would bury the result. The detail is preserved either
               way — it goes to stdout when loud, into `failures` when quiet.

    Returns:
        The boolean the check decided.
    """
    ok = got == want
    if quiet:
        if not ok:
            failures.append(f"{label}: got {got!r}, want {want!r}")
        return ok
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + ("" if ok else f" — got {got!r}, want {want!r}"))
    if not ok:
        failures.append(label)
    return ok


def check_close(label: str, got: Any, want: Any, *,
                rel_tol: float = 1e-6, abs_tol: float = 1e-6) -> bool:
    """Assert `got ≈ want` for floats, falling back to equality otherwise.

    The fallback is what lets a suite use one function throughout: a check
    whose expected value is a bool, a string or a list still works, and only
    genuine float comparisons go through `math.isclose`.

    Args:
        label:   what is being asserted.
        got:     the value produced. A `None` here never compares close — it
                 goes to the equality path, so `want=None` still works and a
                 missing value is never silently tolerated as "near enough".
        want:    the value expected.
        rel_tol: relative tolerance passed to `math.isclose`.
        abs_tol: absolute tolerance passed to `math.isclose`. Kept separate
                 from rel_tol because the two suites using this pass different
                 pairs — indicators wants both at 1e-6, similarity wants a far
                 tighter 1e-9 absolute against the same 1e-6 relative.

    Returns:
        The boolean the check decided.
    """
    if isinstance(want, float) and isinstance(got, (int, float)) and not isinstance(got, bool):
        ok = math.isclose(got, want, rel_tol=rel_tol, abs_tol=abs_tol)
    else:
        ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + ("" if ok else f" — got {got!r}, want {want!r}"))
    if not ok:
        failures.append(label)
    return ok


def report(name: str, *, summary: str | None = None,
           raise_on_failure: bool = True) -> int:
    """Print the run's verdict and produce its exit code.

    Args:
        name:    the suite's name, used in both the failure header and the
                 default success line.
        summary: overrides the success line entirely, for suites that say
                 something more specific than "all checks passed" — e.g.
                 similarity reports how many features it covered.
        raise_on_failure: when True (the default) a failure raises
                 SystemExit(1), which is what a top-level script wants. The
                 live trade-gateway suite runs inside a `main()` and needs the
                 code returned instead, so it passes False.

    Returns:
        0 when every check passed, 1 otherwise.
    """
    if failures:
        print(f"\n{name}: {len(failures)} check(s) FAILED")
        for f in failures:
            print(f"  - {f}")
        if raise_on_failure:
            raise SystemExit(1)
        return 1
    print(f"\n{summary or f'{name}: all checks passed'}")
    return 0


def reset() -> None:
    """Clear recorded failures. Only needed if a suite reports more than once."""
    failures.clear()
