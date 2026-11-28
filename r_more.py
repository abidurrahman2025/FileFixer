"""More jobs: find files, extract emails/links, line tools, split text, CSV tools, folder report."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import zipfile
from xml.etree import ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

import readers
from engine import (Action, Namer, Plan, SKIP_NAMES, UserError, check_cancel, check_editable, check_file,
                    check_folder, decode_bytes, friendly, norm, parse_exts, size_text)
from r_extract import RESULT
from recipes import Field, Recipe, check_new_name, natural_key, note_problems, plural, rel, with_extension

NL = "\r\n" if os.name == "nt" else "\n"


def output_fields(default_help: str) -> list:
    return [
        Field("result", "Where should the results go?", "choice", RESULT[0], RESULT),
        Field("outname", "Name for the new file", "text", "", help=default_help),
        Field("outdir", "Save the new file in", "folder", "", help="Optional. Leave empty to save it next to the original."),
    ]


def deliver(plan: Plan, src: Optional[Path], body: str, v: dict, default_name: str,
            enc: str = "utf-8", what: str = "results") -> Plan:
    """Attach either 'new file' or 'replace original (with backup)' to a plan."""
    if v["result"] == RESULT[1] and src is not None:
        if readers.is_document(src):
            raise UserError("Replacing the original is only possible for plain text files. "
                            "Choose “Create a new file” for Word, Excel and PDF files.")
        check_editable(src)
        text, _ = decode_bytes(src.read_bytes())
        new = body.replace("\n", "\r\n") if "\r\n" in text else body
        plan.actions.append(Action("edit", f"The content of {src.name} will be replaced by the {what}", src, src,
                                   transform=lambda t, n=new: n))
        plan.open_path = src
        plan.notes.append("The original is saved to the Recycle folder first, so “Undo last change” restores it.")
        return plan
    name = (v.get("outname") or "").strip() or default_name
    name = with_extension(name, Path(default_name).suffix or ".txt")
    check_new_name(name, "The file name")
    outdir = check_folder(v["outdir"]) if (v.get("outdir") or "").strip() else (src.parent if src else None)
    if outdir is None:
        raise UserError("Please choose where to save the new file.")
    dst = Namer().reserve(outdir / name)
    if dst.name != name:
        plan.notes.append(f"A file called {name} already exists, so the new one will be named {dst.name}.")
    text = body.replace("\n", NL)
    plan.actions.append(Action("write", f"A new file will be created:   {dst}", dst=dst, produce=lambda t=text, e=enc: (t, e)))
    plan.open_path = dst
    if src is not None:
        plan.notes.append(f"The original file {src.name} is not changed.")
    return plan


def preview_lines(lines: list, n: int = 60) -> list:
    return lines[:n] + ([f"... and {len(lines) - n} more"] if len(lines) > n else [])


# --------------------------------------------------------------------------
def _walk(folder: Path, sub: bool, want_dirs: bool = False, problems: Optional[list] = None):
    """Yield (path, is_dir) for everything below `folder`, never following links.

    Anything that can't be read (access denied, a disk error ...) is never hidden: a plain-English line
    about it is added to `problems` so the job can tell the user the result may be incomplete.
    """
    def warn(path, e) -> None:
        if problems is not None:
            problems.append(f"{path}: {friendly(e)}")

    stack = [folder]
    while stack:
        check_cancel()
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError as e:
            warn(d, e)
            continue
        for e in entries:
            if e.name.lower() in SKIP_NAMES or e.name.startswith("~$"):
                continue
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if want_dirs:
                        yield Path(e.path), True
                    if sub:
                        stack.append(Path(e.path))
                elif e.is_file(follow_symlinks=False):
                    yield Path(e.path), False
            except OSError as ex:
                warn(e.path, ex)


# ==========================================================================
class FindFiles(Recipe):
    key = "find"
    name = "Find files"
    blurb = "Search a folder by name, type, date or size, or find empty folders. You get a list and can save it."
    fields = [
        Field("folder", "Folder to search", "folder"),
        Field("sub", "Include subfolders", "check", True),
        Field("what", "Look for", "choice", "Files", ["Files", "Empty folders"]),
        Field("name_has", "Name contains", "text", "", help="Optional. Capitals don't matter."),
        Field("types", "Only these file types", "text", "", help="Optional. For example: pdf, docx."),
        Field("days", "Changed in the last (days)", "text", "", help="Optional. For example: 30."),
        Field("min_mb", "Bigger than (MB)", "text", "", help="Optional. For example: 100 to find unusually large files."),
        Field("order", "List order", "choice", "By name", ["By name", "Biggest first", "Newest first"]),
        Field("save", "Save the list as (optional)", "text", "", help="A file name such as found.txt. Leave empty to only view the list."),
    ]

    def plan(self, v):
        folder = check_folder(v["folder"])
        exts = parse_exts(v["types"])
        needle = v["name_has"].strip().lower()
        try:
            days = float(v["days"]) if v["days"].strip() else None
            mb = float(v["min_mb"]) if v["min_mb"].strip() else None
        except ValueError:
            raise UserError("“Changed in the last” and “Bigger than” need to be numbers, like 30 or 100.")
        plan = Plan(self.name)
        rows, problems = [], []
        if v["what"] == "Empty folders":
            for p, is_dir in _walk(folder, v["sub"], want_dirs=True, problems=problems):
                if is_dir and (not needle or needle in p.name.lower()):
                    try:
                        with os.scandir(p) as it:
                            empty = next(it, None) is None
                        if empty:
                            rows.append((p, 0, p.stat().st_mtime))
                    except OSError as e:
                        problems.append(f"{p}: {friendly(e)}")
        else:
            cutoff = time.time() - days * 86400 if days is not None else None
            for p, is_dir in _walk(folder, v["sub"], problems=problems):
                if needle and needle not in p.name.lower():
                    continue
                if exts and p.suffix.lower() not in exts:
                    continue
                try:
                    st = p.stat()
                except OSError as e:
                    problems.append(f"{p}: {friendly(e)}")
                    continue
                if cutoff is not None and st.st_mtime < cutoff:
                    continue
                if mb is not None and st.st_size < mb * 1024 * 1024:
                    continue
                rows.append((p, st.st_size, st.st_mtime))
        if v["order"] == "Biggest first":
            rows.sort(key=lambda r: -r[1])
        elif v["order"] == "Newest first":
            rows.sort(key=lambda r: -r[2])
        else:
            rows.sort(key=lambda r: natural_key(str(r[0])))
        lines = [f"{rel(p, folder)}   ({size_text(s)}, {datetime.fromtimestamp(m):%Y-%m-%d})" if s or v["what"] == "Files"
                 else f"{rel(p, folder)}" for p, s, m in rows]
        plan.report = preview_lines(lines, 300)
        note_problems(plan, problems, "the list")
        noun = "empty folder" if v["what"] == "Empty folders" else "file"
        total = sum(s for _, s, _ in rows)
        plan.summary = (f"Found {plural(len(rows), noun)}" + (f" ({size_text(total)} in total)." if rows and noun == "file" else ".")
                        if rows else f"No matching {noun}s were found.")
        if rows and v["save"].strip():
            text = "\n".join(str(p) for p, _, _ in rows)
            deliver(plan, None, text, dict(v, outname=v["save"], result=RESULT[0], outdir=str(folder)), "found.txt", what="list")
        return plan


# ==========================================================================
EMAIL = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
URL = r"https?://[^\s<>\"')\]]+[^\s<>\"')\].,;:!?]"
WHAT = ["Email addresses", "Web links (URLs)", "My own pattern (regular expression)"]


class ExtractPatterns(Recipe):
    key = "emails"
    name = "Extract emails and links"
    blurb = "Collect every email address or web link from a file (text, Word, Excel or PDF), or from a whole folder."
    fields = [
        Field("file", "File to read", "file", help="Leave empty if you choose a folder below."),
        Field("folder", "…or a whole folder", "folder", "", help="Reads every text, Word, Excel and PDF file in it."),
        Field("sub", "Include subfolders", "check", False),
        Field("what", "Extract", "choice", WHAT[0], WHAT),
        Field("pattern", "Your pattern", "text", "", help="Only for “My own pattern”."),
        Field("dedupe", "Show each result once", "check", True),
        Field("numbering", "Numbered list", "check", False),
    ] + output_fields("Leave empty to use Extracted.txt.")

    def plan(self, v):
        if v["file"].strip():
            files = [check_file(v["file"])]
            src = files[0]
        elif v["folder"].strip():
            folder = check_folder(v["folder"])
            walk_problems: list = []
            files = [p for p, _ in _walk(folder, v["sub"], problems=walk_problems)
                     if p.suffix.lower() in readers.PLAIN_HINT | readers.DOC_TYPES]
            src = None
            if not files:
                raise UserError("There are no readable files in that folder.")
        else:
            raise UserError("Please choose a file or a folder.")
        pat = {WHAT[0]: EMAIL, WHAT[1]: URL}.get(v["what"], v["pattern"])
        if not pat:
            raise UserError("Please type your pattern.")
        try:
            rx = re.compile(pat)
        except re.error as e:
            raise UserError(f"This pattern isn't valid: {e}")
        found, skipped = [], 0
        for p in files:
            try:
                text, _ = readers.read_document(p)
            except UserError:
                if src is not None:
                    raise
                skipped += 1
                continue
            found += [m.group(0) for m in rx.finditer(text)]
        if v["dedupe"]:
            found = list(dict.fromkeys(found))
        plan = Plan(self.name)
        if skipped:
            plan.notes.append(f"{plural(skipped, 'file')} couldn't be read and were skipped.")
        if src is None:
            note_problems(plan, walk_problems)
        if not found:
            plan.summary = ("None of the " + plural(len(files), "file") + " I could read had anything matching."
                            if skipped < len(files) else
                            "None of the files in that folder could be read, so nothing was found.")
            return plan
        body = "\n".join(f"{i}. {x}" for i, x in enumerate(found, 1)) if v["numbering"] else "\n".join(found)
        plan.report = preview_lines(body.split("\n"))
        if src is None and v["result"] != RESULT[0]:
            raise UserError("When reading a whole folder, results can only go into a new file.")
        if src is None:
            v = dict(v, outdir=v["outdir"] or v["folder"])
        deliver(plan, src, body, v, "Extracted.txt")
        plan.summary = f"I found {plural(len(found), 'result')}. They will be saved as {plan.actions[0].dst.name}."
        return plan


# ==========================================================================
LINE_OPS = ["Remove duplicate lines (keep the first)", "Sort lines A to Z", "Sort lines Z to A",
            "Sort lines by number at the start", "Remove blank lines", "Trim spaces at the ends of lines",
            "Reverse the order of lines", "Keep only lines containing some text", "Remove lines containing some text"]


class LineTools(Recipe):
    key = "lines"
    name = "Clean up lines of text"
    blurb = "Remove duplicate lines, sort, trim, filter or reverse the lines of a text file."
    fields = [
        Field("file", "Text file", "file"),
        Field("op", "What do you want to do?", "choice", LINE_OPS[0], LINE_OPS),
        Field("text", "Text to look for", "text", "", help="Only for the last two choices."),
        Field("case", "Match capital letters exactly", "check", False),
    ] + output_fields("Leave empty to use the file's name + Clean.txt.")

    def plan(self, v):
        src = check_file(v["file"])
        text, _ = readers.read_document(src)
        lines = text.splitlines()
        op, needle = v["op"], v["text"]
        fold = (lambda s: s) if v["case"] else (lambda s: s.casefold())
        if op == LINE_OPS[0]:
            seen, out = set(), []
            for ln in lines:
                k = fold(ln)
                if k not in seen:
                    seen.add(k)
                    out.append(ln)
        elif op in LINE_OPS[1:3]:
            out = sorted(lines, key=lambda s: natural_key(s), reverse=op == LINE_OPS[2])
        elif op == LINE_OPS[3]:
            def num(s):
                m = re.match(r"\s*(-?\d+(?:\.\d+)?)", s)
                return (0, float(m.group(1))) if m else (1, 0.0)
            out = sorted(lines, key=num)
        elif op == LINE_OPS[4]:
            out = [ln for ln in lines if ln.strip()]
        elif op == LINE_OPS[5]:
            out = [ln.strip() for ln in lines]
        elif op == LINE_OPS[6]:
            out = lines[::-1]
        else:
            if needle == "":
                raise UserError("Please type the text to look for.")
            keep = op == LINE_OPS[7]
            out = [ln for ln in lines if (fold(needle) in fold(ln)) == keep]
        plan = Plan(self.name)
        if out == lines:
            plan.summary = "Nothing would change."
            return plan
        body = "\n".join(out)
        plan.report = preview_lines(out)
        deliver(plan, src, body, v, f"{src.stem}Clean.txt", what="cleaned lines")
        plan.summary = f"{plural(len(lines), 'line')} become {plural(len(out), 'line')}."
        return plan


# ==========================================================================
class SplitText(Recipe):
    key = "split"
    name = "Split a large text file"
    blurb = "Cut a big text file into smaller files. The original stays untouched."
    fields = [
        Field("file", "Text file", "file"),
        Field("by", "Split by", "choice", "Lines per file", ["Lines per file", "Number of parts"]),
        Field("n", "How many", "text", "1000"),
        Field("outdir", "Save the parts in", "folder", "", help="Optional. Leave empty to make a new folder next to the original."),
    ]

    def plan(self, v):
        src = check_file(v["file"])
        text, enc = readers.read_document(src)
        if readers.is_document(src):
            raise UserError("Splitting works on plain text files.")
        try:
            n = int(v["n"])
            assert n > 0
        except (ValueError, AssertionError):
            raise UserError("“How many” needs to be a whole number above zero.")
        lines = text.splitlines(keepends=True)
        if not lines:
            raise UserError("That file is empty.")
        per = n if v["by"] == "Lines per file" else -(-len(lines) // n)
        chunks = [lines[i:i + per] for i in range(0, len(lines), per)]
        if len(chunks) < 2:
            raise UserError("That would produce only one part, so there is nothing to split.")
        outdir = check_folder(v["outdir"]) if v["outdir"].strip() else src.parent
        folder = Namer().reserve(outdir / f"{src.stem} parts") if not v["outdir"].strip() else outdir
        plan = Plan(self.name)
        namer = Namer()
        width = max(3, len(str(len(chunks))))
        for i, ch in enumerate(chunks, 1):
            dst = namer.reserve(folder / f"{src.stem}_part{str(i).zfill(width)}{src.suffix or '.txt'}")
            body = "".join(ch)
            plan.actions.append(Action("write", f"{dst.name}   ({plural(len(ch), 'line')})", dst=dst,
                                       produce=lambda b=body, e=enc: (b, e)))
        plan.summary = f"{src.name} will be split into {plural(len(chunks), 'part')} in {folder}."
        plan.notes.append("The original file is not changed.")
        plan.open_path = folder
        return plan


# ==========================================================================
CSV_OPS = ["Keep only rows where a column matches", "Keep only some columns", "Remove duplicate rows",
           "Sort rows by a column", "Summarize a column (count, total, average)", "Convert to another format"]
CONDS = ["equals", "contains", "starts with", "is greater than", "is less than"]
FORMATS = ["Tab-separated (.tsv)", "JSON (.json)", "Comma-separated (.csv)"]


def read_table(p: Path) -> tuple:
    """Return (header, rows, delimiter, encoding)."""
    if p.suffix.lower() == ".json":
        text, enc = decode_bytes(p.read_bytes())
        try:
            data = json.loads(text)
        except ValueError as e:
            raise UserError(f"{p.name} isn't valid JSON: {e}")
        if not (isinstance(data, list) and data and all(isinstance(d, dict) for d in data)):
            raise UserError("Only JSON that is a list of records (objects) can be turned into a table.")
        head = list(dict.fromkeys(k for d in data for k in d))
        return head, [[("" if d.get(k) is None else str(d.get(k))) for k in head] for d in data], ",", enc
    if p.suffix.lower() == ".xlsx":
        try:
            rows = readers.xlsx_rows(p, first_sheet_only=True)        # a table is one sheet, the first one
        except (zipfile.BadZipFile, KeyError, ET.ParseError):
            raise UserError(f"{p.name} looks damaged or isn't a real .xlsx file.")
        except OSError as e:
            raise UserError(f"I couldn't read {p.name}: {e.strerror or e}")
        rows = [r for r in rows if any(c.strip() for c in r)]
        if not rows:
            raise UserError(f"{p.name} has no data in its first sheet.")
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]           # ragged rows become a rectangle
        return rows[0], rows[1:], ",", "utf-8"
    text, enc = readers.read_document(p)
    try:
        delim = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
    except csv.Error:
        delim = "\t" if p.suffix.lower() == ".tsv" else ","
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delim))
    rows = [r for r in rows if r]
    if not rows:
        raise UserError(f"{p.name} has no data.")
    return rows[0], rows[1:], delim, enc


def write_table(head: list, rows: list, delim: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delim, lineterminator="\n")
    w.writerow(head)
    w.writerows(rows)
    return buf.getvalue().rstrip("\n")


def col_index(head: list, ref: str) -> int:
    ref = ref.strip()
    if not ref:
        raise UserError("Please type the column name (as written in the first row) or its number.")
    low = [h.strip().lower() for h in head]
    if ref.lower() in low:
        return low.index(ref.lower())
    if ref.isdigit() and 1 <= int(ref) <= len(head):
        return int(ref) - 1
    raise UserError(f"There is no column called “{ref}”. The columns are: " + ", ".join(head))


def to_num(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return None


class CsvTools(Recipe):
    key = "csv"
    name = "Work with a spreadsheet file"
    blurb = "Filter rows, keep columns, remove duplicates, sort, summarize or convert a CSV (also reads .xlsx and .json)."
    fields = [
        Field("file", "Spreadsheet file", "file", help="A .csv, .tsv, .xlsx or list-style .json file."),
        Field("op", "What do you want to do?", "choice", CSV_OPS[0], CSV_OPS),
        Field("column", "Column", "text", "", help="The column name from the first row, or its number. For “Keep only some columns”, "
                                                    "list several separated by commas."),
        Field("cond", "Rows where the column", "choice", CONDS[0], CONDS, help="Only for “Keep only rows where a column matches”."),
        Field("value", "Value", "text", ""),
        Field("desc", "Sort biggest/Z first", "check", False),
        Field("fmt", "Convert to", "choice", FORMATS[0], FORMATS),
    ] + output_fields("Leave empty to use the file's name + Result.")[1:]

    def plan(self, v):
        src = check_file(v["file"])
        head, rows, delim, enc = read_table(src)
        op, plan = v["op"], Plan(self.name)
        ext = ".csv" if delim == "," else (".tsv" if delim == "\t" else src.suffix or ".csv")
        if op == CSV_OPS[0]:
            i, cond, val = col_index(head, v["column"]), v["cond"], v["value"]
            out = []
            for r in rows:
                cell = r[i] if i < len(r) else ""
                if cond in CONDS[3:]:
                    a, b = to_num(cell), to_num(val)
                    if b is None:
                        raise UserError("For greater/less than, the value must be a number.")
                    ok = a is not None and (a > b if cond == CONDS[3] else a < b)
                elif cond == CONDS[0]:
                    ok = cell.strip().casefold() == val.strip().casefold()
                elif cond == CONDS[1]:
                    ok = val.casefold() in cell.casefold()
                else:
                    ok = cell.casefold().startswith(val.casefold())
                if ok:
                    out.append(r)
            plan.summary = f"{plural(len(out), 'row')} of {len(rows)} match."
            res = (head, out)
        elif op == CSV_OPS[1]:
            idx = [col_index(head, c) for c in v["column"].split(",")]
            res = ([head[i] for i in idx], [[r[i] if i < len(r) else "" for i in idx] for r in rows])
            plan.summary = f"Keeping {plural(len(idx), 'column')}."
        elif op == CSV_OPS[2]:
            seen, out = set(), []
            for r in rows:
                k = tuple(r)
                if k not in seen:
                    seen.add(k)
                    out.append(r)
            res = (head, out)
            plan.summary = f"{plural(len(rows) - len(out), 'duplicate row')} removed, {len(out)} remain."
        elif op == CSV_OPS[3]:
            i = col_index(head, v["column"])
            cell = lambda r: r[i] if i < len(r) else ""          # short (ragged) rows have an empty cell here
            nums = [to_num(cell(r)) for r in rows]
            numeric = all(n is not None for n in nums if n is not None) and any(n is not None for n in nums)
            key = (lambda r: (to_num(cell(r)) is None, to_num(cell(r)) or 0)) if numeric else (lambda r: natural_key(cell(r)))
            res = (head, sorted(rows, key=key, reverse=v["desc"]))
            plan.summary = f"{plural(len(rows), 'row')} sorted by {head[i]}."
        elif op == CSV_OPS[4]:
            i = col_index(head, v["column"])
            cells = [r[i] if i < len(r) else "" for r in rows]
            nums = [n for n in map(to_num, cells) if n is not None]
            lines = [f"Column: {head[i]}", f"Rows: {len(cells)}", f"Empty cells: {sum(1 for c in cells if not c.strip())}"]
            if nums:
                lines += [f"Numbers found: {len(nums)}", f"Total: {sum(nums):g}", f"Average: {sum(nums) / len(nums):g}",
                          f"Smallest: {min(nums):g}", f"Biggest: {max(nums):g}"]
            else:
                top = Counter(c for c in cells if c.strip()).most_common(5)
                lines += ["No numbers in this column. Most common values:"] + [f"  {k}  ({n})" for k, n in top]
            plan.report = lines
            plan.summary = "Summary of the column (nothing was changed)."
            body = "\n".join(lines)
            if v["outname"].strip():
                deliver(plan, src, body, dict(v, result=RESULT[0]), "Summary.txt", what="summary")
            return plan
        else:
            fmt = v["fmt"]
            if fmt == FORMATS[1]:
                body = json.dumps([dict(zip(head, r)) for r in rows], indent=2, ensure_ascii=False)
                new_ext = ".json"
            else:
                d = "\t" if fmt == FORMATS[0] else ","
                body, new_ext = write_table(head, rows, d), (".tsv" if d == "\t" else ".csv")
            plan.summary = f"{plural(len(rows), 'row')} converted."
            deliver(plan, None, body, dict(v, result=RESULT[0], outdir=v["outdir"] or str(src.parent)),
                    f"{src.stem}{new_ext}", enc="utf-8-sig" if enc == "utf-8-sig" else "utf-8")
            return plan
        head2, rows2 = res
        body = write_table(head2, rows2, delim)
        plan.report = preview_lines([delim.join(head2)] + [delim.join(r) for r in rows2[:60]], 61)
        encoding = "utf-8-sig" if enc == "utf-8-sig" else "utf-8"
        outv = dict(v, result=RESULT[0], outdir=v["outdir"] or str(src.parent))
        deliver(plan, None, body, outv, f"{src.stem}Result{ext}", enc=encoding)
        return plan


class CombineCsv(Recipe):
    key = "combinecsv"
    name = "Combine spreadsheet files"
    blurb = "Stack the rows of many CSV files into one. Columns are matched by name; the originals are untouched."
    fields = [
        Field("folder", "Folder with the CSV files", "folder"),
        Field("sub", "Include subfolders", "check", False),
        Field("outname", "Name of the new file", "text", "combined.csv"),
        Field("addsrc", "Add a column showing each row's file name", "check", True),
    ]

    def plan(self, v):
        folder = check_folder(v["folder"])
        name = with_extension(v["outname"].strip() or "combined.csv", ".csv")
        check_new_name(name, "The file name")
        problems: list = []
        files = sorted((p for p, _ in _walk(folder, v["sub"], problems=problems)
                        if p.suffix.lower() == ".csv" and norm(p.name) != norm(name)),
                       key=lambda p: natural_key(p.name))
        if len(files) < 2:
            raise UserError("I need at least two .csv files in that folder.")
        heads, tables = [], []
        for p in files:
            try:
                h, r, d, _ = read_table(p)
            except UserError as e:
                raise UserError(f"{p.name} can't be combined: {e}")
            tables.append((p, h, r))
            heads += [x for x in h if x not in heads]
        out_head = (["Source file"] if v["addsrc"] else []) + heads
        rows = []
        for p, h, r in tables:
            pos = {c: i for i, c in enumerate(h)}
            for row in r:
                rows.append(([p.name] if v["addsrc"] else []) + [row[pos[c]] if c in pos and pos[c] < len(row) else "" for c in heads])
        plan = Plan(self.name)
        note_problems(plan, problems)
        plan.report = [f"{p.name}   ({plural(len(r), 'row')})" for p, _, r in tables]
        deliver(plan, None, write_table(out_head, rows, ","), {"result": RESULT[0], "outname": name, "outdir": str(folder)},
                "combined.csv")
        plan.summary = f"{plural(len(files), 'file')} will be combined into {plural(len(rows), 'row')}."
        return plan


# ==========================================================================
class FolderReport(Recipe):
    key = "report"
    name = "Folder report"
    blurb = "Summarize a folder: how many files, how big, which types, the biggest and newest files. Save it as a text file."
    fields = [
        Field("folder", "Folder", "folder"),
        Field("sub", "Include subfolders", "check", True),
        Field("save", "Save the report as", "text", "", help="Optional file name, for example report.txt."),
    ]

    def plan(self, v):
        folder = check_folder(v["folder"])
        files, problems = [], []
        for p, _ in _walk(folder, v["sub"], problems=problems):
            try:
                st = p.stat()
                files.append((p, st.st_size, st.st_mtime))
            except OSError as e:
                problems.append(f"{p}: {friendly(e)}")
        plan = Plan(self.name)
        note_problems(plan, problems)
        if not files:
            plan.summary = "That folder has no files."
            return plan
        total = sum(s for _, s, _ in files)
        types = Counter((p.suffix.lower() or "(no type)") for p, _, _ in files)
        by_size = sorted(files, key=lambda f: -f[1])[:10]
        newest = max(files, key=lambda f: f[2])
        oldest = min(files, key=lambda f: f[2])
        lines = [f"Report for {folder}", f"Made {datetime.now():%Y-%m-%d %H:%M}", "",
                 f"Files: {len(files)}", f"Total size: {size_text(total)}",
                 f"Newest: {rel(newest[0], folder)} ({datetime.fromtimestamp(newest[2]):%Y-%m-%d})",
                 f"Oldest: {rel(oldest[0], folder)} ({datetime.fromtimestamp(oldest[2]):%Y-%m-%d})", "", "File types:"]
        lines += [f"  {k}: {n}" for k, n in types.most_common(15)]
        lines += ["", "Biggest files:"] + [f"  {size_text(s)}  {rel(p, folder)}" for p, s, _ in by_size]
        if problems:
            lines += ["", f"Note: {plural(len(problems), 'place')} could not be read, so these numbers may be incomplete."]
        plan.report = lines
        plan.summary = f"{plural(len(files), 'file')}, {size_text(total)} in total."
        if v["save"].strip():
            deliver(plan, None, "\n".join(lines), {"result": RESULT[0], "outname": v["save"], "outdir": str(folder)},
                    "report.txt", what="report")
        return plan
