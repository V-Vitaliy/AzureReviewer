from openai import OpenAI

from .errors import ReviewerError
from .git_diff import Changes
from .models import Chunk, Review
from .prompt import instructions, review_input

ATTEMPTS = 2
MAX_OUTPUT_TOKENS = 4000


def review_changes(llm: OpenAI, deployment: str, structured: bool, changes: Changes,
                   chunks: list[Chunk], project_instructions: str = "") -> Review:
    for attempt in range(ATTEMPTS):
        response = llm.responses.create(
            model=deployment,
            instructions=instructions(structured, project_instructions),
            input=review_input(changes, chunks, retry=attempt > 0),
            text={"format": response_format(structured)},
            max_output_tokens=MAX_OUTPUT_TOKENS,
            store=False,
        )
        if response.status != "completed":
            continue
        try:
            return validate(response.output_text, changes, chunks)
        except ValueError:
            continue
    raise ReviewerError(f"Model returned an invalid review {ATTEMPTS} times; no findings accepted")


def response_format(structured: bool) -> dict:
    if structured:
        return {"type": "json_schema", "name": "code_review", "schema": Review.model_json_schema(), "strict": True}
    return {"type": "json_object"}


def validate(raw: str, changes: Changes, chunks: list[Chunk]) -> Review:
    review = Review.model_validate_json(raw)
    known = {chunk.id for chunk in chunks}
    for finding in review.findings:
        if finding.file not in changes.lines:
            raise ReviewerError("Model referenced a file outside the diff")
        if finding.line is not None and finding.line not in changes.lines[finding.file]:
            raise ReviewerError("Model referenced a line that was not added")
        if not set(finding.documentation_ids) <= known:
            raise ReviewerError("Model referenced unknown documentation")
    return review
