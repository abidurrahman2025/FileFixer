"""Compare and synchronize same-named folders in different places.

A matching NAME never means matching CONTENT.  Every group of same-named folders is
verified by comparing file contents (hashes) and labelled Identical / Similar / Share no files.

Safety rules:
  * Nothing is overwritten silently: files that are replaced are backed up to the Recycle folder.
  * Deletions are never propagated unless the user opts in, and "deleted" files go to Recycle.
  * If two places changed the same file differently, it is a CONFLICT and is skipped unless
    the user picked a resolution rule.
  * Symbolic links are never followed.  A folder never syncs with itself or with a folder inside it.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import engine
from engine import (Action, Plan, SKIP_NAMES, UserError, check_cancel, check_folder, norm, size_text)
from recipes import Field, Recipe, _file_hash, plural

SYNC_MODES = ["Make the others match the reference folder (one-way)",
              "Bring changes both ways (two-way)"]
CONFLICT_RULES = ["Skip it and tell me (safest)", "Keep the newest version", "Keep the reference folder's version"]
STATE_FILE = engine.APP_DIR / "sync_state.json"
CACHE_FILE = engine.APP_DIR / "hashcache.json"
HEAVY_DIRS = {"node_modules", "$recycle.bin", "system volume information"}
LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def K(rel: str) -> str:
    """Key used to match the same file in different folders (case-insensitive on Windows)."""
    return rel.lower() if os.name == "nt" else rel


# --------------------------------------------------------------------------
# Hash cache: re-reading big files is skipped when size and modified time are unchanged
# --------------------------------------------------------------------------
class HashCache:
    _lock = threading.Lock()
    _data: Optional[dict] = None
    _dirty = False

    @classmethod
    def load(cls) -> dict:
        if cls._data is None:
            try:
                cls._data = json.loads(CACHE_FILE.read_text("utf-8"))
            except Exception:
                cls._data = {}
        return cls._data

    @classmethod
    def get(cls, p: Path, size: int, mtime: float) -> str:
        key = f"{norm(p)}|{size}|{int(mtime * 1000)}"
        with cls._lock:
            data = cls.load()
            if key in data:
                return data[key]
        h = _file_hash(p)
        with cls._lock:
            cls.load()[key] = h
            cls._dirty = True
        return h

    @classmethod
    def flush(cls) -> None:
        with cls._lock:
            if not cls._dirty or cls._data is None:
                return
            if len(cls._data) > 150_000:
                cls._data = dict(list(cls._data.items())[-100_000:])
            try:
                engine.APP_DIR.mkdir(parents=True, exist_ok=True)
                tmp = CACHE_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(cls._data), "utf-8")
                os.replace(tmp, CACHE_FILE)
                cls._dirty = False
            except OSError:
                pass


# --------------------------------------------------------------------------
# One folder in a group
# --------------------------------------------------------------------------
class Member:
    def __init__(self, path: Path, label: str, skip: set, notes: list) -> None:
        self.path, self.label = path, label
        self.files: dict = {}          # key -> (size, mtime, original relative path)
        self.dirs: dict = {}           # key -> original relative path
        self._hash: dict = {}
        self.unreadable = False
        self._scan(skip, notes)

    def _scan(self, skip: set, notes: list) -> None:
        stack = [(self.path, "")]
        while stack:
            check_cancel()
            d, prefix = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError as e:
                notes.append(f"Couldn't open {d}: {e.strerror or e}")
                if d == self.path:
                    self.unreadable = True
                continue
            for e in entries:
                name = e.name
                low = name.lower()
                if low in skip or low in SKIP_NAMES or name.startswith("~$") or name.endswith(".fftmp"):
                    continue
                rel = prefix + name
                try:
                    if e.is_symlink():
                        notes.append(f"Skipped a link (not followed): {self.path.name}/{rel}")
                    elif e.is_dir(follow_symlinks=False):
                        self.dirs[K(rel)] = rel
                        stack.append((Path(e.path), rel + "/"))
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        self.files[K(rel)] = (st.st_size, st.st_mtime, rel)
                except OSError as ex:
                    notes.append(f"Couldn't read {self.path.name}/{rel}: {ex.strerror or ex}")

    def h(self, key: str) -> Optional[str]:
        """Content hash of a file (None if it is missing or unreadable)."""
        if key not in self.files:
            return None
        if key not in self._hash:
            size, mtime, rel = self.files[key]
            try:
                self._hash[key] = HashCache.get(self.path / rel, size, mtime)
            except OSError:
                self._hash[key] = "unreadable:" + self.label + key
        return self._hash[key]

    def abs(self, key: str, orig: Optional[str] = None) -> Path:
        rel = self.files[key][2] if key in self.files else (self.dirs.get(key) or orig)
        return self.path / rel

    def describe(self, key: str) -> str:
        if key not in self.files:
            return f"{self.label}: missing"
        size, mtime, _ = self.files[key]
        return f"{self.label}: {size_text(size)}, changed {datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M}"


# --------------------------------------------------------------------------
# Finding same-named folders
# --------------------------------------------------------------------------
def parse_paths(text: str) -> list:
    out = []
    for line in str(text or "").replace(";", "\n").splitlines():
        line = line.strip().strip('"')
        if line:
            out.append(line)
    return out


def find_groups(references: list, roots: list, skip: set, notes: list) -> list:
    """Return [(folder name, [paths...])] - the reference folder first, then every same-named folder found."""
    groups: dict = {}
    order: list = []
    for ref in references:
        name = ref.name.lower()
        if name not in groups:
            groups[name] = [ref]
            order.append((name, ref.name))
        elif norm(ref) not in {norm(p) for p in groups[name]}:
            groups[name].append(ref)
    app = engine.APP_DIR.resolve()
    for root in roots:
        if root.name.lower() in groups and norm(root) not in {norm(x) for x in groups[root.name.lower()]}:
            groups[root.name.lower()].append(root)            # a search place that is itself a match
            continue
        stack = [root]
        while stack:
            check_cancel()
            d = stack.pop()
            try:
                subs = sorted((e for e in os.scandir(d) if e.is_dir(follow_symlinks=False)), key=lambda e: e.name)
            except OSError as e:
                notes.append(f"Couldn't search inside {d}: {getattr(e, 'strerror', None) or e}")
                continue
            for e in subs:
                low = e.name.lower()
                if low in skip or low in HEAVY_DIRS:
                    continue
                p = Path(e.path)
                if p == app:
                    continue
                if low in groups:
                    if norm(p) not in {norm(x) for x in groups[low]}:
                        groups[low].append(p)
                    continue                                    # never descend into a match (avoids nesting)
                stack.append(p)
    result = []
    for low, disp in order:
        members, seen = [], []
        for p in groups[low]:
            rp = p.resolve()
            if any(rp == s or s in rp.parents or rp in s.parents for s in seen):
                notes.append(f"Left out {p}: it is the same as, or inside, another matching folder.")
                continue
            seen.append(rp)
            members.append(p)
        result.append((disp, members))
    return result


# --------------------------------------------------------------------------
# Comparing
# --------------------------------------------------------------------------
def verdict(members: list) -> str:
    union = set()
    for m in members:
        union |= set(m.files)
    if not union:
        return "All empty"
    same = 0
    for k in union:
        check_cancel()
        pres = [m for m in members if k in m.files]
        if len(pres) == len(members) and len({m.files[k][0] for m in pres}) == 1 and len({m.h(k) for m in pres}) == 1:
            same += 1
    dirs_same = len({frozenset(m.dirs) for m in members}) == 1
    if same == len(union) and dirs_same:
        return "Identical (verified by contents)"
    if same == 0:
        return "SAME NAME ONLY - these folders share no identical files"
    return f"Similar - {same} of {len(union)} files are identical"


def differences(members: list, limit: int = 400) -> list:
    """Plain-English list of what differs between the members of a group."""
    lines = []
    union = set()
    for m in members:
        union |= set(m.files)
    for k in sorted(union):
        check_cancel()
        pres = [m for m in members if k in m.files]
        orig = pres[0].files[k][2]
        if len(pres) < len(members):
            lines.append(f"Only in {', '.join(m.label for m in pres)}:  {orig}")
        elif len({m.files[k][0] for m in pres}) > 1 or len({m.h(k) for m in pres}) > 1:
            newest = max(pres, key=lambda m: m.files[k][1])
            lines.append(f"Different:  {orig}   (newest in {newest.label};  " +
                         "; ".join(m.describe(k) for m in pres) + ")")
    dkeys = set()
    for m in members:
        dkeys |= set(m.dirs)
    for k in sorted(dkeys):
        have = [m for m in members if k in m.dirs]
        if len(have) < len(members):
            lines.append(f"Folder only in {', '.join(m.label for m in have)}:  {have[0].dirs[k]}/")
    if len(lines) > limit:
        lines = lines[:limit] + [f"... and {len(lines) - limit} more differences"]
    return lines


# --------------------------------------------------------------------------
# Sync state: what the folders looked like after the last successful sync
# --------------------------------------------------------------------------
def group_key(paths: list) -> str:
    return hashlib.sha1("|".join(sorted(norm(Path(p).resolve()) for p in paths)).encode()).hexdigest()[:16]


def load_state() -> dict:
    try:
        d = json.loads(STATE_FILE.read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(key: str, base: dict, paths: list) -> None:
    st = load_state()
    st[key] = {"files": base, "paths": [str(p) for p in paths], "time": datetime.now().isoformat(timespec="seconds")}
    engine.APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st), "utf-8")
    os.replace(tmp, STATE_FILE)


def snapshot(paths: list, skip: set, old_base: dict) -> dict:
    """Rebuild the 'last agreed' record: files that are now identical everywhere."""
    notes: list = []
    members = [Member(Path(p), LABELS[i % 26], skip, notes) for i, p in enumerate(paths)]
    base = {}
    union = set()
    for m in members:
        union |= set(m.files)
    for k in union:
        hs = [m.h(k) for m in members]
        if None not in hs and len(set(hs)) == 1:
            base[k] = hs[0]
        elif k in old_base and any(h is not None for h in hs):
            base[k] = old_base[k]
    HashCache.flush()
    return base


# --------------------------------------------------------------------------
# Building the list of changes
# --------------------------------------------------------------------------
@dataclass
class Change:
    kind: str                       # mkdir | copy | update | move | trash
    dst: Member
    key: str
    src: Optional[Member] = None
    new_key: Optional[str] = None   # for move
    new_rel: Optional[str] = None   # for move: new name with its original capitalisation


@dataclass
class Analysis:
    changes: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)       # (key, [(member, hash)])
    skipped_deletes: list = field(default_factory=list)  # (key, [members that deleted it])


def _pick_winner(pres: list, key: str, rule: str, reference: Member) -> Optional[Member]:
    if rule == CONFLICT_RULES[1]:
        return max(pres, key=lambda m: m.files[key][1])
    if rule == CONFLICT_RULES[2] and reference in pres:
        return reference
    return None


def analyze(members: list, mode: str, base: dict, deletes: bool, rule: str) -> Analysis:
    out = Analysis()
    ref = members[0]
    two_way = mode == SYNC_MODES[1]
    handled: set = set()                                   # (member label, key) already dealt with

    def add(kind, dst, key, src=None, new_key=None, new_rel=None):
        out.changes.append(Change(kind, dst, key, src, new_key, new_rel))

    def push(winner: Member, key: str, targets: list) -> None:
        for t in targets:
            add("update" if key in t.files else "copy", t, key, winner)

    # ---- renames -----------------------------------------------------------
    if two_way:
        for i in members:
            gone = {k: base[k] for k in base if k not in i.files and K(k) == k}
            new = [k for k in i.files if k not in base]
            by_hash: dict = {}
            for k in new:
                by_hash.setdefault(i.h(k), []).append(k)
            for old, bh in gone.items():
                cands = by_hash.get(bh, [])
                if len(cands) != 1 or sum(1 for v in gone.values() if v == bh) != 1:
                    continue
                nk = cands[0]
                others = [m for m in members if m is not i]
                if all(old in m.files and m.h(old) == bh and nk not in m.files for m in others):
                    for m in others:
                        add("move", m, old, new_key=nk, new_rel=i.files[nk][2])
                    handled |= {old, nk}
    else:
        for d in members[1:]:
            s_only = [k for k in ref.files if k not in d.files]
            d_only = [k for k in d.files if k not in ref.files]
            if s_only and d_only:
                sizes: dict = {}
                for k in d_only:
                    sizes.setdefault(d.files[k][0], []).append(k)
                sh: dict = {}
                for k in s_only:
                    if ref.files[k][0] in sizes:
                        sh.setdefault(ref.h(k), []).append(k)
                dh: dict = {}
                for ks in sizes.values():
                    for k in ks:
                        dh.setdefault(d.h(k), []).append(k)
                for h, sks in sh.items():
                    if len(sks) == 1 and len(dh.get(h, [])) == 1:
                        add("move", d, dh[h][0], new_key=sks[0], new_rel=ref.files[sks[0]][2])
                        handled |= {(d.label, dh[h][0]), (d.label, sks[0])}

    # ---- folders ------------------------------------------------------------
    dkeys: set = set()
    for m in members:
        dkeys |= set(m.dirs)
    for k in sorted(dkeys, key=lambda x: x.count("/")):
        src = next(m for m in members if k in m.dirs)
        for m in members:
            if k not in m.dirs and (two_way or m is not ref):
                if not two_way and src is not ref:
                    continue
                add("mkdir", m, k, src)

    # ---- files --------------------------------------------------------------
    if two_way:
        union: set = set()
        for m in members:
            union |= set(m.files)
        for k in sorted(union):
            check_cancel()
            if k in handled:
                continue
            cur = {m: m.h(k) for m in members}
            present = {m: h for m, h in cur.items() if h is not None}
            if len(set(cur.values())) == 1:
                continue
            b = base.get(k)
            if b is None:
                if len(set(present.values())) == 1:
                    winner = next(iter(present))
                    push(winner, k, [m for m in members if m not in present])
                    continue
                conflict = True
            else:
                changed = {m: h for m, h in cur.items() if h != b}
                states = set(changed.values())
                if not changed:
                    continue
                if len(states) == 1:
                    state = next(iter(states))
                    if state is None:
                        keepers = [m for m in members if m not in changed and k in m.files]
                        if deletes:
                            for m in keepers:
                                add("trash", m, k)
                        elif keepers:
                            out.skipped_deletes.append((k, list(changed)))
                    else:
                        winner = next(iter(changed))
                        push(winner, k, [m for m in members if cur[m] != state])
                    continue
                conflict = True
            if conflict:
                win = _pick_winner(list(present), k, rule, ref) if present else None
                if win is None:
                    out.conflicts.append((k, list(cur.items())))
                else:
                    push(win, k, [m for m in members if cur[m] != cur[win]])
    else:
        for d in members[1:]:
            for k in sorted(ref.files):
                check_cancel()
                if (d.label, k) in handled:
                    continue
                sh, dh = ref.h(k), d.h(k)
                if sh == dh:
                    continue
                if dh is None:
                    add("copy", d, k, ref)
                    continue
                b = base.get(k)
                if b is not None and sh != b and dh != b:         # both sides changed since the last sync
                    win = _pick_winner([ref, d], k, rule, ref)
                    if win is None:
                        out.conflicts.append((k, [(ref, sh), (d, dh)]))
                    elif win is ref:
                        add("update", d, k, ref)
                    continue
                add("update", d, k, ref)
            for k in sorted(d.files):
                if k not in ref.files and (d.label, k) not in handled:
                    if deletes:
                        add("trash", d, k)
                    else:
                        out.skipped_deletes.append((k, [ref]))
    return out


def to_actions(an: Analysis, folder_label: Callable) -> list:
    order = {"mkdir": 0, "move": 1, "copy": 2, "update": 3, "trash": 4}
    acts = []
    for c in sorted(an.changes, key=lambda c: (order[c.kind], c.key)):
        d = c.dst
        if c.kind == "mkdir":
            acts.append(Action("mkdir", f"[{d.label}] create folder:  {c.src.dirs[c.key]}/",
                               dst=d.path / c.src.dirs[c.key]))
        elif c.kind == "copy":
            rel = c.src.files[c.key][2]
            acts.append(Action("copy", f"[{c.src.label} → {d.label}] new file:  {rel}  ({size_text(c.src.files[c.key][0])})",
                               c.src.abs(c.key), d.path / rel))
        elif c.kind == "update":
            rel = d.files[c.key][2]
            acts.append(Action("overwrite", f"[{c.src.label} → {d.label}] replace with newer/chosen version:  {rel}   (old copy is backed up)",
                               c.src.abs(c.key), d.abs(c.key)))
        elif c.kind == "move":
            old = d.files[c.key][2]
            acts.append(Action("move", f"[{d.label}] rename:  {old}  →  {c.new_rel}", d.abs(c.key), d.path / c.new_rel))
        elif c.kind == "trash":
            acts.append(Action("trash", f"[{d.label}] remove (to Recycle):  {d.files[c.key][2]}", d.abs(c.key)))
    return acts


class SyncPlanner:
    """Builds a Plan for one or several groups of same-named folders."""

    def __init__(self, v: dict) -> None:
        self.v = v
        self.skip = {s.strip().lower() for s in str(v.get("skip", "")).replace(";", ",").split(",") if s.strip()}

    def references(self) -> list:
        refs = [check_folder(self.v["folder"])]
        for p in parse_paths(self.v.get("more_refs", "")):
            refs.append(check_folder(p))
        return refs

    def roots(self) -> list:
        roots = [check_folder(p) for p in parse_paths(self.v.get("roots", ""))]
        if not roots:
            raise UserError("Please add at least one place to search for same-named folders "
                            "(use “Add a folder...” under “Look for same-named folders in”).")
        return roots

    def groups(self, notes: list) -> list:
        return find_groups(self.references(), self.roots(), self.skip, notes)

    def build(self, compare_only: bool) -> Plan:
        plan = Plan("Compare folders" if compare_only else "Synchronize folders")
        groups = self.groups(plan.notes)
        total = {"mkdir": 0, "copy": 0, "update": 0, "move": 0, "trash": 0}
        nconf = nskip = 0
        two_way = self.v.get("mode", SYNC_MODES[0]) == SYNC_MODES[1]
        any_group = False
        for name, paths in groups:
            if len(paths) < 2:
                plan.report.append(f"■ “{name}”: no other folder with this name was found.")
                continue
            any_group = True
            members = [Member(p, LABELS[i % 26], self.skip, plan.notes) for i, p in enumerate(paths)]
            if any(m.unreadable for m in members):
                plan.report.append(f"■ “{name}”: skipped because one of the folders can't be opened.")
                continue
            plan.report.append(f"■ “{name}” - found in {len(members)} places:   {verdict(members)}")
            for m in members:
                tag = "  (reference)" if m is members[0] else ""
                plan.report.append(f"     {m.label} = {m.path}{tag}   [{len(m.files)} files]")
            if compare_only:
                diffs = differences(members)
                plan.report += [f"     {x}" for x in diffs] or ["     No differences."]
                plan.report.append("")
                continue
            key = group_key(paths)
            base = load_state().get(key, {}).get("files", {})
            an = analyze(members, self.v["mode"], base, bool(self.v["deletes"]), self.v["conflicts"])
            acts = to_actions(an, None)
            for c in an.changes:
                total[c.kind] += 1
            plan.actions += acts
            nconf += len(an.conflicts)
            nskip += len(an.skipped_deletes)
            for k, states in an.conflicts:
                orig = next((m.files[k][2] for m, _ in states if k in m.files), k)
                plan.report.append(f"     CONFLICT (skipped):  {orig}   -   " + "; ".join(m.describe(k) for m, _ in states))
            for k, who in an.skipped_deletes[:50]:
                orig = next((m.files[k][2] for m in members if k in m.files), k)
                plan.report.append(f"     Not removed (deletions are off):  {orig}")
            plan.report.append("")
            plan.after = self._remember(plan.after, key, paths)
        plan.selectable = bool(self.v.get("review", True)) and not compare_only
        HashCache.flush()
        if compare_only:
            plan.summary = ("Compared the folders. Nothing was changed." if any_group
                            else "No other folders with the same names were found.")
            return plan
        n = len(plan.actions)
        if not any_group:
            plan.summary = "No other folders with the same names were found."
        elif not n:
            plan.summary = "Everything is already in step - nothing to do." + (f"  ({plural(nconf, 'conflict')} need your decision.)" if nconf else "")
        else:
            parts = [f"{total['copy']} new", f"{total['update']} replaced", f"{total['move']} renamed",
                     f"{total['mkdir']} folders", f"{total['trash']} removed"]
            plan.summary = (f"{plural(n, 'change')} will be made ({', '.join(p for p in parts if not p.startswith('0 '))})."
                            + (f"  {plural(nconf, 'conflict')} skipped." if nconf else ""))
        if n:
            plan.notes.append("Replaced files are backed up to the Recycle folder first, so “Undo last change” restores them.")
        return plan

    def _remember(self, prev, key, paths):
        skip = self.skip

        def after(result, prev=prev):
            if prev:
                prev(result)
            save_state(key, snapshot(paths, skip, load_state().get(key, {}).get("files", {})), paths)
        return after


FOLDER_FIELDS = [
    Field("folder", "Reference folder", "folder", help="The folder you want to match against, or the one you changed."),
    Field("more_refs", "More reference folders (optional)", "folderlist", "", height=2,
          help="Other folder names you also want to keep in step."),
    Field("roots", "Look for same-named folders in", "folderlist", "", height=3,
          help="The places to search, for example C:/portfolio and C:/apps. Folders with the same name are found automatically."),
]


class CompareFolders(Recipe):
    key = "compare"
    name = "Compare same-named folders"
    blurb = ("Finds folders with the same name in different places and shows, by comparing contents, "
             "whether they are really the same. Nothing is changed.")
    fields = FOLDER_FIELDS + [Field("skip", "Ignore folders called", "text", ".git, node_modules")]

    def plan(self, v):
        return SyncPlanner(v).build(compare_only=True)


class SyncFolders(Recipe):
    key = "sync"
    name = "Sync same-named folders"
    blurb = ("Keeps same-named folders in step. Every group is verified by contents first, replaced files are backed up, "
             "deletions are off unless you allow them, and conflicts are never decided silently.")
    fields = FOLDER_FIELDS + [
        Field("mode", "How should they sync?", "choice", SYNC_MODES[0], SYNC_MODES,
              help="One-way: the reference folder is the source of truth. Two-way: changes made in any place spread to the others."),
        Field("conflicts", "If two places changed the same file", "choice", CONFLICT_RULES[0], CONFLICT_RULES),
        Field("deletes", "Also remove files that were deleted elsewhere", "check", False,
              help="Off by default. When on, removed files go to the Recycle folder, never gone for good."),
        Field("review", "Let me approve each change one by one", "check", True),
        Field("skip", "Ignore folders called", "text", ".git, node_modules"),
    ]

    def plan(self, v):
        return SyncPlanner(v).build(compare_only=False)


# --------------------------------------------------------------------------
# Continuous sync: check on a timer, then apply automatically or wait for approval
# --------------------------------------------------------------------------
WATCH_INTERVALS = {"Every 10 seconds": 10, "Every 30 seconds": 30, "Every minute": 60, "Every 5 minutes": 300}
WATCH_ACTION = ["Ask me before applying changes", "Apply changes automatically"]


class SyncWatcher:
    """Polls the folders (no extra software needed) and keeps them in step.

    Loop-proof: after every apply the 'last agreed' snapshot is rewritten, so the next check sees nothing to do.
    Files modified in the last few seconds are left for the next round so half-written files are never copied.
    """

    def __init__(self, values: dict, interval: float, auto: bool,
                 log: Callable[[str], None], on_pending: Callable, settle: float = 2.0) -> None:
        self.v = dict(values, review=False)
        self.interval, self.auto, self.log, self.on_pending, self.settle = interval, auto, log, on_pending, settle
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending_sig = None
        self._seen_conflicts: set = set()
        self.pending: Optional[Plan] = None
        self.cycles = 0

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _unsettled(self, a: Action, now: float) -> bool:
        try:
            return a.src is not None and a.kind in ("copy", "overwrite") and now - a.src.stat().st_mtime < self.settle
        except OSError:
            return True

    def check_once(self) -> Optional[Plan]:
        """One round.  Returns the plan that was applied or is waiting (None if nothing to do)."""
        with self.lock:
            plan = SyncPlanner(self.v).build(compare_only=False)
            self.cycles += 1
            conflicts = {ln.strip() for ln in plan.report if "CONFLICT" in ln}
            for c in sorted(conflicts - self._seen_conflicts):
                self.log("Needs your decision - " + c)
            self._seen_conflicts = conflicts
            now = time.time()
            plan.actions = [a for a in plan.actions if not self._unsettled(a, now)]
            if not plan.actions:
                self._pending_sig, self.pending = None, None
                return None
            if self.auto:
                self.apply(plan)
                return plan
            sig = tuple(sorted(a.sentence for a in plan.actions))
            self.pending = plan
            if sig != self._pending_sig:
                self._pending_sig = sig
                self.log(f"{plural(len(plan.actions), 'change')} waiting for your approval.")
                self.on_pending(plan)
            return plan

    def apply(self, plan: Plan) -> dict:
        with self.lock:
            res = engine.apply_plan(plan)
            self.pending, self._pending_sig = None, None
            self.log(f"Applied {plural(res['done'], 'change')}." + (f"  {len(res['problems'])} problem(s): " + "; ".join(res["problems"][:3])
                                                                       if res["problems"] else ""))
            return res

    def _run(self) -> None:
        self.log("Started watching.")
        while not self._stop.is_set():
            try:
                self.check_once()
            except engine.Cancelled:
                engine.reset_cancel()
            except UserError as e:
                self.log(f"Paused this round: {e}")
            except Exception as e:                      # keep watching whatever happens
                self.log(f"Problem during check: {e}")
            self._stop.wait(self.interval)
        self.log("Stopped watching.")


class WatchFolders(SyncFolders):
    key = "watch"
    name = "Watch and sync automatically"
    blurb = ("Keeps same-named folders in step as you work. FileFixer checks on a timer, then either asks you "
             "before applying changes or applies them automatically. Conflicts are never decided silently, and nothing "
             "is deleted unless you allow it. Works while FileFixer stays open.")
    live = True
    fields = FOLDER_FIELDS + [
        Field("mode", "How should they sync?", "choice", SYNC_MODES[1], SYNC_MODES),
        Field("conflicts", "If two places changed the same file", "choice", CONFLICT_RULES[0], CONFLICT_RULES),
        Field("deletes", "Also remove files that were deleted elsewhere", "check", False,
              help="Off by default. Removed files go to the Recycle folder."),
        Field("interval", "Check", "choice", "Every 30 seconds", list(WATCH_INTERVALS)),
        Field("auto", "When changes are found", "choice", WATCH_ACTION[0], WATCH_ACTION),
        Field("skip", "Ignore folders called", "text", ".git, node_modules"),
    ]

    def plan(self, v):
        raise UserError("Use the “Start watching” button for this job.")
