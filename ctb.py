#!/usr/bin/env python3
"""ctb: deterministic checks for Drata Custom Test JSON. Standard library only, one file, safe to paste into a code sandbox.

The LLM writes the JSON. This file proves it, with no model involved:

  facts   render a JSON Schema as the compact fact table the prompt expects
  prompt  fill PROMPT.md with that fact table, ready to use as a system prompt
  check   static check against your schema: facts, operators, value types, enums, limits, wrapper mistakes
  test    run sample records through filter + condition and compare with what you expected (pass / fail / skip)

Input for check and test is either the builder's whole answer (--answer, '-' for stdin) or the two blobs
(--condition, optionally --filter and --mode). Exit code: 0 clean, 1 findings, 2 usage problem.

  python3 ctb.py facts  --schema schema.json
  python3 ctb.py check  --schema schema.json --answer answer.md
  python3 ctb.py test   --answer answer.md --cases cases.json

cases.json is a list of {"name": "...", "record": {...}, "expect": "pass" | "fail" | "skip"}. Records need only the fields the
test reads. Date strings may be written relative to now: "$now", "$days_ago(5)", "$days_ahead(3)", "$hours_ago(2)".
Semantics (operators, dates, empty arrays, thresholds) mirror Drata's rules engine; see README.md.
"""
import argparse
import difflib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

DATE_OPS = {"isBefore", "isAfter", "withinLastDays", "withinNextDays", "withinLastHours", "olderThanDays"}
NESTED = {"all", "any", "none", "notAll"}
OPS = {
    "string": {"equal", "notEqual", "in", "notIn", "contains", "doesNotContain", "startsWith", "endsWith", "exist", "empty"} | DATE_OPS,
    "number": {"equal", "notEqual", "in", "notIn", "lessThan", "lessThanOrEqual", "greaterThan", "greaterThanOrEqual", "exist"} | DATE_OPS,
    "boolean": {"equal", "notEqual", "in", "notIn", "exist"},
    "null": {"equal", "notEqual", "exist"},
    "object": {"equal", "notEqual", "empty", "exist"},
    "array": {"contains", "doesNotContain", "intersectsAny", "intersectsAll", "exist", "empty"} | NESTED,
}
ALL_OPS = set().union(*OPS.values())
LITERAL = {"string", "boolean", "number", "null"}
LITERAL_TYPE_OPS = {"equal", "notEqual", "contains", "doesNotContain", "in", "notIn", "intersectsAny", "intersectsAll"}
VALUE_KINDS = {
    **{op: LITERAL | {"fact"} for op in ("equal", "notEqual", "contains", "doesNotContain")},
    **{op: {"array"} for op in ("in", "notIn")},
    **{op: {"array", "fact"} for op in ("intersectsAny", "intersectsAll")},
    **{op: {"number", "fact"} for op in ("lessThan", "lessThanOrEqual", "greaterThan", "greaterThanOrEqual",
                                        "withinLastDays", "withinNextDays", "withinLastHours", "olderThanDays")},
    "exist": {"boolean", "fact"}, "empty": {"boolean", "fact"},
    "startsWith": {"string", "fact"}, "endsWith": {"string", "fact"},
    "isBefore": {"string", "number", "fact"}, "isAfter": {"string", "number", "fact"},
    **{op: {"condition"} for op in NESTED},
}
ALIASES = {
    "equals": "equal", "eq": "equal", "==": "equal", "exists": "exist", "ne": "notEqual", "not_equal": "notEqual",
    "gt": "greaterThan", ">": "greaterThan", "gte": "greaterThanOrEqual", ">=": "greaterThanOrEqual",
    "lt": "lessThan", "<": "lessThan", "lte": "lessThanOrEqual", "<=": "lessThanOrEqual", "not_in": "notIn",
    "not_contains": "doesNotContain", "notcontains": "doesNotContain",
}
ASSERTIONS = {"all results must pass": "nofail", "at least one result must pass": "hasonepass",
              "one failing result tolerated": "failonce"}
