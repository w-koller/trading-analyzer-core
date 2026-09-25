"""Checks for the streamed reject-and-correct loop (cloud #53).

Run from backend/:  .venv/bin/python -m tests.test_llm_stream

What this suite pins, in the order it matters:

  1. The stream is held to the SAME rules as the blocking loop: the caller's
     validator decides, a rejection hands the model its own output plus the
     fault in the SAME words `llm_json.generate_validated_json` uses, and
     running out of attempts raises the same error. Checked by running both
     loops over identical fake responses and comparing what each sent.
  2. Nothing unvalidated is ever presented as the answer: the JSON being
     written is reported only as a character count, and `result` arrives
     once, after validation.
  3. The reasoning channel is streamed, whichever way the model sends it —
     Ollama's `delta.reasoning`, or inline <think> tags.
  4. The SSE wrapper gives the GPU slot back on success, on failure and when
     the client leaves — a leaked slot is half the box's capacity (#50).

Offline throughout: fake async clients, no network, no model.
"""

import asyncio
import json

from app.services import llm_json, llm_slots, llm_stream
from tests.harness import check, check_eq, report


class Bad(Exception):
    pass


class Transport(Exception):
    pass


class Exhausted(Exception):
    pass


def validate(raw: str) -> dict:
    obj = json.loads(raw)
    if obj.get("n", 0) < 0:
        raise Bad("n must not be negative")
    return obj


def delta(content=None, reasoning=None):
    d = type("D", (), {"content": content})()
    if reasoning is not None:
        d.reasoning = reasoning
    return type("Chunk", (), {"choices": [type("C", (), {"delta": d})()]})()


class FakeStream:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self.chunks.pop(0)

    async def close(self):
        self.closed = True


class FakeAsyncClient:
    """Each create() returns the next queued list of chunks, recording what it
    was sent. A queued Exception is raised instead."""

    def __init__(self, *attempts):
        self.attempts = list(attempts)
        self.calls = []
        self.streams = []
        outer = self

        class _Completions:
            async def create(self, **kw):
                outer.calls.append({**kw, "messages": [dict(m) for m in kw["messages"]]})
                nxt = outer.attempts.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                s = FakeStream(nxt)
                outer.streams.append(s)
                return s

        self.chat = type("Chat", (), {"completions": _Completions()})()


KW = dict(model="m", system_prompt="sys", user_prompt="usr", validate=validate,
          subject="US.T", label="thing", correction_hint="exactly one key",
          transport_error=Transport, exhausted_error=Exhausted, max_retries=3)


def run(client, **over):
    async def go():
        out = []
        async for ev in llm_stream.stream_validated_json(client, **{**KW, **over}):
            out.append(ev)
        return out
    return asyncio.run(go())


# --- 3. reasoning, both ways it arrives; 2. the answer is never streamed ---
good = FakeAsyncClient([
    delta(reasoning="Let me look at "), delta(reasoning="the numbers."),
    delta(content="<think>inline scratch</think>"),
    delta(content='{"n": 1, "text": "' + "x" * 120 + '"}'),
])
events = run(good)
names = [e[0] for e in events]
reasoning = "".join(e[1] for e in events if e[0] == "reasoning")
check_eq("delta.reasoning and inline <think> both reach the reasoning channel",
         reasoning, "Let me look at the numbers.inline scratch")
check("the JSON being written is reported as a count, never as text",
      any(e[0] == "writing" and isinstance(e[1], dict) and "chars" in e[1] for e in events)
      and not any(e[0] == "writing" and "x" * 10 in str(e[1]) for e in events))
check_eq("the order is attempt … checking, result — and result is last",
         (names[0], names[-2], names[-1]), ("attempt", "checking", "result"))
check_eq("the result is the VALIDATED object", events[-1][1]["n"], 1)
check_eq("exactly one result event", names.count("result"), 1)
check("the model stream is closed afterwards", good.streams[0].closed)
check("temperature and response_format match the blocking loop",
      good.calls[0]["temperature"] == llm_json.TEMPERATURE
      and good.calls[0]["response_format"] == llm_json.RESPONSE_FORMAT
      and good.calls[0]["stream"] is True)


