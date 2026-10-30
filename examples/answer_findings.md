**Test name:** No Open Critical Finding Older Than 30 Days
**Description:** No open critical finding was first seen more than 30 days ago, and the data was refreshed in the last 2 days.
**Evaluation threshold:** All results must pass
**Checks:** PASS: fresh data and no open critical finding older than 30 days. FAIL: stale data, or any such finding.

**1. Filtering criteria** - click **Inclusion**, then paste into the Advanced editor:
```json
{"all":[{"fact":"evidenceType","operator":"equal","value":"finding_summary"}]}
```

**2. Condition group** - paste into the Advanced editor:
```json
{"all":[
  {"fact":"generatedAt","operator":"withinLastDays","value":2},
  {"fact":"findings","operator":"none","value":{"all":[
    {"fact":"open","operator":"equal","value":true},
    {"fact":"severity","operator":"equal","value":"critical"},
    {"fact":"firstSeen","operator":"olderThanDays","value":30}
  ]}}
]}
```
