import re
import time
from collections.abc import Iterator
from contextlib import contextmanager

from azure.search.documents import SearchClient

from .errors import ReviewerError
from .git_diff import Changes
from .models import Chunk

BATCH_SIZE = 100
READY_ATTEMPTS = 5
MAX_QUERIES = 6
TERMS_PER_QUERY = 10
HITS_PER_QUERY = 4
MAX_CONTEXT_CHARS = 16_000


class RunSearch:
    """Uploads documentation for a single run, retrieves context and deletes it afterwards."""

    def __init__(self, client: SearchClient, project_id: str, run_id: str) -> None:
        self.client = client
        self.project_id = project_id
        self.run_id = run_id
        self.scope = f"project_id eq {quote(project_id)} and run_id eq {quote(run_id)}"

    @contextmanager
    def temporary(self, chunks: list[Chunk], warnings: list[str]) -> Iterator[None]:
        attempted: list[str] = []
        try:
            for batch in batches(chunks):
                # Recorded before the request: a failed upload may still store part of the batch.
                attempted.extend(chunk.id for chunk in batch)
                self.upload(batch)
            if chunks and not self.wait_until_indexed(len(chunks)):
                warnings.append("Search indexing did not finish in time; context may be incomplete.")
            yield
        finally:
            self.delete(attempted, warnings)

    def upload(self, batch: list[Chunk]) -> None:
        results = self.client.upload_documents(documents=[
            {"id": c.id, "content": c.content, "source": c.source,
             "project_id": self.project_id, "run_id": self.run_id} for c in batch])
        if len(results) != len(batch) or not all(r.succeeded for r in results):
            raise ReviewerError("Search upload failed for one or more documents")

    def wait_until_indexed(self, expected: int) -> bool:
        for attempt in range(READY_ATTEMPTS):
            if attempt:
                time.sleep(1)
            count = self.client.search(search_text="*", filter=self.scope, include_total_count=True,
                                       top=1, select=["id"]).get_count()
            if count is not None and count >= expected:
                return True
        return False

    def delete(self, ids: list[str], warnings: list[str]) -> None:
        for batch in batches(ids):
            try:
                results = self.client.delete_documents(documents=[{"id": i} for i in batch])
                deleted = len(results) == len(batch) and all(r.succeeded for r in results)
            except Exception:
                deleted = False
            if not deleted:
                warnings.append("Search cleanup failed; temporary documents may remain.")

    def retrieve(self, queries: list[str], chunks: list[Chunk], limit: int = MAX_CONTEXT_CHARS) -> list[Chunk]:
        by_id = {chunk.id: chunk for chunk in chunks}
        selected: list[Chunk] = []
        seen: set[str] = set()
        total = 0
        for query in queries:
            hits = self.client.search(search_text=query, filter=self.scope, search_fields=["content"],
                                      select=["id"], top=HITS_PER_QUERY, query_type="simple")
            for hit in hits:
                chunk = by_id.get(hit["id"])
                if chunk is None or chunk.content in seen or total + len(chunk.content) > limit:
                    continue
                selected.append(chunk)
                seen.add(chunk.content)
                total += len(chunk.content)
        return selected


def search_queries(changes: Changes) -> list[str]:
    """Build short full-text queries from changed file paths and identifiers."""
    changed = "\n".join(row[1:] for row in changes.text.splitlines()
                        if row.startswith(("+", "-")) and not row.startswith(("+++", "---")))
    terms = (re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", " ".join(changes.lines))
             + re.findall(r"[A-Za-z_][A-Za-z_0-9]{3,}", changed))
    unique = list(dict.fromkeys(terms))[:MAX_QUERIES * TERMS_PER_QUERY]
    return [" ".join(unique[i:i + TERMS_PER_QUERY]) for i in range(0, len(unique), TERMS_PER_QUERY)]


def quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def batches(items: list) -> list[list]:
    return [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