# --- 1. a rejection is corrected in the blocking loop's exact words --------
bad_then_good = FakeAsyncClient([delta(content='{"n": -1}')], [delta(content='{"n": 2}')])
events = run(bad_then_good)
rejected = [e[1] for e in events if e[0] == "rejected"]
check_eq("a rejected attempt is reported, with its reason",
         (len(rejected), rejected[0]["reason"] if rejected else None, rejected[0]["final"] if rejected else None),
         (1, "n must not be negative", False))
check_eq("...then the corrected answer is the result", events[-1], ("result", {"n": 2}))


class SyncFake:
    """The blocking loop's fake: same two responses, records messages."""

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.calls = []
        outer = self

        class _C:
            def create(self, **kw):
                outer.calls.append([dict(m) for m in kw["messages"]])
                text = outer.bodies.pop(0)
                return type("R", (), {"choices": [type("C", (), {
                    "message": type("M", (), {"content": text, "reasoning": None})()})()]})()

        self.chat = type("Chat", (), {"completions": _C()})()


sync = SyncFake('{"n": -1}', '{"n": 2}')
llm_json.generate_validated_json(sync, **KW)
check_eq("the correction turn is byte-identical to the blocking loop's",
         bad_then_good.calls[1]["messages"], sync.calls[1])
check("...and carries the fault and the hint",
      "n must not be negative" in bad_then_good.calls[1]["messages"][-1]["content"]
      and "exactly one key" in bad_then_good.calls[1]["messages"][-1]["content"])


# --- running out of attempts ------------------------------------------------
stubborn = FakeAsyncClient(*([delta(content='{"n": -1}')] for _ in range(3)))
seen: list = []
try:
    async def consume():
        async for ev in llm_stream.stream_validated_json(stubborn, **KW):
            seen.append(ev)
    asyncio.run(consume())
    check("an answer that never validates raises", False)
except Exhausted as exc:
    check("an answer that never validates raises the caller's exhausted error",
          "after 3 attempts" in str(exc), str(exc))
check_eq("...after exactly max_retries calls", len(stubborn.calls), 3)
check("...the last rejection is marked final, and no result was ever sent",
      [e for e in seen if e[0] == "rejected"][-1][1]["final"] is True
      and not any(e[0] == "result" for e in seen))


# --- transport failures -----------------------------------------------------
try:
    run(FakeAsyncClient(RuntimeError("connection refused")))
    check("an unreachable model raises", False)
except Transport as exc:
    check("an unreachable model raises the caller's transport error",
          "connection refused" in str(exc))


# --- 4. the SSE wrapper gives the slot back ---------------------------------
class FakeRequest:
    def __init__(self, leave_after=None):
        self.polls = 0
        self.leave_after = leave_after

    async def is_disconnected(self):
        self.polls += 1
        return self.leave_after is not None and self.polls > self.leave_after


def serve(client, request):
    events = llm_stream.stream_validated_json(client, **KW)
    resp = llm_stream.sse_response(events, request=request, slot_label="test",
                                   meta={"model": "m"}, result_extra={"code": "US.T"},
                                   errors=(Transport, Exhausted))

    async def body():
        return [chunk async for chunk in resp.body_iterator]
    return asyncio.run(body())


frames = serve(FakeAsyncClient([delta(reasoning="hm"), delta(content='{"n": 3}')]),
               FakeRequest())
text = "".join(frames)
check("a served stream opens with meta and closes with done",
      frames[0].startswith("event: meta") and frames[-1].startswith("event: done"))
check("the result frame carries the extra fields and the validated object",
      'event: result\ndata: {"code": "US.T", "n": 3}' in text)
check_eq("the slot is released after a completed stream", llm_slots.stats()["active"], 0)

frames = serve(FakeAsyncClient(*([delta(content='{"n": -1}')] for _ in range(3))),
               FakeRequest())
check("an exhausted stream ends in an error frame, not a crash",
      frames[-1].startswith("event: error") and "after 3 attempts" in frames[-1])
check_eq("...and the slot is released", llm_slots.stats()["active"], 0)

leaving = FakeAsyncClient([delta(reasoning=f"step {i} ") for i in range(20)]
                          + [delta(content='{"n": 1}')])
frames = serve(leaving, FakeRequest(leave_after=2))
check("a client that leaves ends the stream before any result",
      not any(f.startswith("event: result") for f in frames))
check("...the model stream is closed, so Ollama stops generating",
      leaving.streams and leaving.streams[0].closed)
check_eq("...and the slot is released", llm_slots.stats()["active"], 0)

report("llm stream")
