# AzureReviewer

AI code reviewer for GitHub pull requests. It reviews the PR diff against the
project's own documentation using Azure OpenAI and posts the result to the PR:
inline comments on changed lines, one summary comment and a check that fails on
serious findings. It never runs code from the reviewed repository and supplements,
not replaces, tests and human review.

## How it works

```text
git merge-base(base, head) ──► diff ───────────────────────────────┐
        └─► README/docs at merge-base ─► text (PDF via Document    │
                                         Intelligence) ─► chunks   │
                                                ▼                  ▼
                            temporary Azure AI Search ─► context ─► Azure OpenAI
                                                                   ▼
                              validated findings ─► PR review + summary comment
```

1. **Diff** (`git_diff.py`): `git diff merge-base..head`, the same diff GitHub shows.
   Binary, lock, generated, vendor and `.env*` files are skipped. Diffs over
   60,000 characters fail instead of being truncated.
2. **Documentation** (`documents.py`, `pdf.py`): `README.md`, `docs/**/*.md` and
   `docs/**/*.pdf`, read from the merge-base commit, so a PR cannot rewrite the
   rules it is reviewed against. PDFs go through Document Intelligence
   (`prebuilt-layout`, first two pages on F0). Text is split into ≤1,800-character chunks.
3. **Retrieval** (`search.py`): chunks are uploaded to an existing Azure AI Search
   index under a unique `run_id`, searched with full-text queries built from changed
   paths and identifiers (≤16,000 characters of context), and deleted afterwards.
   There is no vector store: Search is used as a temporary BM25 index, and Document
   Intelligence only extracts text.
4. **Review** (`prompt.py`, `review.py`): the model gets the diff, the list of added
   lines and the retrieved documentation as untrusted JSON and returns findings in
   a strict schema. Each finding must reference a file from the diff, an added line
   (or `null`) and only retrieved documentation; otherwise one retry, then failure.
5. **Output** (`markdown.py`, `github.py`): Markdown is printed to stdout and, with
   `--github-pr`, published as a PR review (≤25 inline comments, already posted ones
   are skipped) and a single summary comment that is updated on every push.

`pipeline.py` wires the steps together; `cli.py` parses arguments and applies the
`--fail-on` gate.

## Using it in another repository

1. Push this repository to GitHub and tag a release: `git tag v1 && git push origin v1`.
2. If it is private: *Settings → Actions → General → Access →* "Accessible from
   repositories owned by the user".
3. In the target repository add secrets `AZURE_OPENAI_API_KEY`,
   `AZURE_SEARCH_ADMIN_KEY`, `AZURE_DI_API_KEY` and variables
   `AZURE_OPENAI_ENDPOINT` (ends with `/openai/v1`), `AZURE_OPENAI_DEPLOYMENT`,
   `AZURE_SEARCH_ENDPOINT`, `AZURE_DI_ENDPOINT`.
4. Add `.github/workflows/azure-review.yml`:

```yaml
name: Azure review
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
permissions:
  contents: read
  pull-requests: write
concurrency:
  group: azure-review-${{ github.event.pull_request.number }}
  cancel-in-progress: false
jobs:
  azure-review:
    if: ${{ !github.event.pull_request.draft && github.event.pull_request.head.repo.full_name == github.repository }}
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: V-Vitaliy/AzureReviewer@v1
        with:
          azure-openai-endpoint: ${{ vars.AZURE_OPENAI_ENDPOINT }}
          azure-openai-deployment: ${{ vars.AZURE_OPENAI_DEPLOYMENT }}
          azure-openai-api-key: ${{ secrets.AZURE_OPENAI_API_KEY }}
          azure-search-endpoint: ${{ vars.AZURE_SEARCH_ENDPOINT }}
          azure-search-admin-key: ${{ secrets.AZURE_SEARCH_ADMIN_KEY }}
          azure-di-endpoint: ${{ vars.AZURE_DI_ENDPOINT }}
          azure-di-api-key: ${{ secrets.AZURE_DI_API_KEY }}
          fail-on: high
```

5. Optionally make the `azure-review` check required in branch protection, so PRs
   with `high`/`critical` findings cannot be merged.

Optional inputs: `azure-search-index` (default `project-docs`),
`azure-openai-structured-outputs` (`false` for deployments without JSON schema
support), `doc-patterns` (one glob per line), `fail-on` (`none`…`critical`),
`project-id`.

Notes:

- `fetch-depth: 0` is required to find the merge-base.
- Fork and Dependabot PRs do not receive secrets and are skipped by the `if:`.
- Anyone who can push a branch can change the workflow in their PR and read the
  secrets; this is inherent to `pull_request` and fine for trusted collaborators.
- Every push to a PR is a billable model call (plus Document Intelligence for PDFs).
- `cancel-in-progress: false` keeps cleanup of temporary Search documents intact.
  A killed job can still leave them; they are tagged with the `run_id` from the log.

This repository reviews its own PRs with `uses: ./` in `.github/workflows/review.yml`.

## Azure prerequisites

An existing Azure OpenAI deployment, Document Intelligence resource and Azure AI
Search index with fields `id` (key), `content` (searchable), `source`,
`project_id` and `run_id` (filterable strings). One index can serve many
repositories: every query filters by `project_id` and `run_id`.

## Local run

```bash
python -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # fill in endpoints and keys
.venv/bin/azure-reviewer --repo /path/to/repo --base origin/main --head HEAD \
  --project-id my-project --env-file .env
```

Without `--github-pr` the review is only printed. Exit code is 1 if the review
failed or a finding reaches `--fail-on` (default `none`).

Docker:

```bash
docker build -t azure-reviewer .
docker run --rm --user "$(id -u):$(id -g)" --env-file .env -v /path/to/repo:/repo:ro \
  azure-reviewer --repo /repo --base origin/main --head HEAD --project-id my-project
```

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Offline: Git, documents, search isolation and cleanup, prompt, validation,
GitHub publishing and the gate are tested with mocks.
