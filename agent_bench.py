#!/usr/bin/env python3
"""
Test bench: generates real agent traces and feeds them to the inspector.

Scenario modelled on the insurance case: an agent reads a claim file, checks a
policy, and issues a payment. One of the tool calls has an irreversible
financial consequence.

Providers:
    --provider anthropic   (ANTHROPIC_API_KEY)
    --provider openai      (OPENAI_API_KEY)
    --provider ollama      (free, local: ollama serve + ollama pull qwen3)
    --provider fake        (no external dependency)

Modes:
    --instrument-tools   manually instruments tool execution.
                         Shows what auto-instrumentation does NOT do.
    --dump FILE          writes every span, full values, as JSON.
                         Compare across providers with compare.py.
    --full               does not abbreviate values in the tree.

IMPORTANT - parameter symmetry.
The same call parameters are passed to every provider wherever the SDK allows
it (max_tokens, temperature). An avoidable asymmetry would produce false
divergences: an attribute present on one side and absent on the other because
the scenario did not supply it, not because the SDK fails to emit it.

Known exception: the installed anthropic SDK (1.5.0) no longer exposes
`temperature` on Messages.create() - verified via inspect.signature(), the
parameter has given way to a different set (output_config, service_tier,
inference_geo...). Here the asymmetry is real, not a bias of the scenario: you
cannot supply a parameter the API no longer accepts. The only remaining
generation setting, `output_config.effort`, is used instead - but it is NOT an
equivalent of temperature (reasoning depth, not sampling randomness). That is a
divergence axis in its own right, to be kept as such in the comparison table
rather than hidden.
"""

import argparse
import datetime
import hashlib
import json
import os
import sys

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.resources import Resource

from forensic_inspector import ForensicInspector

# --------------------------------------------------------------------------
# Call parameters - IDENTICAL across all providers
# --------------------------------------------------------------------------

MAX_TOKENS = 1024
TEMPERATURE = 0.0          # openai / ollama only, see note above
ANTHROPIC_EFFORT = "low"   # anthropic only - NOT an equivalent of temperature, it is
                           # the only generation setting still exposed on
                           # Messages.create() in the installed SDK (1.5.0)

MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o-mini",
    "ollama": "qwen3",
    "fake": "fake-model",
}

# --------------------------------------------------------------------------
# Tools exposed to the agent
# --------------------------------------------------------------------------

CONTRACTS = {
    "88213": {"status": "active", "limit": 1500, "holder": "client_44190"},
    "88214": {"status": "terminated", "limit": 1500, "holder": "client_44191"},
}

PAYMENTS = []


def get_contract(contract_id: str) -> dict:
    return CONTRACTS.get(contract_id, {"status": "", "limit": 0})


def issue_payment(amount: float, beneficiary: str) -> dict:
    PAYMENTS.append({"amount": amount, "beneficiary": beneficiary})
    return {"status": "executed", "reference": f"PAY-{len(PAYMENTS):05d}"}


TOOLS_SPEC = [
    {
        "name": "get_contract",
        "description": "Retrieves the status and coverage limit of a policy.",
        "input_schema": {
            "type": "object",
            "properties": {"contract_id": {"type": "string"}},
            "required": ["contract_id"],
        },
    },
    {
        "name": "issue_payment",
        "description": "Issues a claim settlement transfer. Irreversible action.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "beneficiary": {"type": "string"},
            },
            "required": ["amount", "beneficiary"],
        },
    },
]

DISPATCH = {"get_contract": get_contract, "issue_payment": issue_payment}

PROMPT = (
    "You are handling a motor claim. Claim 44190, policy 88213, "
    "amount claimed 1180 euros. Check the policy, then settle the claim "
    "if the policy is active and the amount is under the coverage limit."
)

# Agent identity - none of this is provided by the conventions.
AGENT = {
    "name": "claims-agent",
    "version": "2.1.0",
    "instance_id": "claims-agent@prod-eu-west-1#7",
    "business_object": "claim:44190",
}


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Tool execution
# --------------------------------------------------------------------------

INSTRUMENT_TOOLS = False


