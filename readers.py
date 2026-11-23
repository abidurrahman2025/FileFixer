"""Read text out of the file types FileFixer supports.

Plain text (any encoding), Word (.docx), Excel (.xlsx) and PDF (.pdf, needs the free 'pypdf' add-on).
Only stdlib is needed except for PDF.  Anything else is refused with a clear message.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from engine import NotText, UserError, decode_bytes

MAX_BYTES = 200 * 1024 * 1024
PLAIN_HINT = {".txt", ".md", ".csv", ".tsv", ".log", ".json", ".xml", ".html", ".htm", ".ini", ".yml", ".yaml", ".py",
              ".js", ".css", ".sql", ".rtf", ".cfg", ".bat", ".ps1", ".sh", ".tex", ""}
DOC_TYPES = {".docx", ".xlsx", ".pdf"}

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def is_document(p: Path) -> bool:
    return p.suffix.lower() in DOC_TYPES


def _docx(p: Path) -> str:
    with zipfile.ZipFile(p) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for para in root.iter(_W + "p"):
        parts = []
        for el in para.iter():
            if el.tag == _W + "t":
                parts.append(el.text or "")
            elif el.tag == _W + "tab":
                parts.append("\t")
            elif el.tag in (_W + "br", _W + "cr"):
                parts.append("\n")
        paras.append("".join(parts))
    return "\n".join(paras)


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n


def _xlsx(p: Path) -> str:
    with zipfile.ZipFile(p) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_S + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_S + "t")))
        sheets = sorted((n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[0]))
        out = []
        for name in sheets:
            for row in ET.fromstring(z.read(name)).iter(_S + "row"):
                cells, last = [], 0
                for c in row.iter(_S + "c"):
                    idx = _col(c.get("r", "A1"))
                    cells += [""] * (idx - last - 1)
                    last = idx
                    v = c.find(_S + "v")
                    t = c.get("t")
                    if t == "s" and v is not None:
                        cells.append(shared[int(v.text)])
                    elif t == "inlineStr":
                        cells.append("".join(x.text or "" for x in c.iter(_S + "t")))
                    else:
                        cells.append(v.text if v is not None and v.text else "")
                out.append("\t".join(cells))
    return "\n".join(out)


def _pdf(p: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise UserError("Reading PDF files needs a free add-on.\nOpen a command window and type:   pip install pypdf\n"
                        "then start FileFixer again.")
    try:
        reader = PdfReader(str(p))
        if reader.is_encrypted:
            raise UserError(f"{p.name} is password-protected, so I can't read it.")
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except UserError:
        raise
    except Exception as e:
        raise UserError(f"I couldn't read {p.name} as a PDF: {e}")
    if not text.strip():
        raise UserError(f"{p.name} has no readable text (it may be a scanned picture).")
    return text


def read_document(p: Path) -> tuple:
    """Return (text, encoding_or_kind).  Raises UserError with a plain-language reason on failure."""
    try:
        if p.stat().st_size > MAX_BYTES:
            raise UserError(f"{p.name} is larger than 200 MB, which is too big for this job.")
        ext = p.suffix.lower()
        if ext == ".docx":
            return _docx(p), "docx"
        if ext == ".xlsx":
            return _xlsx(p), "xlsx"
        if ext == ".pdf":
            return _pdf(p), "pdf"
        return decode_bytes(p.read_bytes())
    except NotText:
        raise UserError(f"{p.name} doesn't look like a text, Word, Excel or PDF file.")
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise UserError(f"{p.name} looks damaged or isn't a real {p.suffix} file.")
    except OSError as e:
        raise UserError(f"I couldn't read {p.name}: {e.strerror or e}")
