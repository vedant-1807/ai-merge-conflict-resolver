# GitHub App setup

1. **Create the App.** Go to <https://github.com/settings/apps/new> and use the values in [`app.yml`](../app.yml).
   - Permissions: Contents RW, Pull requests RW, Issues RW, Checks RW, Metadata R.
   - Events: Pull request, Issue comment, Check run.
   - Webhook URL: `https://<host>/webhooks/github`. Set a random webhook secret.
2. **Private key.** Generate one and save it as `github-app.pem` in the repository root. It's ignored by git.
3. **Configure `.env`**:

   ```
   GITHUB__APP_ID=123456
   GITHUB__PRIVATE_KEY_PATH=/app/github-app.pem
   GITHUB__WEBHOOK_SECRET=<the secret>
   LLM__ANTHROPIC_API_KEY=sk-ant-...
   ```

4. **Expose the webhook locally** with a tunnel, for example `npx smee-client --url https://smee.io/<id> --target http://localhost:8000/webhooks/github`.
5. **Install the App** on a repository. Open a PR that conflicts with `main`, or comment `/resolve` on one. Only owners, members and collaborators can trigger `/resolve`.

## What you'll see

- **Verified:** a draft PR `Resolve merge conflicts in #N` targeting the PR's branch, a comment linking to it, and a green `MergeAgent` check run with the verification table.
- **Not verified:** a comment explaining which gate failed, with the best candidate patch attached, and a neutral check run.
- **PRs from forks:** the verified patch is posted as a comment, since the App can't push to forks.

## Optional: per-repo verification config

Commit `.mergeagent.yml` to the default branch:

```yaml
image: python:3.12-slim
setup: ["pip install -e '.[test]'"]
build: python -m compileall -q src
typecheck: mypy src
test: pytest -q --junitxml=/tmp/junit.xml
test_report: /tmp/junit.xml
acceptance: ["python -m mypkg --help"]
```
