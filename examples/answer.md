**Test name:** API Keys Rotated Within 90 Days
**Description:** Every API key was created in the last 90 days.
**Evaluation threshold:** All results must pass
**Checks:** PASS: `keyCreatedAt` is within the last 90 days. FAIL: older, or missing.
**Assumptions:**
- A key with no `keyCreatedAt` (null) fails.

**1. Filtering criteria** - click **Inclusion**, then paste into the Advanced editor:
```json
{"all":[{"fact":"evidenceType","operator":"equal","value":"api_key"}]}
```

**2. Condition group** - paste into the Advanced editor:
```json
{"all":[{"fact":"keyCreatedAt","operator":"withinLastDays","value":90}]}
```

**Before publishing:** save as draft and run it. Confirm the number of in-scope results is above 0. A filter that matches nothing makes the test pass by default.
