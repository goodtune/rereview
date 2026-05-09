"""Webhook handler logic. Imported by __main__.py for the DO Functions runtime
and by the test suite directly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

import requests

GITHUB_API = "https://api.github.com"


def _raw_body(http: dict) -> bytes:
    body = http.get("body", "")
    if isinstance(body, bytes):
        return body
    if http.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode("utf-8")


def _headers_lower(http: dict) -> dict:
    return {k.lower(): v for k, v in (http.get("headers") or {}).items()}


def _verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", signature)


def _gh_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "rereview-do-function",
    })
    return s


def _select_reviewers(reviews: list[dict], author_login: str, pending: set[str]) -> list[str]:
    latest: dict[str, str] = {}
    for review in reviews:
        user = (review.get("user") or {})
        login = user.get("login")
        if not login:
            continue
        if user.get("type") == "Bot" or login.endswith("[bot]"):
            continue
        if login == author_login:
            continue
        latest[login] = review.get("state", "")
    return sorted(
        login for login, state in latest.items()
        if state != "DISMISSED" and login not in pending
    )


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def main(args: dict) -> dict:
    http = args.get("http") or {}
    headers = _headers_lower(http)
    raw = _raw_body(http)

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not secret or not token:
        return _response(500, {"error": "missing GITHUB_WEBHOOK_SECRET or GITHUB_TOKEN"})

    if not _verify_signature(secret, raw, headers.get("x-hub-signature-256")):
        return _response(401, {"error": "invalid signature"})

    if headers.get("x-github-event") != "pull_request":
        return _response(200, {"ignored": "event"})

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _response(400, {"error": "invalid JSON body"})

    if payload.get("action") != "synchronize":
        return _response(200, {"ignored": "action"})

    try:
        repo = payload["repository"]["full_name"]
        number = payload["pull_request"]["number"]
        author = payload["pull_request"]["user"]["login"]
    except (KeyError, TypeError):
        return _response(400, {"error": "missing expected fields"})

    try:
        gh = _gh_session(token)
        reviews = gh.get(f"{GITHUB_API}/repos/{repo}/pulls/{number}/reviews", timeout=10)
        reviews.raise_for_status()
        pending_resp = gh.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{number}/requested_reviewers", timeout=10
        )
        pending_resp.raise_for_status()
        pending = {u.get("login") for u in (pending_resp.json().get("users") or []) if u.get("login")}

        reviewers = _select_reviewers(reviews.json(), author, pending)
        if not reviewers:
            return _response(200, {"requested": [], "skipped_pending": sorted(pending)})

        post = gh.post(
            f"{GITHUB_API}/repos/{repo}/pulls/{number}/requested_reviewers",
            json={"reviewers": reviewers},
            timeout=10,
        )
        if post.status_code == 422:
            return _response(200, {"requested": [], "error": "422", "attempted": reviewers})
        post.raise_for_status()
        return _response(200, {"requested": reviewers})
    except requests.RequestException as exc:
        return _response(500, {"error": "github api failure", "detail": str(exc)})
