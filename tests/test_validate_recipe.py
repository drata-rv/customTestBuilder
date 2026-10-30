"""validate_recipe.py against an in-process mock of Drata's validate-recipe endpoint (no real Drata calls)."""
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "validate_recipe.py")
GOOD = """**Evaluation threshold:** At least one result must pass
**1. Filtering criteria** - click **Exclusion**, then paste into the Advanced editor:
```json
{"all":[{"fact":"recordType","operator":"notEqual","value":"scanSummary"}]}
```
**2. Condition group** - paste into the Advanced editor:
```json
{"all":[{"fact":"generatedAt","operator":"withinLastDays","value":3}]}
```
"""
BAD = GOOD.replace('"operator":"withinLastDays"', '"operator":"equals"').replace('"recordType"', '"nope"')


class Mock(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Mock.seen.append({"path": self.path, "body": body})
        if self.headers.get("Authorization") != "Bearer test-key":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"message":"nope"}')
            return
        entry = body["recipe"]["providers"][0]["resources"][0]
        errors = [{"type": "invalidOperator", "message": "Operator equals is not supported",
                   "location": f"/providers/0/resources/0/evaluator/all/{i}/operator"}
                  for i, r in enumerate(entry["evaluator"].get("all", [])) if r.get("operator") == "equals"]
        errors += [{"type": "invalidFact", "message": "Property not found: 'nope'",
                    "location": f"/providers/0/resources/0/filteringCriteria/evaluator/all/{i}/fact"}
                   for i, r in enumerate(entry.get("filteringCriteria", {}).get("evaluator", {}).get("all", [])) if r.get("fact") == "nope"]
        out = json.dumps({"isValid": not errors, "errors": errors}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)


class ValidateRecipe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.env = {**os.environ, "DRATA_API_KEY": "test-key",
                   "DRATA_BASE_URL": f"http://127.0.0.1:{cls.server.server_address[1]}/public/v2"}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def run_script(self, args, stdin=GOOD, env=None):
        base = [sys.executable, "-B", SCRIPT, "--workspace-id", "7", "--provider", "HR System", "--resource", "Employees"]
        p = subprocess.run(base + args, capture_output=True, text=True, input=stdin, env=env or self.env)
        return p.returncode, p.stdout + p.stderr

    def test_valid_answer(self):
        Mock.seen.clear()
        code, out = self.run_script(["--answer", "-"])
        self.assertEqual(code, 0, out)
        self.assertIn("exclusion filter", out)
        self.assertIn("hasonepass", out)
        sent = Mock.seen[0]
        self.assertEqual(sent["path"], "/public/v2/workspaces/7/monitoring-tests/action-validate-recipe")
        self.assertEqual(sorted(sent["body"]["recipe"]), ["allConnections", "assertion", "providers"])

    def test_invalid_answer_lists_located_errors(self):
        code, out = self.run_script(["--answer", "-"], stdin=BAD)
        self.assertEqual(code, 1)
        for text in ("Condition: all > rule 1 > operator", "Filter: all > rule 1 > fact", "Property not found"):
            self.assertIn(text, out)

    def test_wrapped_blob_rejected_locally(self):
        code, out = self.run_script(["--answer", "-"], stdin='```json\n{"mode":"inclusion","evaluator":{"all":[]}}\n```\n')
        self.assertEqual(code, 2)
        self.assertIn("bare", out)

    def test_bad_key_missing_key_and_plain_http(self):
        code, out = self.run_script(["--answer", "-"], env={**self.env, "DRATA_API_KEY": "wrong"})
        self.assertEqual(code, 2)
        self.assertIn("HTTP 401", out)
        env = {k: v for k, v in self.env.items() if k != "DRATA_API_KEY"}
        code, out = self.run_script(["--answer", "-"], env=env)
        self.assertIn("DRATA_API_KEY", out)
        code, out = self.run_script(["--answer", "-"], env={**self.env, "DRATA_BASE_URL": "http://example.com/public/v2"})
        self.assertIn("plain http", out)

    def test_unknown_threshold_and_dry_run(self):
        code, out = self.run_script(["--answer", "-", "--threshold", "sometimes"])
        self.assertEqual(code, 2)
        env = {k: v for k, v in self.env.items() if k != "DRATA_API_KEY"}
        code, out = self.run_script(["--answer", "-", "--dry-run"], env=env)
        self.assertEqual(code, 0, out)
        self.assertIn('"allConnections": true', out)
        self.assertIn('"mode": "exclusion"', out)


if __name__ == "__main__":
    unittest.main()
