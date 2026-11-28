"""Chainable text operations (Advanced Tools: regex builder, workflow builder, batch processing).

A workflow is a list of steps; each step is {"op": key, "params": {...}}.  The text produced by one step is
the input of the next, so workflows chain for real.  Nothing here touches the disk except save/load of
workflow definitions; results are written through engine plans (preview, backup, undo).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import engine
import r_extract as X
import readers
from engine import Action, Namer, Plan, UserError, check_folder
from recipes import check_name_text, check_new_name, name_problem, natural_key, plural, with_extension

CASE = ("case", "check", False, None, "Match capital letters exactly")

# op key -> (label, [(param key, kind, default, choices, label)])
OPS = {
    "extract": ("Keep only the words that match", [
        ("how", "choice", X.HOW[0], X.HOW, "Words that"), ("patterns", "text", "", None, "This text (separate several with | )"),
        CASE, ("punct", "choice", X.PUNCT[0], X.PUNCT, "Punctuation"),
        ("split", "choice", X.SPLIT[0], X.SPLIT, "Words are separated by"),
        ("dedupe", "check", False, None, "Each only once")]),
    "number": ("Number the lines (1. 2. 3. ...)", []),
    "dedupe_lines": ("Remove duplicate lines", [CASE]),
    "sort_lines": ("Sort the lines", [("order", "choice", "A to Z", ["A to Z", "Z to A"], "Order")]),
    "trim": ("Trim spaces at the ends of lines", []),
    "remove_blank": ("Remove blank lines", []),
    "reverse": ("Reverse the order of lines", []),
    "keep_lines": ("Keep only lines containing", [("text", "text", "", None, "Text"), CASE]),
    "drop_lines": ("Remove lines containing", [("text", "text", "", None, "Text"), CASE]),
    "replace": ("Replace text", [("find", "text", "", None, "Find"), ("replace", "text", "", None, "Replace with"), CASE]),
    "regex_extract": ("Keep only what matches a pattern (regex)", [
        ("pattern", "text", "", None, "Pattern"), ("ignorecase", "check", False, None, "Ignore capital letters")]),
    "regex_replace": ("Replace what matches a pattern (regex)", [
        ("pattern", "text", "", None, "Pattern"), ("repl", "text", "", None, "Replace with (\\1 = group 1)"),
        ("ignorecase", "check", False, None, "Ignore capital letters")]),
    "lowercase": ("Make everything lowercase", []),
    "uppercase": ("Make everything UPPERCASE", []),
    "first_n": ("Keep only the first N lines", [("n", "text", "10", None, "N")]),
}
WORKFLOW_DIR = engine.APP_DIR / "workflows"
RUNS_FILE = engine.APP_DIR / "runs.json"
NL = "\r\n" if os.name == "nt" else "\n"


def default_params(op: str) -> dict:
    return {p[0]: p[2] for p in OPS[op][1]}


def describe(step: dict) -> str:
    op, p = step["op"], step.get("params", {})
    bits = [f"{k}={v!r}" for k, v in p.items() if v not in ("", False, None) and k not in ("how", "punct", "split")]
    if op == "extract":
        bits.insert(0, p.get("how", ""))
    return OPS[op][0] + (f"  ({', '.join(bits)})" if bits else "")


def _rx(pattern: str, ignore: bool):
    if pattern == "":
        raise UserError("A pattern is empty. Please type one.")
    try:
        return re.compile(pattern, re.IGNORECASE if ignore else 0)
    except re.error as e:
        raise UserError(f"This pattern isn't valid: {e}")


def apply_step(step: dict, text: str) -> str:
    op = step.get("op")
    if op not in OPS:
        raise UserError(f"Unknown step “{op}”.")
    p = {**default_params(op), **step.get("params", {})}
    lines = text.splitlines()
    fold = (lambda s: s) if p.get("case") else (lambda s: s.casefold())
    if op == "extract":
        pats = [x for x in re.split(r"\s*\|\s*", p["patterns"]) if x] if p["how"] != X.HOW[4] else [p["patterns"]]
        return "\n".join(X.find_matches(text, pats, p["how"], bool(p["case"]), p["punct"], p["split"], "", bool(p["dedupe"])))
    if op == "number":
        return "\n".join(f"{i}. {ln}" for i, ln in enumerate([ln for ln in lines if ln.strip()], 1))
    if op == "dedupe_lines":
        seen, out = set(), []
        for ln in lines:
            k = fold(ln)
            if k not in seen:
                seen.add(k)
                out.append(ln)
        return "\n".join(out)
    if op == "sort_lines":
        return "\n".join(sorted(lines, key=natural_key, reverse=p["order"] == "Z to A"))
    if op == "trim":
        return "\n".join(ln.strip() for ln in lines)
    if op == "remove_blank":
        return "\n".join(ln for ln in lines if ln.strip())
    if op == "reverse":
        return "\n".join(lines[::-1])
    if op in ("keep_lines", "drop_lines"):
        if p["text"] == "":
            raise UserError("A step is missing its text.")
        keep = op == "keep_lines"
        return "\n".join(ln for ln in lines if (fold(p["text"]) in fold(ln)) == keep)
    if op == "replace":
        if p["find"] == "":
            raise UserError("A “Replace text” step is missing what to find.")
        rx = re.compile(re.escape(p["find"]), 0 if p["case"] else re.IGNORECASE)
        return rx.sub(lambda _m: p["replace"], text)
    if op == "regex_extract":
        rx = _rx(p["pattern"], p["ignorecase"])
        return "\n".join(m.group(0) for m in rx.finditer(text))
    if op == "regex_replace":
        rx = _rx(p["pattern"], p["ignorecase"])
        try:
            return rx.sub(p["repl"], text)
        except (re.error, IndexError) as e:
            raise UserError(f"The replacement isn't valid: {e}")
    if op == "lowercase":
        return text.lower()
    if op == "uppercase":
        return text.upper()
    if op == "first_n":
        try:
            n = int(p["n"])
        except ValueError:
            raise UserError("“Keep only the first N lines” needs a number.")
        return "\n".join(lines[:max(n, 0)])
    raise UserError(f"Unknown step “{op}”.")


def run_steps(steps: list, text: str) -> tuple:
    """Return (final text, [(step description, line count after)])."""
    if not steps:
        raise UserError("Add at least one step first.")
    log = []
    for s in steps:
        text = apply_step(s, text)
        log.append((describe(s), len(text.splitlines()) if text else 0))
    return text, log


# ---- saving workflows --------------------------------------------------------
def _safe(name: str) -> str:
    name = name.strip()
    if not name:
        raise UserError("Please give it a name.")
    check_name_text(name, "The name")
    if name_problem(name) or name.strip(". ") == "":
        raise UserError(f"“{name}” can't be used as a name (it can't end with a dot or a space, "
                        "or be a name Windows keeps for itself). Please choose another.")
    return name


def save_workflow(name: str, steps: list) -> Path:
    name = _safe(name)
    p = WORKFLOW_DIR / f"{name}.json"
    try:
        engine.write_text_atomic(p, json.dumps({"name": name, "steps": steps}, indent=1))
    except OSError as e:
        raise UserError(f"I couldn't save the workflow: {e.strerror or e}")
    return p


def list_workflows() -> list:
    return sorted(p.stem for p in WORKFLOW_DIR.glob("*.json")) if WORKFLOW_DIR.exists() else []


def load_workflow(name: str) -> list:
    try:
        data = json.loads((WORKFLOW_DIR / f"{_safe(name)}.json").read_text("utf-8"))
        steps = data["steps"]
        for s in steps:
            if s["op"] not in OPS:
                raise ValueError(s["op"])
        return steps
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise UserError(f"I couldn't open that workflow ({e}).")


def delete_workflow(name: str) -> None:
    try:
        (WORKFLOW_DIR / f"{_safe(name)}.json").unlink()
    except OSError:
        pass


# ---- saved scripts -------------------------------------------------------------
SCRIPT_DIR = engine.APP_DIR / "scripts"


def save_script(name: str, code: str) -> Path:
    name = _safe(name)
    p = SCRIPT_DIR / (name if name.endswith(".py") else name + ".py")
    try:
        engine.write_text_atomic(p, code)
    except OSError as e:
        raise UserError(f"I couldn't save the script: {e.strerror or e}")
    return p


def list_scripts() -> list:
    return sorted(p.name for p in SCRIPT_DIR.glob("*.py")) if SCRIPT_DIR.exists() else []


def load_script(name: str) -> str:
    try:
        return (SCRIPT_DIR / Path(name).name).read_text("utf-8")
    except OSError as e:
        raise UserError(f"I couldn't open that script ({e.strerror or e}).")


# ---- run log (for Execution History) -------------------------------------------
def log_run(kind: str, name: str, status: str, detail: str, payload=None) -> None:
    with engine.HISTORY_LOCK:
        try:
            runs = json.loads(RUNS_FILE.read_text("utf-8"))
        except Exception:
            runs = []
        runs.append({"time": engine.datetime.now().isoformat(timespec="seconds"), "kind": kind, "name": name,
                     "status": status, "detail": detail[:20000], "payload": payload})
        engine.write_text_atomic(RUNS_FILE, json.dumps(runs[-200:], indent=1))


def load_runs() -> list:
    try:
        return json.loads(RUNS_FILE.read_text("utf-8"))
    except Exception:
        return []


# ---- regex builder ----------------------------------------------------------------
REGEX_MODES = ["Show the matches", "Keep only the matches", "Replace the matches", "Remove the matches"]


def regex_apply(pattern: str, text: str, mode: str, repl: str = "", ignorecase: bool = False,
                multiline: bool = False) -> tuple:
    """Return (result text, match spans).  Spans are (start, end) positions in `text`."""
    if pattern == "":
        raise UserError("Please type a pattern.")
    try:
        rx = re.compile(pattern, (re.IGNORECASE if ignorecase else 0) | (re.MULTILINE if multiline else 0))
    except re.error as e:
        raise UserError(f"This pattern isn't valid: {e}")
    spans = [m.span() for m in rx.finditer(text)]
    if mode == REGEX_MODES[0]:
        return "\n".join(f"{i}. {text[a:b]}" for i, (a, b) in enumerate(spans, 1)), spans
    if mode == REGEX_MODES[1]:
        return "\n".join(text[a:b] for a, b in spans), spans
    if mode == REGEX_MODES[2]:
        try:
            return rx.sub(repl, text), spans
        except (re.error, IndexError) as e:
            raise UserError(f"The replacement isn't valid: {e}")
    return rx.sub("", text), spans


def file_output_plan(title: str, src: Path, body: str, outdir: Optional[str], outname: str, default: str) -> Plan:
    """A plan that saves `body` as a new text file (never overwrites)."""
    name = with_extension(outname.strip() or default, ".txt")
    check_new_name(name, "The file name")
    folder = check_folder(outdir) if outdir and outdir.strip() else src.parent
    dst = Namer().reserve(folder / name)
    plan = Plan(title)
    text = body.replace("\n", NL)
    plan.actions.append(Action("write", f"A new file will be created:   {dst}", dst=dst, produce=lambda t=text: (t, "utf-8")))
    plan.open_path = dst
    plan.notes.append(f"The original file {src.name} is not changed.")
    if dst.name != name:
        plan.notes.append(f"{name} already exists, so the new file is named {dst.name}.")
    return plan


def batch_plan(steps: list, files: list, outdir: str, suffix: str) -> Plan:
    """Apply the same workflow to many files; each result becomes its own new file."""
    folder = check_folder(outdir)
    check_name_text(suffix, "The ending")
    plan, namer, failed = Plan("Batch processing"), Namer(), []
    for f in files:
        try:
            text, _ = readers.read_document(Path(f))
            out, _ = run_steps(steps, text)
        except UserError as e:
            failed.append(f"{Path(f).name}: {e}")
            continue
        new_name = f"{Path(f).stem}{suffix}.txt"
        if name_problem(new_name):
            failed.append(f"{Path(f).name}: the new name isn't allowed on Windows")
            continue
        dst = namer.reserve(folder / new_name)
        text = out.replace("\n", NL)
        plan.actions.append(Action("write", f"{Path(f).name}   →   {dst.name}   ({plural(len(out.splitlines()), 'line')})",
                                   dst=dst, produce=lambda t=text: (t, "utf-8")))
    plan.notes += [f"Skipped {x}" for x in failed]
    plan.summary = (f"{plural(len(plan.actions), 'new file')} will be created in {folder}." if plan.actions
                    else "Nothing to create.")
    plan.notes.append("Your original files are not changed.")
    plan.open_path = folder if plan.actions else None
    return plan
