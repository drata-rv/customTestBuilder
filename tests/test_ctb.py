import importlib.util
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("ctb", os.path.join(ROOT, "ctb.py"))
ctb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctb)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
SCHEMA = json.load(open(os.path.join(ROOT, "examples", "schema.json")))


def ev(rule, record):
    return ctb.evaluate(rule, record, NOW)


def rule(fact, operator, value, **extra):
    return {"fact": fact, "operator": operator, "value": value, **extra}


def check(condition, filter_node=None):
    rep = ctb.run_check(SCHEMA, json.dumps(filter_node) if filter_node else None, json.dumps(condition))
    return [i["message"] for i in rep.of("error")], rep


class StaticCheck(unittest.TestCase):
    def test_valid_rule_passes(self):
        errors, _ = check({"all": [rule("versioning", "equal", "Enabled")]})
        self.assertEqual(errors, [])

    def test_date_count_on_date_field_is_not_a_type_error(self):
        errors, _ = check({"all": [rule("keyCreatedAt", "withinLastDays", 90)]})
        self.assertEqual(errors, [])

    def test_unknown_fact_suggests_closest(self):
        errors, rep = check({"all": [rule("versionin", "equal", "Enabled")]})
        self.assertTrue(any("unknown fact" in e for e in errors))
        self.assertIn("versioning", rep.of("error")[0]["hint"])

    def test_alias_operator_gets_a_hint(self):
        errors, rep = check({"all": [rule("versioning", "equals", "Enabled")]})
        self.assertTrue(any("unknown operator" in e for e in errors))
        self.assertIn("'equal'", rep.of("error")[0]["hint"])

    def test_operator_not_offered_for_type(self):
        errors, _ = check({"all": [rule("mfaEnabled", "contains", True)]})
        self.assertTrue(any("not offered" in e for e in errors))

    def test_quoted_boolean_and_number_hints(self):
        _, rep = check({"all": [rule("mfaEnabled", "equal", "true"), rule("openCriticalCount", "greaterThan", "5")]})
        hints = " ".join(i["hint"] or "" for i in rep.of("error"))
        self.assertIn("JSON true/false", hints)
        self.assertIn("JSON numbers", hints)

    def test_enum_violation(self):
        errors, _ = check({"all": [rule("versioning", "equal", "enabled")]})
        self.assertTrue(any("not an allowed value" in e for e in errors))

    def test_wrapper_is_rejected(self):
        errors, _ = check({"mode": "inclusion", "evaluator": {"all": [rule("versioning", "equal", "Enabled")]}})
        self.assertTrue(any("wrapped" in e for e in errors))

    def test_group_needs_exactly_one_of_all_any(self):
        errors, _ = check({"all": [rule("versioning", "equal", "Enabled")], "any": []})
        self.assertTrue(errors)

    def test_nested_array_facts_come_from_items(self):
        ok, _ = check({"all": [rule("findings", "none", {"all": [rule("open", "equal", True)]})]})
        bad, _ = check({"all": [rule("findings", "none", {"all": [rule("evidenceType", "equal", "bucket")]})]})
        self.assertEqual(ok, [])
        self.assertTrue(any("unknown fact" in e for e in bad))

    def test_path_length_and_object_property(self):
        ok, _ = check({"all": [rule("findings", "greaterThan", 0, path="length"),
                               rule("settings", "equal", "REGIONAL", path="availabilityType")]})
        bad, _ = check({"all": [rule("settings", "equal", "x", path="missing")]})
        self.assertEqual(ok, [])
        self.assertTrue(any("unknown property" in e for e in bad))

    def test_null_note_tells_the_truth_per_operator(self):
        _, rep = check({"all": [rule("versioning", "notEqual", "Disabled"), rule("keyCreatedAt", "withinLastDays", 90)]})
        notes = {n["where"]: n["message"] for n in rep.of("note") if "can be null" in n["message"]}
        self.assertIn("makes this rule TRUE", notes["condition > all[1]"])    # notEqual passes null
        self.assertIn("makes this rule false", notes["condition > all[2]"])   # date operators fail null

    def test_check_in_filter_warning(self):
        _, rep = check({"all": [rule("evidenceType", "equal", "bucket")]}, {"all": [rule("evidenceType", "equal", "bucket")]})
        self.assertTrue(any("both filter and condition" in w["message"] for w in rep.of("warning")))

    def test_line_limit(self):
        many = {"all": [rule("versioning", "equal", "Enabled")] * 30}
        text = json.dumps(many, indent=2)
        rep = ctb.run_check(SCHEMA, None, text)
        self.assertTrue(any("lines" in i["message"] for i in rep.of("error")))

    def test_fact_to_fact_value(self):
        ok, _ = check({"all": [{"fact": "generatedAt", "operator": "isAfter", "value": {"fact": "keyCreatedAt"}}]})
        bad, _ = check({"all": [{"fact": "generatedAt", "operator": "isAfter", "value": {"fact": "nope"}}]})
        self.assertEqual(ok, [])
        self.assertTrue(bad)


