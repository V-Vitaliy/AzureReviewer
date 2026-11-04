import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from openai import OpenAI

from .config import Settings
from .documents import collect
from .errors import ReviewerError
from .git_diff import from_refs
from .models import Report
from .review import review_changes
from .search import RunSearch, search_queries

LOG = logging.getLogger(__name__)
SDK_RETRIES = 2


def run_review(repo: Path, base: str, head: str, project_id: str, doc_patterns: list[str],
               env_file: Path | None = None) -> Report:
    report = Report()
    stage = "diff"
    try:
        changes = from_refs(repo, base, head)
        report.base, report.head, report.skipped = changes.base, changes.head, changes.skipped
        if not changes.text.strip():
            report.status, report.summary = "empty", "No reviewable changes; Azure was not called."
            return report
        stage = "configuration"
        settings = Settings.load(env_file)
        with azure_clients(settings) as (di, search_client, llm):
            stage = "documentation"
            docs = collect(repo, changes.base, doc_patterns, di)
            report.skipped += docs.skipped
            report.warnings += docs.warnings
            stage = "search"
            run_id = uuid4().hex
            LOG.info("Indexing %d documentation chunks (run %s)", len(docs.chunks), run_id)
            search = RunSearch(search_client, project_id, run_id)
            with search.temporary(docs.chunks, report.warnings):
                if docs.chunks:
                    report.context = search.retrieve(search_queries(changes), docs.chunks)
                    if not report.context:
                        report.warnings.append("Search found no relevant documentation.")
                stage = "model review"
                LOG.info("Reviewing changes with %d documentation chunks", len(report.context))
                review = review_changes(llm, settings.deployment, settings.structured_outputs,
                                        changes, report.context)
        report.summary, report.findings = review.summary, review.findings
    except Exception as error:
        # SDK exception messages can echo request payloads, so only our own messages are shown.
        detail = str(error) if isinstance(error, ReviewerError) else type(error).__name__
        LOG.error("Review failed during %s: %s", stage, detail)
        report.status, report.summary = "failed", f"Review failed during {stage}: {detail}"
    return report


@contextmanager
def azure_clients(settings: Settings) -> Iterator[tuple[DocumentIntelligenceClient, SearchClient, OpenAI]]:
    options = dict(connection_timeout=10, read_timeout=settings.timeout, retry_total=SDK_RETRIES)
    with (
        DocumentIntelligenceClient(settings.di_endpoint, AzureKeyCredential(settings.di_key.get_secret_value()),
                                   **options) as di,
        SearchClient(settings.search_endpoint, settings.search_index,
                     AzureKeyCredential(settings.search_key.get_secret_value()), **options) as search,
        OpenAI(base_url=settings.openai_endpoint, api_key=settings.openai_key.get_secret_value(),
               timeout=settings.timeout, max_retries=SDK_RETRIES) as llm,
    ):
        yield di, search, llm
