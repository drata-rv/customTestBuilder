#!/usr/bin/env python3
"""Check Custom Test JSON against Drata's own recipe validator. Validate only: nothing is created or changed.

Calls POST /workspaces/{workspaceId}/monitoring-tests/action-validate-recipe, which checks the JSON against the real
Custom Connection (connection name, resource name, every fact, operator and value type) and returns each error
with its location.

Feed it the builder's answer as-is:

    DRATA_API_KEY=... python3 validate_recipe.py --workspace-id 1 --provider "My Connection" \
        --resource "Employees" --answer answer.md

or the two blobs separately (filter optional):

    python3 validate_recipe.py --workspace-id 1 --provider "My Connection" --resource "Employees" \
        --condition condition.json --filter filter.json --mode inclusion

--provider is the Custom Connection's name, --resource the resource name, both exactly as shown in Drata.
Credentials come from the environment only: DRATA_API_KEY (Bearer token). Region: DRATA_REGION=us|eu|apac (default us).
DRATA_BASE_URL overrides the host; plain http is accepted for localhost only.

Exit code: 0 valid, 1 Drata reports errors, 2 usage, auth or network problem.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASES = {
    "us": "https://public-api.drata.com/public/v2",
    "eu": "https://public-api.eu.drata.com/public/v2",
    "apac": "https://public-api.apac.drata.com/public/v2",
}
ASSERTIONS = {
    "all results must pass": "nofail",
    "at least one result must pass": "hasonepass",
    "one failing result tolerated": "failonce",
}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def die(message, code=2):
    print(message, file=sys.stderr)
    sys.exit(code)


def read_text(path):
    return sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()


def load_blob(label, text):
    try:
        node = json.loads(text)
    except json.JSONDecodeError as err:
        die(f"{label}: not valid JSON ({err})")
    # The UI takes a bare group; the API wraps the filter itself. A wrapped blob is the usual paste mistake.
    if not isinstance(node, dict) or set(node) not in ({"all"}, {"any"}):
        keys = sorted(node) if isinstance(node, dict) else type(node).__name__
        die(f'{label}: must be a bare {{"all": [...]}} or {{"any": [...]}}, found {keys}')
    return node


def from_answer(text):
    blobs = re.findall(r"```json\s*(.*?)```", text, flags=re.S)
    if len(blobs) not in (1, 2):
        die(f"answer: expected 1 or 2 json blocks (condition, or filter then condition), found {len(blobs)}")
    mode = re.search(r"click \*\*(Inclusion|Exclusion)\*\*", text, flags=re.I)
    threshold = re.search(r"Evaluation threshold:\*\*\s*(.+)", text, flags=re.I)
    return {
        "filter": blobs[0] if len(blobs) == 2 else None,
        "condition": blobs[-1],
        "mode": mode.group(1).lower() if mode else None,
        "threshold": threshold.group(1).strip().rstrip(".") if threshold else None,
    }


def base_url():
    url = os.environ.get("DRATA_BASE_URL") or BASES.get(os.environ.get("DRATA_REGION", "us").lower())
    if not url:
        die("DRATA_REGION must be us, eu or apac")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS):
        die("refusing to send the API key over plain http to a non-local host")
    return url.rstrip("/")


def build_recipe(provider, resource, condition, filter_node, mode, assertion):
    entry = {"resource": resource, "evaluator": condition}
    if filter_node:
        entry["filteringCriteria"] = {"mode": mode, "evaluator": filter_node}
    return {
        "assertion": assertion,
        "allConnections": True,
        "providers": [{"provider": provider, "resources": [entry]}],
    }


def where(location):
    """'/providers/0/resources/0/evaluator/all/1/operator' -> 'Condition: all > rule 2 > operator'."""
    rest = re.sub(r"^/providers/\d+/resources/\d+/?", "", location or "")
    parts = [p for p in rest.split("/") if p]
    if not parts:
        return location or "(recipe)"
    label = "Filter" if parts[0] == "filteringCriteria" else "Condition" if parts[0] == "evaluator" else None
    if parts[0] == "filteringCriteria":
        parts = parts[1:]
    if parts and parts[0] == "evaluator":
        parts = parts[1:]
    steps = [f"rule {int(p) + 1}" if p.isdigit() else p for p in parts]
    return f"{label}: {' > '.join(steps)}" if label and steps else label or " > ".join(steps)


def post(url, key, body):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        hint = {
            401: "API key rejected. Check DRATA_API_KEY and DRATA_REGION.",
            403: "Key lacks permission. It needs the permission to create monitoring tests.",
            404: "Not found. Check --workspace-id and DRATA_REGION.",
        }.get(err.code, "")
        die(f"HTTP {err.code} from Drata. {hint}\n{err.read().decode(errors='replace')[:500]}")
    except urllib.error.URLError as err:
        die(f"network error: {err.reason}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace-id", required=True, type=int)
    parser.add_argument("--provider", required=True, help="Custom Connection name")
    parser.add_argument("--resource", required=True, help="resource name in that connection")
    parser.add_argument("--answer", help="builder answer file with its json blocks ('-' for stdin)")
    parser.add_argument("--condition", help="condition JSON file")
    parser.add_argument("--filter", help="filtering criteria JSON file")
    parser.add_argument("--mode", choices=["inclusion", "exclusion"], help="filter mode (default inclusion)")
    parser.add_argument("--threshold", help="evaluation threshold text, or nofail/hasonepass/failonce")
    parser.add_argument("--dry-run", action="store_true", help="print the request body and stop")
    args = parser.parse_args()

    if bool(args.answer) == bool(args.condition):
        die("give either --answer, or --condition (with optional --filter)")
    if args.answer:
        found = from_answer(read_text(args.answer))
    else:
        found = {"filter": read_text(args.filter) if args.filter else None, "condition": read_text(args.condition)}
    condition = load_blob("condition", found["condition"])
    filter_node = load_blob("filter", found["filter"]) if found.get("filter") else None

    mode = args.mode or found.get("mode") or "inclusion"
    threshold = (args.threshold or found.get("threshold") or "all results must pass").strip()
    assertion = ASSERTIONS.get(threshold.lower(), threshold.lower())
    if assertion not in ASSERTIONS.values():
        die(f"unknown threshold {threshold!r}; use one of {sorted(ASSERTIONS)} or {sorted(ASSERTIONS.values())}")

    body = {"recipe": build_recipe(args.provider, args.resource, condition, filter_node, mode, assertion)}
    if args.dry_run:
        print(json.dumps(body, indent=2))
        return

    key = os.environ.get("DRATA_API_KEY")
    if not key:
        die("set DRATA_API_KEY in the environment")
    result = post(f"{base_url()}/workspaces/{args.workspace_id}/monitoring-tests/action-validate-recipe", key, body)

    if result.get("isValid"):
        print(f"VALID  ({mode} filter, assertion {assertion})" if filter_node else f"VALID  (no filter, assertion {assertion})")
        return
    print("INVALID")
    for error in result.get("errors", []):
        print(f"  [{error.get('type', '?')}] {where(error.get('location'))}: {error.get('message', '')}")
    sys.exit(1)


if __name__ == "__main__":
    main()
