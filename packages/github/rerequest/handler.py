"""Webhook handler logic. Imported by __main__.py for the DO Functions runtime
and by the test suite directly.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import sys

import requests

GITHUB_API = "https://api.github.com"
# Per-request timeout for GitHub calls. The handler makes at most three
# sequential calls (24s worst case) which sits comfortably under the 60s
# action timeout configured in project.yml.
HTTP_TIMEOUT = 8


class _BadBody(Exception):
    pass


def _raw_body(http: dict) -> bytes:
    body = http.get("body", "")
    if isinstance(body, bytes):
        return body
    if not isinstance(body, str):
        raise _BadBody("body is not a string or bytes")
    if http.get("isBase64Encoded"):
        try:
            return base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _BadBody(f"invalid base64 body: {exc}") from exc
    return body.encode("utf-8")


def _headers_lower(http: dict) -> dict:
    return {k.lower(): v for k, v in (http.get("headers") or {}).items()}


def _verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", signature)


def _signature_debug(secret: str, body: bytes, received: str | None) -> dict:
    # HMAC outputs aren't sensitive without the secret, so returning the
    # computed digest is safe. secret_tail_repr surfaces trailing
    # whitespace/newlines (the most common signature-mismatch cause) without
    # leaking enough of the secret to reconstruct it.
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "secret_len": len(secret),
        "secret_tail_repr": repr(secret[-4:]) if secret else "''",
        "body_len": len(body),
        "received_sig": received or "",
        "computed_sig": f"sha256={digest}",
    }


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
    # Sort by submitted_at so the last-write per login is the actual latest
    # review, regardless of the order GitHub returned them in. Reviews with no
    # submitted_at sort to the front (treated as oldest).
    ordered = sorted(reviews, key=lambda r: r.get("submitted_at") or "")
    latest: dict[str, str] = {}
    for review in ordered:
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
    try:
        raw = _raw_body(http)
    except _BadBody as exc:
        return _response(400, {"error": str(exc)})

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not secret or not token:
        return _response(500, {"error": "missing GITHUB_WEBHOOK_SECRET or GITHUB_TOKEN"})

    received_sig = headers.get("x-hub-signature-256")
    if not _verify_signature(secret, raw, received_sig):
        body = {"error": "invalid signature"}
        if os.environ.get("DEBUG") == "1":
            body["debug"] = _signature_debug(secret, raw, received_sig)
        return _response(401, body)

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
        # per_page=100 covers the vast majority of PRs in a single call. PRs
        # with more than 100 reviews are rare; if it ever matters, add Link
        # header pagination here.
        reviews = gh.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{number}/reviews",
            params={"per_page": 100},
            timeout=HTTP_TIMEOUT,
        )
        reviews.raise_for_status()
        pending_resp = gh.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{number}/requested_reviewers",
            params={"per_page": 100},
            timeout=HTTP_TIMEOUT,
        )
        pending_resp.raise_for_status()
        pending = {u.get("login") for u in (pending_resp.json().get("users") or []) if u.get("login")}

        reviewers = _select_reviewers(reviews.json(), author, pending)
        if not reviewers:
            return _response(200, {"requested": [], "skipped_pending": sorted(pending)})

        post = gh.post(
            f"{GITHUB_API}/repos/{repo}/pulls/{number}/requested_reviewers",
            json={"reviewers": reviewers},
            timeout=HTTP_TIMEOUT,
        )
        if post.status_code == 422:
            print(
                f"rerequest: github 422 for {repo}#{number} attempted={reviewers} body={post.text}",
                file=sys.stderr,
            )
            return _response(200, {"requested": [], "error": "422", "attempted": reviewers})
        post.raise_for_status()
        return _response(200, {"requested": reviewers})
    except requests.RequestException as exc:
        return _response(500, {"error": "github api failure", "detail": str(exc)})