class Semantics(unittest.TestCase):
    """Boundaries follow Drata's rules engine (platform/packages/json-rules-engine)."""

    def test_within_last_days_boundaries(self):
        r = lambda d: ev(rule("d", "withinLastDays", 90), {"d": d})
        self.assertTrue(r("2026-10-30T00:00:00Z"))   # today
        self.assertTrue(r("2026-08-01T23:59:00Z"))   # exactly 90 days ago counts (calendar days, UTC)
        self.assertFalse(r("2026-07-31T23:59:00Z"))  # 91 days ago
        self.assertFalse(r("2026-11-02T00:00:00Z"))  # future
        self.assertFalse(r(None))
        self.assertFalse(r("not a date"))

    def test_older_than_days_is_strict(self):
        r = lambda d: ev(rule("d", "olderThanDays", 90), {"d": d})
        self.assertFalse(r("2026-08-01T00:00:00Z"))
        self.assertTrue(r("2026-07-31T00:00:00Z"))

    def test_is_after_now_and_epoch_millis(self):
        self.assertTrue(ev(rule("d", "isAfter", "now"), {"d": "2027-01-01T00:00:00Z"}))
        self.assertFalse(ev(rule("d", "isAfter", "now"), {"d": "2026-01-01T00:00:00Z"}))
        self.assertTrue(ev(rule("d", "isBefore", "now"), {"d": 0}))

    def test_equal_keeps_booleans_apart_from_numbers(self):
        self.assertFalse(ev(rule("x", "equal", True), {"x": 1}))
        self.assertTrue(ev(rule("x", "equal", None), {"x": None}))
        self.assertFalse(ev(rule("x", "equal", True), {"x": None}))
        self.assertFalse(ev(rule("x", "equal", False), {"x": None}))

    def test_exist_and_empty(self):
        self.assertTrue(ev(rule("x", "exist", False), {}))
        self.assertTrue(ev(rule("x", "empty", True), {"x": ""}))
        self.assertTrue(ev(rule("x", "empty", True), {"x": 5}))  # lodash isEmpty(5) is true
        self.assertFalse(ev(rule("x", "empty", True), {"x": [1]}))

    def test_in_contains_intersects(self):
        self.assertTrue(ev(rule("s", "in", ["a", "b"]), {"s": "a"}))
        self.assertTrue(ev(rule("s", "notIn", ["a", "b"]), {"s": "c"}))
        self.assertTrue(ev(rule("p", "doesNotContain", 22), {"p": [80, 443]}))
        self.assertFalse(ev(rule("p", "contains", 22), {"p": [80]}))
        self.assertTrue(ev(rule("s", "contains", "ell"), {"s": "hello"}))
        self.assertFalse(ev(rule("s", "doesNotContain", "x"), {"s": None}))  # null fails both
        self.assertTrue(ev(rule("p", "intersectsAny", [22, 80]), {"p": [80]}))
        self.assertTrue(ev(rule("p", "intersectsAll", [80, 443, 8080]), {"p": [80, 443]}))
        self.assertFalse(ev(rule("p", "intersectsAll", [80]), {"p": [80, 443]}))

    def test_array_operators_on_empty_arrays(self):
        inner = {"all": [rule("open", "equal", True)]}
        empty = {"findings": []}
        self.assertTrue(ev(rule("findings", "all", inner), empty))
        self.assertTrue(ev(rule("findings", "none", inner), empty))
        self.assertFalse(ev(rule("findings", "any", inner), empty))
        self.assertFalse(ev(rule("findings", "notAll", inner), empty))
        mixed = {"findings": [{"open": True}, {"open": False}]}
        self.assertFalse(ev(rule("findings", "all", inner), mixed))
        self.assertTrue(ev(rule("findings", "any", inner), mixed))
        self.assertTrue(ev(rule("findings", "notAll", inner), mixed))
        self.assertFalse(ev(rule("findings", "none", inner), mixed))

    def test_single_rule_as_nested_value(self):
        self.assertTrue(ev(rule("findings", "all", rule("open", "equal", True)), {"findings": [{"open": True}]}))

    def test_path_length_object_property_and_fact_value(self):
        self.assertTrue(ev(rule("findings", "greaterThan", 0, path="length"), {"findings": [1]}))
        self.assertFalse(ev(rule("findings", "greaterThan", 0, path="length"), {}))
        self.assertTrue(ev(rule("settings", "equal", "REGIONAL", path="availabilityType"), {"settings": {"availabilityType": "REGIONAL"}}))
        self.assertTrue(ev(rule("a", "lessThan", {"fact": "b"}), {"a": 1, "b": 2}))

    def test_exclusion_mode_and_roll_up(self):
        f = {"all": [rule("evidenceType", "equal", "bucket")]}
        self.assertFalse(ctb.in_scope(f, "exclusion", {"evidenceType": "bucket"}, NOW))
        self.assertTrue(ctb.in_scope(f, "exclusion", {"evidenceType": "instance"}, NOW))
        self.assertTrue(ctb.roll_up("nofail", []))        # empty scope passes
        self.assertFalse(ctb.roll_up("hasonepass", []))   # empty scope fails
        self.assertTrue(ctb.roll_up("failonce", [True, False]))
        self.assertFalse(ctb.roll_up("failonce", [False, False]))
        self.assertFalse(ctb.roll_up("nofail", [True, False]))

    def test_relative_date_templates(self):
        out = ctb.expand({"a": "$days_ago(5)", "b": ["$now", "$hours_ago(2)", "$days_ahead(1)", "plain"]}, NOW)
        self.assertEqual(out["a"], "2026-10-25T12:00:00Z")
        self.assertEqual(out["b"], ["2026-10-30T12:00:00Z", "2026-10-30T10:00:00Z", "2026-10-31T12:00:00Z", "plain"])


