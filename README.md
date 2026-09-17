# Agent Trace Forensics

**What can you actually prove about what your AI agent did?**

A claims-processing agent issued a €1,180 payment. The transfer happened — the
function ran, the record exists.

With the official OpenTelemetry GenAI auto-instrumentation, **zero spans attest
to it**. On both Anthropic and OpenAI.

This repository is the reproducible protocol behind that finding, plus a
forensic inspector that measures the gap between what your traces contain and
what you would need to defend an action.

---

## TL;DR

| | auto-instrumentation only | + manual tool instrumentation |
|---|---|---|
| Spans | 4 | 6 |
| Root traces | 1 | 1 |
| **Executions proven** | **0** | **2** |
| Forensic attributes at normalised location | 4/11 | 8/11 |
| Recoverable in total | 7/11 | 8/11 |

Identical on both providers. The fix is roughly two dozen lines of code that
no instrumentation package writes for you, and that nothing in the docs tells
you to write.

8/11 is the ceiling: no amount of instrumentation reaches the remaining three.

---

## Why this matters

Observability and forensics are not the same problem.

| Observability | Forensics |
|---|---|
| Sampled | Exhaustive on consequential actions |
| Mutable, deletable | Tamper-evident |
| Short retention | Retention matched to liability |
| User identity | Distinct agent identity |
| Trust the vendor | Verifiable offline |

An observability trace tells you what happened *if you trust it*. A forensic
record has to hold up *against someone who disputes it* — a regulator, a
customer, an insurer, an internal audit committee.

Most teams running agents in production have the first and believe they have
the second.

---

## The scenario

A three-step claims agent, deliberately mundane:

1. Read a claim file (claim 44190, policy 88213, €1,180 claimed)
2. `get_contract` — check the policy status and coverage limit
3. `issue_payment` — issue the transfer if the policy is active and the
   amount is under the limit

One of those actions is irreversible and financial. That is the one an auditor
asks about.

---

## Findings

### 1. No execution is traced by default

Both providers: four spans, none of them `execute_tool`.

This is not what the spec asks for. On the `execute_tool` span, the conventions
state that **"GenAI instrumentations that can instrument tool execution calls
SHOULD do so, unless another instrumentation can reliably cover all supported
tool types."** Neither package does, on either provider.

The same note adds that application developers are encouraged to follow the
convention for tools invoked by their own code, and to manually instrument any
tool call automatic instrumentation does not cover. Today that second sentence
is carrying all the weight: the burden the spec places on instrumentations has
landed entirely on you.

So by default you capture the model's side of the conversation and nothing
else.

### 2. You can prove intent, not action

The tool call *is* in the trace — just not where the spec normalises it.
Inside `gen_ai.output.messages`, each assistant turn carries:

```json
{
  "arguments": { "amount": 1180, "beneficiary": "client_44190" },
  "name": "issue_payment",
  "id": "toolu_018RmF8kpxzy8vambkdmyMzz",
  "type": "tool_call"
}
```

Both providers use the same shape. Anthropic adds a preceding `text` part
carrying its reasoning; OpenAI goes straight to the call. That is a model
behaviour difference, not an instrumentation one.

The distinction that matters:

- **Provable**: what the model decided to do, and when.
- **Not provable**: that it happened, when, or what the system returned.

"The model requested a transfer" and "a transfer was made" are different
claims. Only the first is in your traces. That gap is where disputes live.

There is a second-order problem. An investigator following the spec looks for
`gen_ai.tool.call.arguments` and finds nothing. The data is recoverable, but
only if you know to open a nested JSON blob the conventions do not point you
to.

### 3. The agent's permitted scope is lost on one provider

`gen_ai.tool.definitions` — the list of tools exposed to the model — appears
on OpenAI spans and not on Anthropic ones. Same tool specification sent to
both.

This is not cosmetic. The first question after an incident is whether the
action was within the agent's mandate. Without the tool definitions captured
at call time, you cannot reconstruct what the agent was *allowed* to do at the
moment it acted. If `issue_payment` was added to the agent the day before the
incident, the trace will never say so.

