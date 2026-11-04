import io

from azure.ai.documentintelligence import DocumentIntelligenceClient
from pypdf import PdfReader

from .errors import ReviewerError

# The free (F0) Document Intelligence tier analyzes at most two pages per request.
MAX_PAGES = 2
POLL_TIMEOUT = 180

Page = tuple[int | None, str]


def extract_pdf(di: DocumentIntelligenceClient, path: str, data: bytes) -> tuple[list[Page], list[str]]:
    total = page_count(data)
    requested = min(total, MAX_PAGES)
    warnings = []
    if total > MAX_PAGES:
        warnings.append(f"{path}: only pages 1-{MAX_PAGES} of {total} were extracted.")
    poller = di.begin_analyze_document("prebuilt-layout", body=io.BytesIO(data),
                                       pages=f"1-{requested}" if requested > 1 else "1")
    result = poller.result(timeout=POLL_TIMEOUT)
    if not poller.done():
        raise TimeoutError("Document Intelligence polling deadline exceeded")
    pages = result.pages or []
    if not set(range(1, requested + 1)) <= {page.page_number for page in pages}:
        warnings.append(f"{path}: Document Intelligence did not return all requested pages.")
    return [(page.page_number, "\n".join(line.content for line in page.lines or [])) for page in pages], warnings


def page_count(data: bytes) -> int:
    try:
        reader = PdfReader(io.BytesIO(data))
        encrypted, count = reader.is_encrypted, len(reader.pages)
    except Exception:
        raise ReviewerError("Unreadable PDF") from None
    if encrypted or not count:
        raise ReviewerError("Encrypted or empty PDF")
    return count
