# OpenCodeReview GitHub PR Reviewer

## Overview

OpenCodeReview is configured as an independent AI code reviewer for StrikeNova pull requests. It provides repository-aware analysis, line-level inline comments, PR summaries, and incremental non-destructive review behavior.

## Installation

### Workflow

**Path:** `.github/workflows/open-code-review.yml`

**Action:** `alibaba/open-code-review@bccbc15f785269400735d5255540c231e6c02b6d` (pinned to v1.12.9)

**OCR CLI Version:** `1.12.9` (pinned for reproducibility)

### Triggers

The workflow automatically runs on:
- `pull_request_target`: opened, synchronize, reopened, ready_for_review
- `issue_comment`: created (for manual `/open-code-review` or `@open-code-review` triggers)

### Manual Trigger

Comment on any PR:
- `/open-code-review`
- `@open-code-review`

## Configuration

### Required Secrets and Variables

Configure in **Settings → Secrets and variables → Actions**:

| Name | Type | Description |
|------|------|-------------|
| `OCR_LLM_URL` | Secret | LLM API endpoint URL |
| `OCR_LLM_AUTH_TOKEN` | Secret | LLM API authentication token |
| `OCR_LLM_MODEL` | Variable | Model name (e.g., `gpt-4o`, `claude-sonnet-4-20250514`) |
| `OCR_LLM_USE_ANTHROPIC` | Variable | `true` for Anthropic Claude, `false` for OpenAI-compatible |

### LLM Provider Examples

**OpenAI:**
```
OCR_LLM_URL=https://api.openai.com/v1/chat/completions
OCR_LLM_MODEL=gpt-4o
OCR_LLM_USE_ANTHROPIC=false
```

**Anthropic:**
```
OCR_LLM_URL=https://api.anthropic.com/v1/messages
OCR_LLM_MODEL=claude-sonnet-4-20250514
OCR_LLM_USE_ANTHROPIC=true
```

**OpenRouter:**
```
OCR_LLM_URL=https://openrouter.ai/api/v1/chat/completions
OCR_LLM_MODEL=openai/gpt-4o
OCR_LLM_USE_ANTHROPIC=false
```

## Capabilities Enabled

| Feature | Status | Configuration |
|---------|--------|---------------|
| Inline review | ✅ Enabled | Default behavior |
| PR summary | ✅ Enabled | Default behavior |
| Incremental review | ✅ Enabled | `incremental: 'true'` |
| Sticky summary | ✅ Enabled | `sticky_summary: 'true'` |
| Manual trigger | ✅ Enabled | `/open-code-review` or `@open-code-review` |
| Artifacts upload | ✅ Enabled | `upload_artifacts: 'true'` |
| Review effort | ✅ High | `effort: high` |
| Concurrency | ✅ 5 | `review_concurrency: 5` |
| Repository context | ✅ Enabled | Default behavior |
| Progress logging | ✅ Enabled | `stream_progress: 'true'` |
| Custom rules | ✅ Enabled | `.github/open-code-review-rules.json` |

## Security

### GitHub Permissions

```yaml
permissions:
  contents: read
  pull-requests: write
```

- **contents: read** — Required to fetch PR diff and repository context
- **pull-requests: write** — Required to post inline review comments and summary

### Secret Handling

- LLM credentials are stored as GitHub Actions secrets/variables
- Secrets are only exposed to the OpenCodeReview step
- No credentials are committed to the repository
- `GITHUB_TOKEN` is automatically provided by GitHub Actions

### Fork/PR Security

The workflow uses `pull_request_target` which:
- Runs in the context of the base repository (not the fork)
- Has access to secrets (required for LLM API calls)
- Reviews PR code as untrusted input
- Does not execute PR code

### What OpenCodeReview Does NOT Do

- ❌ Modify application source code
- ❌ Merge PRs
- ❌ Deploy anything
- ❌ Alter product architecture
- ❌ Override ADRs
- ❌ Access production systems

## Custom Rules

Custom rules are defined in `.github/open-code-review-rules.json` and focus on:

- **Security**: Credential exposure, SQL injection, authentication/authorization, session handling, broker token exposure
- **Database**: Alembic migrations, transaction boundaries, race conditions, query patterns
- **Financial**: Decimal precision, GEX conventions, timezone handling, server-authoritative paper trading
- **WebSocket**: Session authentication, connection cleanup
- **API**: Error handling, HTTP status codes, rate limiting
- **Testing**: Test coverage, regression tests, security tests
- **Performance**: Query limits, database indexes
- **Maintainability**: Environment-based configuration, type hints

## Review Flow

```
GitHub PR Opened/Updated
        ↓
OpenCodeReview Triggered
        ↓
OCR Installs & Configures LLM
        ↓
Repository-Aware Diff Analysis
        ↓
Custom Rules Applied
        ↓
Inline Comments Posted (if findings)
        ↓
Summary Comment Posted (sticky)
        ↓
Artifacts Uploaded
```

## Artifacts

When enabled, the following artifacts are uploaded:
- `ocr-review-result-<run_id>-<run_attempt>` — Raw JSON review result
- `ocr-stderr.log` — OCR stderr log

Download from the workflow run's **Artifacts** section.

## Concurrency

```yaml
concurrency:
  group: open-code-review-${{ github.event.pull_request.number || github.event.issue.number }}
  cancel-in-progress: true
```

- Only one review runs per PR at a time
- New pushes cancel stale reviews
- Prevents duplicate comments from concurrent runs

## Troubleshooting

### Common Issues

1. **Job fails / "Failed to parse OCR output"**
   - Check `OCR_LLM_URL` and `OCR_LLM_AUTH_TOKEN` are set correctly
   - Inspect the uploaded `ocr-stderr.log` artifact

2. **"Cannot find merge-base"**
   - Ensure `permissions: contents: read` is set
   - Verify the base branch is accessible

3. **Review comments not on expected lines**
   - Comments are attached to the PR head commit
   - If the PR was force-pushed mid-review, GitHub rejects inline posts
   - The comment is rendered in the summary instead

4. **No summary or comments at all**
   - Confirm `permissions: pull-requests: write` is set
   - Verify `github_token` has the required scopes

### Debugging

- **Artifacts**: Download `ocr-review-result-*.json` and `ocr-stderr.log` from the run
- **Step log**: The "Run OpenCodeReview" step prints JSON result and stderr
- **Action outputs**: `comments_total`, `comments_inline`, `comments_skipped`, `comments_routed`, `comments_failed`, `comments_resolved`, `summary_comment_url`
- **GitHub step debug**: Set repository secret `ACTIONS_STEP_DEBUG=true`

## Updating

To update OpenCodeReview:

1. Check the [releases page](https://github.com/alibaba/open-code-review/releases)
2. Update the Action commit SHA in the workflow
3. Update `ocr_version` to match the new CLI version
4. Test on a PR before merging

## Verification

### Local Validation

```bash
# Validate YAML syntax
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/open-code-review.yml', 'r'))"

# Validate JSON syntax
python3 -c "import json; json.load(open('.github/open-code-review-rules.json', 'r'))"
```

### GitHub Actions Verification

1. Create a test PR
2. Verify the workflow triggers
3. Check the OpenCodeReview step completes
4. Verify inline comments appear (if findings exist)
5. Verify summary comment is posted
6. Verify artifacts are uploaded
7. Test manual trigger with `/open-code-review`

## Other Reviewers

OpenCodeReview operates independently alongside:
- **CodeRabbit** — AI code review
- **Greptile** — AI code review
- **CI/tests** — Automated testing
- **Human/authorized agent** — Final decision

No AI reviewer depends on another. Each provides independent analysis.
