# Devin Dispatcher

Run [Devin](https://devin.ai) like a worker on a job queue, not a chatbot in a tab.

---

## The whole system, end to end

```
You label an issue with "devin-fix-this" in the target repo
       │
       ▼
GitHub Actions fires a workflow YAML in the target repo
       │
       ▼
That workflow runs:
   docker run ghcr.io/angpenghian/devin-dispatcher:latest \
              dispatch fix-issue --repo X/Y --issue N
       │
       ▼
The container renders the prompt → calls Devin's v3 API
       │
       ▼
Devin clones the repo in its own VM, fixes the issue, opens a PR,
and comments back on the issue with the result
       │
       ▼
The container writes a JSON report → dashboard renders it
```

Adding a new trigger (e.g., scheduled scan, PR review comment, Jira ticket) is one more YAML file in the target repo that calls the same image with a different task description.

## What's in this repo (10 things)

```
dispatcher.py            ← the whole CLI: 1 file, ~200 lines
prompt.j2                ← ONE prompt: universal guardrails + a {{ task_description }} slot
output_schema.json       ← the JSON shape Devin must return
docs/index.html          ← static dashboard for GH Pages
Dockerfile               ← packages dispatcher.py into ghcr.io/.../latest
docker-compose.yml       ← `docker compose run` for local testing
pyproject.toml           ← deps (click, httpx, jinja2)
.env.example             ← env vars the dispatcher reads
README.md                ← this
.github/workflows/build-image.yml  ← rebuilds + pushes the image on push to main
```

The dispatcher itself is **one Python file**. Every Devin session goes through one function: `run_devin()`.

## Target-repo wiring

Drop this single file into `<target-repo>/.github/workflows/`:

### `devin-fix-issue.yml`

```yaml
name: Devin — Fix Issue
on:
  issues:
    types: [labeled]
  workflow_dispatch:
    inputs:
      issue_number:
        description: "Issue number"
        required: true

permissions:
  contents: read
  issues: write

jobs:
  dispatch:
    if: >
      github.event_name == 'workflow_dispatch' ||
      github.event.label.name == 'devin-fix-this'
    runs-on: ubuntu-latest
    env:
      ISSUE_NUMBER: ${{ github.event.issue.number || github.event.inputs.issue_number }}
      REPO: ${{ github.repository }}
      BRANCH: ${{ github.event.repository.default_branch || 'master' }}
    steps:
      - name: Validate inputs
        run: |
          case "$ISSUE_NUMBER" in ''|*[!0-9]*) echo "issue number invalid"; exit 1 ;; esac
      - name: Pull dispatcher image
        run: docker pull ghcr.io/angpenghian/devin-dispatcher:latest
      - name: Dispatch
        env:
          DEVIN_API_KEY: ${{ secrets.DEVIN_API_KEY }}
          DEVIN_ORG_ID: ${{ secrets.DEVIN_ORG_ID }}
        run: |
          mkdir -p reports
          docker run --rm \
            -e DEVIN_API_KEY -e DEVIN_ORG_ID \
            -v "$PWD/reports:/app/reports" \
            ghcr.io/angpenghian/devin-dispatcher:latest \
            dispatch fix-issue --repo "$REPO" --branch "$BRANCH" --issue "$ISSUE_NUMBER"
      - name: Push run report to dispatcher repo (optional — needs DISPATCHER_PUSH_TOKEN)
        if: always()
        env:
          GH_TOKEN: ${{ secrets.DISPATCHER_PUSH_TOKEN }}
          DISPATCHER_REPO: ${{ secrets.DISPATCHER_REPORTS_REPO }}
        run: |
          if [ ! -d reports ] || [ -z "$(ls -A reports 2>/dev/null)" ]; then exit 0; fi
          if [ -z "$GH_TOKEN" ] || [ -z "$DISPATCHER_REPO" ]; then exit 0; fi
          git clone "https://x-access-token:${GH_TOKEN}@github.com/${DISPATCHER_REPO}.git" target
          mkdir -p target/reports
          cp reports/*.json target/reports/
          cd target
          git config user.email "devin-dispatcher-bot@users.noreply.github.com"
          git config user.name "devin-dispatcher-bot"
          git add reports/ && git commit -m "report from ${REPO}" || exit 0
          git push origin main
```

## Required secrets

In the **target repo** GitHub Actions secrets:

| Secret | Value |
|---|---|
| `DEVIN_API_KEY` | `cog_...` from Devin Settings → API keys |
| `DEVIN_ORG_ID` | `org-...` from Devin org settings URL |
| `DISPATCHER_REPORTS_REPO` | `angpenghian/devin-dispatcher` (only if you want the dashboard to populate) |
| `DISPATCHER_PUSH_TOKEN` | A fine-grained PAT with `contents:write` on the dispatcher repo (only for the dashboard) |

In **Devin's web app** (one-time):
- Settings → Integrations → GitHub → connect the target repo

## Run locally

```bash
cp .env.example .env
$EDITOR .env

docker compose build
docker compose run --rm dispatcher \
  dispatch fix-issue --repo angpenghian/superset --issue 1
```

## Live dashboard

**https://angpenghian.github.io/devin-dispatcher** — static HTML page, reads `reports/*.json` from this repo via the GitHub Contents API. KPIs (dispatches, PRs opened, success rate, ACUs consumed, engineer-hours saved) and a live table with clickthrough to every Devin session and PR. Auto-refreshes every 30s.

## Adding another trigger

The dispatcher image already handles arbitrary tasks. To add a new event (e.g., a PR review comment that mentions `@devin`), drop one more workflow file into the target repo:

```yaml
on:
  pull_request_review_comment:
    types: [created]
jobs:
  dispatch:
    if: contains(github.event.comment.body, '@devin')
    runs-on: ubuntu-latest
    steps:
      - env:
          DEVIN_API_KEY: ${{ secrets.DEVIN_API_KEY }}
          DEVIN_ORG_ID:  ${{ secrets.DEVIN_ORG_ID }}
        run: |
          docker run --rm -e DEVIN_API_KEY -e DEVIN_ORG_ID \
            ghcr.io/angpenghian/devin-dispatcher:latest \
            dispatch any \
              --repo "${{ github.repository }}" \
              --slug "review-comment-${{ github.event.comment.id }}" \
              --task "Address this review comment: ${{ github.event.comment.html_url }}"
```

Same image. Same secrets. Same dashboard. New event source.

## Production path

The in-repo workflow pattern above is the fastest way to demo. In a real customer engagement you'd ship this as a **GitHub App** — the customer installs one App from the Marketplace, zero YAMLs in their repo. That's how Dependabot, Renovate, and CodeRabbit all work. Same dispatcher image, just triggered by App webhooks instead of in-repo workflows.
