# customTestBuilder

Describe a Drata Custom Test in plain English. Get the two JSON blobs (filter and condition) for the Advanced editor, checked by code before you paste them.

The idea: the model is good at turning words into JSON and bad at being sure the JSON is right. Code is the opposite. So the model drafts, and deterministic code proves.

```
 plain English ──► LLM + PROMPT.md ──► filter.json + condition.json
                                              │
              ┌───────────────────────────────┤  the LLM runs these itself (or you do)
              ▼                               ▼
   ctb.py check                       ctb.py test
   static: every fact, operator,      behavioural: sample records go through the
   value type and enum is valid       test; actual pass/fail/skip must equal what the
   for YOUR schema                    LLM expected from your words
              └───────────────┬───────────────┘
                              ▼
                   validate_recipe.py  ──►  Drata's own validator, against your real connection
                              ▼
                       paste into Drata
```

| File | Role |
|---|---|
| `PROMPT.md` | System prompt. Teaches the grammar, the two-blob shape, the traps (check in the filter, null fails, empty scope passes), and when to ask. Section 13 makes the model self-check. |
| `ctb.py` | Offline, stdlib-only. `facts` / `prompt` build the prompt from your schema. `check` and `test` prove the JSON. Paste it into any code sandbox as-is. |
| `validate_recipe.py` | Online. Sends the answer to Drata's validate-recipe endpoint. Validate only, creates nothing. |
| `examples/` | A schema, two builder answers and their test cases. |
| `tests/` | 37 unit tests. `python3 -m unittest discover -s tests` |

## Use

```bash
# 1. build the system prompt from your connection's JSON Schema
python3 ctb.py prompt --schema schema.json --notes "evidenceType separates record types" --out system_prompt.md

# 2. chat with any model using system_prompt.md; save its answer as answer.md

# 3. prove it, no model involved
python3 ctb.py check --schema schema.json --answer answer.md
python3 ctb.py test  --answer answer.md --cases cases.json

# 4. ask Drata (needs DRATA_API_KEY, optional DRATA_REGION=us|eu|apac)
python3 validate_recipe.py --workspace-id 1 --provider "My Connection" --resource "Employees" --answer answer.md
```

Three ways to run it, from lightest to strongest:

1. **Chat only.** Prompt in, JSON out. The model self-checks by reading (section 10).
2. **Agentic.** Give the model `ctb.py` and your schema. Section 13 has it write test cases from your words, run `check` and `test`, and fix its JSON until both pass. Its verdict is the code's, not its own.
3. **Human gate.** You run `validate_recipe.py` before pasting.

## What `ctb.py` proves

- **check** (static): unknown facts and operators (with "did you mean"), operator not offered for the field's type, wrong value types (quoted booleans), values outside an enum, item facts of arrays of objects, `path` use, wrapper mistakes, the 70-line editor limit, and a compliance check hiding in the filter.
- **test** (behavioural): runs records through filter and condition using Drata's rules-engine semantics (UTC calendar-day date windows, strict `olderThanDays`, `all`/`none` pass and `any`/`notAll` fail on empty arrays, null handling, inclusion vs exclusion, the three evaluation thresholds). It requires an in-scope pass, an in-scope fail and an out-of-scope case, so a test that can never fail or never apply does not get through. Dates can be relative (`"$days_ago(5)"`).

Both exit 0 when clean, 1 on findings, so they work as gates in a loop or CI.

## Checked so far

- 37 unit tests pass.
- `check` passes all 22 custom tests in the OCI, Vipr and Sophos connectors.
- Agentic mode, two runs with a small model (one on a Vipr-style schema, one on a generic schema): both wrote test cases from the request first, then ran `check` and `test`. One of them caught a real mistake: its first draft let a null value pass a `notEqual` rule, `test` flagged it, and it added an `exist` rule. `check` now warns about that case up front. Two runs is a smoke test, not an eval.
- `validate_recipe.py` has only been run against a local mock of the endpoint. Its route and body shape come from Drata's public API source. The first real run confirms it.
- The 7-rule warning in `check` comes from earlier testing; Drata's docs state only the 70-line limit.

Maintained by the Drata Solutions Architect team.
