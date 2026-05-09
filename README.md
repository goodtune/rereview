# rereview

Serverless function to request another code review.

## Deploy (DigitalOcean Functions)

1. Create a fine-grained GitHub PAT with:
   - Pull requests: Read and write
   - Metadata: Read
2. Generate a webhook secret:
   - `python -c "import secrets; print(secrets.token_hex(32))"`
3. Export environment variables used by `project.yml`:
   - `export GITHUB_WEBHOOK_SECRET=...`
   - `export GITHUB_TOKEN=...`
4. Deploy:
   - `doctl serverless deploy .`
5. Configure a GitHub webhook:
   - Event: Pull requests
   - Content type: `application/json`
   - Secret: same value as `GITHUB_WEBHOOK_SECRET`
   - URL: the deployed function URL

## Local tests

- `pip install requests responses`
- `python -m unittest -q`
