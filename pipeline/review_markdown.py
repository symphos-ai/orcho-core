"""
pipeline/review_markdown.py — Deterministic markdown rendering for parsed
reviewer output.

Reviewer phases parse model JSON into :class:`ParsedReview` and Orcho renders
human-readable markdown from it. This is a one-way transform — the rendered
markdown is for logs, session output, evidence, and repair_changes context. It is never
parsed back; the JSON contract is the only machine ground truth.
"""
from __future__ import annotations

from pipeline.review_parser import ParsedReview, ReviewFinding


def render_review_markdown(
    review: ParsedReview,
    *,
    title: str = "Review",
) -> str:
    """Render a :class:`ParsedReview` as stable, human-readable markdown."""
    lines: list[str] = [f"# {title}", ""]
    lines.append(f"**Verdict:** {review.verdict}")
    lines.append("")
    lines.append(f"**Short summary:** **{review.short_summary}**")

    if review.findings:
        lines.append("")
        lines.append("## Findings")
        for finding in review.findings:
            lines.append("")
            lines.extend(_render_finding(finding))

    if review.risks:
        lines.append("")
        lines.append("## Risks")
        lines.append("")
        for risk in review.risks:
            lines.append(f"- {risk}")

    if review.checks:
        lines.append("")
        lines.append("## Checks")
        lines.append("")
        for check in review.checks:
            lines.append(f"- {check}")

    return "\n".join(lines).rstrip() + "\n"


def render_fix_critique(review: ParsedReview) -> str:
    """Render a compact critique block intended for repair_changes context.

    Same content as :func:`render_review_markdown` but titled "Critique" so
    a downstream repair_changes agent reads it as actionable feedback rather than a
    review summary.
    """
    return render_review_markdown(review, title="Critique")


def _render_finding(finding: ReviewFinding) -> list[str]:
    out: list[str] = [
        f"### {finding.id} [{finding.severity}] {finding.title}",
    ]
    if finding.file:
        location = finding.file
        if finding.line is not None:
            location = f"{finding.file}:{finding.line}"
        out.append("")
        out.append(f"File: `{location}`")
    out.append("")
    out.append(finding.body)
    if finding.required_fix:
        out.append("")
        out.append(f"**Required fix:** {finding.required_fix}")
    return out
