"""The five version-one recipes.

A recipe describes its form (``fields``) and turns the filled-in form into a
``Plan`` (a list of plain-English actions).  Recipes never touch the disk
except to read; engine.apply_plan does the changing.

To add a new recipe: subclass Recipe, fill in name/blurb/fields, write plan(),
and add an instance to ALL at the bottom.  The GUI picks it up automatically.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from engine import (check_cancel, watch_file, Action, Namer, NotText, Plan, UserError, check_folder, decode_bytes,
                    list_files, looks_like_text, norm, parse_exts, size_text)


@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"            # folder | text | choice | check
    default: object = ""
    choices: Optional[list] = None
    help: str = ""
    height: int = 5               # rows, for kind="multiline"


class Recipe:
    key = ""
    name = ""
    blurb = ""
    fields: list = []

    def plan(self, v: dict) -> Plan:
        raise NotImplementedError


BAD_CHARS = set('\\/:*?"<>|')


def rel(p: Path, base: Path) -> str:
    try:
        return str(p.relative_to(base))
    except ValueError:
        return str(p)


def plural(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def check_name_text(text: str, what: str) -> None:
    bad = sorted(set(text) & BAD_CHARS)
    if bad:
        raise UserError(f"{what} can't contain these characters: {' '.join(bad)}\n"
                        "(Windows doesn't allow them in file names.)")


RESERVED_NAMES = ({"CON", "PRN", "AUX", "NUL"}
                  | {f"COM{d}" for d in "123456789\u00b9\u00b2\u00b3"}
                  | {f"LPT{d}" for d in "123456789\u00b9\u00b2\u00b3"})


def name_problem(name: str) -> Optional[str]:
    """Why Windows would refuse this whole file or folder name, or None if it is fine.

    Windows drops a trailing dot or space and treats CON, PRN, AUX, NUL, COM1-9 and
    LPT1-9 as devices (with or without an extension), so none of those can be created.
    """
    if name != name.rstrip(". "):
        return "it ends with a dot or a space"
    if name.split(".")[0].rstrip(" ").upper() in RESERVED_NAMES:
        return "Windows keeps that name for itself"
    return None


def check_new_name(name: str, what: str) -> None:
    """Like check_name_text, but for a complete name the user typed for a new file or folder."""
    check_name_text(name, what)
    why = name_problem(name)
    if why:
        raise UserError(f"{what} can't be “{name}” because {why}.\n"
                        "(Windows doesn't allow that in file names. Please choose a different name.)")


def with_extension(name: str, default_ext: str) -> str:
    """Add the default extension unless the name already ends in a real one.

    "notes" -> "notes.txt", "notes.csv" stays, but "v1.2" or "Report. final" are not extensions, so they
    become "v1.2.txt" / "Report. final.txt" instead of being saved with no useful type.
    """
    ext = Path(name).suffix
    if re.fullmatch(r"\.[A-Za-z][A-Za-z0-9]{0,7}", ext):
        return name
    return name + default_ext


def note_problems(plan: Plan, problems: list, what: str = "The result") -> None:
    """Tell the user, in the preview, about places that could not be read."""
    if not problems:
        return
    shown = problems[:5]
    plan.notes.append(f"{plural(len(problems), 'place')} could not be read, so {what.lower()} may be incomplete:")
    plan.notes += [f"    {x}" for x in shown]
    if len(problems) > len(shown):
        plan.notes.append(f"    ... and {len(problems) - len(shown)} more")


def natural_key(name: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name.lower())]


FOLDER = Field("folder", "Folder", "folder", help="The folder with the files you want to work on.")


# ==========================================================================
# 1. Bulk rename
# ==========================================================================
R_MODES = [
    "Add text to the start of each name",
    "Add text to the end of each name (before the .ext)",
    "Replace some text in each name",
    "Number the files in order",
    "Put the date last changed at the start",
    "Replace spaces with underscores",
    "Make names all lowercase",
    "Make names ALL UPPERCASE",
]
R_ORDERS = ["Name (A to Z)", "Date changed (oldest first)", "Date changed (newest first)"]


class BulkRename(Recipe):
    key = "rename"
    name = "Rename many files"
    blurb = "Give lots of files new names in one go - add text, replace text, number them, and more."
    fields = [
        FOLDER,
        Field("sub", "Include subfolders", "check", False),
        Field("only", "Only these file types", "text", "",
              help="Optional. For example: jpg, png.  Leave empty to use every file."),
        Field("only_name", "Only files whose name contains", "text", "",
              help="Optional. For example: Draft. Capitals don't matter."),
        Field("mode", "What do you want to do?", "choice", R_MODES[0], R_MODES),
        Field("text", "Text to use", "text", "",
              help="Used by the first four choices. For 'Replace', this is the text to look for. "
                   "For 'Number', it becomes the new name (e.g. Holiday -> Holiday 001)."),
        Field("text2", "Replace it with", "text", "",
              help="Only for 'Replace some text'. Leave empty to remove the text."),
        Field("order", "Number files by", "choice", R_ORDERS[0], R_ORDERS, help="Only for 'Number the files'."),
    ]

    def plan(self, v):
        folder = check_folder(v["folder"])
        mode, text, text2 = v["mode"], v["text"], v["text2"]
        if mode in R_MODES[:3] and not text:
            raise UserError("Please type the text to use in the 'Text to use' box.")
        check_name_text(text, "The text")
        check_name_text(text2, "The replacement text")

        problems: list = []
        files = list_files(folder, v["sub"], parse_exts(v["only"]), problems)
        if v.get("only_name", "").strip():
            files = [p for p in files if v["only_name"].strip().lower() in p.name.lower()]
        if mode == R_MODES[3]:
            if v["order"] == R_ORDERS[0]:
                files.sort(key=lambda p: (str(p.parent).lower(), natural_key(p.name)))
            else:
                files.sort(key=lambda p: p.stat().st_mtime, reverse=v["order"] == R_ORDERS[2])
        width = max(3, len(str(len(files))))

        plan = Plan(self.name)
        namer, unchanged, skipped, bad_names = Namer(), 0, 0, 0
        for i, p in enumerate(files, 1):
            stem, ext = p.stem, p.suffix
            if mode == R_MODES[0]:
                new = text + p.name
            elif mode == R_MODES[1]:
                new = stem + text + ext
            elif mode == R_MODES[2]:
                new = p.name.replace(text, text2)
            elif mode == R_MODES[3]:
                num = str(i).zfill(width)
                new = f"{text} {num}{ext}" if text else f"{num} {p.name}"
            elif mode == R_MODES[4]:
                new = f"{datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d} {p.name}"
            elif mode == R_MODES[5]:
                new = p.name.replace(" ", "_")
            elif mode == R_MODES[6]:
                new = p.name.lower()
            else:
                new = p.name.upper()
            if new == p.name:
                unchanged += 1
                continue
            if not new.strip(". "):
                skipped += 1
                continue
            if name_problem(new):
                bad_names += 1
                continue
            dst = namer.reserve(p.with_name(new), src=p)
            plan.actions.append(Action("move", f"{rel(p, folder)}   →   {dst.name}", p, dst))

        n = len(plan.actions)
        plan.summary = (f"{plural(n, 'file')} will be renamed." if n else
                        "Nothing to rename - no file would change.")
        if unchanged and n:
            plan.notes.append(f"{plural(unchanged, 'file')} already match and will be left alone.")
        if skipped:
            plan.notes.append(f"{plural(skipped, 'file')} skipped because the new name would be empty.")
        if bad_names:
            plan.notes.append(f"{plural(bad_names, 'file')} skipped because the new name isn't allowed on Windows.")
        if not files:
            plan.notes.append("No matching files were found in that folder.")
        note_problems(plan, problems)
        return plan


# ==========================================================================
# 2. Organize a folder
# ==========================================================================
CATEGORIES = {
    "Pictures": "jpg jpeg png gif bmp tif tiff webp heic svg ico raw cr2 nef",
    "Documents": "doc docx pdf txt rtf odt md pages tex epub",
    "Spreadsheets": "xls xlsx xlsm csv tsv ods numbers",
    "Presentations": "ppt pptx odp key",
    "Videos": "mp4 mov avi mkv wmv flv webm m4v mpg mpeg",
    "Music": "mp3 wav flac aac ogg m4a wma",
    "Archives": "zip rar 7z tar gz bz2 xz",
    "Programs": "exe msi dmg apk",
    "Code": "py js ts html css json java c cpp h cs go rs php sh bat xml yml yaml",
}
EXT_TO_CAT = {"." + e: cat for cat, exts in CATEGORIES.items() for e in exts.split()}
O_MODES = ["By type (Pictures, Documents, Music ...)", "By type, then by month", "By month only",
           "By file extension (PDF, JPG, TXT ...)"]


class Organize(Recipe):
    key = "organize"
    name = "Organize a messy folder"
    blurb = "Sort the files in a folder into tidy subfolders - like Pictures, Documents, Music - or by month."
    fields = [
        FOLDER,
        Field("how", "Sort", "choice", O_MODES[0], O_MODES),
        Field("sub", "Also include files inside subfolders", "check", False,
              help="If ticked, files found in subfolders are pulled up into the sorted folders too."),
        Field("days", "Only files changed in the last (days)", "text", "", help="Optional. For example: 30."),
    ]

    def plan(self, v):
        folder = check_folder(v["folder"])
        problems: list = []
        files = list_files(folder, v["sub"], None, problems)
        if str(v.get("days", "")).strip():
            try:
                cutoff = datetime.now().timestamp() - float(v["days"]) * 86400
            except ValueError:
                raise UserError("“Only files changed in the last” needs a number, like 30.")
            files = [p for p in files if p.stat().st_mtime >= cutoff]
        plan = Plan(self.name)
        namer, in_place, dest_folders, bad_names = Namer(), 0, set(), 0
        for p in files:
            cat = EXT_TO_CAT.get(p.suffix.lower(), "Other")
            month = f"{datetime.fromtimestamp(p.stat().st_mtime):%Y-%m}"
            if v["how"] == O_MODES[0]:
                target = folder / cat
            elif v["how"] == O_MODES[3]:
                target = folder / ((p.suffix.lstrip(".").upper() or "No extension"))
            elif v["how"] == O_MODES[1]:
                target = folder / cat / month
            else:
                target = folder / month
            if norm(p.parent) == norm(target):
                in_place += 1
                continue
            if any(name_problem(part) for part in target.relative_to(folder).parts):
                bad_names += 1                      # e.g. a file ending in ".con" would need a folder called CON
                continue
            dst = namer.reserve(target / p.name)
            dest_folders.add(target)
            plan.actions.append(Action("move", f"{rel(p, folder)}   →   {rel(target, folder)}{os.sep}", p, dst))
        n = len(plan.actions)
        plan.summary = (f"{plural(n, 'file')} will be moved into {plural(len(dest_folders), 'folder')}."
                        if n else "Nothing to move - everything is already sorted.")
        if in_place and n:
            plan.notes.append(f"{plural(in_place, 'file')} are already in the right place.")
        if bad_names:
            plan.notes.append(f"{plural(bad_names, 'file')} skipped because the folder it would go into "
                              "isn't allowed on Windows.")
        if not files:
            plan.notes.append("No files were found in that folder.")
        note_problems(plan, problems)
        return plan


# ==========================================================================
# 3. Find and replace in text files
# ==========================================================================
class FindReplace(Recipe):
    key = "replace"
    name = "Find and replace in text files"
    blurb = "Change a word or phrase in many text files at once. Every file is backed up first, so it can be undone."
    fields = [
        FOLDER,
        Field("sub", "Include subfolders", "check", True),
        Field("types", "Only these file types", "text", "txt, md, csv, log, html, xml, json, ini",
              help="Leave empty to try every file that contains text."),
        Field("find", "Find this text", "text", ""),
        Field("replace", "Replace it with", "text", "", help="Leave empty to delete the text you find."),
        Field("case", "Match capital letters exactly", "check", False),
        Field("whole", "Whole words only", "check", False, help="So that finding 'cat' does not change 'category'."),
    ]

    def plan(self, v):
        folder = check_folder(v["folder"])
        find = v["find"]
        if find == "":
            raise UserError("Please type the text you want to find.")
        pat = re.escape(find)
        if v["whole"]:
            pat = r"(?<!\w)" + pat + r"(?!\w)"
        rx = re.compile(pat, 0 if v["case"] else re.IGNORECASE)
        repl = v["replace"]

        plan = Plan(self.name)
        total, binary, big = 0, 0, 0
        problems: list = []
        for p in list_files(folder, v["sub"], parse_exts(v["types"]), problems):
            try:
                if p.stat().st_size > 100 * 1024 * 1024:
                    big += 1
                    continue
                text, _ = decode_bytes(p.read_bytes())
            except NotText:
                binary += 1
                continue
            except OSError as e:
                plan.notes.append(f"Couldn't read {p.name}: {e.strerror or e}")
                continue
            matches = list(rx.finditer(text))
            if not matches:
                continue
            m = matches[0]
            ls = text.rfind("\n", 0, m.start()) + 1
            le = text.find("\n", m.end())
            line = text[ls:(le if le != -1 else len(text))].rstrip("\r")
            new_line = rx.sub(lambda _m: repl, line)
            clip = lambda s: s if len(s) <= 90 else s[:87] + "..."
            n = len(matches)
            total += n
            plan.actions.append(Action(
                "edit", f"{rel(p, folder)}   -   {plural(n, 'change')}", p, p,
                detail=f"before:  {clip(line.strip())}\nafter:   {clip(new_line.strip())}",
                transform=lambda t, rx=rx, repl=repl: rx.sub(lambda _m: repl, t)))
        nf = len(plan.actions)
        plan.summary = (f"{plural(total, 'change')} will be made in {plural(nf, 'file')}." if nf
                        else "That text wasn't found in any file.")
        if nf:
            plan.notes.append("Each file is saved to the Recycle folder before it is changed.")
        if binary:
            plan.notes.append(f"{plural(binary, 'file')} skipped because they aren't plain text (pictures, programs, ...).")
        if big:
            plan.notes.append(f"{plural(big, 'file')} skipped because they are over 100 MB.")
        note_problems(plan, problems)
        return plan


# ==========================================================================
# 4. Find duplicate files
# ==========================================================================
D_KEEP = ["Keep the oldest copy", "Keep the newest copy", "Keep the one with the shortest name"]


def _file_hash(p: Path, limit: Optional[int] = None) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        remaining = limit
        while True:
            check_cancel()
            chunk = fh.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
            if not chunk:
                break
            h.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
                if remaining <= 0:
                    break
    return h.hexdigest()


def group_identical(files: list) -> tuple:
    """Group files whose contents are identical.  Returns (groups, unreadable_count)."""
    by_size: dict = {}
    for p in files:
        try:
            s = p.stat().st_size
        except OSError:
            continue
        if s > 0:
            by_size.setdefault(s, []).append(p)
    groups, unreadable = [], 0
    for same in by_size.values():
        if len(same) < 2:
            continue
        quick: dict = {}
        for p in same:
            try:
                quick.setdefault(_file_hash(p, 65536), []).append(p)
            except OSError:
                unreadable += 1
        for cands in quick.values():
            if len(cands) < 2:
                continue
            full: dict = {}
            for p in cands:
                try:
                    full.setdefault(_file_hash(p), []).append(p)
                except OSError:
                    unreadable += 1
            groups += [g for g in full.values() if len(g) > 1]
    return groups, unreadable


class Duplicates(Recipe):
    key = "duplicates"
    name = "Find duplicate files"
    blurb = "Find files that are exact copies of each other and move the extra copies to the Recycle folder."
    fields = [
        FOLDER,
        Field("sub", "Include subfolders", "check", True),
        Field("more", "Also look in these folders (optional)", "folderlist", "", height=2,
              help="Add other folders to find copies spread across several places."),
        Field("keep", "Which copy should stay?", "choice", D_KEEP[0], D_KEEP,
              help="Files are compared by their contents, not their names."),
    ]

    def plan(self, v):
        folder = check_folder(v["folder"])
        problems: list = []
        files = list_files(folder, v["sub"], None, problems)
        seen = {norm(p) for p in files}
        for extra in str(v.get("more", "")).replace(";", "\n").splitlines():
            if extra.strip():
                for p in list_files(check_folder(extra), v["sub"], None, problems):
                    if norm(p) not in seen:
                        seen.add(norm(p))
                        files.append(p)
        found, unreadable = group_identical(files)

        plan = Plan(self.name)
        groups, extra_bytes = 0, 0
        for dupes in found:
            if v["keep"] == D_KEEP[0]:
                dupes.sort(key=lambda p: (p.stat().st_mtime, str(p)))
            elif v["keep"] == D_KEEP[1]:
                dupes.sort(key=lambda p: (-p.stat().st_mtime, str(p)))
            else:
                dupes.sort(key=lambda p: (len(p.name), p.stat().st_mtime, str(p)))
            keeper, rest = dupes[0], dupes[1:]
            groups += 1
            for d in rest:
                extra_bytes += keeper.stat().st_size
                # Both the extra copy and the copy that stays must be unchanged when the move happens,
                # or the "extra" copy might no longer be a copy.
                plan.actions.append(Action(
                    "trash", f"{rel(d, folder)}   is a copy of   {rel(keeper, folder)}", d,
                    guard=[watch_file(keeper)]))
        n = len(plan.actions)
        plan.actions.sort(key=lambda a: str(a.src).lower())
        plan.summary = (f"Found {plural(groups, 'set')} of identical files. {plural(n, 'extra copy', 'extra copies')} "
                        f"({size_text(extra_bytes)}) will move to the Recycle folder; one copy of each stays."
                        if n else "No duplicate files were found.")
        if unreadable:
            plan.notes.append(f"{plural(unreadable, 'file')} couldn't be read and were skipped.")
        note_problems(plan, problems, "the list of copies")
        return plan


# ==========================================================================
# 5. Merge text files
# ==========================================================================
M_ORDERS = ["Name (A to Z, numbers in order)", "Date changed (oldest first)", "Date changed (newest first)"]


class MergeText(Recipe):
    key = "merge"
    name = "Combine text files into one"
    blurb = "Join many text files into a single file. Your original files are left untouched."
    fields = [
        FOLDER,
        Field("sub", "Include subfolders", "check", False),
        Field("types", "Only these file types", "text", "txt", help="For example: txt, md.  Leave empty to use every text file."),
        Field("out", "Name of the new file", "text", "combined.txt", help="It is saved inside the folder you chose."),
        Field("order", "Put the files in this order", "choice", M_ORDERS[0], M_ORDERS),
        Field("headings", "Write each file's name above its text", "check", True),
    ]

    def plan(self, v):
        folder = check_folder(v["folder"])
        out_name = v["out"].strip()
        if not out_name:
            raise UserError("Please type a name for the new file.")
        if not Path(out_name).suffix:
            out_name += ".txt"
        check_new_name(out_name, "The file name")
        wanted_out = folder / out_name

        # A file with the output's name is left out at any depth, so an old combined.txt
        # inside a subfolder is never merged into the new one.
        out_key = norm(out_name)
        problems: list = []
        files = [p for p in list_files(folder, v["sub"], parse_exts(v["types"]), problems)
                 if norm(p.name) != out_key]
        not_text = [p for p in files if not looks_like_text(p)]
        files = [p for p in files if p not in not_text]
        if v["order"] == M_ORDERS[0]:
            files.sort(key=lambda p: (str(p.parent).lower(), natural_key(p.name)))
        else:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=v["order"] == M_ORDERS[2])

        plan = Plan(self.name)
        note_problems(plan, problems)
        if not_text:
            plan.notes.append(f"{plural(len(not_text), 'file')} skipped because they aren't plain text.")
        if len(files) < 2:
            plan.summary = "There need to be at least two text files to combine."
            return plan

        out = Namer().reserve(wanted_out)
        headings = v["headings"]
        listing = list(files)

        def produce():
            parts = []
            for p in listing:
                try:
                    text, _ = decode_bytes(p.read_bytes())
                except Exception:
                    text = "[This file could not be read]"
                text = text.replace("\r\n", "\n").replace("\r", "\n")
                if text and not text.endswith("\n"):
                    text += "\n"
                parts.append((f"===== {rel(p, folder)} =====\n" if headings else "") + text)
            body = "\n".join(parts)
            return (body.replace("\n", "\r\n") if os.name == "nt" else body), "utf-8"

        total = sum(p.stat().st_size for p in files)
        lines = [f"{i:>3}. {rel(p, folder)}" for i, p in enumerate(files, 1)]
        if len(lines) > 40:
            lines = lines[:40] + [f"     ... and {len(files) - 40} more"]
        plan.actions.append(Action(
            "write", f"{plural(len(files), 'file')} will be combined into a new file:   {out.name}",
            dst=out, detail="\n".join(lines), produce=produce))
        plan.summary = f"A new file called {out.name} (about {size_text(total)}) will be created."
        return plan


# ==========================================================================
# 6. Build a delivery package
# ==========================================================================
P_NAMING = ["Keep the original names",
            "Tidy the names (no odd characters, spaces become _)",
            "Tidy the names and put the date first (2026-10-30_name)"]
DEFAULT_RULES = """\
# Folder name = words to look for in the file name, and/or file types (.ext)
# A file goes into the FIRST folder (from the top) that matches it.
1_Contracts = contract, agreement, nda, signed
2_Reports = report, summary, analysis
3_Data = .xlsx, .xls, .csv
4_Images = .jpg, .jpeg, .png, .gif
5_Notes = notes, meeting, .txt, .md
"""
JUNK_EXTS = {".tmp", ".bak", ".crdownload", ".part", ".fftmp", ".swp"}


def parse_rules(text: str) -> list:
    """'Reports = report, .docx' lines -> [(folder, {'.docx'}, ['report'])]."""
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise UserError(f"This line needs an equals sign:\n    {line}\n\n"
                            "Write it like:   Reports = report, summary, .docx")
        name, rhs = (s.strip() for s in line.split("=", 1))
        if not name.strip(". "):
            raise UserError(f"This line has no folder name before the equals sign:\n    {line}")
        check_new_name(name, "A folder name")
        exts, words = set(), []
        for tok in re.split(r"[,;]", rhs):
            tok = tok.strip().lower().lstrip("*").strip()
            if not tok:
                continue
            (exts.add(tok) if tok.startswith(".") else words.append(tok))
        if not exts and not words:
            raise UserError(f"No words or file types after the equals sign:\n    {line}")
        rules.append((name, exts, words))
    if not rules:
        raise UserError("Please add at least one category folder, like:   Reports = report, summary, .docx")
    return rules


def categorize(p: Path, rules: list, other: str) -> str:
    stem = p.stem.lower()
    for name, exts, words in rules:
        if p.suffix.lower() in exts or any(w in stem for w in words):
            return name
    return other


def tidy_name(name: str) -> str:
    p = Path(name)
    stem = re.sub(r"[^\w\-. ()]", "", p.stem)
    stem = re.sub(r"[ _]+", "_", stem).strip(" ._-")
    return (stem or "file") + p.suffix.lower()


class DeliveryPackage(Recipe):
    key = "package"
    name = "Build a delivery package"
    blurb = ("Turns a messy folder into a tidy package: duplicates and junk left out, files copied into "
             "category folders you define, tidy names, a contents list and a .zip. "
             "Your original files are never touched - the package is made from copies.")
    fields = [
        Field("folder", "Messy folder", "folder", help="The folder full of files you want to package."),
        Field("sub", "Include subfolders", "check", True),
        Field("name", "Name of the package", "text", "Delivery Package"),
        Field("outdir", "Save it in", "folder", "",
              help="Optional. Leave empty to save it next to your messy folder."),
        Field("rules", "Your category folders", "multiline", DEFAULT_RULES, height=7,
              help="One per line:  Folder name = word, word, .ext   Words are looked for in the file name; "
                   ".ext matches the file type. Change, add or delete lines freely."),
        Field("other", "Folder for everything else", "text", "9_Other"),
        Field("naming", "File names", "choice", P_NAMING[0], P_NAMING),
        Field("dedupe", "Leave out duplicate files", "check", True),
        Field("junk", "Leave out temporary and junk files", "check", True),
        Field("contents", "Add a contents list (CONTENTS.txt)", "check", True),
        Field("zip", "Also create a .zip file to send", "check", True),
    ]

    def plan(self, v):
        folder = check_folder(v["folder"])
        name = v["name"].strip().rstrip(". ")
        if not name:
            raise UserError("Please type a name for the package.")
        check_new_name(name, "The package name")
        rules = parse_rules(v["rules"])
        other = v["other"].strip() or "Other"
        check_new_name(other, "The 'everything else' folder name")

        try:
            out_parent = check_folder(v["outdir"] if v["outdir"].strip() else str(folder.parent))
        except UserError:
            raise UserError("Please choose where to save the package in the 'Save it in' box - "
                            "the place next to your folder isn't suitable.")
        pkg = Namer().reserve(out_parent / name)
        if pkg == folder or folder in pkg.parents:
            raise UserError("The package can't be saved inside the folder you are packaging.\n"
                            "Choose a different place in the 'Save it in' box.")

        problems: list = []
        files = list_files(folder, v["sub"], None, problems)
        junk = 0
        if v["junk"]:
            kept = [p for p in files if p.suffix.lower() not in JUNK_EXTS and not p.name.endswith("~")]
            junk, files = len(files) - len(kept), kept
        dupes = 0
        if v["dedupe"]:
            groups, _ = group_identical(files)
            drop = set()
            for g in groups:
                g.sort(key=lambda p: (p.stat().st_mtime, str(p)))
                drop.update(g[1:])
            files = [p for p in files if p not in drop]
            dupes = len(drop)

        plan = Plan(self.name)
        note_problems(plan, problems)
        if not files:
            plan.summary = "There are no files to package."
            return plan

        namer, entries, bad_names = Namer(), [], 0
        for p in files:
            cat = categorize(p, rules, other)
            new = p.name if v["naming"] == P_NAMING[0] else tidy_name(p.name)
            if v["naming"] == P_NAMING[2]:
                new = f"{datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d}_{new}"
            if name_problem(new):
                bad_names += 1
                continue
            entries.append((cat, namer.reserve(pkg / cat / new), p))
        entries.sort(key=lambda e: (natural_key(e[0]), natural_key(e[1].name)))

        if not entries:
            plan.summary = "There are no files to package."
            if bad_names:
                plan.notes.append(f"{plural(bad_names, 'file')} skipped because the new name isn't allowed on Windows.")
            return plan

        for cat, dst, p in entries:
            plan.actions.append(Action("copy", f"{rel(p, folder)}   →   {cat}{os.sep}{dst.name}", p, dst))

        cats: dict = {}
        for cat, dst, p in entries:
            cats.setdefault(cat, []).append((dst.name, p.stat().st_size))
        total = sum(s for items in cats.values() for _, s in items)

        if v["contents"]:
            when = datetime.now()

            def produce():
                lines = [pkg.name, f"Prepared {when:%d %B %Y}",
                         f"{plural(len(entries), 'file')} in {plural(len(cats), 'folder')}, {size_text(total)}", ""]
                for cat in sorted(cats, key=natural_key):
                    lines.append(f"=== {cat}  ({plural(len(cats[cat]), 'file')}) ===")
                    lines += [f"  {n}   ({size_text(s)})" for n, s in cats[cat]]
                    lines.append("")
                body = "\n".join(lines)
                return (body.replace("\n", "\r\n") if os.name == "nt" else body), "utf-8"

            plan.actions.append(Action("write", "A contents list will be added:   CONTENTS.txt",
                                       dst=pkg / "CONTENTS.txt", produce=produce))
        zip_path = None
        if v["zip"]:
            zip_path = Namer().reserve(out_parent / (pkg.name + ".zip"))
            plan.actions.append(Action("zip", f"Everything will be zipped into:   {zip_path.name}", pkg, zip_path))

        plan.summary = (f"A package called “{pkg.name}” will be created in {out_parent}: "
                        f"{plural(len(entries), 'file')} in {plural(len(cats), 'folder')} "
                        f"({size_text(total)})" + (", plus a .zip file." if zip_path else "."))
        plan.notes.append("Your original files are not touched - the package is made from copies.")
        if bad_names:
            plan.notes.append(f"{plural(bad_names, 'file')} skipped because the new name isn't allowed on Windows.")
        if dupes:
            plan.notes.append(f"{plural(dupes, 'duplicate file')} left out (one copy of each is kept).")
        if junk:
            plan.notes.append(f"{plural(junk, 'temporary/junk file')} left out.")
        n_other = len(cats.get(other, []))
        if n_other:
            plan.notes.append(f"{plural(n_other, 'file')} didn't match any of your categories and go into "
                              f"“{other}”. Add words or file types to your categories if that is too many.")
        return plan


ALL = [BulkRename(), Organize(), FindReplace(), Duplicates(), MergeText(), DeliveryPackage()]
