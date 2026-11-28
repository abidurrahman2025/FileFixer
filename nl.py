"""Plain-English requests -> a validated job with its form pre-filled.

This is a rule-based interpreter (no internet, no AI service, no API key).  It only ever produces one of the
app's own jobs with explicit form values; it can never run commands.  Anything it can't understand is
explained honestly, with examples, instead of guessed at.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import r_extract as X
import r_more as M
import r_sync as S
import recipes as R


@dataclass
class Intent:
    key: str
    values: dict = field(default_factory=dict)
    explain: str = ""
    missing: list = field(default_factory=list)       # details the user still has to fill in
    notes: list = field(default_factory=list)


SUGGESTIONS = [
    "Find every word starting with itm_ in this file and put them into a numbered list",
    "Find duplicate files in this folder",
    "Extract all email addresses from this file",
    "Find all files modified in the last 30 days",
    "Organize these files by extension",
    "Rename all files containing the word Draft",
    "Synchronize all folders named folder1 in both directions",
]

FILE_EXT = r"(?:txt|csv|tsv|md|log|json|docx|xlsx|pdf|xml|html?|ini)"
Q = "\"'\u201c\u201d\u2018\u2019"


def _has(t: str, *words: str) -> bool:
    return any(re.search(r"\b" + w + r"\b", t) for w in words)


def _quoted(text: str) -> list:
    return [a or b or c for a, b, c in re.findall(r"\"([^\"]+)\"|\u201c([^\u201d]+)\u201d|'([^']+)'", text)]


def _clean_term(s: str, quoted: bool) -> str:
    return s if quoted else s.rstrip(".,;:!?")


def _days(t: str) -> Optional[str]:
    m = re.search(r"(?:last|past|previous)\s+(\d+)\s+(day|week|month|year)s?", t)
    if m:
        return str(int(m.group(1)) * {"day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)])
    m = re.search(r"(?:last|past)\s+(week|month|year)", t)
    if m:
        return str({"week": 7, "month": 30, "year": 365}[m.group(1)])
    if "yesterday" in t or "today" in t:
        return "1"
    return None


def _size_mb(t: str) -> Optional[str]:
    m = re.search(r"(?:(?:larger|bigger|greater|more)\s+than|(?:over|above|at least))\s+(\d+(?:\.\d+)?)\s*(kb|mb|gb)", t)
    if not m:
        return None
    n = float(m.group(1)) * {"kb": 1 / 1024, "mb": 1, "gb": 1024}[m.group(2)]
    return f"{n:g}"


def _term_after(text: str, pattern: str) -> Optional[str]:
    """First word or quoted phrase following a cue like 'start with'."""
    m = re.search(pattern + r"\s+(?:the\s+)?(?:text\s+|string\s+|letters?\s+|word\s+|prefix\s+|suffix\s+)?"
                  r"(\"[^\"]+\"|\u201c[^\u201d]+\u201d|'[^']+'|[^\s\"'\u201c\u201d,;]+)", text, re.I)
    if not m:
        return None
    raw = m.group(m.lastindex)
    quoted = raw[0] in Q
    return _clean_term(raw.strip(Q) if quoted else raw, quoted)


class Context:
    def __init__(self, files: list, folders: list) -> None:
        self.files = [Path(f) for f in files]
        self.folders = [Path(f) for f in folders]

    def file_named(self, text: str) -> Optional[Path]:
        names = [q for q in _quoted(text)] + re.findall(rf"[^\s\"'\u201c\u201d<>|]+\.{FILE_EXT}\b", text, re.I)
        for n in names:
            n = n.strip()
            for f in self.files:
                if f.name.lower() == Path(n).name.lower():
                    return f
            p = Path(n)
            if p.is_absolute() and p.is_file():
                return p
            for d in self.folders:
                if (d / n).is_file():
                    return d / n
        return None

    def one_file(self, text: str) -> Optional[Path]:
        return self.file_named(text) or (self.files[0] if self.files else None)

    def one_folder(self) -> Optional[Path]:
        if self.folders:
            return self.folders[0]
        if self.files:
            return self.files[0].parent
        return None


# --------------------------------------------------------------------------
def interpret(text: str, files: list = (), folders: list = ()) -> Optional[Intent]:
    text = (text or "").strip()
    if not text:
        return None
    t = text.lower()
    ctx = Context(files, folders)
    for fn in (_sync, _compare, _duplicates, _emails, _rename, _replace_text, _organize, _lines, _csv, _split,
               _merge, _extract_words, _find_files, _report):
        r = fn(text, t, ctx)
        if r:
            return r
    return None


def _need_file(i: Intent, ctx: Context, text: str, what: str = "file") -> Intent:
    f = ctx.one_file(text)
    if f:
        i.values["file"] = str(f)
    else:
        i.missing.append(f"the {what} to use (click “Choose files”)")
    return i


def _need_folder(i: Intent, ctx: Context, key: str = "folder") -> Intent:
    d = ctx.one_folder()
    if d:
        i.values[key] = str(d)
    else:
        i.missing.append("the folder to use (click “Choose folders”)")
    return i


def _extract_words(text, t, ctx):
    if not _has(t, "word", "words", "items", "entries", "terms", "strings", "tokens", "text"):
        return None
    if _has(t, "files", "folders") and not _has(t, "word", "words"):
        return None
    how, term = None, None
    for cue, label in ((r"(?:start|starts|starting|begin|begins|beginning)\s+with", X.HOW[0]),
                       (r"(?:end|ends|ending)\s+with", X.HOW[1]),
                       (r"(?:contain|contains|containing|include|includes|including|has|have)", X.HOW[2]),
                       (r"(?:equal|equals|exactly|is exactly|match(?:es)? exactly)", X.HOW[3])):
        term = _term_after(text, cue)
        if term:
            how = label
            break
    if not term:
        return None
    if _has(t, "regex", "regular expression"):
        how = X.HOW[4]
    v = {"how": how, "patterns": term}
    if _has(t, "ignore case", "case-insensitive", "case insensitive", "any case", "regardless of case"):
        v["case"] = False
    if _has(t, "unique", "duplicates", "duplicate", "once", "dedupe", "deduplicate") and re.search(
            r"(without|remove|no|unique|once|dedup|skip|ignore)", t):
        v["dedupe"] = True
    if re.search(r"(no numbers|without numbers|not numbered|no numbering)", t):
        v["numbering"] = X.NUMBERING[1]
    if re.search(r"(strip|ignore|remove)\s+(?:the\s+)?punctuation", t):
        v["punct"] = X.PUNCT[1]
    if re.search(r"\blines?\b", t) and _has(t, "line", "lines") and not _has(t, "word", "words"):
        v["split"] = X.SPLIT[1]
    if re.search(r"(replace|overwrite|modify|change)\s+(?:the\s+)?(?:original|file itself)", t) or "in place" in t:
        v["result"] = X.RESULT[1]
    name = _term_after(text, r"(?:save(?:\s+(?:it|them|results?))?\s+as|call(?:ed)?\s+(?:it|the new file)?|named|name it)")
    if name and re.search(r"\.\w+$|^[\w\-. ]+$", name) and name.lower() not in ("a", "the", "new"):
        v["outname"] = name
    i = Intent("extract", v)
    _need_file(i, ctx, text)
    srcname = Path(v["file"]).name if "file" in v else "the file"
    kind = {X.HOW[0]: "start with", X.HOW[1]: "end with", X.HOW[2]: "contain", X.HOW[3]: "are exactly"}.get(how, "match")
    i.explain = (f"I will look in {srcname} for words that {kind} “{term}”, keep each word complete, "
                 f"in the order they appear" + (", each only once" if v.get("dedupe") else "") +
                 (" as a numbered list." if v.get("numbering") != X.NUMBERING[1] else " as a plain list.") +
                 (" The results will replace the original (a backup is kept)." if v.get("result") == X.RESULT[1]
                  else " The results go into a new file; your original stays untouched."))
    return i


ADDRESS_NOUN = r"\b(emails?|e-mails?|email addresses|urls?|links?|web addresses|websites)\b"
OTHER_VERBS = ("send", "write", "compose", "draft", "forward", "reply", "delete", "remove", "open", "rename", "sort",
               "sync", "synchronize", "copy", "move", "split", "merge", "combine", "number")
WHERE_TO_LOOK = r"\b(?:in|from|inside|within|of)\b.*\b(?:files?|folders?|directory|documents?|docs?|spreadsheets?|pdfs?|texts?|notes)\b"


def _emails(text, t, ctx):
    if not re.search(ADDRESS_NOUN, t):
        return None
    if not _has(t, "extract", "find", "get", "list", "collect", "pull", "grab", "show"):
        # No asking-verb: "emails in this folder" still means collect them, but "send emails" does not.
        if _has(t, *OTHER_VERBS) or not re.search(WHERE_TO_LOOK, t):
            return None
    if _has(t, "files") and not _has(t, "address", "addresses", "emails", "email", "urls", "links"):
        return None
    is_url = bool(re.search(r"\b(urls?|links?|web addresses|websites)\b", t)) and not re.search(r"\bemail", t)
    v = {"what": M.WHAT[1] if is_url else M.WHAT[0]}
    i = Intent("emails", v)
    f = ctx.file_named(text) or (ctx.files[0] if ctx.files else None)
    if f:
        v["file"] = str(f)
    elif ctx.folders:
        v["file"], v["folder"], v["sub"] = "", str(ctx.folders[0]), True
    else:
        i.missing.append("the file or folder to read (click “Choose files” or “Choose folders”)")
    i.explain = f"I will collect every {'web link' if is_url else 'email address'} " + \
                (f"from {Path(v['file']).name}" if v.get("file") else "from the files in your folder") + \
                ", each only once, and save them as a new text file."
    return i


def _duplicates(text, t, ctx):
    if not re.search(r"\bduplicat\w*|\bidentical (files|copies)|\bcopies of\b|\bsame files\b", t) or _has(t, "lines", "rows", "records", "line"):
        return None
    i = Intent("duplicates", {"sub": True})
    _need_folder(i, ctx)
    if len(ctx.folders) > 1:
        i.values["more"] = "\n".join(str(p) for p in ctx.folders[1:])
    if re.search(r"\bnewest\b", t):
        i.values["keep"] = R.D_KEEP[1]
    i.explain = ("I will compare the contents of every file in " +
                 (f"{len(ctx.folders)} folders" if len(ctx.folders) > 1 else "your folder") +
                 " and move extra copies to the Recycle folder, keeping one of each.")
    if _has(t, "computer", "pc", "drive", "disk"):
        i.notes.append("I can't scan a whole computer safely. Choose the folders you want checked.")
    return i


def _rename(text, t, ctx):
    if not re.search(r"\brename\b|\bchange (the )?(file )?names?\b|\bnumber (the )?files\b|\badd .* (to|in) (the )?(file )?names?\b"
                     r"|\b(?:add|put|prepend|append)\b.*\b(?:start|beginning|front|end)\b.*\b(?:file )?names?\b", t):
        return None
    i = Intent("rename", {"sub": bool(re.search(r"subfolders|recursive|every folder|all folders", t))})
    _need_folder(i, ctx)
    containing = _term_after(text, r"(?:containing|contain|contains|with|has|including)\s+(?:the\s+)?(?:word|text|name)?")
    to = None
    m = re.search(r"\b(?:to|with|as|into|by)\s+(?:the\s+)?(?:word\s+|text\s+)?(\"[^\"]+\"|\u201c[^\u201d]+\u201d|'[^']+'|[^\s\"']+)\s*$", text.strip().rstrip("."), re.I)
    if m:
        to = m.group(1)
    if re.search(r"\badd\b.*\b(date)\b", t):
        i.values["mode"] = R.R_MODES[4]
        i.explain = "I will put each file's date last changed at the start of its name."
        return i
    if re.search(r"\b(number|numbered|sequence)\b", t):
        i.values["mode"] = R.R_MODES[3]
        prefix = _term_after(text, r"(?:called|named|as)")
        i.values["text"] = prefix or ""
        i.explain = "I will number the files in order (A to Z by name)."
        return i
    if re.search(r"\blowercase|lower case\b", t):
        i.values["mode"] = R.R_MODES[6]
        i.explain = "I will make all file names lowercase."
        return i
    if re.search(r"\buppercase|upper case\b", t):
        i.values["mode"] = R.R_MODES[7]
        i.explain = "I will make all file names UPPERCASE."
        return i
    if re.search(r"spaces?\b.*underscores?", t):
        i.values["mode"] = R.R_MODES[5]
        i.explain = "I will replace spaces in file names with underscores."
        return i
    word = r"(\"[^\"]+\"|\u201c[^\u201d]+\u201d|'[^']+'|[^\s\"\u201c\u201d']+)"       # a quoted phrase (spaces allowed) or one word
    pre = re.search(r"\b(?:add|put|prepend)\s+(?:the\s+)?(?:prefix\s+)?" + word + r"\s+(?:to|at|in)\s+(?:the\s+)?(?:start|beginning|front)", text, re.I)
    suf = re.search(r"\b(?:add|put|append)\s+(?:the\s+)?(?:suffix\s+)?" + word + r"\s+(?:to|at|in)\s+(?:the\s+)?end", text, re.I)
    if pre:
        piece = pre.group(1).strip(Q)
        i.values.update(mode=R.R_MODES[0], text=piece)
        i.explain = f"I will add “{piece}” to the start of each file name."
        return i
    if suf:
        piece = suf.group(1).strip(Q)
        i.values.update(mode=R.R_MODES[1], text=piece)
        i.explain = f"I will add “{piece}” to the end of each file name (before the .ext)."
        return i
    if containing:
        i.values.update(mode=R.R_MODES[2], only_name=containing, text=containing)
        if to and to.lower().strip(".") != containing.lower():
            i.values["text2"] = to.strip(Q)
            i.explain = f"I will find files with “{containing}” in their names and replace that text with “{to.strip(Q)}”."
        else:
            i.missing.append(f"what “{containing}” should be replaced with (type it in “Replace it with”; leave empty to remove it)")
            i.explain = f"I will find files with “{containing}” in their names and change that part of the name."
        return i
    i.missing.append("what kind of renaming you want (choose it in “What do you want to do?”)")
    i.explain = "I will help you rename many files."
    return i


def _replace_text(text, t, ctx):
    m = re.search(r"\b(?:replace|change|swap)\s+(\"[^\"]+\"|\u201c[^\u201d]+\u201d|'[^']+'|\S+)\s+(?:with|to|by)\s+(\"[^\"]+\"|\u201c[^\u201d]+\u201d|'[^']+'|[^\s\"']+)", text, re.I)
    if not m or not re.search(r"\b(inside|in|within|across|throughout)\b.*\b(files?|folders?|documents?|notes?|text)\b", t) or \
            re.search(r"\b(file )?names?\b", t):
        return None
    a, b = m.group(1).strip(Q), m.group(2).strip(Q).rstrip(".,;")
    i = Intent("replace", {"find": a, "replace": b})
    d = ctx.one_folder()
    f = ctx.file_named(text) or (ctx.files[0] if len(ctx.files) == 1 and not ctx.folders else None)
    if f:
        i.values.update(folder=str(f.parent), types=f.suffix.lstrip("."), sub=False)
        i.notes.append(f"This job works on a folder, so it will check all .{f.suffix.lstrip('.')} files next to {f.name}. "
                       "The preview shows exactly which files change.")
    elif d:
        i.values["folder"] = str(d)
    else:
        i.missing.append("the folder to use (click “Choose folders”)")
    if _has(t, "ignore case", "case-insensitive") or "case insensitive" in t:
        i.values["case"] = False
    i.explain = f"I will replace “{a}” with “{b}” in the text files, backing up each file first."
    return i


def _organize(text, t, ctx):
    if not re.search(r"\b(organi[sz]e|sort|group|tidy|arrange|clean up)\b", t) or not _has(t, "files", "folder", "folders", "downloads", "them", "these"):
        return None
    if re.search(r"\blines?\b|\brows?\b", t):
        return None
    i = Intent("organize")
    _need_folder(i, ctx)
    if re.search(r"\bextension|file type|format\b", t):
        i.values["how"] = R.O_MODES[3]
        how = "by file extension"
    elif re.search(r"\bmonth|date\b", t):
        i.values["how"] = R.O_MODES[2]
        how = "by month"
    else:
        i.values["how"] = R.O_MODES[0]
        how = "by type (Pictures, Documents, ...)"
    d = _days(t)
    if d:
        i.values["days"] = d
    i.explain = f"I will move the files in your folder into subfolders {how}" + (f", only files changed in the last {d} days." if d else ".")
    return i


def _find_files(text, t, ctx):
    if not re.search(r"\b(find|search|list|show|locate|look for|get)\b", t) or not _has(t, "files", "file", "folders", "folder"):
        return None
    i = Intent("find", {"sub": True})
    _need_folder(i, ctx)
    parts = []
    if re.search(r"\bempty folders?\b", t):
        i.values["what"] = "Empty folders"
        parts.append("empty folders")
    d, mb = _days(t), _size_mb(t)
    if d and re.search(r"modif|chang|edit|updat|creat|new", t):
        i.values["days"] = d
        parts.append(f"changed in the last {d} days")
    if mb:
        i.values["min_mb"] = mb
        i.values["order"] = "Biggest first"
        parts.append(f"bigger than {mb} MB")
    elif re.search(r"\b(large|big|huge)\b", t):
        i.values.update(min_mb="100", order="Biggest first")
        parts.append("bigger than 100 MB (my idea of “large”; change it if you like)")
    ext = re.findall(r"\.(%s)\b|\b(%s)\s+(?:files|documents)" % (FILE_EXT, FILE_EXT), t)
    if ext:
        i.values["types"] = ", ".join(sorted({a or b for a, b in ext}))
        parts.append(f"of type {i.values['types']}")
    term = _term_after(text, r"(?:containing|contain|contains|named|called|with the (?:word|name)|with)\s+(?:the\s+)?(?:word\s+|name\s+)?")
    if term and term.lower() not in ("a", "the", "any", "extension", "extensions"):
        i.values["name_has"] = term
        parts.append(f"with “{term}” in the name")
    i.explain = "I will search your folder for " + (", ".join(parts) if parts else "files") + " and show you the list."
    return i


def _sync(text, t, ctx):
    if not re.search(r"\b(sync|synchroni[sz]e|synchronization|keep .* in step|mirror|propagate)\b", t) and \
            not re.search(r"(same|identical|matching) (names?|named)", t):
        return None
    name = _term_after(text, r"(?:folders?\s+)?(?:named|called|with the name)")
    both = bool(re.search(r"both ways|two-way|two way|either (way|direction)|each other|every direction", t))
    live = bool(re.search(r"\b(whenever|automatically|continuous\w*|as i work|keep watching|monitor|watch)\b", t))
    i = Intent("watch" if live else "sync", {"mode": S.SYNC_MODES[1] if (both or live) else S.SYNC_MODES[0]})
    folders = ctx.folders
    if name:
        match = [f for f in folders if f.name.lower() == name.lower()]
        if match:
            i.values["folder"] = str(match[0])
            i.values["roots"] = "\n".join(sorted({str(f.parent) for f in folders}))
        elif folders:
            hit, looked = None, 0
            for r in folders:
                for dp, dn, _ in os.walk(r):
                    looked += 1
                    if looked > 4000:                # this runs on the GUI thread: never walk forever
                        i.notes.append("I stopped looking after 4000 folders - if it wasn't found, "
                                       "pick the folder itself with “Choose folders”.")
                        break
                    if any(d.lower() == name.lower() for d in dn):
                        hit = Path(dp) / next(d for d in dn if d.lower() == name.lower())
                        break
                    dn[:] = dn[:200]
                if hit:
                    break
            if hit:
                i.values["folder"] = str(hit)
                i.values["roots"] = "\n".join(str(f) for f in folders)
            else:
                i.missing.append(f"a folder called “{name}” - none was found in the places you chose")
        else:
            i.missing.append("the places to search (click “Choose folders” and pick e.g. C:/portfolio and C:/apps)")
    elif len(folders) >= 2 and len({f.name.lower() for f in folders}) == 1:
        i.values["folder"] = str(folders[0])
        i.values["roots"] = "\n".join(sorted({str(f.parent) for f in folders}))
    else:
        i.missing.append("which folder name to look for, and where (choose the folders, or say “folders named X”)")
    if re.search(r"\b(delete|deletions?|remove)\b.*\b(too|also|as well|propagate)|propagate (the )?delet", t):
        i.values["deletes"] = True
    i.explain = ("I will find folders with the same name, check by contents whether they really match, and " +
                 ("keep watching them" if live else "show you every difference") +
                 (", spreading changes in both directions." if i.values["mode"] == S.SYNC_MODES[1] else
                  ", making the others match the reference folder.") + " Nothing is deleted unless you allow it.")
    return i


def _compare(text, t, ctx):
    if not re.search(r"\bcompare|difference between|differences between|what.s different\b", t) or not _has(t, "folder", "folders", "directories", "directory"):
        return None
    name = _term_after(text, r"(?:folders?\s+)?(?:named|called)")
    i = Intent("compare")
    folders = ctx.folders
    if name and folders:
        match = [f for f in folders if f.name.lower() == name.lower()]
        i.values["folder"] = str(match[0] if match else folders[0])
        i.values["roots"] = "\n".join(sorted({str(f.parent) for f in folders if f.name.lower() == name.lower()} or {str(f) for f in folders}))
    elif len(folders) >= 2:
        i.values["folder"] = str(folders[0])
        i.values["roots"] = "\n".join(sorted({str(f.parent) for f in folders}))
    else:
        i.missing.append("the folders to compare (click “Choose folders” and pick two or more)")
    i.explain = "I will find folders with the same name and show, by comparing contents, how they differ. Nothing is changed."
    return i


def _lines(text, t, ctx):
    if not re.search(r"\blines?\b", t) or _has(t, "words"):
        return None
    ops = [(r"(duplicate|repeated|unique)", M.LINE_OPS[0], "remove duplicate lines (keeping the first)"),
           (r"(blank|empty) lines", M.LINE_OPS[4], "remove blank lines"),
           (r"\b(reverse)\b", M.LINE_OPS[6], "reverse the order of the lines"),
           (r"\bsort\b.*\b(z to a|descending|reverse)\b", M.LINE_OPS[2], "sort the lines Z to A"),
           (r"\bsort\b", M.LINE_OPS[1], "sort the lines A to Z"),
           (r"\b(trim|strip)\b", M.LINE_OPS[5], "trim spaces from the ends of lines")]
    for pat, op, desc in ops:
        if re.search(pat, t):
            i = Intent("lines", {"op": op})
            _need_file(i, ctx, text)
            i.explain = f"I will {desc} and save the result as a new file (your original stays untouched)."
            return i
    term = _term_after(text, r"(?:containing|contain|contains|with)")
    if term and re.search(r"\b(keep|only|filter|find|extract)\b", t):
        i = Intent("lines", {"op": M.LINE_OPS[8 - (0 if re.search(r"\b(remove|delete|without|drop)\b", t) else 1)], "text": term})
        _need_file(i, ctx, text)
        i.explain = f"I will {'remove' if i.values['op'] == M.LINE_OPS[8] else 'keep only'} the lines containing “{term}”."
        return i
    return None


def _csv(text, t, ctx):
    if not (re.search(r"\b(csv|spreadsheet|column|columns|rows?)\b", t) or ".csv" in t or ".xlsx" in t):
        return None
    if re.search(r"\b(combine|merge)\b", t) and re.search(r"\b(csv|spreadsheets?)\b", t):
        i = Intent("combinecsv", {"outname": "combined.csv"})
        _need_folder(i, ctx)
        i.explain = "I will stack the rows of all the CSV files in your folder into one new file."
        return i
    i = Intent("csv")
    _need_file(i, ctx, text, "spreadsheet file")
    col = _term_after(text, r"\b(?:column|of|in)\s+(?:the\s+)?(?:column\s+)?") or ""
    mm = re.search(r"\b(?:sum|total|average|mean|count|summari[sz]e|summary|statistics|stats)\b.*?(?:of|for|in)\s+(?:the\s+)?(?:column\s+)?(\"[^\"]+\"|\w+)", text, re.I)
    if mm:
        i.values.update(op=M.CSV_OPS[4], column=mm.group(1).strip('"'))
        i.explain = f"I will summarize the column “{i.values['column']}” (count, total, average...)."
        return i
    if re.search(r"\bduplicate rows?|duplicate records?|remove duplicates\b", t):
        i.values["op"] = M.CSV_OPS[2]
        i.explain = "I will remove duplicate rows and save the result as a new file."
        return i
    m = re.search(r"\b(?:where|with)\s+(?:the\s+)?(?:column\s+)?(\"[^\"]+\"|\w+)\s+(?:column\s+)?(is greater than|is more than|>|is less than|is under|<|contains|starts with|is|equals|=)\s+(\"[^\"]+\"|[^\s]+)", text, re.I)
    if m and re.search(r"\b(filter|keep|only|rows|find|show|extract)\b", t):
        cond = {"is greater than": M.CONDS[3], "is more than": M.CONDS[3], ">": M.CONDS[3], "is less than": M.CONDS[4],
                "is under": M.CONDS[4], "<": M.CONDS[4], "contains": M.CONDS[1], "starts with": M.CONDS[2]}.get(m.group(2).lower(), M.CONDS[0])
        i.values.update(op=M.CSV_OPS[0], column=m.group(1).strip('"'), cond=cond, value=m.group(3).strip('"').rstrip("."))
        i.explain = f"I will keep only the rows where {i.values['column']} {cond} {i.values['value']}, saved as a new file."
        return i
    mc = re.search(r"\b(?:keep|extract|select|only)\s+(?:the\s+)?columns?\s+(.+)$", text, re.I)
    if mc:
        cols = [c.strip(" .\"'") for c in re.split(r",|\band\b", mc.group(1)) if c.strip(" .\"'")]
        i.values.update(op=M.CSV_OPS[1], column=", ".join(cols))
        i.explain = f"I will keep only the columns {', '.join(cols)}."
        return i
    mv = re.search(r"\bconvert\b.*\b(json|tsv|csv)\b", t)
    if mv:
        i.values.update(op=M.CSV_OPS[5], fmt={"json": M.FORMATS[1], "tsv": M.FORMATS[0], "csv": M.FORMATS[2]}[mv.group(1)])
        i.explain = f"I will convert the file to {mv.group(1).upper()} as a new file."
        return i
    ms = re.search(r"\bsort\b.*?\bby\s+(?:the\s+)?(?:column\s+)?(\"[^\"]+\"|\w+)", text, re.I)
    if ms:
        i.values.update(op=M.CSV_OPS[3], column=ms.group(1).strip('"'), desc=bool(re.search(r"descending|biggest|largest|z to a|high", t)))
        i.explain = f"I will sort the rows by {i.values['column']}."
        return i
    return None


def _split(text, t, ctx):
    if not re.search(r"\bsplit\b|\bbreak up\b|\bcut\b", t):
        return None
    i = Intent("split")
    _need_file(i, ctx, text)
    m = re.search(r"(\d+)\s+lines?", t)
    p = re.search(r"into\s+(\d+)\s+(?:parts|files|pieces|chunks)", t)
    if p:
        i.values.update(by="Number of parts", n=p.group(1))
        i.explain = f"I will split the file into {p.group(1)} parts."
    else:
        i.values.update(by="Lines per file", n=m.group(1) if m else "1000")
        i.explain = f"I will split the file into parts of {i.values['n']} lines each."
    return i


def _merge(text, t, ctx):
    if not re.search(r"\b(merge|combine|join|concatenate|put together)\b", t) or not re.search(r"\b(text|txt|files?|notes)\b", t):
        return None
    i = Intent("merge")
    _need_folder(i, ctx)
    i.explain = "I will join the text files in your folder into one new file (your originals are untouched)."
    return i


def _report(text, t, ctx):
    if not re.search(r"\b(report|summary|summari[sz]e|overview|statistics|how (big|large|many))\b", t) or not _has(t, "folder", "folders", "directory"):
        return None
    i = Intent("report")
    _need_folder(i, ctx)
    i.explain = "I will summarize your folder: file count, total size, types, and the biggest and newest files."
    return i


HELP = ("I couldn't match that to something FileFixer can do yet. Things I understand:\n" +
        "\n".join(f"  • {s}" for s in SUGGESTIONS) +
        "\n\nYou can also pick a job from the left, or use Advanced Tools for full control.")