class CaseRunner(unittest.TestCase):
    cond = {"all": [rule("versioning", "equal", "Enabled")]}
    flt = {"all": [rule("evidenceType", "equal", "bucket")]}

    def run_cases(self, cases):
        return ctb.run_test(self.flt, "inclusion", self.cond, "nofail", cases, NOW, True)

    def test_scope_pass_fail_and_mismatch(self):
        rows, verdict, missing = self.run_cases([
            {"name": "good", "record": {"evidenceType": "bucket", "versioning": "Enabled"}, "expect": "pass"},
            {"name": "bad", "record": {"evidenceType": "bucket", "versioning": "Disabled"}, "expect": "pass"},
            {"name": "other", "record": {"evidenceType": "instance"}, "expect": "skip"},
        ])
        self.assertEqual([r["actual"] for r in rows], ["pass", "fail", "skip"])
        self.assertEqual([r["match"] for r in rows], [True, False, True])
        self.assertFalse(verdict)
        self.assertEqual(missing, [])
        self.assertIn("versioning", rows[1]["why"][0])

    def test_coverage_gaps_are_reported(self):
        _, _, missing = self.run_cases([{"record": {"evidenceType": "bucket", "versioning": "Enabled"}}])
        self.assertEqual(sorted(missing), ["fail", "skip"])


class CommandLine(unittest.TestCase):
    def run_ctb(self, *args):
        p = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "ctb.py"), *args], capture_output=True, text=True, cwd=ROOT)
        return p.returncode, p.stdout + p.stderr

    def test_examples_check_and_test_clean(self):
        for answer, cases in (("answer.md", "cases.json"), ("answer_findings.md", "cases_findings.json")):
            code, out = self.run_ctb("check", "--schema", "examples/schema.json", "--answer", f"examples/{answer}")
            self.assertEqual(code, 0, out)
            code, out = self.run_ctb("test", "--answer", f"examples/{answer}", "--cases", f"examples/{cases}")
            self.assertEqual(code, 0, out)

    def test_expected_test_result_is_enforced(self):
        # cases_findings.json expects the whole test to FAIL; under hasonepass one passing record makes it PASS
        code, out = self.run_ctb("test", "--answer", "examples/answer_findings.md", "--cases", "examples/cases_findings.json",
                                 "--threshold", "hasonepass")
        self.assertEqual(code, 1, out)
        self.assertIn("expected fail", out)

    def test_facts_and_prompt(self):
        code, out = self.run_ctb("facts", "--schema", "examples/schema.json")
        self.assertEqual(code, 0)
        self.assertIn("versioning: string|null (values: Enabled, Suspended, Disabled)", out)
        self.assertIn("findings: array of object {", out)
        code, out = self.run_ctb("prompt", "--schema", "examples/schema.json", "--notes", "evidenceType separates record types")
        self.assertEqual(code, 0)
        self.assertIn("evidenceType: string (values: bucket, api_key", out)
        self.assertIn("- evidenceType separates record types", out)
        self.assertNotIn("PASTE YOUR", out)
        self.assertNotIn("BEGIN PROMPT", out)

    def test_bad_answer_fails_with_hint(self):
        bad = "```json\n{\"all\":[{\"fact\":\"versioning\",\"operator\":\"equals\",\"value\":\"Enabled\"}]}\n```\n"
        p = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "ctb.py"), "check", "--schema", "examples/schema.json", "--answer", "-"],
                           input=bad, capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(p.returncode, 1)
        self.assertIn("did you mean 'equal'", p.stdout)


if __name__ == "__main__":
    unittest.main()
