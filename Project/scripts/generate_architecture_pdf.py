#!/usr/bin/env python3
"""Convert CommonSource-Technical-Overview.md to PDF using fpdf2."""

from __future__ import annotations

import re
from pathlib import Path

from fpdf import FPDF

ROOT = Path(__file__).resolve().parents[1]
MD_PATH = ROOT / "docs" / "CommonSource-Technical-Overview.md"
PDF_PATH = ROOT / "docs" / "CommonSource-Technical-Overview.pdf"


def sanitize(text: str) -> str:
    text = text.replace("\t", "    ")
    text = text.encode("latin-1", errors="replace").decode("latin-1")
    return text


class DocPDF(FPDF):
    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 8, f"Page {self.page_no()}", align="C")


def mc(pdf: FPDF, h: float, text: str) -> None:
    w = pdf.epw
    pdf.multi_cell(w, h, sanitize(text))


def write_pdf(md_text: str, out_path: Path) -> None:
    pdf = DocPDF()
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)

    in_code = False
    for raw in md_text.splitlines():
        line = raw.rstrip()

        if line.strip().startswith("```"):
            in_code = not in_code
            continue

        if re.match(r"^-{3,}$", line.strip()):
            pdf.ln(2)
            continue

        if in_code:
            pdf.set_font("Courier", size=7)
            chunk = sanitize(line) or " "
            if len(chunk) > 110:
                chunk = chunk[:107] + "..."
            mc(pdf, 4, chunk)
            pdf.set_font("Helvetica", size=10)
            continue

        if not line.strip():
            pdf.ln(3)
            continue

        if line.startswith("# "):
            pdf.ln(4)
            pdf.set_font("Helvetica", "B", 16)
            mc(pdf, 8, line[2:].strip())
            pdf.set_font("Helvetica", size=10)
            continue

        if line.startswith("## "):
            pdf.ln(3)
            pdf.set_font("Helvetica", "B", 13)
            mc(pdf, 7, line[3:].strip())
            pdf.ln(1)
            pdf.set_font("Helvetica", size=10)
            continue

        if line.startswith("### "):
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 11)
            mc(pdf, 6, line[4:].strip())
            pdf.set_font("Helvetica", size=10)
            continue

        if line.startswith("> "):
            pdf.set_font("Helvetica", "I", 9)
            mc(pdf, 5, line[2:].strip())
            pdf.set_font("Helvetica", size=10)
            continue

        if line.startswith("|") and "|" in line[1:]:
            if re.match(r"^\|[\s\-:|]+\|$", line):
                continue
            pdf.set_font("Helvetica", size=9)
            mc(pdf, 5, re.sub(r"\s*\|\s*", " | ", line.strip("| ")))
            pdf.set_font("Helvetica", size=10)
            continue

        if line.startswith("- ") or line.startswith("* "):
            mc(pdf, 5, "  - " + line[2:].strip())
            continue

        text = line
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        mc(pdf, 5, text)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))


def main() -> None:
    if not MD_PATH.exists():
        raise SystemExit(f"Missing: {MD_PATH}")
    md_text = MD_PATH.read_text(encoding="utf-8")
    write_pdf(md_text, PDF_PATH)
    print(f"Wrote {PDF_PATH}")


if __name__ == "__main__":
    main()
