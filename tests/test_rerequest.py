import hashlib
import hmac
import json
import os
import unittest

import responses

from packages.github.rerequest.__main__ import GITHUB_API, main, select_reviewers


class RerequestTests(unittest.TestCase):
    def setUp(self):
        os.environ["GITHUB_WEBHOOK_SECRET"] = "secret123"
        os.environ["GITHUB_TOKEN"] = "token123"

    def signed_headers(self, payload, event="pull_request"):
        raw = json.dumps(payload).encode("utf-8")
        sig = "sha256=" + hmac.new(b"secret123", raw, hashlib.sha256).hexdigest()
        return {
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": sig,
        }, raw.decode("utf-8")

    def test_signature_valid_invalid_missing(self):
        payload = {"action": "synchronize"}
        headers, body = self.signed_headers(payload)

        ok = main({"http": {"headers": headers, "body": body}})
        self.assertNotEqual(ok["statusCode"], 401)

        bad = dict(headers)
        bad["X-Hub-Signature-256"] = "sha256=bad"
        invalid = main({"http": {"headers": bad, "body": body}})
        self.assertEqual(invalid["statusCode"], 401)

        missing = main({"http": {"headers": {"X-GitHub-Event": "pull_request"}, "body": body}})
        self.assertEqual(missing["statusCode"], 401)

    def test_event_filtering_ignored(self):
        payload = {"action": "synchronize", "repository": {"full_name": "goodtune/rereview"}, "pull_request": {"number": 1, "user": {"login": "author"}}}

        headers, body = self.signed_headers(payload, event="issues")
        res = main({"http": {"headers": headers, "body": body}})
        self.assertEqual(res["statusCode"], 200)
        self.assertIn("ignored", json.loads(res["body"]))

        payload["action"] = "opened"
        headers, body = self.signed_headers(payload, event="pull_request")
        res = main({"http": {"headers": headers, "body": body}})
        self.assertEqual(res["statusCode"], 200)
        self.assertTrue(json.loads(res["body"])["ignored"])

    def test_reviewer_selection_rules(self):
        reviews = [
            {"user": {"login": "alice", "type": "User"}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-01-01T00:00:00Z"},
            {"user": {"login": "alice", "type": "User"}, "state": "COMMENTED", "submitted_at": "2026-01-02T00:00:00Z"},
            {"user": {"login": "bob", "type": "User"}, "state": "APPROVED", "submitted_at": "2026-01-01T00:00:00Z"},
            {"user": {"login": "bob", "type": "User"}, "state": "DISMISSED", "submitted_at": "2026-01-03T00:00:00Z"},
            {"user": {"login": "carol", "type": "User"}, "state": "COMMENTED", "submitted_at": "2026-01-04T00:00:00Z"},
            {"user": {"login": "dependabot[bot]", "type": "Bot"}, "state": "COMMENTED", "submitted_at": "2026-01-04T00:00:00Z"},
            {"user": {"login": "author", "type": "User"}, "state": "COMMENTED", "submitted_at": "2026-01-04T00:00:00Z"},
        ]
        selected = select_reviewers(reviews, "author", ["carol"])
        self.assertEqual(selected, ["alice"])

    @responses.activate
    def test_main_synchronize_requests_reviewers_and_handles_422(self):
        payload = {
            "action": "synchronize",
            "repository": {"full_name": "goodtune/rereview"},
            "pull_request": {"number": 7, "user": {"login": "author"}},
        }
        headers, body = self.signed_headers(payload)
        base = f"{GITHUB_API}/repos/goodtune/rereview/pulls/7"

        responses.add(
            responses.GET,
            f"{base}/reviews",
            json=[
                {"user": {"login": "alice", "type": "User"}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-01-01T00:00:00Z"},
                {"user": {"login": "bob", "type": "User"}, "state": "APPROVED", "submitted_at": "2026-01-02T00:00:00Z"},
            ],
            status=200,
        )
        responses.add(responses.GET, f"{base}/requested_reviewers", json={"users": [{"login": "bob"}]}, status=200)
        responses.add(responses.POST, f"{base}/requested_reviewers", json={"message": "unprocessable"}, status=422)

        res = main({"http": {"headers": headers, "body": body}})
        self.assertEqual(res["statusCode"], 200)
        body_json = json.loads(res["body"])
        self.assertEqual(body_json["candidates"], ["alice"])
        self.assertEqual(body_json["requested"], [])
        self.assertEqual(body_json["error"], "partial_failure_422")


if __name__ == "__main__":
    unittest.main()