### 4. The fix is structural, and it is yours to write

Adding `execute_tool` spans with propagated `tool_call_id`, under an enclosing
`invoke_agent` span, takes executions proven from 0 to 2 and normalised
coverage from 4/11 to 8/11.

What makes it work is not the extra attributes — it is the **tree**. The
`tool_call_id` on the execution span matches the id the model emitted, so the
chain from decision to action holds. Provider and model resolve by walking up
to the parent span.

Which yields an operational consequence: **sampling is unsafe on consequential
actions.** Attributes are spread across the tree. Drop a parent span and the
child action becomes uninterpretable even though its own span is intact.

---

## What no amount of instrumentation fixes

Three attributes remain absent after correct instrumentation. Two are gaps in
the conventions: the attribute exists, but the spec puts this use case outside
its scope. The third is an artefact of this scenario, not a gap.

**`gen_ai.agent.id`** — exists, but the spec scopes it to *hosted* agent
resources (a Bedrock ARN, a GCP Agent Registry identifier) and explicitly
discourages recording in-memory instance ids. For a self-hosted agent, which
covers most enterprise deployments, there is no appropriate attribute. Actions
remain indistinguishable from those of the human whose credentials the agent
runs under.

**`gen_ai.conversation.id`** — exists, but the spec says instrumentations
should not invent one when no natural identifier is available. No UUID, no
trace id, no hash. So it is frequently absent, and nothing ties actions to a
business object.

**`gen_ai.system_instructions`** — absent here because this scenario sends no
separate system prompt, not because of a configuration issue.

This repository injects the missing pieces as non-standard attributes —
`agent.instance.id`, `actor.type`, `prompt.system.hash`, `business.object.id` —
to show what a forensic-grade record requires. They are not conventions. That
is the point.

One attribute is deliberately *not* in that list. `gen_ai.agent.version` is a
convention, `conditionally_required` on `invoke_agent`, and the scenario sets
it there. It is worth naming because it is the easiest mistake to make in this
exercise: a custom `agent.version` injected next to the others would look like
evidence that the conventions fall short, when it would only show the benchmark
had not read them closely enough. Classify the cause before blaming the spec —
the list above is one line shorter for it.

---

## Provider divergences

Measured with identical settings, content capture on, same tool specification.

**Anthropic only**
`gen_ai.usage.cache_creation.input_tokens`, `gen_ai.usage.cache_read.input_tokens`
— prompt caching accounting. No forensic relevance.

**OpenAI only**
`gen_ai.tool.definitions` — **forensically significant**, see finding 3.
`openai.response.service_tier`, `openai.response.system_fingerprint` —
provider-specific conventions, which are part of the spec (see `model/openai/`
in the conventions repository), not ad-hoc attributes.

**Excluded from the comparison**
`gen_ai.request.temperature`. The model configuration used on Anthropic does
not accept it alongside the effort setting, so the parameter was never sent.
Its absence says nothing about the instrumentation.

This is a general methodological point: perfect parameter symmetry across
providers is not achievable. Every missing attribute has to be classified
before it counts as a divergence:

1. **Not sent** — the API rejects it. Not an instrumentation finding.
2. **Sent but not emitted** — a real divergence.
3. **Concept absent** at that provider — worth noting, no weight.

`compare.py` checks run symmetry and refuses to draw conclusions when the two
runs are not comparable.

---

## Reproduce

Requires Python 3.12 or later.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env    # add your API keys
set -a && source .env && set +a
```

```bash
# The full protocol: four runs plus comparisons.
# Note: this overwrites the committed dumps and full_report.txt with your run.
./compare_providers.sh

# Or individually. Dump to your own filenames so the committed evidence
# stays intact.
python agent_bench.py --provider anthropic --tree
python agent_bench.py --provider anthropic --instrument-tools --tree
python agent_bench.py --provider openai --dump openai.mine.json
python compare.py anthropic.json openai.mine.json

# No API key, no network — validates the plumbing only
python agent_bench.py --provider fake --instrument-tools --tree

