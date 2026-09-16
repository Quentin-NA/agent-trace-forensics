#!/usr/bin/env python3
"""
Forensic inspector for GenAI traces.

This is not an observability tool. It does not tell you whether your agent
works well. It answers a single question: with these traces, what can I prove?

Installs as an OTel SpanProcessor, so it slots into any existing pipeline
without touching the instrumented code.

This is the skeleton of a forensic-readiness audit.
"""

import datetime
import json
from collections import defaultdict
from opentelemetry.sdk.trace import SpanProcessor


# Attributes required for an action to be defensible as evidence.
# Each entry: (attribute, why it matters when absent)
FORENSIC_REQUIREMENTS = {
    "action": {
        "gen_ai.tool.name": "we do not know which tool was called",
        "gen_ai.tool.call.id": "cannot link the call back to the model decision",
        "gen_ai.tool.call.arguments": "we know an action happened, not which one "
                                      "(amount, recipient, target)",
        "gen_ai.tool.call.result": "we do not know whether the action succeeded, "
                                   "nor what it returned",
    },
    "actor": {
        "gen_ai.agent.id": "no stable agent identity",
        "gen_ai.agent.name": "no agent name",
        "gen_ai.agent.version": "no way to know which version acted",
    },
    "context": {
        "gen_ai.conversation.id": "cannot group the actions of a single session",
        "gen_ai.provider.name": "unknown provider",
        "gen_ai.request.model": "unknown model",
        "gen_ai.system_instructions": "system prompt not captured: no way to know "
                                      "under which instructions the agent acted",
    },
}

# Attributes no convention provides, which your processor must inject.
INJECTION_NEEDED = {
    "agent.identity.self_hosted": "gen_ai.agent.id is reserved for HOSTED agents "
                                  "(Bedrock ARN, GCP Agent Registry). For a "
                                  "self-hosted agent, no attribute exists.",
    "prompt.system.hash": "hash of the system prompt at the time of the action",
    "config.hash": "hash of the configuration (temperature, exposed tools, guardrails)",
    "business.object.id": "link to the business object (claim, order, transaction)",
}

# Concept -> real attribute(s) which, when present in the trace, already
# satisfy that need. Avoids listing under "TO INJECT" what an
# --instrument-tools run (or equivalent) has already set.
INJECTION_SATISFIED_BY = {
    "agent.identity.self_hosted": ("agent.instance.id", "actor.type"),
    "prompt.system.hash": ("prompt.system.hash",),
    "config.hash": ("config.hash",),
    "business.object.id": ("business.object.id",),
}


def _iso(ns):
    """Nanoseconds since epoch -> ISO-8601 UTC string, or None."""
    if ns is None:
        return None
    return datetime.datetime.fromtimestamp(
        ns / 1e9, datetime.timezone.utc).isoformat()


