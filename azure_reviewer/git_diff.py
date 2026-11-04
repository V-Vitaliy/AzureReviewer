import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .errors import ReviewerError
from .models import Skipped

MAX_DIFF_CHARS = 60_000
EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "vendor", "dist", "build",
                 "__pycache__", ".idea", ".next", "coverage", "generated"}
BINARY_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".gz", ".exe", ".dll",
                   ".so", ".pyc", ".woff", ".woff2", ".ico", ".mp4", ".db", ".sqlite", ".lock"}
LOCK_FILES = {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml"}
GENERATED_SUFFIXES = (".min.js", ".min.css", ".generated.py")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
OLD_PATH = re.compile(r"^--- (?:a/(.*)|/dev/null)$", re.M)
NEW_PATH = re.compile(r"^\+\+\+ (?:b/(.*)|/dev/null)$", re.M)


@dataclass
class Changes:
    text: str = ""
    lines: dict[str, set[int]] = field(default_factory=dict)
    skipped: list[Skipped] = field(default_factory=list)
    base: str | None = None
    head: str | None = None


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
    if result.returncode:
        # stderr can contain repository-controlled text, so it is not shown.
        raise ReviewerError(f"git {args[0]} failed (exit {result.returncode})")
    return result.stdout


def from_refs(repo: Path, base: str, head: str) -> Changes:
    base_sha, head_sha = revision(repo, base), revision(repo, head)
    ancestor = git(repo, "merge-base", base_sha, head_sha).decode().strip()
    patch = git(repo, "-c", "core.quotePath=true", "diff", "--no-ext-diff", "--no-textconv",
                "--no-renames", "--unified=3", ancestor, head_sha, "--")
    changes = parse_diff(patch.decode("utf-8", errors="replace"))
    changes.base, changes.head = ancestor, head_sha
    return changes


def revision(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def parse_diff(text: str, limit: int = MAX_DIFF_CHARS) -> Changes:
    changes = Changes()
    kept: list[str] = []
    for section in re.split(r"(?=^diff --git )", text, flags=re.M):
        if not section.strip():
            continue
        if not section.startswith("diff --git "):
            raise ReviewerError("Expected a Git unified diff")
        path = changed_path(section)
        if path is None:
            header = section.splitlines()[0].removeprefix("diff --git ")
            changes.skipped.append(Skipped(path=header, reason="Binary, metadata-only or quoted path"))
        elif not reviewable(path) or "\0" in section:
            changes.skipped.append(Skipped(path=path, reason="Excluded file"))
        else:
            changes.lines[path] = added_lines(section)
            kept.append(section)
    changes.text = "".join(kept)
    if len(changes.text) > limit:
        raise ReviewerError(f"Reviewable diff exceeds {limit} characters; split the change")
    return changes


def changed_path(section: str) -> str | None:
    new, old = NEW_PATH.search(section), OLD_PATH.search(section)
    return (new and new.group(1)) or (old and old.group(1)) or None


def reviewable(path: str) -> bool:
    p = PurePosixPath(path)
    return not (p.is_absolute() or ".." in p.parts or EXCLUDED_DIRS.intersection(p.parts)
                or p.suffix.lower() in BINARY_SUFFIXES or p.name in LOCK_FILES
                or p.name.endswith(GENERATED_SUFFIXES) or p.name.startswith(".env"))


def added_lines(section: str) -> set[int]:
    lines: set[int] = set()
    number, in_hunk = 0, False
    for row in section.splitlines():
        if match := HUNK.match(row):
            number, in_hunk = int(match.group(1)), True
        elif in_hunk and row.startswith("+"):
            lines.add(number)
            number += 1
        elif in_hunk and row.startswith(" "):
            number += 1
    return lines
