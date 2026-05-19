"""ReportRenderer — module port of ``gmb-presenter-demo/scripts/render-report.py``.

Takes the accumulated outputs of Session B's pipeline (customer name,
description, chart URL, bullets) and renders the two-slide HTML report
by substituting placeholders in a per-specialist template, then
atomically writing the file into ``reports/<slug>-<YYYY-MM-DD>.html``.

Per ``modular-extension.md § 6``, each specialist owns its own template
under ``reports/templates/<agent_id>.html``. The v1 financial specialist
uses ``reports/templates/financial.html``.

All validation mirrors the original shell-script behavior:

- ``chart_url`` must start with ``https://``.
- ``bullets`` must have 3–5 non-empty strings.
- The template must contain all required placeholders.
- HTML-escape every text field except ``CHART_URL`` (used inside
  ``<img src="…">``).

Writes happen via tempfile + ``os.replace`` so chokidar (the visor's
file watcher) sees one complete file event — never a partial write.
"""

from __future__ import annotations

import html
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from src.models.financial import ReportBundle


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Placeholder contract
# ─────────────────────────────────────────────────────────────

PLACEHOLDERS: tuple[str, ...] = (
    "CUSTOMER_NAME",
    "DESCRIPTION",
    "CHART_URL",
    "CHART_TITLE",
    "REPORT_DATE",
    "FOOTER_NOTE",
    "EXECUTIVE_SUMMARY",
)

_PLACEHOLDER_PATTERN = re.compile(r"\{\{[A-Z_]+\}\}")

# Which fields get HTML-escaped during substitution.
# CHART_URL goes into src="…" and must remain a URL (template wraps it in quotes).
# EXECUTIVE_SUMMARY is pre-built <li> HTML and must NOT be double-escaped.
_RAW_FIELDS: frozenset[str] = frozenset({"CHART_URL", "EXECUTIVE_SUMMARY"})


class ReportRenderError(ValueError):
    """Raised on validation failures (bad URL, bullet count, template, …)."""


# ─────────────────────────────────────────────────────────────
# ReportRenderer
# ─────────────────────────────────────────────────────────────


