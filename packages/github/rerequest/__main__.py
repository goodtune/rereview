import base64
import hashlib
import hmac
import json
import os

import requests

GITHUB_API = "https://api.github.com"


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def extract_raw_body(http):
    body = http.get("body", "")
    if isinstance(body, bytes):
        return body
    if http.get("isBase64Encoded"):
        return base64.b64decode(body)
    return (body or "").encode("utf-8")


def verify_signature(headers, raw_body):
    signature = headers.get("x-hub-signature-256")
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not signature or not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def select_reviewers(reviews, author_login, pending_reviewers):
    latest_states = {}
    latest_at = {}
    for review in reviews:
        user = review.get("user") or {}
        login = user.get("login")
        if not login or login == author_login:
            continue
        if user.get("type") == "Bot" or login.endswith("[bot]"):
            continue
        submitted_at = review.get("submitted_at") or ""
        if login not in latest_at or submitted_at >= latest_at[login]:
            latest_at[login] = submitted_at
            latest_states[login] = (review.get("state") or "").upper()

    candidates = {login for login, state in latest_states.items() if state != "DISMISSED"}
    return sorted(candidates - set(pending_reviewers))


def github_request(method, path, token, **kwargs):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    return requests.request(method, f"{GITHUB_API}{path}", headers=headers, timeout=10, **kwargs)


def rerequest_reviews(repo_full_name, pr_number, author_login, token):
    owner, repo = repo_full_name.split("/", 1)
    base = f"/repos/{owner}/{repo}/pulls/{pr_number}"

    reviews_resp = github_request("GET", f"{base}/reviews", token)
    reviews_resp.raise_for_status()
    reviews = reviews_resp.json()

    requested_resp = github_request("GET", f"{base}/requested_reviewers", token)
    requested_resp.raise_for_status()
    pending = [u.get("login") for u in requested_resp.json().get("users", []) if u.get("login")]

    to_request = select_reviewers(reviews, author_login, pending)
    result = {
        "repository": repo_full_name,
        "pull_request": pr_number,
        "requested": [],
        "pending": sorted(pending),
        "candidates": to_request,
    }

    if not to_request:
        return result

    post_resp = github_request("POST", f"{base}/requested_reviewers", token, json={"reviewers": to_request})
    if post_resp.status_code == 422:
        print(f"GitHub returned 422 while requesting reviewers: {post_resp.text}")
        result["error"] = "partial_failure_422"
        return result
    post_resp.raise_for_status()
    result["requested"] = to_request
    return result


def main(args):
    try:
        http = (args or {}).get("http") or {}
        headers = {str(k).lower(): v for k, v in (http.get("headers") or {}).items()}
        raw_body = extract_raw_body(http)

        if not verify_signature(headers, raw_body):
            return response(401, {"error": "invalid signature"})

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return response(400, {"error": "invalid json"})

        event = headers.get("x-github-event")
        action = payload.get("action")
        if event != "pull_request" or action != "synchronize":
            return response(200, {"ignored": True, "event": event, "action": action})

        try:
            repo_full_name = payload["repository"]["full_name"]
            pr_number = payload["pull_request"]["number"]
            author_login = payload["pull_request"]["user"]["login"]
        except (KeyError, TypeError):
            return response(400, {"error": "missing fields"})

        token = os.environ["GITHUB_TOKEN"]
        return response(200, rerequest_reviews(repo_full_name, pr_number, author_login, token))
    except Exception as exc:
        print(f"Unhandled error: {exc}")
        return response(500, {"error": "internal error"})
