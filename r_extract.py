"""Text extraction: pull out the words (or lines) that match a pattern, in their original order.

The pure functions here (`tokenize`, `find_matches`, `format_results`) are used by the job,
the plain-English command parser and the Advanced Tools, so they all behave identically.
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Iterator, Optional

import readers
from engine import (Action, Namer, NotText, Plan, UserError, check_file, check_folder, decode_bytes)
from recipes import Field, Recipe, check_name_text, plural

HOW = ["Starts with", "Ends with", "Contains", "Is exactly", "Regular expression (advanced)"]
SPLIT = ["Whitespace (spaces, tabs, line breaks)", "Each line is one item", "Only my own separators"]
PUNCT = ["Keep punctuation attached (nothing is altered)", "Ignore punctuation at the edges of words"]
NUMBERING = ["Numbered list (1. word)", "One per line, no numbers"]
RESULT = ["Create a new file (recommended - the original stays untouched)",
          "Replace the original file (a backup is kept)"]
MAX_BYTES = 200 * 1024 * 1024


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def tokenize(text: str, split: str = SPLIT[0], seps: str = "") -> Iterator[str]:
    """Yield the items of `text` in order of appearance."""
    if split == SPLIT[1]:
        for line in text.splitlines():
            if line.strip():
                yield line
        return
    if split == SPLIT[2]:
        chars = "".join(sorted(set(seps))) + "\r\n"
        rx = re.compile("[" + re.escape(chars) + "]+")
        for line in text.splitlines():
            for tok in rx.split(line):
                if tok:
                    yield tok
        return
    for line in text.splitlines():
        yield from line.split()


def strip_edges(tok: str, keep: frozenset = frozenset()) -> str:
    """Remove punctuation from both ends of a word (underscores and characters in `keep` stay)."""
    i, j = 0, len(tok)
    while i < j and tok[i] not in keep and tok[i] != "_" and unicodedata.category(tok[i])[0] in "PS":
        i += 1
    while j > i and tok[j - 1] not in keep and tok[j - 1] != "_" and unicodedata.category(tok[j - 1])[0] in "PS":
        j -= 1
    return tok[i:j]


def find_matches(text: str, patterns: list, how: str = HOW[0], case: bool = True,
                 punct: str = PUNCT[0], split: str = SPLIT[0], seps: str = "",
                 dedupe: bool = False) -> list:
    """Return the matching items, complete and in their original order."""
    patterns = [p for p in patterns if p != ""]
    if not patterns:
        raise UserError("Please type at least one word or pattern to look for.")
    if how == HOW[4]:
        try:
            rxs = [re.compile(p, 0 if case else re.IGNORECASE) for p in patterns]
        except re.error as e:
            raise UserError(f"This pattern isn't valid: {e}")
        test = lambda t: any(r.search(t) for r in rxs)
    else:
        fold = (lambda s: s) if case else (lambda s: s.casefold())
        pats = [fold(_nfc(p)) for p in patterns]
        if how == HOW[0]:
            test = lambda t: any(t.startswith(p) for p in pats)
        elif how == HOW[1]:
            test = lambda t: any(t.endswith(p) for p in pats)
        elif how == HOW[2]:
            test = lambda t: any(p in t for p in pats)
        else:
            test = lambda t: any(t == p for p in pats)
    keep = frozenset("".join(patterns)) if how != HOW[4] else frozenset()
    strip = punct == PUNCT[1]
    seen, out = set(), []
    for tok in tokenize(text, split, seps):
        if strip:
            tok = strip_edges(tok, keep)
            if not tok:
                continue
        probe = _nfc(tok)
        if how != HOW[4] and not case:
            probe = probe.casefold()
        if not test(probe):
            continue
        if dedupe:
            if tok in seen:
                continue
            seen.add(tok)
        out.append(tok)
    return out


def format_results(matches: list, numbered: bool = True) -> str:
    if numbered:
        return "\n".join(f"{i}. {w}" for i, w in enumerate(matches, 1))
    return "\n".join(matches)


def read_text_file(p: Path) -> tuple:
    return readers.read_document(p)


class ExtractWords(Recipe):
    key = "extract"
    name = "Extract words from a file"
    blurb = ("Pull out only the words that match, such as every word starting with itm_, keep each word complete, "
             "in the order it appears, and save them as a list.")
    fields = [
        Field("file", "File to read", "file", help="A text file (.txt, .md, .csv, .log ...), or a Word, Excel or PDF file."),
        Field("how", "Find words that", "choice", HOW[0], HOW),
        Field("patterns", "This text (one per line)", "multiline", "itm_", height=3,
              help="Several lines means: a word matches if it fits any one of them."),
        Field("case", "Match capital letters exactly", "check", True),
        Field("dedupe", "Show each word only once (first appearance)", "check", False),
        Field("punct", "Punctuation", "choice", PUNCT[0], PUNCT),
        Field("split", "A “word” is separated by", "choice", SPLIT[0], SPLIT),
        Field("seps", "Your separators", "text", ", ; |", help="Only used for “Only my own separators”. Line breaks always separate."),
        Field("numbering", "List style", "choice", NUMBERING[0], NUMBERING),
        Field("result", "Where should the results go?", "choice", RESULT[0], RESULT),
        Field("outname", "Name for the new file", "text", "",
              help="Leave empty to use the file's name + List.txt (for example GroceryItemsList.txt)."),
        Field("outdir", "Save the new file in", "folder", "", help="Optional. Leave empty to save it next to the original."),
    ]

    def plan(self, v):
        src = check_file(v["file"])
        patterns = [ln.strip("\r") for ln in v["patterns"].split("\n") if ln.strip("\r") != ""]
        text, enc = read_text_file(src)
        if v["result"] == RESULT[1] and readers.is_document(src):
            raise UserError("Replacing the original is only possible for plain text files. "
                            "Choose “Create a new file” for Word, Excel and PDF files.")
        matches = find_matches(text, patterns, v["how"], v["case"], v["punct"], v["split"], v["seps"], v["dedupe"])
        numbered = v["numbering"] == NUMBERING[0]
        body = format_results(matches, numbered)

        plan = Plan(self.name)
        if not matches:
            plan.summary = "I found 0 matching words, so there is nothing to save."
            return plan
        shown = format_results(matches[:60], numbered)
        if len(matches) > 60:
            shown += f"\n... and {len(matches) - 60} more"
        plan.report = shown.split("\n")

        if v["result"] == RESULT[1]:
            crlf = "\r\n" in text
            new_text = body.replace("\n", "\r\n") if crlf else body
            plan.actions.append(Action("edit", f"The text in {src.name} will be replaced by the results", src, src,
                                       transform=lambda t, n=new_text: n))
            plan.open_path = src
            plan.notes.append(f"The original is saved to the Recycle folder first, so “Undo last change” restores it.")
            plan.summary = (f"I found {plural(len(matches), 'matching word')}. "
                            f"{src.name} will be replaced by the list.")
            return plan

        name = v["outname"].strip() or f"{src.stem}List.txt"
        check_name_text(name, "The file name")
        if not Path(name).suffix:
            name += ".txt"
        outdir = check_folder(v["outdir"]) if v["outdir"].strip() else src.parent
        dst = Namer().reserve(outdir / name)
        if dst.resolve() == src:
            raise UserError("The new file would replace the original. Please choose a different name.")
        if dst.name != name:
            plan.notes.append(f"A file called {name} already exists, so the new one will be named {dst.name}.")
        out_text = body.replace("\n", "\r\n") if os.name == "nt" else body
        plan.actions.append(Action("write", f"A new file will be created:   {dst}", dst=dst,
                                   produce=lambda t=out_text: (t, "utf-8")))
        plan.open_path = dst
        plan.notes.append(f"The original file {src.name} is not changed.")
        plan.summary = f"I found {plural(len(matches), 'matching word')}. They will be saved as {dst.name}."
        return plan