def call_tool(name: str, args: dict, tool_call_id: str = None):
    """Execute a tool, with or without instrumentation.

    SDK auto-instrumentation wraps ONLY the model call. Tool execution is
    ordinary application code: if nobody instruments it, no span attests that
    the action took place.

    IMPORTANT - call this function WHILE an enclosing span (the agent turn) is
    active in the current context. Otherwise the execute_tool span starts its
    own root trace, unlinked from the model decision that requested it -
    exactly the defect observed and fixed here.
    """
    if not INSTRUMENT_TOOLS:
        return DISPATCH[name](**args)

    tracer = trace.get_tracer("bench.tools")
    with tracer.start_as_current_span(f"execute_tool {name}") as sp:
        # --- OTel conventions ---
        sp.set_attribute("gen_ai.operation.name", "execute_tool")
        sp.set_attribute("gen_ai.tool.name", name)
        sp.set_attribute("gen_ai.tool.type", "function")
        sp.set_attribute("gen_ai.tool.call.arguments", json.dumps(args))
        if tool_call_id:
            sp.set_attribute("gen_ai.tool.call.id", tool_call_id)

        # --- outside the conventions: what the processor must inject ---
        # gen_ai.agent.version is NOT injected here: the conventions define it
        # and place it on invoke_agent, where this scenario now sets it. The
        # inspector resolves it up the parent chain.
        sp.set_attribute("agent.instance.id", AGENT["instance_id"])
        sp.set_attribute("actor.type", "agent")          # vs "human"
        sp.set_attribute("prompt.system.hash", sha256(PROMPT))
        sp.set_attribute("business.object.id", AGENT["business_object"])

        result = DISPATCH[name](**args)
        sp.set_attribute("gen_ai.tool.call.result", json.dumps(result))
        return result


# --------------------------------------------------------------------------
# OTel setup
# --------------------------------------------------------------------------

def setup_tracing(show_raw: bool):
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")

    provider = TracerProvider(
        resource=Resource.create({"service.name": "forensic-bench"})
    )
    inspector = ForensicInspector()
    provider.add_span_processor(inspector)
    if show_raw:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    return inspector


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------

def run_anthropic(model):
    from opentelemetry.instrumentation.genai.anthropic import AnthropicInstrumentor
    AnthropicInstrumentor().instrument()

    import anthropic
    client = anthropic.Anthropic()

    tracer = trace.get_tracer("bench.agent")
    with tracer.start_as_current_span(f"invoke_agent {AGENT['name']}") as agent_span:
        agent_span.set_attribute("gen_ai.operation.name", "invoke_agent")
        agent_span.set_attribute("gen_ai.agent.name", AGENT["name"])
        # conditionally_required "When available" on invoke_agent. The value is
        # available, so it is set - no custom attribute needed for it.
        agent_span.set_attribute("gen_ai.agent.version", AGENT["version"])
        agent_span.set_attribute("gen_ai.provider.name", "anthropic")
        agent_span.set_attribute("gen_ai.request.model", model)

        messages = [{"role": "user", "content": PROMPT}]
        for _ in range(6):
            resp = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                # no temperature: absent from Messages.create() in the installed
                # anthropic SDK (1.5.0). output_config.effort is the only
                # generation setting left, but it is NOT an equivalent
                # (reasoning depth, not sampling randomness).
                output_config={"effort": ANTHROPIC_EFFORT},
                tools=TOOLS_SPEC,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                break
            results = []
            for c in calls:
                out = call_tool(c.name, c.input, tool_call_id=c.id)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": c.id,
                    "content": json.dumps(out),
                })
            messages.append({"role": "user", "content": results})


def run_openai(model, base_url=None):
    from opentelemetry.instrumentation.genai.openai import OpenAIInstrumentor
    OpenAIInstrumentor().instrument()

    from openai import OpenAI
    kwargs = {}
    if base_url:
        kwargs["base_url"] = base_url
        kwargs["api_key"] = "ollama"
    client = OpenAI(**kwargs)

    tools = [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    } for t in TOOLS_SPEC]

    tracer = trace.get_tracer("bench.agent")
    with tracer.start_as_current_span(f"invoke_agent {AGENT['name']}") as agent_span:
        agent_span.set_attribute("gen_ai.operation.name", "invoke_agent")
        agent_span.set_attribute("gen_ai.agent.name", AGENT["name"])
        # conditionally_required "When available" on invoke_agent. The value is
        # available, so it is set - no custom attribute needed for it.
        agent_span.set_attribute("gen_ai.agent.version", AGENT["version"])
        agent_span.set_attribute("gen_ai.provider.name", "openai")
        agent_span.set_attribute("gen_ai.request.model", model)

        messages = [{"role": "user", "content": PROMPT}]
        for _ in range(6):
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                max_tokens=MAX_TOKENS,        # symmetry with Anthropic
                temperature=TEMPERATURE,
            )
            msg = resp.choices[0].message
            messages.append(msg)
            if not msg.tool_calls:
                break
            for tc in msg.tool_calls:
                out = call_tool(tc.function.name, json.loads(tc.function.arguments),
                                 tool_call_id=tc.id)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(out),
                })


