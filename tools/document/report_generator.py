from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from tools.document.reporting import (
    DocxReportGenerator,
    render_markdown_pdf_with_pandoc,
    save_markdown_report,
)


class ReportGenerator:
    """Coordinates MD, DOCX, and PDF report generation."""

    @staticmethod
    def _append_generation_info(content: str, elapsed_seconds: float | None) -> str:
        if elapsed_seconds is None:
            return content

        mins, secs = divmod(int(elapsed_seconds), 60)
        return (
            content
            + "\n\n---\n\n"
            + "## 附：报告生成信息\n\n"
            + f"- **总耗时**: {mins}m {secs}s\n"
            + f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        )

    @staticmethod
    def generate_report(
        content: str,
        contract_name: str,
        elapsed_seconds: float = None,
        contract_path: str = None,
        results: list = None,
        output_dir: str | Path | None = None,
    ) -> dict:
        """Generate MD, DOCX, and PDF reports from markdown content."""
        if not output_dir:
            raise ValueError("output_dir is required for report generation.")

        report_dir = Path(output_dir)
        md_dir = report_dir / "reports_md"
        docx_dir = report_dir / "reports_docx"
        pdf_dir = report_dir / "reports_pdf"
        for directory in (md_dir, docx_dir, pdf_dir):
            directory.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"审查报告_{contract_name}_{timestamp}"
        report_content = ReportGenerator._append_generation_info(content, elapsed_seconds)

        errors: dict[str, str] = {}

        md_path = save_markdown_report(report_content, md_dir / f"{base_name}.md")

        docx_output_path = docx_dir / f"{base_name}.docx"
        docx_path = DocxReportGenerator.generate_docx_report(
            report_content,
            contract_path=contract_path,
            results=results,
            output_path=str(docx_output_path),
        )
        if not docx_path:
            errors["docx"] = "DOCX report generation failed."

        pdf_output_path = pdf_dir / f"{base_name}.pdf"
        pdf_path = None
        try:
            pdf_path = render_markdown_pdf_with_pandoc(md_path, pdf_output_path)
        except Exception as exc:
            errors["pdf"] = str(exc)

        return {
            "md": os.path.abspath(md_path),
            "docx": os.path.abspath(docx_path) if docx_path else None,
            "pdf": os.path.abspath(pdf_path) if pdf_path else None,
            "errors": errors,
        }

    @staticmethod
    def _build_report_base_name(contract_name: str) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"审查报告_{contract_name}_{timestamp}"

    @staticmethod
    def generate_markdown_report(
        content: str,
        contract_name: str,
        elapsed_seconds: float = None,
        output_dir: str | Path | None = None,
    ) -> str:
        """Generate only a markdown report."""
        if not output_dir:
            raise ValueError("output_dir is required for markdown report generation.")

        md_dir = Path(output_dir)
        md_dir.mkdir(parents=True, exist_ok=True)

        report_content = ReportGenerator._append_generation_info(content, elapsed_seconds)
        output_path = md_dir / f"{ReportGenerator._build_report_base_name(contract_name)}.md"
        return os.path.abspath(save_markdown_report(report_content, output_path))

    @staticmethod
    def generate_docx_report(
        contract_name: str,
        contract_path: str,
        results: list,
        summary_sections: dict | None = None,
        output_dir: str | Path | None = None,
        output_path: str | Path | None = None,
        annotation_events_path: str | Path | None = None,
    ) -> str | None:
        """Generate only the annotated original-contract DOCX."""
        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            if not output_dir:
                raise ValueError("output_dir or output_path is required for DOCX report generation.")
            docx_dir = Path(output_dir)
            docx_dir.mkdir(parents=True, exist_ok=True)
            output_path = docx_dir / f"{ReportGenerator._build_report_base_name(contract_name)}.docx"

        docx_path = DocxReportGenerator.generate_annotated_docx(
            contract_path=contract_path,
            results=results,
            output_path=str(output_path),
            summary_sections=summary_sections,
            annotation_events_path=(
                str(annotation_events_path) if annotation_events_path is not None else None
            ),
        )
        return os.path.abspath(docx_path) if docx_path else None

    @staticmethod
    def generate_pdf_report(
        content: str,
        contract_name: str,
        elapsed_seconds: float = None,
        output_dir: str | Path | None = None,
    ) -> str:
        """Generate only a PDF report from markdown content."""
        if not output_dir:
            raise ValueError("output_dir is required for PDF report generation.")

        report_dir = Path(output_dir)
        md_dir = report_dir / "reports_md"
        pdf_dir = report_dir / "reports_pdf"
        md_dir.mkdir(parents=True, exist_ok=True)
        pdf_dir.mkdir(parents=True, exist_ok=True)

        base_name = ReportGenerator._build_report_base_name(contract_name)
        report_content = ReportGenerator._append_generation_info(content, elapsed_seconds)
        md_path = save_markdown_report(report_content, md_dir / f"{base_name}.md")
        pdf_path = render_markdown_pdf_with_pandoc(md_path, pdf_dir / f"{base_name}.pdf")
        return os.path.abspath(pdf_path)
