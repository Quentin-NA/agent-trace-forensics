# CLAUDE.md

Working rules for this repository. Read before making changes.

---

## What this project is

A reproducible protocol demonstrating that OpenTelemetry GenAI
auto-instrumentation traces **what a model decided**, never **what was
executed**. A payment is issued; no span attests to it.

The repository's value is that its numbers can be independently reproduced.
Everything below protects that property.

---

## The thesis — do not drift from it

**Agent forensics is a structural problem, not an attribute problem.**

You can have every attribute the spec defines and prove nothing, because what
makes an action accountable is the link between decision, execution, and
actor — and that link lives in the span tree, not in the attribute list.

The most common wrong move here is to "improve coverage" by adding attributes.
Coverage is a diagnostic, not a goal. If a change raises the coverage number
without strengthening the decision → execution chain, it misses the point.

---

## Evidence rules

These are strict. The repository's credibility depends on them.

- **Every number in the README comes from an actual run.** Never estimate,
  extrapolate, or round toward a nicer figure. If a run has not been done, the
  number does not go in.
- **Classify every missing attribute before calling it a divergence.** Three
  causes, only one of which is a finding:
  1. *Not sent* — the API rejects the parameter. Not an instrumentation
     finding.
  2. *Sent but not emitted* — a real divergence.
  3. *Concept absent* at that provider — note it, no weight.
  `gen_ai.request.temperature` is category 1 and is excluded from the
  comparison. Do not reintroduce it.
- **Perfect parameter symmetry across providers is not achievable.** Providers
  reject different parameter combinations. `compare.py` must keep checking run
  symmetry and refusing to conclude when runs differ.
- **Distinguish "absent because opt_in" from "absent because no span exists".**
  Enabling content capture will not produce `gen_ai.tool.call.result` when no
  `execute_tool` span was created. Misattributing a cause is the error class
  this project exists to expose — do not commit it in the tooling.
- **Pin versions.** Any published finding names the conventions commit, the
  instrumentation package versions, the models, and the run date.
- **Verify every spec claim against the YAML before writing it.** The README
  once stated that `gen_ai.agent.version` had no home in the conventions. It
  does: registry plus `conditionally_required` on `invoke_agent`. The benchmark
  simply was not setting it, which cost one point of coverage and misattributed
  a benchmark gap to the spec. That is the error class this project exists to
  expose. Read `model/*.yaml` at the pinned commit, not the generated Markdown.
- **A scenario change invalidates the committed dumps.** `prompt.system.hash`
  is the SHA-256 of `PROMPT`; the dumps also carry the tool descriptions, the
  `CONTRACTS` values and `business.object.id`. Touching any of them requires a
  full re-run against the real providers and a README updated from that run.
- **The published dumps predate timestamp capture.** `on_end` records span
  start and end times and `dump_tree` orders chronologically, but the four
  committed dumps were produced before that and carry neither. Code paths
  reading `start_time` must fall back, as `dump_tree` does.
- **`full_report.txt` is regenerable from the dumps.** `report()` and
  `compare.py` are pure functions of the spans, so the artefact can be rebuilt
  without any API call. A rebuild must say so in its header.

---

## Safety

- `.env` is gitignored and stays that way. No API key in code, in tests, in
  comments, in commit messages.
- Before any push, verify no key has entered the history.
- `forensic_inspector.py` must never persist prompt or output content in
  clear. Hashes only. A forensic tool that becomes a GDPR liability is
  unusable — this constraint is not negotiable and applies to every future
  component.
- Default to `--provider fake` when testing changes. It needs no key and no
  network. Only run against real providers when the change affects what the
  providers emit.

---

## Architecture

```
agent_bench.py          scenario, provider-switchable
forensic_inspector.py   SpanProcessor — reports what is provable
compare.py              divergence table between two dumps
compare_providers.sh    full protocol in one command

*.json                  committed span dumps — the evidence behind the README
full_report.txt         full protocol output, regenerable from those dumps
```

**`forensic_inspector.py` is an ordinary span processor.** It slots into an
existing pipeline without touching instrumented code. That property is a
product requirement, not an implementation detail — do not introduce anything
requiring changes to the traced application.

**Attribute resolution walks the parent chain.** Provider and model live on
the chat or agent span, not on `execute_tool`. Resolution up the tree is
correct behaviour and must be preserved; it is also what demonstrates that
sampling is unsafe on consequential actions.

**Non-standard attributes are deliberate.** `agent.instance.id`,
`actor.type`, `prompt.system.hash`, `business.object.id` are not conventions.
`gen_ai.agent.version` is one, and is set on `invoke_agent` rather than
injected - do not reintroduce a custom duplicate of it. They exist to show what the conventions lack. Keep them
clearly separated from `gen_ai.*` in both code and output.

---

## Scope

**In scope**: the measurement protocol, the inspector, provider comparison.

**Out of scope**, and deliberately so:
- Integrity — hash chaining, signing, tamper-evidence. Stated as a limitation
  in the README. Do not start implementing it here.
- The Java span processor. Separate project, separate repository.
- Multi-agent, MCP servers, streaming, LangChain and other frameworks.

If a change would expand scope, say so rather than doing it.

---

## Writing

- English, for the OpenTelemetry community.
- Lead with the finding, not with the tool. The first paragraph states that a
  €1,180 payment happened and nothing proves it.
- No commercial language. No services, no availability, no pricing. The
  repository establishes competence; that is all it does.
- State limitations plainly and at length. That section is what makes the rest
  credible.
- Claims about the spec cite the spec. When unsure, check the YAML in
  `model/` of the conventions repository rather than the generated Markdown —
  the YAML is the source of truth.
