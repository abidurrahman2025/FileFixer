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


MAX_PART_BYTES = 100 * 1024 * 1024      # biggest piece of a Word/Excel file we will unpack into memory


def _part(z: zipfile.ZipFile, name: str) -> bytes:
    """Read one piece of a zip file, refusing a piece that unpacks to something enormous (a zip bomb)."""
    with z.open(name) as f:                       # KeyError if it is missing, like z.read
        data = f.read(MAX_PART_BYTES + 1)
    if len(data) > MAX_PART_BYTES:
        raise UserError("This file unpacks to far more data than a normal document, so I won't open it.")
    return data


def is_document(p: Path) -> bool:
    return p.suffix.lower() in DOC_TYPES


def _docx(p: Path) -> str:
    with zipfile.ZipFile(p) as z:
        root = ET.fromstring(_part(z, "word/document.xml"))
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
    """Column number (A = 1) of a cell reference like 'C7'. 0 when there are no letters in it."""
    m = re.match(r"\$?([A-Za-z]+)", ref or "")
    n = 0
    for ch in (m.group(1).upper() if m else ""):
        n = n * 26 + ord(ch) - 64
    return n


_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _xlsx_sheet_files(z: zipfile.ZipFile) -> list:
    """[(sheet name, file inside the zip)] in the order the workbook shows its sheets.

    Falls back to sheet1.xml, sheet2.xml ... (numeric order) if the workbook can't be understood.
    """
    names = set(z.namelist())
    try:
        wb = ET.fromstring(_part(z, "xl/workbook.xml"))
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(_part(z, "xl/_rels/workbook.xml.rels"))
                if r.tag == _PKG_REL + "Relationship"}
        found = []
        for sh in wb.iter(_S + "sheet"):
            target = rels.get(sh.get(_REL + "id"))
            if not target:
                continue
            path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            path = re.sub(r"[^/]+/\.\./", "", path)
            if path in names and "/worksheets/" in path:
                found.append((sh.get("name") or Path(path).stem, path))
        if found:
            return found
    except (KeyError, ET.ParseError, AttributeError):
        pass
    files = sorted((n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                   key=lambda n: int(re.findall(r"\d+", n)[0]))
    return [(Path(n).stem, n) for n in files]


def xlsx_sheet_names(p: Path) -> list:
    """The real sheet names of a workbook, in workbook order."""
    try:
        with zipfile.ZipFile(p) as z:
            return [name for name, _ in _xlsx_sheet_files(z)]
    except (zipfile.BadZipFile, OSError):
        return []


def _cell_text(c, shared: list) -> str:
    v = c.find(_S + "v")
    t = c.get("t")
    if t == "inlineStr":
        return "".join(x.text or "" for x in c.iter(_S + "t"))
    if t == "e" or v is None or v.text is None:
        return ""                                  # error cells (#DIV/0! ...) and empty cells show as blank
    if t == "s":
        try:
            return shared[int(v.text)]
        except (ValueError, IndexError):
            return ""
    if t == "b":
        return "TRUE" if v.text.strip() == "1" else "FALSE"
    return v.text


def xlsx_rows(p: Path, first_sheet_only: bool = False) -> list:
    """The cells of a workbook as a list of rows (each a list of text), sheets in workbook order."""
    with zipfile.ZipFile(p) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(_part(z, "xl/sharedStrings.xml")).iter(_S + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_S + "t")))
        out = []
        for _name, path in _xlsx_sheet_files(z):
            for row in ET.fromstring(_part(z, path)).iter(_S + "row"):
                cells, last = [], 0
                for c in row.iter(_S + "c"):
                    # A cell with no usable reference goes right after the one before it.
                    idx = max(_col(c.get("r", "")), last + 1)
                    cells += [""] * (idx - last - 1)
                    last = idx
                    cells.append(_cell_text(c, shared))
                out.append(cells)
            if first_sheet_only:
                break
    return out


def _xlsx(p: Path) -> str:
    # Tabs and line breaks inside a cell would break the row/column layout of the text, so they become spaces.
    return "\n".join("\t".join(re.sub(r"[\t\r\n]+", " ", c) for c in row) for row in xlsx_rows(p))


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
