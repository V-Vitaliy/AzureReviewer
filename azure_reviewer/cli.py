import argparse
import logging
import os
from pathlib import Path

from .documents import DEFAULT_DOCS_DIR, default_patterns, instructions_path, normalize_repo_path
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
                        args.doc_patterns or default_patterns(args.docs_dir), LEVELS[args.level],
                        args.env_file, args.instructions_path)
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
    parser.add_argument("--docs-dir", default=DEFAULT_DOCS_DIR,
                        help="Documentation directory ('.' for the root); sets the default globs "
                             "and the review instructions path")
    parser.add_argument("--instructions-path",
                        help="Review instructions file, read from the merge-base commit "
                             "(default <docs-dir>/review_instructions.md)")
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
    try:
        args.docs_dir = normalize_repo_path(args.docs_dir)
    except ReviewerError as error:
        parser.error(f"--docs-dir {error}")
    if args.instructions_path is None or not args.instructions_path.strip():
        args.instructions_path = instructions_path(args.docs_dir)
    else:
        try:
            args.instructions_path = normalize_repo_path(args.instructions_path)
        except ReviewerError as error:
            parser.error(f"--instructions-path {error}")
        if not args.instructions_path:
            parser.error("--instructions-path must name a file")
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
