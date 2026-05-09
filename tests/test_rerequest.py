import base64
import hashlib
import hmac
import json

import responses

import handler as rerequest


SECRET = "testsecret"


def _sign(body: bytes) -> str:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _event(payload: dict, *, event: str = "pull_request", sig: str | None = None) -> dict:
    body = json.dumps(payload)
    raw = body.encode()
    return {
        "http": {
            "headers": {
                "X-GitHub-Event": event,
                "X-Hub-Signature-256": _sign(raw) if sig is None else sig,
                "Content-Type": "application/json",
            },
            "body": body,
        }
    }


def _synchronize_payload(repo="o/r", number=7, author="alice"):
    return {
        "action": "synchronize",
        "repository": {"full_name": repo},
        "pull_request": {"number": number, "user": {"login": author}},
    }


# ---------- signature verification ----------


def test_invalid_signature_returns_401():
    args = _event(_synchronize_payload(), sig="sha256=deadbeef")
    resp = rerequest.main(args)
    assert resp["statusCode"] == 401
    body = json.loads(resp["body"])
    assert body == {"error": "invalid signature"}


def test_invalid_signature_with_debug_includes_diagnostic(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    args = _event(_synchronize_payload(), sig="sha256=deadbeef")
    resp = rerequest.main(args)
    assert resp["statusCode"] == 401
    body = json.loads(resp["body"])
    assert body["error"] == "invalid signature"
    debug = body["debug"]
    assert debug["secret_len"] == len(SECRET)
    assert debug["received_sig"] == "sha256=deadbeef"
    assert debug["computed_sig"].startswith("sha256=")
    assert debug["body_len"] > 0


def test_missing_signature_returns_401():
    body = json.dumps(_synchronize_payload())
    args = {"http": {"headers": {"X-GitHub-Event": "pull_request"}, "body": body}}
    resp = rerequest.main(args)
    assert resp["statusCode"] == 401


def test_valid_signature_passes():
    args = _event({"action": "ignored", "repository": {"full_name": "o/r"},
                   "pull_request": {"number": 1, "user": {"login": "a"}}})
    resp = rerequest.main(args)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"ignored": "action"}


# ---------- event filtering ----------


def test_non_pull_request_event_ignored():
    args = _event(_synchronize_payload(), event="push")
    resp = rerequest.main(args)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"ignored": "event"}


def test_pull_request_with_other_action_ignored():
    payload = _synchronize_payload()
    payload["action"] = "opened"
    args = _event(payload)
    resp = rerequest.main(args)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"ignored": "action"}


def test_invalid_json_returns_400():
    raw = b"not json"
    args = {"http": {
        "headers": {
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(raw),
        },
        "body": "not json",
    }}
    resp = rerequest.main(args)
    assert resp["statusCode"] == 400


def test_invalid_base64_body_returns_400():
    args = {"http": {
        "headers": {"X-GitHub-Event": "pull_request"},
        "body": "!!! not base64 !!!",
        "isBase64Encoded": True,
    }}
    resp = rerequest.main(args)
    assert resp["statusCode"] == 400


def test_base64_encoded_body_is_decoded():
    # Use action="opened" so the handler short-circuits before any GitHub
    # call — this keeps the test hermetic (no network) while still exercising
    # the base64 decode + signature verification path.
    payload = _synchronize_payload()
    payload["action"] = "opened"
    raw = json.dumps(payload).encode()
    encoded = base64.b64encode(raw).decode()
    args = {"http": {
        "headers": {
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(raw),
        },
        "body": encoded,
        "isBase64Encoded": True,
    }}
    resp = rerequest.main(args)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"ignored": "action"}


# ---------- reviewer set logic ----------


def test_select_reviewers_dedupes_and_filters():
    reviews = [
        {"user": {"login": "alice", "type": "User"}, "state": "COMMENTED"},  # author
        {"user": {"login": "bob", "type": "User"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "bob", "type": "User"}, "state": "APPROVED"},
        {"user": {"login": "carol", "type": "User"}, "state": "DISMISSED"},
        {"user": {"login": "dependabot[bot]", "type": "Bot"}, "state": "COMMENTED"},
        {"user": {"login": "dave", "type": "User"}, "state": "APPROVED"},
        {"user": {"login": "eve", "type": "User"}, "state": "COMMENTED"},
    ]
    result = rerequest._select_reviewers(reviews, author_login="alice", pending={"eve"})
    assert result == ["bob", "dave"]


