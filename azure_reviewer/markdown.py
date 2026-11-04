import html
import re

from .models import Finding, Report

SPECIAL = re.compile(r"([\\`*_{}\[\]()#+.!|>~-])")


def escape(text: str) -> str:
    """Neutralize untrusted model or repository text before it is rendered on GitHub."""
    return SPECIAL.sub(r"\\\1", html.escape(text)).replace("\n", " ")


def render(report: Report) -> str:
    rows = ["## Azure code review", "", f"**Status:** {report.status}"]
    if report.head:
        rows[-1] += f" · commit `{report.head[:12]}`"
    rows += ["", escape(report.summary), "", "### Findings", ""]
    rows += [f"- **{f.severity}** {escape(location(f))}: {escape(f.explanation)} "
             f"*Suggestion:* {escape(f.suggestion)}" for f in report.findings]
    if not report.findings:
        rows.append("No findings. This is not proof of correctness.")
    sections = {
        "Warnings": [escape(w) for w in report.warnings],
        "Skipped files": [f"{escape(s.path)}: {escape(s.reason)}" for s in report.skipped],
        "Documentation used": [f"`{c.id}` {escape(c.source)}" + (f", page {c.page}" if c.page else "")
                               for c in report.context],
    }
    for title, items in sections.items():
        if items:
            rows += ["", f"### {title}", ""] + [f"- {item}" for item in items]
    return "\n".join(rows) + "\n"


def comment(finding: Finding) -> str:
    rows = [f"**{finding.severity}**: {escape(finding.explanation)}", "",
            f"**Suggestion:** {escape(finding.suggestion)}"]
    if finding.documentation_ids:
        rows += ["", "**Documentation:** " + ", ".join(f"`{i}`" for i in finding.documentation_ids)]
    return "\n".join(rows)


def location(finding: Finding) -> str:
    return finding.file if finding.line is None else f"{finding.file}:{finding.line}"
