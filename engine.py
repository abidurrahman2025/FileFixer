"""FileFixer engine: plans, safe apply, undo.  No GUI code lives here.

Safety rules the whole app relies on:
  * Nothing is ever permanently deleted.  "Removed" files are moved into the
    Recycle folder, and files that get edited are backed up there first.
  * Every change is written to a history log so the last batch can be undone.
"""
from __future__ import annotations

import codecs
import errno
import json
import os
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

APP_DIR = Path(os.environ.get("FILEFIXER_HOME") or (Path.home() / "FileFixer"))
RECYCLE_DIR = APP_DIR / "Recycle"
HISTORY_FILE = APP_DIR / "history.json"
MAX_HISTORY = 50

SKIP_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}


class Cancelled(Exception):
    """The user pressed Cancel."""


_cancel = threading.Event()
HISTORY_LOCK = threading.RLock()


def request_cancel() -> None:
    _cancel.set()


def reset_cancel() -> None:
    _cancel.clear()


def check_cancel() -> None:
    if _cancel.is_set():
        raise Cancelled()


class UserError(Exception):
    """A problem explained in plain language, safe to show to the user."""


class NotText(Exception):
    """The file does not look like a text file."""


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------
@dataclass
class Action:
    kind: str                       # "move" | "copy" | "overwrite" | "mkdir" | "write" | "edit" | "trash" | "zip"
    sentence: str                   # plain-English line shown in the preview
    src: Optional[Path] = None
    dst: Optional[Path] = None
    detail: str = ""                # extra preview lines (optional)
    transform: Optional[Callable[[str], str]] = None        # for "edit"
    produce: Optional[Callable[[], tuple]] = None           # for "write": -> (text, encoding)
    # What the files looked like when the preview was made: [(path, size, mtime)], or size None = "must not exist".
    # If anything has changed by the time the action runs, it is refused instead of acting on stale information.
    guard: list = field(default_factory=list)


@dataclass
class Plan:
    title: str
    actions: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    summary: str = ""
    report: list = field(default_factory=list)      # extra lines shown in the preview (no changes implied)
    open_path: Optional[Path] = None                # a result the user can open afterwards
    selectable: bool = False                        # True: user may tick/untick individual changes
    after: Optional[Callable] = None                # called with the apply result (e.g. to save a snapshot)


# --------------------------------------------------------------------------
# Helpers shared by the recipes
# --------------------------------------------------------------------------
def norm(p) -> str:
    return os.path.normcase(str(p))


def _protected_dirs() -> list:
    dirs = []
    for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)",
                "ProgramW6432", "ProgramData"):
        v = os.environ.get(var)
        if v:
            dirs.append(Path(v))
    if os.name != "nt":
        dirs += [Path(p) for p in ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64",
                                   "/proc", "/sbin", "/sys", "/usr")]
    return dirs


def check_folder(text) -> Path:
    """Turn what the user picked into a safe, existing folder or explain why not."""
    if not text or not str(text).strip():
        raise UserError("Please choose a folder first.")
    p = Path(str(text).strip().strip('"')).expanduser()
    if not p.exists():
        raise UserError(f"I can't find this folder:\n{p}")
    if not p.is_dir():
        raise UserError("That is a file, not a folder. Please choose a folder.")
    p = p.resolve()
    if p == Path(p.anchor):
        raise UserError("Please choose a specific folder, not a whole drive.")
    for bad in _protected_dirs():
        try:
            b = bad.resolve()
        except OSError:
            continue
        if p == b or b in p.parents:
            raise UserError("That folder belongs to your computer's system, so FileFixer won't touch it.\n"
                            "Please choose one of your own folders (Documents, Downloads, ...).")
    app = APP_DIR.resolve()
    if p == app or app in p.parents:
        raise UserError("That is FileFixer's own folder. Please choose a different one.")
    return p


def check_file(text) -> Path:
    """Turn what the user picked into an existing regular file or explain why not."""
    if not text or not str(text).strip():
        raise UserError("Please choose a file first.")
    p = Path(str(text).strip().strip('"')).expanduser()
    if not p.exists():
        raise UserError(f"I can't find this file:\n{p}")
    if not p.is_file():
        raise UserError("That is a folder, not a file. Please choose a file.")
    return p.resolve()


def parse_exts(text) -> Optional[set]:
    """'txt, .MD *.csv' -> {'.txt', '.md', '.csv'}; empty -> None (meaning 'all')."""
    out = set()
    for part in str(text or "").replace(";", ",").replace(" ", ",").split(","):
        part = part.strip().lower().lstrip("*").strip()
        if not part:
            continue
        out.add(part if part.startswith(".") else "." + part)
    return out or None


