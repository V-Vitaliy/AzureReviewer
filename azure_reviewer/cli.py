import argparse
import logging
import os
from pathlib import Path

from .documents import DEFAULT_PATTERNS
from .errors import ReviewerError
from .github import GitHub, publish
from .levels import DEFAULT_LEVEL, LEVELS
from .markdown import render
from .models import SEVERITIES, Report
from .pipeline import run_review

LOG = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging()
    github = github_from_env() if args.github_pr else None
    report = run_review(args.repo, args.base, args.head, args.project_id,
                        args.doc_patterns or DEFAULT_PATTERNS, LEVELS[args.level], args.env_file)
    exit_code = 1 if should_fail(report, args.fail_on) else 0
    if github:
        try:
            publish(github, args.github_pr, report)
        except ReviewerError as error:
            LOG.error("Publishing failed: %s", error)
            exit_code = 1
    print(render(report))
    return exit_code


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review a Git change with Azure OpenAI and project documentation")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True, help="Target branch or commit")
    parser.add_argument("--head", required=True, help="Changed branch or commit")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--doc-pattern", dest="doc_patterns", action="append",
                        help="Documentation glob; repeat to replace the defaults")
    parser.add_argument("--level", choices=LEVELS, default=DEFAULT_LEVEL,
                        help="Review level; selects AZURE_OPENAI_DEPLOYMENT_<LEVEL> and context limits")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--github-pr", type=int, help="Publish the review to this pull request")
    parser.add_argument("--fail-on", choices=["none", *SEVERITIES], default="none",
                        help="Exit with 1 when a finding has this severity or higher")
    args = parser.parse_args(argv)
    if not args.project_id.strip() or len(args.project_id) > 128:
        parser.error("--project-id must contain 1-128 characters")
    return args


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # SDK debug logs can contain request payloads.
    for name in ("azure", "openai", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def github_from_env() -> GitHub:
    token, repository = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPOSITORY")
    if not token or not repository:
        raise SystemExit("--github-pr requires GITHUB_TOKEN and GITHUB_REPOSITORY")
    return GitHub(token, repository, os.getenv("GITHUB_API_URL", "https://api.github.com"))


def should_fail(report: Report, threshold: str) -> bool:
    if report.status == "failed":
        return True
    if threshold == "none":
        return False
    return any(SEVERITIES.index(f.severity) >= SEVERITIES.index(threshold) for f in report.findings)
