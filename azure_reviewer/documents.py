import fnmatch
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from uuid import uuid4

from azure.ai.documentintelligence import DocumentIntelligenceClient

from .errors import ReviewerError
from .git_diff import EXCLUDED_DIRS, git
from .models import Chunk, Skipped
from .pdf import Page, extract_pdf

DEFAULT_PATTERNS = ["README.md", "docs/**/*.md", "docs/**/*.pdf"]
CHUNK_SIZE = 1800
MAX_CHUNKS = 300
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
REGULAR_FILE_MODES = {"100644", "100755"}


@dataclass
class Documentation:
    chunks: list[Chunk] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def skip(self, path: str, reason: str) -> None:
        self.skipped.append(Skipped(path=path, reason=reason))


def collect(repo: Path, commit: str, patterns: list[str], di: DocumentIntelligenceClient) -> Documentation:
    """Read documentation from a commit's Git objects, without touching the working tree."""
    docs = Documentation()
    total = 0
    for path, mode, oid in tracked_files(repo, commit):
        if not matches(path, patterns):
            continue
        suffix = PurePosixPath(path).suffix.lower()
        if mode not in REGULAR_FILE_MODES or EXCLUDED_DIRS.intersection(PurePosixPath(path).parts):
            docs.skip(path, "Excluded directory, symlink or non-regular file")
            continue
        if suffix not in (".md", ".pdf"):
            docs.skip(path, "Only Markdown and PDF documentation is supported")
            continue
        size = int(git(repo, "cat-file", "-s", oid))
        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            docs.skip(path, "Document size limit exceeded")
            continue
        total += size
        data = git(repo, "cat-file", "blob", oid)
        try:
            if suffix == ".md":
                pages = read_markdown(data)
            else:
                pages, warnings = extract_pdf(di, path, data)
                docs.warnings.extend(warnings)
        except ReviewerError as error:
            docs.skip(path, str(error))
            continue
        if not add_chunks(docs.chunks, path, pages):
            docs.skip(path, "No extractable text")
    if not docs.chunks:
        docs.warnings.append("No documentation found; the diff is reviewed alone.")
    return docs


def tracked_files(repo: Path, commit: str) -> list[tuple[str, str, str]]:
    files = []
    for entry in git(repo, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            mode, _, oid = metadata.decode().split()
            files.append((name.decode("utf-8", errors="replace"), mode, oid))
    return sorted(files)


def matches(path: str, patterns: list[str]) -> bool:
    # Unlike fnmatch, "**/" here also matches zero directories.
    return any(fnmatch.fnmatchcase(path, p) or ("**/" in p and matches(path, [p.replace("**/", "", 1)]))
               for p in patterns)


def read_markdown(data: bytes) -> list[Page]:
    try:
        return [(None, data.decode("utf-8"))]
    except UnicodeDecodeError:
        raise ReviewerError("Markdown is not UTF-8") from None


def add_chunks(chunks: list[Chunk], path: str, pages: list[Page]) -> int:
    before = len(chunks)
    for page, text in pages:
        for part in split_text(text):
            if len(chunks) >= MAX_CHUNKS:
                raise ReviewerError("Documentation exceeds the chunk limit; narrow --doc-pattern")
            chunks.append(Chunk(id=uuid4().hex, content=part, source=path, page=page))
    return len(chunks) - before


def split_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """Pack paragraphs into chunks of at most `size` characters, splitting oversized ones."""
    chunks: list[str] = []
    current = ""
    for paragraph in text.replace("\r\n", "\n").split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if current and len(current) + len(paragraph) + 2 <= size:
            current += "\n\n" + paragraph
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > size:
            chunks.append(paragraph[:size])
            paragraph = paragraph[size:]
        current = paragraph
    if current:
        chunks.append(current)
    return chunks
