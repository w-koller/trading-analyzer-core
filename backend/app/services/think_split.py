"""Incremental <think>…</think> splitting for streamed model output.

Lifted out of `ai_chat.py` when a second streamer needed it (cloud #53): the
"brief me" narratives now stream their reasoning live the way the ticker chat
does, and both must route a model's inline scratchpad the same way. Pure
infrastructure — no knowledge of either caller.
"""

from __future__ import annotations


class ThinkSplitter:
    """Route inline <think>…</think> to the reasoning channel, incrementally.

    `ai_thesis.extract_json` strips these with a regex, which needs the whole
    string — useless on a stream. And this cannot be skipped just because the
    current model does not emit them: the model is runtime-selectable
    (decisions #38) and deepseek-r1 is still the env default. Ollama normally
    surfaces deepseek's chain-of-thought on `delta.reasoning` and keeps
    `content` clean, but that is a property of the server's template for one
    model, not a guarantee — a model that inlines the tags would otherwise
    have its scratchpad rendered as the answer.

    Buffers on a partial tag rather than guessing: '<' may be the start of
    '<think>' or just a less-than sign, and it is only knowable once more
    characters arrive.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self._buf = ""
        self._thinking = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        """Returns [(channel, text)] where channel is 'token' or 'reasoning'."""
        self._buf += chunk
        out: list[tuple[str, str]] = []

        while self._buf:
            tag = self.CLOSE if self._thinking else self.OPEN
            idx = self._buf.find(tag)
            if idx >= 0:
                if idx:
                    out.append(("reasoning" if self._thinking else "token",
                                self._buf[:idx]))
                self._buf = self._buf[idx + len(tag):]
                self._thinking = not self._thinking
                continue

            # No complete tag. Emit everything that cannot be the start of one.
            keep = 0
            for n in range(min(len(tag) - 1, len(self._buf)), 0, -1):
                if self._buf.endswith(tag[:n]):
                    keep = n
                    break
            if keep < len(self._buf):
                out.append(("reasoning" if self._thinking else "token",
                            self._buf[:len(self._buf) - keep]))
            self._buf = self._buf[len(self._buf) - keep:]
            break

        return [(c, t) for c, t in out if t]

    def flush(self) -> list[tuple[str, str]]:
        """Whatever is left when the stream ends — a truncated tag is text."""
        if not self._buf:
            return []
        rest, self._buf = self._buf, ""
        return [("reasoning" if self._thinking else "token", rest)]