MAX_LINES = 70
MAX_RULES = 7
MARKERS = ("=================================== BEGIN PROMPT ===================================",
           "=================================== END PROMPT ===================================")


def die(message, code=2):
    print(message, file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------- schema helpers

def base_types(prop):
    raw = prop.get("type", [])
    raw = raw if isinstance(raw, list) else [raw]
    return {"number" if t == "integer" else t for t in raw}


def operators_for(prop):
    types = base_types(prop)
    return set().union(*(OPS[t] for t in types if t in OPS)) if types else None  # None = type unknown, cannot check


def type_str(prop):
    raw = prop.get("type", [])
    raw = raw if isinstance(raw, list) else [raw]
    ordered = [t for t in raw if t != "null"] + (["null"] if "null" in raw else [])
    text = "|".join(ordered) or "any"
    items = prop.get("items") or {}
    if "array" in raw:
        if items.get("properties"):
            inner = ", ".join(f"{k}: {type_str(v)}" for k, v in items["properties"].items())
            text = text.replace("array", "array of object { %s }" % inner)
        else:
            text = text.replace("array", "array of " + type_str(items))
    if "object" in raw and prop.get("properties"):
        text = text.replace("object", "object { %s }" % ", ".join(f"{k}: {type_str(v)}" for k, v in prop["properties"].items()))
    if prop.get("enum"):
        text += " (values: %s)" % ", ".join(str(v) for v in prop["enum"] if v is not None)
    if prop.get("format") in ("date-time", "date"):
        text += " (%s)" % prop["format"]
    return text


def fact_table(schema):
    lines = []
    for name, prop in schema.get("properties", {}).items():
        note = (prop.get("description") or "").strip().replace("\n", " ")
        lines.append(f"{name}: {type_str(prop)}" + (f" - {note[:100]}" if note else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- input parsing

def read_text(path):
    return sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()


def from_answer(text):
    blobs = re.findall(r"```json\s*(.*?)```", text, flags=re.S)
    if len(blobs) not in (1, 2):
        die(f"answer: expected 1 or 2 json blocks (condition, or filter then condition), found {len(blobs)}")
    mode = re.search(r"click \*\*(Inclusion|Exclusion)\*\*", text, flags=re.I)
    threshold = re.search(r"Evaluation threshold:\*\*\s*(.+)", text, flags=re.I)
    return {"filter": blobs[0] if len(blobs) == 2 else None, "condition": blobs[-1],
            "mode": mode.group(1).lower() if mode else None,
            "threshold": threshold.group(1).strip().rstrip(".") if threshold else None}


def load_inputs(args):
    if bool(args.answer) == bool(args.condition):
        die("give either --answer, or --condition (with optional --filter)")
    if args.answer:
        found = from_answer(read_text(args.answer))
    else:
        found = {"filter": read_text(args.filter) if args.filter else None, "condition": read_text(args.condition)}
    found["mode"] = getattr(args, "mode", None) or found.get("mode") or "inclusion"
    name = (getattr(args, "threshold", None) or found.get("threshold") or "all results must pass").strip().lower()
    found["assertion"] = ASSERTIONS.get(name, name)
    if found["assertion"] not in ASSERTIONS.values():
        die(f"unknown threshold {name!r}; use one of {sorted(ASSERTIONS)} or {sorted(ASSERTIONS.values())}")
    return found


# --------------------------------------------------------------------------- static check

class Report:
    def __init__(self):
        self.items = []
        self.scope = "condition"

    def add(self, level, where, message, hint=None):
        self.items.append({"level": level, "where": where, "message": message, "hint": hint})

    def of(self, level):
        return [i for i in self.items if i["level"] == level]


def json_kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        if "fact" in value and set(value) <= {"fact", "path"}:
            return "fact"
        return "condition" if ({"all", "any"} & set(value) or {"fact", "operator"} <= set(value)) else "object"
    return "object"


def closest(word, options):
    return difflib.get_close_matches(str(word), sorted(options), n=2, cutoff=0.6)


def quoted_hint(value, types):
    if isinstance(value, str) and value.lower() in ("true", "false") and "boolean" in types:
        return "booleans are JSON true/false, not quoted strings"
    if isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?", value) and "number" in types:
        return "numbers are JSON numbers, not quoted strings"
    return None


def suggest_operator(op):
    if not isinstance(op, str):
        return None
    by_lower = {o.lower(): o for o in ALL_OPS}
    guess = ALIASES.get(op.lower()) or by_lower.get(op.lower().replace("_", "")) or next(iter(closest(op, ALL_OPS)), None)
    return f"did you mean {guess!r}?" if guess else None


def check_group(node, props, where, rep, stats):
    if not isinstance(node, dict):
        rep.add("error", where, "must be an object holding all or any")
        return
    keys = set(node)
    if {"mode", "evaluator"} <= keys:
        rep.add("error", where, 'wrapped in {"mode","evaluator"}', "paste only the inner group; Inclusion/Exclusion is a UI button")
        return
    groups = keys & {"all", "any"}
    if len(groups) != 1:
        rep.add("error", where, f'a group has exactly one of "all" or "any", found keys {sorted(keys)}')
        return
    if keys - groups:
        rep.add("error", where, f"extra keys {sorted(keys - groups)} next to the group; the editor takes a bare group")
    kind = next(iter(groups))
    items = node[kind]
    if not isinstance(items, list) or not items:
        rep.add("error", where, f'"{kind}" needs a non-empty list')
        return
    for number, item in enumerate(items, 1):
        label = f"{where} > {kind}[{number}]"
        if isinstance(item, dict) and ({"all", "any"} & set(item)):
            check_group(item, props, label, rep, stats)
        else:
            check_rule(item, props, label, rep, stats)


def check_rule(rule, props, where, rep, stats):
    if not isinstance(rule, dict):
        rep.add("error", where, "a rule must be an object")
        return
    if set(rule) - {"fact", "operator", "value", "path"}:
        rep.add("error", where, f"unknown keys {sorted(set(rule) - {'fact', 'operator', 'value', 'path'})}")
    missing = [k for k in ("fact", "operator", "value") if k not in rule]
    if missing:
        rep.add("error", where, f"missing {missing}")
        return
    fact, op, path, value = rule["fact"], rule["operator"], rule.get("path"), rule["value"]
    if fact not in props:
        rep.add("error", where, f"unknown fact {fact!r}", ("did you mean " + " or ".join(map(repr, closest(fact, props)))) if closest(fact, props) else None)
        return
    prop = props[fact]
    effective = prop
    if path is not None:
        types = base_types(prop)
        if path == "length":
            if "array" not in types:
                rep.add("error", where, f'"path": "length" only works on arrays; {fact} is {"|".join(sorted(types))}')
                return
            effective = {"type": "number"}
        elif "object" not in types:
            rep.add("error", where, f'"path" works on object fields (or "length" on arrays); {fact} is {"|".join(sorted(types))}',
                    "flatten it into a derived field if you need a nested value")
            return
        elif not prop.get("properties"):
            rep.add("warning", where, f"schema lists no properties for object {fact}; cannot verify path {path!r}")
            effective = {}
        elif path not in prop["properties"]:
            rep.add("error", where, f"unknown property {path!r} inside {fact}", "did you mean " + " or ".join(map(repr, closest(path, prop["properties"]))) if closest(path, prop["properties"]) else None)
            return
        else:
            effective = prop["properties"][path]
    if stats is not None:
        stats["rules"] += 1
        stats["facts"].add(fact)
    if op not in ALL_OPS:
        rep.add("error", where, f"unknown operator {op!r}", suggest_operator(op))
        return
    types, allowed = base_types(effective), operators_for(effective)
    if allowed is not None and op not in allowed:
        rep.add("error", where, f"operator {op!r} is not offered for {fact} ({'|'.join(sorted(types))})",
                "use one of: " + ", ".join(sorted(allowed)))
        return
    kind = json_kind(value)
    if kind not in VALUE_KINDS[op]:
        rep.add("error", where, f"value for {op!r} must be {' or '.join(sorted(VALUE_KINDS[op]))}, got {kind}", quoted_hint(value, types))
        return
    if op in NESTED:
        item_props = (effective.get("items") or {}).get("properties")
        if not item_props:
            rep.add("error", where, f"{fact} has no item properties in the schema; {op!r} needs an array of objects")
            return
        group = value if ({"all", "any"} & set(value)) else {"all": [value]}
        check_group(group, item_props, f"{where} > {op}", rep, None)
        return
    if kind == "fact":
        if value["fact"] not in props:
            rep.add("error", where, f"value refers to unknown fact {value['fact']!r}")
        return
    check_literals(op, fact, effective, value, kind, where, rep)
    if rep.scope == "condition" and "null" in types and op not in ("exist", "empty") and value is not None:
        # Ask the engine itself what null does here: notEqual and notIn are TRUE for null, most other operators are false.
        outcome = evaluate(rule, {}, datetime.now(timezone.utc))
        rep.add("note", where, f"{fact} can be null; a null value makes this rule {'TRUE' if outcome else 'false'}",
                "add an exist rule if null should fail" if outcome else None)


def check_literals(op, fact, prop, value, kind, where, rep):
    types = base_types(prop)
    items = prop.get("items") or {}
    element = base_types(items) if op in ("intersectsAny", "intersectsAll") or (op in ("contains", "doesNotContain") and "array" in types) else types
    values = value if kind == "array" else [value]
    enum = items.get("enum") if element is not types else prop.get("enum")
    # Counts and dates (withinLastDays 90, greaterThan 5, isAfter "now") are not values OF the field, so only the
    # membership-style operators compare the literal's type and allowed values with the field's.
    for v in values if op in LITERAL_TYPE_OPS else []:
        k = json_kind(v)
        if element and k not in element and not (k == "null" and ("null" in types or op in ("equal", "notEqual"))):
            rep.add("error", where, f"{v!r} is {k} but {fact} holds {'|'.join(sorted(element))}", quoted_hint(v, element))
        elif enum and k != "null" and v not in enum:
            rep.add("error", where, f"{v!r} is not an allowed value of {fact}", "allowed: " + ", ".join(str(e) for e in enum if e is not None))
    if op in DATE_OPS and isinstance(value, str) and value != "now" and parse_date(value, datetime.now(timezone.utc)) is None:
        rep.add("warning", where, f"{value!r} does not parse as an ISO-8601 date")
    if op in DATE_OPS and prop.get("format") not in ("date-time", "date") and "string" in types:
        rep.add("note", where, f"{fact} is not declared date-time in the schema; make sure it holds ISO-8601 text")
    if op in ("withinLastDays", "withinNextDays", "withinLastHours", "olderThanDays") and isinstance(value, (int, float)) and value < 0:
        rep.add("warning", where, f"negative number for {op!r}")
    if op == "empty" and not (types & {"string", "array", "object"}):
        rep.add("warning", where, f"'empty' is true for any number or boolean, so it says nothing about {fact}")


def check_blob(label, text, props, rep):
    rep.scope = label
    lines = len(text.strip().splitlines())
    if lines > MAX_LINES:
        rep.add("error", label, f"{lines} lines; Drata's editor allows {MAX_LINES} per block", "write one rule per line")
    try:
        node = json.loads(text)
    except json.JSONDecodeError as err:
        rep.add("error", label, f"not valid JSON: {err}")
        return None, set()
    stats = {"rules": 0, "facts": set()}
    check_group(node, props, label, rep, stats)
    if stats["rules"] > MAX_RULES:
        rep.add("warning", label, f"{stats['rules']} rules; more than {MAX_RULES} per block has not been verified in Drata's editor",
                "split into two tests or add a derived field")
    return node, stats["facts"]


def run_check(schema, filter_text, condition_text):
    props, rep = schema.get("properties", {}), Report()
    _, filter_facts = check_blob("filter", filter_text, props, rep) if filter_text else (None, set())
    _, condition_facts = check_blob("condition", condition_text, props, rep)
    overlap = filter_facts & condition_facts
    if overlap:
        rep.add("warning", "filter", f"{sorted(overlap)} appear in both filter and condition",
                "the filter only picks which records are scored; the pass/fail check belongs in the condition")
    return rep


# --------------------------------------------------------------------------- evaluation (mirrors Drata's rules engine)

def parse_date(value, now):
    if value == "now":
        return now
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if isinstance(value, str):
        text = re.sub(r"(\.\d{6})\d+", r"\1", re.sub(r"Z$", "+00:00", value.strip()))
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def midnight(moment):
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def same(a, b):
    return isinstance(a, bool) == isinstance(b, bool) and a == b


def is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def lodash_empty(x):
    return x is None or isinstance(x, (bool, int, float)) or (isinstance(x, (str, list, dict)) and not x)


def get_fact(record, ref):
    value = record.get(ref["fact"]) if isinstance(record, dict) else None
    path = ref.get("path")
    if path is None:
        return value
    if path == "length":
        return len(value) if isinstance(value, list) else None
    return value.get(path) if isinstance(value, dict) else None


def date_compare(op, actual, want, now):
    if op in ("isBefore", "isAfter"):
        a, b = parse_date(actual, now), parse_date(want, now)
        return a is not None and b is not None and (a < b if op == "isBefore" else a > b)
    if isinstance(actual, bool) or not isinstance(actual, (str, int, float)):
        return False
    moment = parse_date(actual, now)
    if moment is None:
        return False
    today = midnight(now)
    if op == "withinLastDays":
        return today - timedelta(days=want) <= midnight(moment) <= today
    if op == "withinNextDays":
        return today <= midnight(moment) <= today + timedelta(days=want)
    if op == "withinLastHours":
        return now - timedelta(hours=want) <= moment <= now
    return midnight(moment) < today - timedelta(days=want)  # olderThanDays: strictly older


def evaluate(node, record, now):
    if "all" in node:
        return all(evaluate(n, record, now) for n in node["all"])
    if "any" in node:
        return any(evaluate(n, record, now) for n in node["any"])
    actual, op, want = get_fact(record, node), node["operator"], node["value"]
    if op in NESTED:
        if not isinstance(actual, list):
            return False
        group = want if ("all" in want or "any" in want) else {"all": [want]}
        hits = [evaluate(group, item, now) for item in actual]
        return {"all": all(hits), "any": any(hits), "none": not any(hits), "notAll": bool(hits) and not all(hits)}[op]
    if isinstance(want, dict) and "fact" in want:
        want = get_fact(record, want)
    if op in ("equal", "notEqual"):
        return same(actual, want) == (op == "equal")
    if op == "exist":
        return (actual is not None) == want
    if op == "empty":
        return lodash_empty(actual) == want
    if op in ("in", "notIn"):
        return isinstance(want, list) and any(same(actual, v) for v in want) == (op == "in")
    if op in ("lessThan", "lessThanOrEqual", "greaterThan", "greaterThanOrEqual"):
        if not is_number(actual) or not is_number(want):
            return False
        return {"lessThan": actual < want, "lessThanOrEqual": actual <= want,
                "greaterThan": actual > want, "greaterThanOrEqual": actual >= want}[op]
    if op in ("contains", "doesNotContain"):
        if isinstance(actual, str):
            found = isinstance(want, str) and want in actual
        elif isinstance(actual, list):
            found = any(same(item, want) for item in actual)
        else:
            return False
        return found == (op == "contains")
    if op in ("startsWith", "endsWith"):
        return isinstance(actual, str) and isinstance(want, str) and (actual.startswith(want) if op == "startsWith" else actual.endswith(want))
    if op in ("intersectsAny", "intersectsAll"):
        if not isinstance(actual, list) or not isinstance(want, list):
            return False
        hit = [any(same(a, w) for w in want) for a in actual]
        return any(hit) if op == "intersectsAny" else all(hit)
    return date_compare(op, actual, want, now)


def in_scope(filter_node, mode, record, now):
    if filter_node is None:
        return True
    return evaluate(filter_node, record, now) == (mode == "inclusion")


def roll_up(assertion, results):
    if assertion == "hasonepass":
        return any(results)  # no records: fails
    if assertion == "failonce":
        return results.count(False) < 2
    return all(results)  # nofail; no records: passes


def failing_rules(node, record, now):
    out = []
    for item in node["all" if "all" in node else "any"]:
        if evaluate(item, record, now):
            continue
        if "all" in item or "any" in item:
            out += failing_rules(item, record, now)
        else:
            shown = repr(get_fact(record, item))
            out.append(f"{item['fact']}{'.' + item['path'] if 'path' in item else ''} = {shown[:60]} fails {item['operator']} {json.dumps(item['value'])[:60]}")
    return out


def expand(value, now):
    if isinstance(value, dict):
        return {k: expand(v, now) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, now) for v in value]
    if isinstance(value, str):
        m = re.fullmatch(r"\$(now|days_ago\((\d+)\)|days_ahead\((\d+)\)|hours_ago\((\d+)\))", value)
        if m:
            delta = (timedelta(0) if m.group(1) == "now" else -timedelta(days=int(m.group(2))) if m.group(2)
                     else timedelta(days=int(m.group(3))) if m.group(3) else -timedelta(hours=int(m.group(4))))
            return (now + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


def run_test(filter_node, mode, condition, assertion, cases, now, require_coverage):
    rows, results = [], []
    for number, case in enumerate(cases, 1):
        record = expand(case.get("record", {}), now)
        if not in_scope(filter_node, mode, record, now):
            actual, why = "skip", []
        else:
            passed = evaluate(condition, record, now)
            results.append(passed)
            actual, why = ("pass", []) if passed else ("fail", failing_rules(condition, record, now))
        expect = case.get("expect")
        rows.append({"case": case.get("name") or f"case {number}", "expect": expect, "actual": actual, "why": why,
                     "match": expect is None or expect == actual})
    seen = {r["actual"] for r in rows}
    missing = [k for k in (("pass", "fail", "skip") if filter_node else ("pass", "fail")) if k not in seen]
    return rows, roll_up(assertion, results), missing if require_coverage else []


# --------------------------------------------------------------------------- commands

def load_schema(path):
    try:
        schema = json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        die(f"schema: {err}")
    if not isinstance(schema.get("properties"), dict):
        die("schema: expected a JSON Schema object with a top-level 'properties'")
    return schema


def cmd_facts(args):
    print(fact_table(load_schema(args.schema)))


def cmd_prompt(args):
    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(here, "PROMPT.md"), encoding="utf-8").read()
    if MARKERS[0] not in source or MARKERS[1] not in source:
        die("PROMPT.md: BEGIN/END PROMPT markers not found")
    body = source.split(MARKERS[0])[1].split(MARKERS[1])[0].strip("\n")
    block = fact_table(load_schema(args.schema)) + ("\n\nNotes:\n" + "\n".join(f"- {n}" for n in args.notes) if args.notes else "")
    body = re.sub(r"<schema>.*?</schema>", lambda _: f"<schema>\n{block}\n</schema>", body, flags=re.S)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(body + "\n")
        print(f"wrote {args.out} ({len(body)} characters)")
    else:
        print(body)


def cmd_check(args):
    found = load_inputs(args)
    rep = run_check(load_schema(args.schema), found["filter"], found["condition"])
    errors = rep.of("error")
    if args.json:
        print(json.dumps({"ok": not errors, "issues": rep.items}, indent=2))
    else:
        for level in ("error", "warning", "note"):
            for i in rep.of(level):
                print(f"{level.upper():7} {i['where']}: {i['message']}" + (f"  ({i['hint']})" if i["hint"] else ""))
        print("CHECK FAILED" if errors else f"CHECK OK  ({len(rep.of('warning'))} warnings, {len(rep.of('note'))} notes)")
    sys.exit(1 if errors else 0)


def cmd_test(args):
    found = load_inputs(args)
    try:
        condition = json.loads(found["condition"])
        filter_node = json.loads(found["filter"]) if found["filter"] else None
        raw = json.loads(read_text(args.cases))
    except (json.JSONDecodeError, OSError) as err:
        die(f"input: {err}")
    cases, expect_test = (raw["cases"], raw.get("expect_test")) if isinstance(raw, dict) else (raw, None)
    for number, case in enumerate(cases, 1):
        if case.get("expect") not in (None, "pass", "fail", "skip"):
            die(f"case {number}: expect must be pass, fail or skip")
    now = parse_date(args.now or os.environ.get("CTB_NOW") or "now", datetime.now(timezone.utc))
    if now is None:
        die("--now must be an ISO-8601 time")
    rows, test_passes, missing = run_test(filter_node, found["mode"], condition, found["assertion"], cases, now, not args.no_coverage)
    bad = [r for r in rows if not r["match"]]
    verdict_off = expect_test is not None and expect_test != ("pass" if test_passes else "fail")
    if args.json:
        print(json.dumps({"ok": not (bad or missing or verdict_off), "test_result": "pass" if test_passes else "fail",
                          "cases": rows, "missing_coverage": missing}, indent=2))
    else:
        for r in rows:
            print(f"{'ok      ' if r['match'] else 'MISMATCH'} {r['case']}: {r['actual']}" + ("" if r["match"] else f" (expected {r['expect']})"))
            for line in r["why"] if not r["match"] or r["actual"] == "fail" else []:
                print(f"           {line}")
        print(f"rolled-up result over these cases ({found['assertion']}): {'PASS' if test_passes else 'FAIL'}" + (f"  (expected {expect_test})" if verdict_off else ""))
        if missing:
            print(f"COVERAGE: add cases for {missing}; each needs at least one in-scope pass, one in-scope fail, and (with a filter) one out-of-scope record")
        print("TEST FAILED" if bad or missing or verdict_off else f"TEST OK  ({len(rows)} cases match)")
    sys.exit(1 if bad or missing or verdict_off else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def inputs(p, schema):
        if schema:
            p.add_argument("--schema", required=True, help="JSON Schema of the connection's resource")
        p.add_argument("--answer", help="builder answer file with its json blocks ('-' for stdin)")
        p.add_argument("--condition", help="condition JSON file")
        p.add_argument("--filter", help="filtering criteria JSON file")
        p.add_argument("--mode", choices=["inclusion", "exclusion"], help="filter mode (default inclusion)")
        p.add_argument("--threshold", help="threshold text, or nofail / hasonepass / failonce")
        p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("facts", help="print the compact fact table for a schema")
    p.add_argument("--schema", required=True)
    p.set_defaults(run=cmd_facts)
    p = sub.add_parser("prompt", help="fill PROMPT.md with a schema's fact table")
    p.add_argument("--schema", required=True)
    p.add_argument("--notes", action="append", default=[], help="one line of context about record types (repeatable)")
    p.add_argument("--out", help="write here instead of stdout")
    p.set_defaults(run=cmd_prompt)
    p = sub.add_parser("check", help="static check against the schema")
    inputs(p, True)
    p.set_defaults(run=cmd_check)
    p = sub.add_parser("test", help="run sample records through the test")
    inputs(p, False)
    p.add_argument("--cases", required=True, help="cases.json")
    p.add_argument("--now", help="freeze 'now' (ISO-8601) for reproducible date checks; also CTB_NOW")
    p.add_argument("--no-coverage", action="store_true", help="do not require pass, fail and out-of-scope cases")
    p.set_defaults(run=cmd_test)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