# Free and local, no API key: ollama serve && ollama pull qwen3
python agent_bench.py --provider ollama --instrument-tools --tree
```

`--full` prints untruncated values. `--dump` writes complete spans as JSON;
the nested blobs in `gen_ai.output.messages` are the interesting part.

### Two setup traps

**The package moved.** GenAI instrumentations now live in the dedicated
`opentelemetry-python-genai` repository. The older
`opentelemetry-instrumentation-openai-v2` still installs silently and is
deprecated. Import paths are `opentelemetry.instrumentation.genai.anthropic`
and `opentelemetry.instrumentation.genai.openai` — with a dot, not an
underscore.

**The conventions version.** Without
`OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`, instrumentations
keep emitting the conventions they emitted before (v1.36.0 or earlier). You end
up reading current docs while observing stale traces.

---

## Files

| | |
|---|---|
| `agent_bench.py` | the scenario, provider-switchable |
| `forensic_inspector.py` | a `SpanProcessor` that reports what is provable |
| `compare.py` | divergence table between two runs |
| `compare_providers.sh` | the full protocol in one command |
| `anthropic.json`, `openai.json` | baseline span dumps from the pinned run |
| `anthropic_instr.json`, `openai_instr.json` | the same runs with `--instrument-tools` |
| `full_report.txt` | full protocol output for those four runs |

`forensic_inspector.py` installs as an ordinary span processor, so it slots
into an existing pipeline without touching instrumented code.

The four dumps are the evidence behind every number above, and both tools are
pure functions of them. You can therefore re-derive the findings offline, with
no API key and no network:

```bash
python compare.py anthropic.json openai.json
python compare.py anthropic_instr.json openai_instr.json
```

Both open with a `RUNS NOT COMPARABLE` banner naming `temperature`. That is the
tool working as intended: the parameter was never sent on Anthropic, so
`compare.py` flags the asymmetry and drops `gen_ai.request.temperature` from
the table rather than reporting it as a divergence. See *Provider divergences*
above.

---

## Limitations

Two providers, one scenario, one agent shape, one language. Nothing here
generalises to multi-agent systems, MCP servers, streaming, or frameworks like
LangChain.

Everything in `gen_ai.*` is still marked Development in the conventions. These
findings are pinned to the versions below and will drift.

The dumps published here carry no timestamps. The inspector records span start
and end times, and orders the action tree chronologically, but that was added
after these runs were produced. So for the findings above, that an action
happened and under which decision is established; when it happened is not.
Re-running populates the timestamps.

The committed dumps contain prompts and model responses in clear. That is
deliberate: the scenario is synthetic — an invented policy number, an invented
claimant, an invented amount — and a provider comparison is only auditable if
you can read what each provider actually emitted. The inspector injects hashes,
never content: `prompt.system.hash` is a SHA-256, not a prompt. But `--dump`
writes spans verbatim. Point it at real traffic and it writes real prompts to
disk. Redact first.

None of this addresses integrity: the spans produced here are freely mutable
and deletable. Tamper-evidence — hash chaining, signing — is a separate problem
and is not solved in this repository.

---

## Versions

Findings pinned to:

- semantic-conventions-genai: commit `b5d8440f6f126738fd50f927752cd669772c517b` (2026-09-08)
- `opentelemetry-instrumentation-genai-anthropic` `1.1b1`
- `opentelemetry-instrumentation-genai-openai` `1.1b0`
- `opentelemetry-util-genai` `1.1b0`
- `opentelemetry-api` / `opentelemetry-sdk` `1.44.0`
- Provider SDKs: `anthropic` `1.5.0`, `openai` `3.13.0`
- Models: `claude-sonnet-4-6`, `gpt-4o-mini` (OpenAI responded as `gpt-4o-mini-2024-07-18`)
- Runs performed on 2026-09-16

Rerunning against later versions will likely produce different results. That is
expected, and tracking it is part of the problem.

---

## License

Copyright 2026 Quentin-NA

Licensed under the Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