def list_files(folder: Path, subfolders: bool, exts: Optional[set] = None) -> list:
    """Regular files in a folder (optionally below it), skipping hidden/system junk."""
    app = APP_DIR.resolve()

    def keep(p: Path) -> bool:
        n = p.name
        if n.startswith(".") or n.startswith("~$") or n.lower() in SKIP_NAMES:
            return False
        if is_link(p) or not p.is_file():
            return False
        return not exts or p.suffix.lower() in exts

    result = []
    if subfolders:
        for root, dirs, files in os.walk(folder):
            check_cancel()
            rp = Path(root)
            dirs[:] = sorted(d for d in dirs
                             if not d.startswith(".")
                             and not is_link(rp / d)
                             and (rp / d).resolve() != app)
            result += [rp / f for f in sorted(files) if keep(rp / f)]
    else:
        result = [p for p in sorted(folder.iterdir()) if keep(p)]
    return result


class Namer:
    """Hands out file names that will not collide with existing files or each other."""

    def __init__(self) -> None:
        self.taken: set = set()

    def reserve(self, dst: Path, src: Optional[Path] = None) -> Path:
        cand, n = dst, 2
        while True:
            key = norm(cand)
            same_file = src is not None and key == norm(src)     # case-only change
            if same_file or (key not in self.taken and not cand.exists()):
                self.taken.add(key)
                return cand
            cand = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
            n += 1


def decode_bytes(b: bytes) -> tuple:
    """Return (text, encoding) so the file can be written back the way it was."""
    if b.startswith(codecs.BOM_UTF8):
        return b.decode("utf-8-sig"), "utf-8-sig"
    if b.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return b.decode("utf-16"), "utf-16"
    if b"\x00" in b[:8192]:
        raise NotText()
    try:
        return b.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return b.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return b.decode("latin-1"), "latin-1"


def looks_like_text(p: Path) -> bool:
    try:
        with open(p, "rb") as fh:
            head = fh.read(8192)
    except OSError:
        return False
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return True
    return b"\x00" not in head


def size_text(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} bytes" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


IS_WINDOWS = os.name == "nt"
_REPARSE_FLAG, _MOUNT_POINT_TAG = 0x400, 0xA0000003


def is_junction_stat(st) -> bool:
    """True for a Windows junction / mount point. Other reparse points (OneDrive placeholders) are real files."""
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_FLAG) and getattr(st, "st_reparse_tag", 0) == _MOUNT_POINT_TAG


def is_link(p) -> bool:
    """A symbolic link or Windows junction: something that points somewhere else and is never followed."""
    try:
        if os.path.islink(p):
            return True
        return IS_WINDOWS and is_junction_stat(os.lstat(p))
    except OSError:
        return False


def friendly(e: BaseException) -> str:
    if getattr(e, "winerror", None) == 206 or getattr(e, "errno", None) == errno.ENAMETOOLONG or \
            (isinstance(e, FileNotFoundError) and IS_WINDOWS and len(str(getattr(e, "filename", "") or "")) > 259):
        return ("the file's full path is too long for Windows (over 260 characters). "
                "Use a shorter folder or file name and try again")
    if isinstance(e, PermissionError):
        return "not allowed (the file may be open in another program, or read-only)"
    if isinstance(e, FileNotFoundError):
        return "the file is no longer there"
    if isinstance(e, UnicodeEncodeError):
        return "the new text has characters this file's format can't store"
    if isinstance(e, NotText):
        return "this doesn't look like a text file"
    return str(e) or e.__class__.__name__


# --------------------------------------------------------------------------
# Applying a plan
# --------------------------------------------------------------------------
def _missing_dirs(d: Path) -> list:
    out = []
    while not d.exists() and d != d.parent:
        out.append(d)
        d = d.parent
    return out


