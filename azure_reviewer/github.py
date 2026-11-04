import hashlib
import json
import re
from urllib.error import URLError
from urllib.request import Request, urlopen

from .errors import ReviewerError
from .markdown import comment, render
from .models import Finding, Report

SUMMARY_MARKER = "<!-- azure-reviewer -->"
FINGERPRINT = re.compile(r"<!-- azure-reviewer:([0-9a-f]{16}) -->")
MAX_INLINE_COMMENTS = 25
MAX_COMMENT_CHARS = 60_000
PAGE_SIZE = 100
TIMEOUT = 30


class GitHub:
    def __init__(self, token: str, repository: str, api_url: str = "https://api.github.com") -> None:
        self.token = token
        self.base_url = f"{api_url.rstrip('/')}/repos/{repository}"

    def request(self, method: str, path: str, body: dict | None = None):
        request = Request(self.base_url + path, method=method,
                          data=None if body is None else json.dumps(body).encode(),
                          headers={"Authorization": f"Bearer {self.token}",
                                   "Accept": "application/vnd.github+json",
                                   "Content-Type": "application/json",
                                   "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read() or b"null")
        except URLError as error:
            status = getattr(error, "code", "network error")
            raise ReviewerError(f"GitHub API {method} {path} failed: {status}") from None

    def list(self, path: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            batch = self.request("GET", f"{path}?per_page={PAGE_SIZE}&page={page}")
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                return items
            page += 1


def publish(github: GitHub, pr: int, report: Report) -> None:
    try:
        post_inline_comments(github, pr, report)
    except ReviewerError as error:
        report.warnings.append(f"Inline comments were not posted: {error}")
    upsert_summary(github, pr, render(report))


def post_inline_comments(github: GitHub, pr: int, report: Report) -> None:
    posted = {match for c in github.list(f"/pulls/{pr}/comments") for match in FINGERPRINT.findall(c["body"])}
    new = [f for f in report.findings if f.line is not None and fingerprint(f) not in posted]
    if not new:
        return
    github.request("POST", f"/pulls/{pr}/reviews", {
        "commit_id": report.head,
        "event": "COMMENT",
        "comments": [{"path": f.file, "line": f.line, "side": "RIGHT",
                      "body": f"{comment(f)}\n\n<!-- azure-reviewer:{fingerprint(f)} -->"}
                     for f in new[:MAX_INLINE_COMMENTS]],
    })


def upsert_summary(github: GitHub, pr: int, text: str) -> None:
    body = f"{SUMMARY_MARKER}\n{text}"
    if len(body) > MAX_COMMENT_CHARS:
        body = body[:MAX_COMMENT_CHARS] + "\n\n*Truncated.*"
    existing = next((c for c in github.list(f"/issues/{pr}/comments") if c["body"].startswith(SUMMARY_MARKER)), None)
    if existing:
        github.request("PATCH", f"/issues/comments/{existing['id']}", {"body": body})
    else:
        github.request("POST", f"/issues/{pr}/comments", {"body": body})


def fingerprint(finding: Finding) -> str:
    return hashlib.sha256(f"{finding.file}:{finding.line}:{finding.explanation}".encode()).hexdigest()[:16]
