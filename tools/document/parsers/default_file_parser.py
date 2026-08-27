import os
from docx import Document
from tools.document.file_cleaner import DocxCleaner


class DefaultFileParser:
    """Local parser for DOCX files."""

    @staticmethod
    def parse_file(file_path: str) -> str:
        """Determines file type and extracts text."""
        ext = os.path.splitext(file_path)[1].lower()

        if not os.path.exists(file_path):
            return f"Error: File not found at {file_path}"

        try:
            if ext == '.docx':
                clean_path = DefaultFileParser._clean_docx(file_path)
                try:
                    return DefaultFileParser._parse_docx(clean_path)
                finally:
                    if clean_path != file_path and os.path.exists(clean_path):
                        os.unlink(clean_path)
            else:
                return "Error: Only .docx files are supported"
        except Exception as e:
            return f"Error parsing file: {str(e)}"

    @staticmethod
    def _clean_docx(path: str) -> str:
        cleaner = DocxCleaner(input_path=path)
        return cleaner.clean()

    @staticmethod
    def _parse_docx(path: str) -> str:
        doc = Document(path)
        preamble = []   # Content before the first heading
        full_text = []
        found_first_heading = False
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            # Convert DOCX heading styles to markdown
            style = para.style.name if para.style else ""
            if style.startswith("Heading"):
                try:
                    level = int(style.split()[-1])
                    level = min(level, 6)
                except (ValueError, IndexError):
                    level = 1
                if not found_first_heading:
                    if preamble:
                        # Use same level as first heading so preamble is a sibling
                        full_text.append(f"{'#' * level} 合同首部信息")
                        full_text.extend(preamble)
                    found_first_heading = True
                full_text.append(f"{'#' * level} {text}")
            else:
                if found_first_heading:
                    full_text.append(text)
                else:
                    preamble.append(text)
        # If the document has no headings at all, emit preamble under a single heading
        if not found_first_heading and preamble:
            full_text.append("# 合同首部信息")
            full_text.extend(preamble)
        return "\n\n".join(full_text)

