#!/usr/bin/env python3
"""
Compares two span dumps produced by agent_bench.py --dump.

Output: the divergence table between providers. It is both the raw material of
the write-up and the functional specification of the span processor, whose
whole purpose is to normalise these variants.

Usage:
    python agent_bench.py --provider anthropic --dump anthropic.json
    python agent_bench.py --provider openai    --dump openai.json
    python compare.py anthropic.json openai.json
"""

import json
import sys


# Attributes whose absence has a forensic consequence, and which one.
FORENSIC_WEIGHT = {
    "gen_ai.tool.definitions":
        "we do not know what the agent was ALLOWED to do at the time",
    "gen_ai.tool.name":
        "we do not know which tool was called",
    "gen_ai.tool.call.id":
        "cannot link the execution back to the model decision",
    "gen_ai.tool.call.arguments":
        "we know an action happened, not which one",
    "gen_ai.tool.call.result":
        "we do not know whether the action succeeded",
    "gen_ai.output.messages":
        "the reasoning and the requested calls are not retained",
    "gen_ai.input.messages":
        "the context given to the model is not retained",
    "gen_ai.conversation.id":
        "cannot group the actions of a single session",
    "gen_ai.system_instructions":
        "we do not know under which instructions the agent acted",
}


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def attr_set(dump):
    s = set()
    for span in dump["spans"]:
        s.update(span["attributes"].keys())
    return s


# Meta keys directly tied to a gen_ai attribute. If check_symmetry() flags them
# as asymmetric (a different value sent depending on the run), the corresponding
# attribute is NOT an SDK divergence - it is removed from the ATTRIBUTES table
# so it cannot be mistaken for a real finding.
SYMMETRY_ATTR_MAP = {
    "max_tokens": "gen_ai.request.max_tokens",
    "temperature": "gen_ai.request.temperature",
}


def check_symmetry(a, b):
    """Checks that the two runs are comparable. Otherwise the divergences
    observed come from the scenario, not from the SDKs.

    Returns (messages, attrs_to_exclude): the messages to print as-is, and the
    set of gen_ai attributes to drop from the divergence table because they
    follow from this scenario asymmetry.
    """
    warnings = []
    exclude = set()
    for key in ("instrument_tools", "capture_content", "max_tokens", "temperature"):
        va, vb = a["meta"].get(key), b["meta"].get(key)
        if va != vb:
            warnings.append(f"{key} : {va!r} vs {vb!r}")
            if key in SYMMETRY_ATTR_MAP:
                exclude.add(SYMMETRY_ATTR_MAP[key])
    return warnings, exclude


def structure(dump):
    spans = dump["spans"]
    traces = {s["trace_id"] for s in spans}
    roots = [s for s in spans if s["parent"] is None]
    tools = [s for s in spans
             if s["attributes"].get("gen_ai.operation.name") == "execute_tool"]
    return {
        "spans": len(spans),
        "traces": len(traces),
        "roots": len(roots),
        "actions": len(tools),
        "names": sorted({s["name"].split()[0] for s in spans}),
    }


def find_tool_call_payload(dump):
    """Where does the tool call and its arguments actually live?

    This is the central question: the same information can exist in different
    places depending on the provider, and be absent from the location the spec
    normalises.
    """
    found = []
    for span in dump["spans"]:
        for k, v in span["attributes"].items():
            if k == "gen_ai.tool.call.arguments":
                found.append((k, "normalised location", str(v)[:80]))
            elif k in ("gen_ai.output.messages", "gen_ai.input.messages"):
                try:
                    parsed = json.loads(v) if isinstance(v, str) else v
                except (json.JSONDecodeError, TypeError):
                    continue
                for msg in parsed if isinstance(parsed, list) else []:
                    for part in msg.get("parts", []) if isinstance(msg, dict) else []:
                        if isinstance(part, dict) and "arguments" in part:
                            found.append((
                                k,
                                f"nested in parts[].{'/'.join(sorted(part))}",
                                json.dumps(part.get("arguments"))[:80],
                            ))
    return found


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)

    a, b = load(sys.argv[1]), load(sys.argv[2])
    na = a["meta"].get("provider", sys.argv[1])
    nb = b["meta"].get("provider", sys.argv[2])
    if na == nb:                      # same provider: disambiguate
        na = f"{na}<{sys.argv[1].split('/')[-1]}>"
        nb = f"{nb}<{sys.argv[2].split('/')[-1]}>"

    print("=" * 74)
    print(f"DIVERGENCES  {na}  vs  {nb}")
    print("=" * 74)

    # --- validity check ---
    warn, exclude = check_symmetry(a, b)
    if warn:
        print("\n/!\\ RUNS NOT COMPARABLE - the gaps below are polluted:")
        for w in warn:
            print(f"    {w}")
        print("    Re-run both with the same settings.\n")
    else:
        print(f"\nRuns comparable (content capture: "
              f"{a['meta'].get('capture_content')}, "
              f"instrumented tools: {a['meta'].get('instrument_tools')}).\n")

    # --- structure ---
    sa, sb = structure(a), structure(b)
    print("-" * 74)
    print("STRUCTURE")
    print(f"{'':24} {na:>22} {nb:>22}")
    for k in ("spans", "traces", "roots", "actions"):
        flag = "  <<" if sa[k] != sb[k] else ""
        print(f"{k:24} {sa[k]:>22} {sb[k]:>22}{flag}")
    print(f"{'span types':24} {','.join(sa['names']):>22} {','.join(sb['names']):>22}")

    if sa["traces"] > 1 or sb["traces"] > 1:
        print("\n  Multiple root traces: the turns of a single claim are not")
        print("  linked. With no enclosing agent span and no conversation.id,")
        print("  the session is shattered.")

    # --- attributes ---
    aa, ab = attr_set(a), attr_set(b)
    print("\n" + "-" * 74)
    print("ATTRIBUTES")

    excluded_here = exclude & (aa ^ ab)
    if excluded_here:
        print(f"\n  ({len(excluded_here)} attribute(s) removed from the table, already "
              f"reported as a scenario asymmetry above: "
              f"{', '.join(sorted(excluded_here))})")

    only_a = sorted((aa - ab) - exclude)
    only_b = sorted((ab - aa) - exclude)

    def show(keys, present, absent):
        if not keys:
            return
        print(f"\n  Present on {present}, ABSENT on {absent}:")
        for k in keys:
            note = FORENSIC_WEIGHT.get(k)
            mark = " [!]" if note else ""
            print(f"    {k}{mark}")
            if note:
                print(f"        -> {note}")

    show(only_a, na, nb)
    show(only_b, nb, na)

    if not only_a and not only_b:
        print("  No attribute divergence.")

    print(f"\n  Common: {len(aa & ab)} attributes.")

    # --- where the call arguments live ---
    print("\n" + "-" * 74)
    print("WHERE THE TOOL CALL ARGUMENTS LIVE")
    for dump, name in ((a, na), (b, nb)):
        hits = find_tool_call_payload(dump)
        print(f"\n  {name}:")
        if not hits:
            print("    not found - neither normalised nor nested.")
        seen = set()
        for attr, where, excerpt in hits:
            key = (attr, where)
            if key in seen:
                continue
            seen.add(key)
            print(f"    {attr}")
            print(f"        {where}")
            print(f"        {excerpt}")

    print("\n" + "=" * 74)
    print("Every [!] line is a piece of security information lost on one")
    print("provider and retained on the other. Every different location for")
    print("the same information is a rule to write into the span processor's")
    print("normalisation table.")


if __name__ == "__main__":
    main()