class ForensicInspector(SpanProcessor):
    """Collects spans and produces a report of what is provable."""

    def __init__(self):
        self.spans = []
        self.meta = {}

    def on_end(self, span):
        self.spans.append({
            "name": span.name,
            "kind": str(span.kind),
            "trace_id": format(span.context.trace_id, "032x"),
            "span_id": format(span.context.span_id, "016x"),
            "parent": format(span.parent.span_id, "016x") if span.parent else None,
            # Without these, the order of two actions is not recoverable from
            # the trace alone - and "in which order" is a forensic question.
            "start_time": _iso(span.start_time),
            "end_time": _iso(span.end_time),
            "attributes": dict(span.attributes or {}),
            "events": [e.name for e in (span.events or [])],
        })

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True

    # ---------- report ----------

    def _tool_spans(self):
        return [s for s in self.spans
                if s["attributes"].get("gen_ai.operation.name") == "execute_tool"
                or s["name"].startswith("execute_tool")]

    def _nested_tool_calls(self):
        """Finds tool-call DECISIONS nested inside gen_ai.output.messages.

        Some providers never set gen_ai.tool.name / tool.call.id /
        tool.call.arguments at the normalised location (an execute_tool span).
        The information still exists, nested in the message content - but it is
        only the model's decision, not proof of execution: no result, no
        confirmation that the action took place.
        """
        found = []
        for s in self.spans:
            v = s["attributes"].get("gen_ai.output.messages")
            if not v:
                continue
            try:
                parsed = json.loads(v) if isinstance(v, str) else v
            except (TypeError, ValueError):
                continue
            for msg in parsed if isinstance(parsed, list) else []:
                if not isinstance(msg, dict):
                    continue
                for part in msg.get("parts", []):
                    if isinstance(part, dict) and part.get("type") == "tool_call":
                        found.append({
                            "span": s,
                            "name": part.get("name"),
                            "id": part.get("id"),
                            "arguments": part.get("arguments"),
                        })
        return found

    def _resolved(self, span):
        """Span attributes, enriched with those inherited from the parent chain.

        The conventions spread information across the tree: provider and model
        live on the chat or agent span, not on execute_tool. An investigator
        must therefore walk up the chain. This is an important design
        constraint: an isolated action span is not self-sufficient, it only
        means something within its tree. If the tree is truncated (sampling,
        log rotation), the action becomes uninterpretable.
        """
        by_id = {s["span_id"]: s for s in self.spans}
        chain = []
        cur = span
        seen = set()
        while cur and cur["span_id"] not in seen:
            seen.add(cur["span_id"])
            chain.append(cur)
            cur = by_id.get(cur["parent"])

        merged, origin = {}, {}
        for s in reversed(chain):          # root first, nearest wins
            for k, v in s["attributes"].items():
                merged[k] = v
                origin[k] = "own" if s is span else f"inherited<-{s['name']}"
        return merged, origin

    def report(self):
        out = []
        w = out.append

        w("=" * 72)
        w("FORENSIC READINESS REPORT")
        w("=" * 72)

        if not self.spans:
            w("\nNo span collected. The instrumentation emitted nothing.")
            w("First audit finding: the system is not traced at all.")
            return "\n".join(out)

        traces = defaultdict(list)
        for s in self.spans:
            traces[s["trace_id"]].append(s)

        w(f"\n{len(self.spans)} spans, {len(traces)} trace(s).")

        tools = self._tool_spans()
        nested = self._nested_tool_calls() if not tools else []

        w(f"{len(tools)} EXECUTED tool action(s) traced (execute_tool span)"
          f" - this is the only proof of execution.")
        if not tools and nested:
            w(f"{len(nested)} model decision(s) detected elsewhere, nested in"
              f" gen_ai.output.messages - this is NOT proof of execution.\n")
        else:
            w("")

        if not tools:
            if nested:
                for i, tc in enumerate(nested, 1):
                    args = str(tc["arguments"])
                    if len(args) > 55:
                        args = args[:52] + "..."
                    w(f"--- Decision {i}: {tc['name']} (no execution proven) " + "-" * 4)
                    w(f"  NESTED   gen_ai.tool.name = {tc['name']}"
                      f"   (in gen_ai.output.messages, not at the normalised location)")
                    w(f"  NESTED   gen_ai.tool.call.id = {tc['id']}"
                      f"   (in gen_ai.output.messages, not at the normalised location)")
                    w(f"  NESTED   gen_ai.tool.call.arguments = {args}"
                      f"   (in gen_ai.output.messages, not at the normalised location)")
                    w("  MISSING  gen_ai.tool.call.result")
                    w("           -> we do not know whether the action succeeded, "
                      "nor what it returned")
                    w("")
                w("  What this proves: the model's INTENT at time T.")
                w("  What this does NOT prove: that the action was executed, when, nor")
                w("  what the system returned. That is exactly the gap where a dispute")
                w("  takes hold.\n")
            else:
                w("No tool decision recoverable either, neither at the normalised")
                w("location nor nested in the message content.")
                w("Nothing the agent does is provable.\n")

        # --- coverage per action ---
        for i, s in enumerate(tools, 1):
            a, origin = self._resolved(s)
            tool = a.get("gen_ai.tool.name", "?")
            w(f"--- Action {i}: {tool} " + "-" * max(4, 52 - len(str(tool))))
            for category, requirements in FORENSIC_REQUIREMENTS.items():
                for attr, consequence in requirements.items():
                    if attr in a:
                        val = str(a[attr])
                        if len(val) > 50:
                            val = val[:47] + "..."
                        src = origin.get(attr, "")
                        tag = "" if src == "own" else f"   ({src})"
                        w(f"  OK       {attr} = {val}{tag}")
                    else:
                        w(f"  MISSING  {attr}")
                        w(f"           -> {consequence}")
            w("")

        # --- summary ---
        w("=" * 72)
        w("SUMMARY")
        w("=" * 72)

        all_attrs = set()
        for s in self.spans:
            all_attrs.update(s["attributes"].keys())

        total_req = sum(len(v) for v in FORENSIC_REQUIREMENTS.values())
        normalised = {attr for cat in FORENSIC_REQUIREMENTS.values()
                      for attr in cat if attr in all_attrs}

        # Three states: normalised location / nested elsewhere / absent.
        # Only tool.name, tool.call.id, tool.call.arguments can be recovered
        # nested in gen_ai.output.messages - see _nested_tool_calls.
        nested_hits = self._nested_tool_calls()
        nested_attrs = set()
        if nested_hits:
            nested_attrs = {"gen_ai.tool.name", "gen_ai.tool.call.id",
                            "gen_ai.tool.call.arguments"} - normalised

        absent = [attr for cat in FORENSIC_REQUIREMENTS.values()
                  for attr in cat if attr not in normalised and attr not in nested_attrs]

        recoverable = len(normalised) + len(nested_attrs)
        w(f"\nCoverage: {len(normalised)}/{total_req} at the normalised location"
          f"  -  {recoverable}/{total_req} recoverable in total"
          f" ({len(normalised)} normalised + {len(nested_attrs)} nested).")
        if nested_attrs:
            w(f"  Nested in gen_ai.output.messages, outside the normalised location: "
              f"{', '.join(sorted(nested_attrs))}")

        if absent:
            w("\nAbsent from the ENTIRE trace (neither normalised nor nested):")
            for m in absent:
                note = ""
                if m in ("gen_ai.tool.call.arguments", "gen_ai.tool.call.result"):
                    if tools:
                        note = ("  [execute_tool span present; opt-in attribute - enable "
                                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT]")
                    else:
                        note = ("  [no execute_tool span: not an opt-in setting, "
                                "the execution instrumentation is missing]")
                if m == "gen_ai.system_instructions":
                    capture_on = str(self.meta.get("capture_content", "")).upper() in (
                        "SPAN_ONLY", "EVENT_ONLY", "SPAN_AND_EVENT")
                    if capture_on:
                        note = ("  [content capture enabled; absent because the scenario "
                                "sends no separate system prompt - everything is in "
                                "the user message]")
                    else:
                        note = "  [content capture disabled by default]"
                w(f"  - {m}{note}")

        w("\n" + "-" * 72)
        still_missing = {k: v for k, v in INJECTION_NEEDED.items()
                         if not any(sat in all_attrs
                                    for sat in INJECTION_SATISFIED_BY.get(k, ()))}
        already_injected = [k for k in INJECTION_NEEDED if k not in still_missing]

        if still_missing:
            w("TO INJECT - no convention provides it, absent from this trace:")
            for k, v in still_missing.items():
                w(f"  * {k}")
                w(f"      {v}")
        else:
            w("TO INJECT - nothing: everything that required manual injection")
            w("is already present in this trace.")

        if already_injected:
            w(f"\nAlready injected in this trace (--instrument-tools or equivalent): "
              f"{', '.join(already_injected)}")

        # --- attribution ---
        w("\n" + "-" * 72)
        w("ATTRIBUTION")
        has_agent = any(k.startswith("gen_ai.agent.") for k in all_attrs)
        if has_agent:
            w("  Agent attributes are present, but check that they distinguish the")
            w("  agent from the human user it runs under.")
        else:
            w("  NO agent attribute. The actions are indistinguishable from those of")
            w("  a human acting within their normal scope. That is the central gap.")

        w("\n" + "-" * 72)
        w("INTEGRITY")
        w("  These spans are freely mutable and deletable.")
        w("  Nothing here constitutes defensible evidence without hash chaining")
        w("  and signing.")

        return "\n".join(out)

    def dump_json(self, path):
        """Writes every span with FULL values, for comparison."""
        payload = {"meta": self.meta, "spans": self.spans}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=str)

    def dump_tree(self, truncate=50):
        """Prints the span tree. truncate=None for full values."""
        out = []
        by_parent = defaultdict(list)
        for s in self.spans:
            by_parent[s["parent"]].append(s)

        def walk(parent, depth):
            # Chronological, not alphabetical: an action tree that reorders
            # actions misrepresents the sequence. Dumps produced before
            # start_time was captured fall back to the name.
            siblings = sorted(by_parent.get(parent, []),
                              key=lambda x: (x.get("start_time") or "", x["name"]))
            for s in siblings:
                op = s["attributes"].get("gen_ai.operation.name", "")
                tag = f"  [{op}]" if op else ""
                out.append("  " * depth + f"|- {s['name']}{tag}")
                for k in sorted(s["attributes"]):
                    v = str(s["attributes"][k])
                    if truncate and len(v) > truncate:
                        v = v[:truncate - 3] + "..."
                    out.append("  " * depth + f"     {k} = {v}")
                walk(s["span_id"], depth + 1)

        out.append("SPAN TREE")
        out.append("=" * 72)
        walk(None, 0)
        return "\n".join(out)