def test_select_reviewers_uses_latest_state_for_dismissed():
    # Most recent for carol is DISMISSED -> excluded.
    # For frank, dismissal preceded a fresh COMMENTED -> included.
    reviews = [
        {"user": {"login": "carol", "type": "User"}, "state": "APPROVED",
         "submitted_at": "2024-01-01T00:00:00Z"},
        {"user": {"login": "carol", "type": "User"}, "state": "DISMISSED",
         "submitted_at": "2024-01-02T00:00:00Z"},
        {"user": {"login": "frank", "type": "User"}, "state": "DISMISSED",
         "submitted_at": "2024-01-01T00:00:00Z"},
        {"user": {"login": "frank", "type": "User"}, "state": "COMMENTED",
         "submitted_at": "2024-01-02T00:00:00Z"},
    ]
    result = rerequest._select_reviewers(reviews, author_login="alice", pending=set())
    assert result == ["frank"]


def test_select_reviewers_orders_by_submitted_at_not_iteration():
    # API returns out-of-order: the DISMISSED is the actual latest, but it
    # arrives first in the list. submitted_at must drive the decision.
    reviews = [
        {"user": {"login": "bob", "type": "User"}, "state": "DISMISSED",
         "submitted_at": "2024-02-01T00:00:00Z"},
        {"user": {"login": "bob", "type": "User"}, "state": "COMMENTED",
         "submitted_at": "2024-01-01T00:00:00Z"},
    ]
    assert rerequest._select_reviewers(reviews, author_login="alice", pending=set()) == []


def test_select_reviewers_empty():
    assert rerequest._select_reviewers([], author_login="alice", pending=set()) == []


# ---------- end-to-end with mocked GitHub ----------


@responses.activate
def test_synchronize_requests_reviewers():
    payload = _synchronize_payload(repo="o/r", number=7, author="alice")
    responses.get(
        "https://api.github.com/repos/o/r/pulls/7/reviews",
        json=[
            {"user": {"login": "bob", "type": "User"}, "state": "CHANGES_REQUESTED"},
            {"user": {"login": "carol", "type": "User"}, "state": "DISMISSED"},
            {"user": {"login": "dave", "type": "User"}, "state": "APPROVED"},
        ],
    )
    responses.get(
        "https://api.github.com/repos/o/r/pulls/7/requested_reviewers",
        json={"users": [{"login": "dave"}], "teams": []},
    )
    post = responses.post(
        "https://api.github.com/repos/o/r/pulls/7/requested_reviewers",
        json={},
        status=201,
    )

    resp = rerequest.main(_event(payload))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"requested": ["bob"]}
    assert post.call_count == 1
    sent = json.loads(responses.calls[-1].request.body)
    assert sent == {"reviewers": ["bob"]}


@responses.activate
def test_synchronize_no_reviewers_skips_post():
    payload = _synchronize_payload()
    responses.get(
        "https://api.github.com/repos/o/r/pulls/7/reviews",
        json=[{"user": {"login": "dave", "type": "User"}, "state": "APPROVED"}],
    )
    responses.get(
        "https://api.github.com/repos/o/r/pulls/7/requested_reviewers",
        json={"users": [{"login": "dave"}], "teams": []},
    )
    resp = rerequest.main(_event(payload))
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["requested"] == []
    # Only the two GETs should have been made.
    assert len(responses.calls) == 2


@responses.activate
def test_github_422_is_logged_not_raised():
    payload = _synchronize_payload()
    responses.get(
        "https://api.github.com/repos/o/r/pulls/7/reviews",
        json=[{"user": {"login": "bob", "type": "User"}, "state": "COMMENTED"}],
    )
    responses.get(
        "https://api.github.com/repos/o/r/pulls/7/requested_reviewers",
        json={"users": [], "teams": []},
    )
    responses.post(
        "https://api.github.com/repos/o/r/pulls/7/requested_reviewers",
        json={"message": "Reviews may only be requested from collaborators."},
        status=422,
    )
    resp = rerequest.main(_event(payload))
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["error"] == "422"
    assert body["attempted"] == ["bob"]