def _unique_file(dirpath: Path, name: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    cand, n = dirpath / name, 2
    stem, suf = Path(name).stem, Path(name).suffix
    while cand.exists():
        cand = dirpath / f"{stem} ({n}){suf}"
        n += 1
    return cand


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if norm(src) == norm(dst) and str(src) != str(dst):          # case-only rename on Windows/Mac
        tmp = src.with_name(f"{src.name}.{uuid.uuid4().hex[:8]}.fftmp")
        os.rename(src, tmp)
        os.rename(tmp, dst)
        return
    if dst.exists():
        raise FileExistsError(f"'{dst.name}' already exists there")
    shutil.move(str(src), str(dst))


def _check_guard(a: Action) -> None:
    for path, size, mtime in a.guard:
        try:
            st = os.lstat(path)
            now = (st.st_size, st.st_mtime)
        except OSError:
            now = None
        want = None if size is None else (size, mtime)
        if now != want:
            raise UserError(f"{Path(path).name} changed after the preview was made, so it was left alone. "
                            "Show me what will happen again to include the newer version")


def _do(a: Action, bdir: Path) -> Optional[dict]:
    _check_guard(a)
    if a.kind == "move":
        made = _missing_dirs(a.dst.parent)
        _move(a.src, a.dst)
        return {"op": "move", "src": str(a.src), "dst": str(a.dst), "made": [str(d) for d in made]}

    if a.kind == "trash":
        target = _unique_file(bdir / "removed", a.src.name)
        _move(a.src, target)
        return {"op": "trash", "src": str(a.src), "dst": str(target), "made": []}

    if a.kind == "edit":
        text, enc = decode_bytes(a.src.read_bytes())
        new = a.transform(text)
        if new == text:
            return None
        data = new.encode(enc)                                   # fail before touching anything
        backup = _unique_file(bdir / "before-edit", a.src.name)
        shutil.copy2(a.src, backup)
        tmp = a.src.with_name(a.src.name + ".fftmp")
        try:
            tmp.write_bytes(data)
            try:
                shutil.copymode(a.src, tmp)
            except OSError:
                pass
            os.replace(tmp, a.src)
        finally:
            if tmp.exists():
                tmp.unlink()
        return {"op": "edit", "src": str(a.src), "dst": str(a.src), "backup": str(backup), "made": []}

    if a.kind == "mkdir":
        if a.dst.exists():
            return None
        made = _missing_dirs(a.dst)
        a.dst.mkdir(parents=True, exist_ok=True)
        return {"op": "mkdir", "src": "", "dst": str(a.dst), "made": [str(d) for d in made[1:]]}

    if a.kind == "overwrite":
        if not a.src.is_file():
            raise FileNotFoundError()
        made = _missing_dirs(a.dst.parent)
        a.dst.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if a.dst.exists():
            backup = _unique_file(bdir / "before-edit", a.dst.name)
            shutil.copy2(a.dst, backup)
        tmp = a.dst.with_name(a.dst.name + ".fftmp")
        try:
            shutil.copy2(a.src, tmp)
            os.replace(tmp, a.dst)
        finally:
            if tmp.exists():
                tmp.unlink()
        if backup is None:
            return {"op": "write", "src": "", "dst": str(a.dst), "made": [str(d) for d in made]}
        return {"op": "edit", "src": str(a.dst), "dst": str(a.dst), "backup": str(backup), "made": []}

    if a.kind == "copy":
        if a.dst.exists():
            raise FileExistsError(f"'{a.dst.name}' already exists there")
        made = _missing_dirs(a.dst.parent)
        a.dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = a.dst.with_name(a.dst.name + ".fftmp")          # copy aside first: an interrupted copy never leaves a half file
        try:
            shutil.copy2(a.src, tmp)
            if a.dst.exists():
                raise FileExistsError(f"'{a.dst.name}' already exists there")
            os.replace(tmp, a.dst)
        finally:
            if tmp.exists():
                tmp.unlink()
        return {"op": "copy", "src": "", "dst": str(a.dst), "made": [str(d) for d in made]}

    if a.kind == "zip":
        if a.dst.exists():
            raise FileExistsError(f"'{a.dst.name}' already exists there")
        if not a.src.is_dir():
            raise FileNotFoundError()
        tmp = a.dst.with_name(a.dst.name + ".fftmp")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as z:
                for f in sorted(a.src.rglob("*")):
                    if f.is_file():
                        z.write(f, Path(a.src.name) / f.relative_to(a.src))
            os.replace(tmp, a.dst)
        finally:
            if tmp.exists():
                tmp.unlink()
        return {"op": "write", "src": "", "dst": str(a.dst), "made": []}

    if a.kind == "write":
        if a.dst.exists():
            raise FileExistsError(f"'{a.dst.name}' already exists there")
        text, enc = a.produce()
        data = text.encode(enc)
        made = _missing_dirs(a.dst.parent)
        a.dst.parent.mkdir(parents=True, exist_ok=True)
        a.dst.write_bytes(data)
        return {"op": "write", "src": "", "dst": str(a.dst), "made": [str(d) for d in made]}

    raise ValueError(f"Unknown action {a.kind}")


def load_history() -> list:
    try:
        data = json.loads(HISTORY_FILE.read_text("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(h: list) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(h[-MAX_HISTORY:], indent=1), "utf-8")
    os.replace(tmp, HISTORY_FILE)


def apply_plan(plan: Plan, progress: Optional[Callable[[int, int], None]] = None) -> dict:
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    bdir = RECYCLE_DIR / batch_id
    records, problems = [], []
    stopped_early = False
    total = len(plan.actions)
    for i, a in enumerate(plan.actions, 1):
        if _cancel.is_set():
            problems.append(f"Stopped early at your request: {total - i + 1} of {total} changes were not made.")
            stopped_early = True
            break
        try:
            rec = _do(a, bdir)
            if rec:
                records.append(rec)
        except Exception as e:                       # one bad file must not stop the rest
            name = (a.src or a.dst).name
            problems.append(f"{name}: {friendly(e)}")
        if progress:
            progress(i, total)
    if records:
        with HISTORY_LOCK:
            h = load_history()
            h.append({"id": batch_id, "title": plan.title, "time": datetime.now().isoformat(timespec="seconds"),
                      "records": records, "undone": False})
            save_history(h)
    result = {"done": len(records), "problems": problems, "batch": batch_id,
              "recycle": str(bdir) if bdir.exists() else "",
              "open_path": str(plan.open_path) if plan.open_path and Path(plan.open_path).exists() else ""}
    if plan.after:
        if stopped_early:
            # The follow-up step (e.g. saving the sync "last agreed" snapshot) must not
            # record a half-applied batch as fully done, or the next run would see
            # nothing to do and never finish the remaining changes.
            problems.append("(bookkeeping) skipped because the run was stopped early - "
                            "show the preview again to finish the remaining changes.")
        else:
            try:
                plan.after(result)
            except Exception as e:                   # a failed bookkeeping step must not hide the result
                problems.append(f"(bookkeeping) {friendly(e)}")
    return result


# --------------------------------------------------------------------------
# Undo
# --------------------------------------------------------------------------
def last_undoable() -> Optional[dict]:
    for b in reversed(load_history()):
        if not b.get("undone"):
            return b
    return None


def undo_last(progress: Optional[Callable[[int, int], None]] = None) -> dict:
    with HISTORY_LOCK:
        return _undo_last(progress)


def _undo_last(progress: Optional[Callable[[int, int], None]] = None) -> dict:
    h = load_history()
    batch = next((b for b in reversed(h) if not b.get("undone")), None)
    if batch is None:
        raise UserError("There is nothing to undo.")
    bdir = RECYCLE_DIR / batch["id"]
    recs = list(reversed(batch["records"]))
    done, problems = 0, []
    retry = bool(batch.get("partial"))
    for i, r in enumerate(recs, 1):                       # undo always finishes: a half-undone batch is worse
        src, dst = Path(r["src"]) if r["src"] else None, Path(r["dst"])
        label = dst.name
        if r.get("restored"):                             # put back by an earlier attempt
            if progress:
                progress(i, len(recs))
            continue
        try:
            if r["op"] in ("move", "trash"):
                if not dst.exists():
                    raise FileNotFoundError()
                if src.exists() and norm(src) != norm(dst):
                    raise FileExistsError(f"something else is now at the original place ({src.name})")
                _move(dst, src)
                for d in r.get("made", []):                  # tidy folders we created
                    try:
                        Path(d).rmdir()
                    except OSError:
                        pass
            elif r["op"] == "edit":
                backup = Path(r["backup"])
                if not backup.exists():
                    raise FileNotFoundError()
                shutil.copy2(backup, dst)
            elif r["op"] == "mkdir":
                try:
                    dst.rmdir()
                except FileNotFoundError:
                    pass
                except OSError:
                    raise OSError("the folder is no longer empty, so it was left in place")
                for d in r.get("made", []):
                    try:
                        Path(d).rmdir()
                    except OSError:
                        pass
            elif r["op"] in ("write", "copy"):
                if dst.exists():
                    _move(dst, _unique_file(bdir / "undone", dst.name))
                for d in r.get("made", []):
                    try:
                        Path(d).rmdir()
                    except OSError:
                        pass
            r["restored"] = True
            done += 1
        except Exception as e:
            problems.append(f"{label}: {friendly(e)}")
        if progress:
            progress(i, len(recs))
    # A batch only counts as undone when everything was put back. Otherwise it stays on the list so the
    # items that failed can be tried again. If a retry gets nowhere, stop offering it so older changes
    # are not blocked, and say so.
    gave_up = bool(problems) and retry and done == 0
    batch["partial"] = bool(problems) and not gave_up
    batch["undone"] = not problems or gave_up
    if gave_up:
        problems.append("These items could not be put back after a second try, so they were left as they are.")
    save_history(h)
    prune_empty_dirs(bdir)
    return {"done": done, "problems": problems, "title": batch["title"], "partial": batch["partial"]}


# --------------------------------------------------------------------------
# Recycle folder hygiene
# --------------------------------------------------------------------------
def prune_empty_dirs(top: Path) -> None:
    """Remove ``top`` and every folder inside it that is empty. A folder with anything in it stays."""
    try:
        for root, _dirs, _files in os.walk(top, topdown=False):
            try:
                os.rmdir(root)
            except OSError:
                pass
    except OSError:
        pass


def _tree_size(top: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(top):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def recycle_usage() -> tuple:
    """(number of backup batches, bytes used) in the Recycle folder."""
    count, size = 0, 0
    try:
        with os.scandir(RECYCLE_DIR) as it:
            entries = [e for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        return 0, 0
    for e in entries:
        count += 1
        size += _tree_size(Path(e.path))
    return count, size


ORPHAN_GRACE_SECONDS = 3600      # a batch folder this new may still be getting written to


def cleanup_recycle() -> dict:
    """Delete backups that Undo can no longer use.

    Kept: batches that are not undone and are among the newest MAX_HISTORY.
    Everything else in Recycle goes, then history entries for undone batches are dropped.
    Returns {"removed": n, "freed": bytes, "kept": n, "problems": [..]}.
    """
    removed, freed, problems = 0, 0, []
    with HISTORY_LOCK:
        h = load_history()
        keep = {b.get("id") for b in h[-MAX_HISTORY:] if not b.get("undone")}
        known = {b.get("id") for b in h}
        try:
            with os.scandir(RECYCLE_DIR) as it:
                dirs = [e for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            dirs = []
        gone = set()
        for e in dirs:
            if e.name in keep:
                continue
            try:
                if e.name not in known and time.time() - e.stat().st_mtime < ORPHAN_GRACE_SECONDS:
                    continue
            except OSError:
                continue
            size = _tree_size(Path(e.path))
            shutil.rmtree(e.path, ignore_errors=True)
            if os.path.exists(e.path):
                problems.append(f"{e.name}: some files could not be deleted")
                continue
            removed, freed = removed + 1, freed + size
            gone.add(e.name)
        pruned = [b for b in h if not (b.get("undone") or b.get("id") in gone)]
        if len(pruned) != len(h):
            save_history(pruned)
    kept = sum(1 for e in dirs if e.name in keep)
    return {"removed": removed, "freed": freed, "kept": kept, "problems": problems}


def describe_batch(b: dict) -> str:
    try:
        when = datetime.fromisoformat(b["time"]).strftime("%b %d, %H:%M")
    except Exception:
        when = b.get("time", "")
    left = sum(1 for r in b["records"] if not r.get("restored"))
    part = f" ({left} still to put back)" if b.get("partial") else ""
    return f"{b['title']} - {len(b['records'])} changes, {when}{part}"


# --------------------------------------------------------------------------
# Activity log (shown on the Home screen and in Advanced Tools > History)
# --------------------------------------------------------------------------
ACTIVITY_FILE = APP_DIR / "activity.json"
MAX_ACTIVITY = 300


def load_activity() -> list:
    try:
        data = json.loads(ACTIVITY_FILE.read_text("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def log_activity(status: str, title: str, detail: str = "", path: str = "", recipe: str = "") -> None:
    """status: done | failed | pending | undone | info"""
    with HISTORY_LOCK:
        try:
            items = load_activity()
            items.append({"time": datetime.now().isoformat(timespec="seconds"), "status": status, "title": title,
                          "detail": detail[:600], "path": path, "recipe": recipe})
            APP_DIR.mkdir(parents=True, exist_ok=True)
            tmp = ACTIVITY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(items[-MAX_ACTIVITY:], indent=1), "utf-8")
            os.replace(tmp, ACTIVITY_FILE)
        except OSError:
            pass                                    # a log problem must never break the real work