def run_fake(model):
    tracer = trace.get_tracer("bench.fake")
    with tracer.start_as_current_span(f"invoke_agent {AGENT['name']}") as agent_span:
        agent_span.set_attribute("gen_ai.operation.name", "invoke_agent")
        agent_span.set_attribute("gen_ai.agent.name", AGENT["name"])
        # conditionally_required "When available" on invoke_agent. The value is
        # available, so it is set - no custom attribute needed for it.
        agent_span.set_attribute("gen_ai.agent.version", AGENT["version"])
        agent_span.set_attribute("gen_ai.provider.name", "fake")
        agent_span.set_attribute("gen_ai.request.model", model)

        with tracer.start_as_current_span(f"chat {model}") as chat:
            chat.set_attribute("gen_ai.operation.name", "chat")
            chat.set_attribute("gen_ai.provider.name", "fake")
            chat.set_attribute("gen_ai.request.model", model)
            chat.set_attribute("gen_ai.request.max_tokens", MAX_TOKENS)
        call_tool("get_contract", {"contract_id": "88213"}, tool_call_id="call_get_contract")
        call_tool("issue_payment", {"amount": 1180, "beneficiary": "client_44190"},
                  tool_call_id="call_issue_payment")


RUNNERS = {
    "anthropic": lambda m: run_anthropic(m),
    "openai": lambda m: run_openai(m),
    "ollama": lambda m: run_openai(m, base_url="http://localhost:11434/v1"),
    "fake": lambda m: run_fake(m),
}


# --------------------------------------------------------------------------

def main():
    global INSTRUMENT_TOOLS

    p = argparse.ArgumentParser()
    p.add_argument("--provider", default="fake", choices=list(RUNNERS))
    p.add_argument("--model", default=None)
    p.add_argument("--raw", action="store_true")
    p.add_argument("--tree", action="store_true")
    p.add_argument("--instrument-tools", action="store_true",
                   help="manually instrument tool execution")
    p.add_argument("--dump", metavar="FILE",
                   help="write every span as JSON, full values")
    p.add_argument("--full", action="store_true",
                   help="do not abbreviate values in the tree")
    args = p.parse_args()

    INSTRUMENT_TOOLS = args.instrument_tools
    model = args.model or MODELS.get(args.provider, "fake-model")

    inspector = setup_tracing(args.raw)
    inspector.meta = {
        # UTC timestamp of the run. The dumps are evidence; evidence without a
        # date proves less. Added after the fact - dumps produced before this
        # change carry no timestamp.
        "run_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "provider": args.provider,
        "model": model,
        "instrument_tools": INSTRUMENT_TOOLS,
        "capture_content": os.environ.get(
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "(unset)"),
        "semconv_opt_in": os.environ.get("OTEL_SEMCONV_STABILITY_OPT_IN", ""),
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE if args.provider in ("openai", "ollama") else None,
        "anthropic_effort": ANTHROPIC_EFFORT if args.provider == "anthropic" else None,
    }

    scenario_failed = False
    try:
        RUNNERS[args.provider](model)
    except ImportError as e:
        print(f"Missing dependency: {e}", file=sys.stderr)
        print("\nExpected import paths (packages from the opentelemetry-python-genai repo):",
              file=sys.stderr)
        print("  opentelemetry.instrumentation.genai.anthropic", file=sys.stderr)
        print("  opentelemetry.instrumentation.genai.openai", file=sys.stderr)
        print("\nCheck the installation:", file=sys.stderr)
        print("  pip install opentelemetry-instrumentation-genai-anthropic "
              "opentelemetry-instrumentation-genai-openai", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[!] The scenario failed: {e}", file=sys.stderr)
        print("[!] The report covers what was traced before the failure.\n",
              file=sys.stderr)
        # The partial report is still printed and dumped - it is diagnostic.
        # But the exit code must say the run failed, otherwise a caller cannot
        # tell a real run from a run that never reached the provider, and will
        # publish worthless dumps over good ones.
        scenario_failed = True

    print()
    if args.tree:
        print(inspector.dump_tree(truncate=None if args.full else 50))
        print()
    print(inspector.report())

    print(f"\nPayments actually issued by the scenario: {PAYMENTS}")
    if not INSTRUMENT_TOOLS and PAYMENTS:
        print("  ^ no span attests these payments. Re-run with "
              "--instrument-tools to see the difference.")

    if args.dump:
        inspector.dump_json(args.dump)
        print(f"\nFull spans written to {args.dump}")

    if scenario_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
