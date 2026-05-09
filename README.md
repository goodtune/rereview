# rereview

A small DigitalOcean serverless function that listens for GitHub `pull_request`
webhooks and re-requests reviews from prior reviewers whenever new commits are
pushed to a PR's head branch.

The use case: after a contributor pushes new commits in response to feedback,
the original reviewers are automatically re-notified — no manual clicking
through the "re-request review" button for each one.

## How it works

On a `pull_request` event with `action == "synchronize"`:

1. Verify the `X-Hub-Signature-256` HMAC against the raw request body.
2. List the PR's reviews and currently-requested reviewers.
3. Compute the set to re-request: distinct reviewer logins, excluding the PR
   author, bots, anyone whose latest review state is `DISMISSED`, and anyone
   already pending.
4. POST the resulting list to `…/pulls/{n}/requested_reviewers`.

All other events and actions are acknowledged with `200 OK` and a no-op.

## Layout

```
project/
├── packages/
│   └── github/
│       └── rerequest/
│           ├── __main__.py       # DO Functions entry point
│           ├── handler.py        # request handling logic
│           └── requirements.txt
├── project.yml                   # DO Functions deployment manifest
└── tests/                        # pytest unit tests
```

## Local testing

```bash
pip install requests pytest responses
pytest
```

## Deployment

Prerequisites: `doctl` installed and authenticated, serverless plugin installed
(`doctl serverless install`).

1. **Create a fine-grained PAT** at <https://github.com/settings/personal-access-tokens>.
   Scope it to the repositories whose PRs you want to manage. Permissions:
   - **Pull requests:** Read and write
   - **Metadata:** Read (mandatory)

2. **Generate a webhook secret:**
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

3. **Export both as environment variables** (the `project.yml` interpolates
   them at deploy time):
   ```bash
   export GITHUB_TOKEN=github_pat_...
   export GITHUB_WEBHOOK_SECRET=...
   ```

4. **Deploy:**
   ```bash
   doctl serverless deploy .
   ```
   Note the function URL printed at the end (or run `doctl serverless
   functions get github/rerequest --url`).

5. **Configure the webhook in GitHub** (repository or organisation Settings →
   Webhooks → Add webhook):
   - **Payload URL:** the deployed function URL
   - **Content type:** `application/json`
   - **Secret:** the value from step 2
   - **Events:** select "Let me choose individual events" → check only
     **Pull requests**

## Environment variables

| Name                    | Purpose                                  |
|-------------------------|------------------------------------------|
| `GITHUB_WEBHOOK_SECRET` | Shared secret for HMAC verification      |
| `GITHUB_TOKEN`          | Fine-grained PAT for GitHub API calls    |

## Out of scope

- Persistent storage.
- Cross-PR reviewer history.
- Retries on GitHub API failures — the next push will try again.
- Forks where reviewers may not have access to the head branch.