@dataclass
class ReportRenderer:
    """Renders a specialist's report HTML into ``reports/<slug>-<date>.html``.

    Attributes:
        repo_root: Project root, used to resolve default template and
            reports directory.
        default_template_path: Used when ``render(...)`` is called
            without ``template_path``. Defaults to the financial template
            for backwards compat; override per-call for other specialists.
        reports_dir: Where the rendered HTML is written. Defaults to
            ``<repo_root>/reports``.
    """

    repo_root: Path
    default_template_path: Path | None = None
    reports_dir: Path | None = None

    def __post_init__(self) -> None:
        # Pin defaults after repo_root is known.
        if self.default_template_path is None:
            self.default_template_path = (
                self.repo_root / "reports" / "templates" / "financial.html"
            )
        if self.reports_dir is None:
            self.reports_dir = self.repo_root / "reports"

    def render(
        self,
        *,
        customer_name: str,
        description: str,
        chart_url: str,
        chart_title: str,
        bullets: list[str],
        slug: str,
        report_date: str,
        footer_note: str = "Generado con Finalysis + AntV + Kiro",
        template_path: Path | None = None,
        out_path: Path | None = None,
    ) -> Path:
        """Render one report and atomically write it to disk.

        Args:
            customer_name: Displayed in the header.
            description: 1–2 sentences below the header.
            chart_url: Must start with ``https://``.
            chart_title: Uppercase short label (``"SMA (50) — 6M"``).
            bullets: 3–5 non-empty strings — the executive summary.
            slug: Filename slug, lowercase, hyphen-separated.
            report_date: ISO YYYY-MM-DD.
            footer_note: Small-print line at the bottom.
            template_path: Override the default template (per-specialist).
            out_path: Override the computed output path. If ``None``,
                writes to ``reports/<slug>-<YYYY-MM-DD>.html``.

        Returns:
            The path written to.

        Raises:
            ReportRenderError: On validation failures.
        """
        # Use the bundle's built-in validation for inputs (single source of truth).
        bundle = ReportBundle(
            customer_name=customer_name,
            description=description,
            chart_url=chart_url,
            chart_title=chart_title,
            bullets=list(bullets),
            slug=slug,
            report_date=report_date,
            footer_note=footer_note,
        )
        return self.render_bundle(bundle,
                                  template_path=template_path,
                                  out_path=out_path)

    def render_bundle(
        self,
        bundle: ReportBundle,
        *,
        template_path: Path | None = None,
        out_path: Path | None = None,
    ) -> Path:
        """Same as ``render`` but takes a pre-built :class:`ReportBundle`."""
        tpl_path = template_path or self.default_template_path
        assert tpl_path is not None  # post_init guarantees this
        if not tpl_path.exists():
            raise ReportRenderError(f"template not found: {tpl_path}")

        template = tpl_path.read_text(encoding="utf-8")

        # Validate every required placeholder is present in the template.
        missing = [p for p in PLACEHOLDERS if f"{{{{{p}}}}}" not in template]
        if missing:
            raise ReportRenderError(
                f"template is missing placeholders: {missing}"
            )

        executive_summary_html = _bullets_to_html(bundle.bullets)

        fields: dict[str, str] = {
            "CUSTOMER_NAME":     html.escape(bundle.customer_name, quote=True),
            "DESCRIPTION":       html.escape(bundle.description, quote=True),
            "CHART_URL":         bundle.chart_url,       # not escaped
            "CHART_TITLE":       html.escape(bundle.chart_title, quote=True),
            "REPORT_DATE":       html.escape(bundle.report_date, quote=True),
            "FOOTER_NOTE":       html.escape(bundle.footer_note, quote=True),
            "EXECUTIVE_SUMMARY": executive_summary_html,  # pre-escaped <li>s
        }

        rendered = _substitute(template, fields)

        # Safety net — no leftover {{FOO}} tokens.
        leftover = _PLACEHOLDER_PATTERN.findall(rendered)
        if leftover:
            raise ReportRenderError(
                f"unsubstituted placeholders remain: {sorted(set(leftover))}"
            )

        # Resolve output path.
        assert self.reports_dir is not None
        output = out_path or (
            self.reports_dir / f"{bundle.slug}-{bundle.report_date}.html"
        )
        output.parent.mkdir(parents=True, exist_ok=True)

        _atomic_write_text(output, rendered)
        logger.info("report rendered: %s (%d bullets)",
                    output, len(bundle.bullets))
        return output


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────


def _bullets_to_html(bullets: list[str]) -> str:
    """Produce the 8-space-indented ``<li>…</li>`` block the template expects.

    The template already provides the wrapping ``<ul>`` — this function
    returns just the lines between. Each bullet string is HTML-escaped
    so user-provided content can't inject markup.
    """
    lines = [
        f"        <li>{html.escape(b.strip(), quote=False)}</li>"
        for b in bullets
    ]
    return "\n".join(lines)


def _substitute(template: str, fields: dict[str, str]) -> str:
    """Replace every ``{{KEY}}`` with ``fields[KEY]``."""
    out = template
    for key, value in fields.items():
        out = out.replace(f"{{{{{key}}}}}", value)
    return out


def _atomic_write_text(path: Path, contents: str) -> None:
    """Write ``contents`` to ``path`` atomically via tempfile + rename.

    chokidar in the visor has ``awaitWriteFinish`` but belt-and-braces
    we use ``os.replace`` so the file appears at its final location in
    one step.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        suffix=".html", prefix=f".{path.name}.", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contents)
        os.replace(tmp_name, path)
    except Exception:
        # Cleanup the tempfile on failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
