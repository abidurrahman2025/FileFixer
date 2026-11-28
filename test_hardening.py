"""Regression tests for the hardening pass.  Everything runs in temporary folders.  Run:  python test_hardening.py"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["FILEFIXER_HOME"] = tempfile.mkdtemp(prefix="ff_home_h_")

import engine                      # noqa: E402
import r_sync as S                 # noqa: E402
import r_more                      # noqa: E402
import recipes                     # noqa: E402
import registry                    # noqa: E402
import workflow as W               # noqa: E402
from engine import Action, Plan, UserError     # noqa: E402

TWO = S.SYNC_MODES[1]
NEWEST = S.CONFLICT_RULES[1]


def vals(recipe, **over):
    v = {f.key: f.default for f in recipe.fields}
    v.update(over)
    return v


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ff_hard_")).resolve()
        engine.save_history([])
        engine.reset_cancel()

    def put(self, folder, rel, data="x", mtime=None):
        p = Path(folder) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def names(self, folder=None):
        folder = folder or self.tmp
        return sorted(str(p.relative_to(folder)).replace(os.sep, "/") for p in Path(folder).rglob("*") if p.is_file())

    def job(self, key, **over):
        r = registry.get(key)
        return r.plan(vals(r, **over))


class SyncBase(Base):
    def setUp(self):
        super().setUp()
        self.a = self.tmp / "portfolio" / "folder1"
        self.b = self.tmp / "apps" / "folder1"
        self.c = self.tmp / "backup" / "folder1"
        for p in (self.a, self.b, self.c):
            p.mkdir(parents=True)

    def plan(self, key="sync", **over):
        base = dict(folder=str(self.a), roots=f"{self.tmp/'apps'}\n{self.tmp/'backup'}", review=False)
        base.update(over)
        return self.job(key, **base)

    def go(self, **over):
        plan = self.plan(**over)
        res = engine.apply_plan(plan)
        self.assertEqual(res["problems"], [])
        return plan, res

    def all3(self, rel, data, mtime=1_000_000_000):
        for f in (self.a, self.b, self.c):
            self.put(f, rel, data, mtime)


# ==========================================================================
class TestTwoWayRenames(SyncBase):
    def test_a_rename_never_hides_other_changes_in_the_same_sync(self):
        for rel in ("r.txt", "u.txt", "c.txt"):
            self.all3(rel, "v1 " + rel)
        self.go(mode=TWO)                                            # records what everybody agrees on
        (self.a / "r.txt").rename(self.a / "r2.txt")                 # a rename in A ...
        self.put(self.b, "u.txt", "edited in B", 1_500_000_000)      # ... an edit in B ...
        self.put(self.c, "n.txt", "new in C")                        # ... a new file in C ...
        self.put(self.a, "c.txt", "A side", 1_400_000_000)           # ... and a conflict
        self.put(self.b, "c.txt", "B side", 1_400_000_100)
        plan = self.plan(mode=TWO)
        self.assertTrue(any("CONFLICT" in x for x in plan.report))
        engine.apply_plan(plan)
        for f in (self.a, self.b, self.c):
            self.assertTrue((f / "r2.txt").exists(), f)
            self.assertFalse((f / "r.txt").exists(), f)
            self.assertEqual((f / "u.txt").read_text(), "edited in B")
            self.assertEqual((f / "n.txt").read_text(), "new in C")
        self.assertEqual((self.a / "c.txt").read_text(), "A side")   # the conflict was left alone
        self.assertEqual((self.b / "c.txt").read_text(), "B side")

    def test_rename_onto_a_name_someone_else_has_is_not_treated_as_a_rename(self):
        self.all3("x.txt", "same content for x")
        self.go(mode=TWO)
        (self.a / "x.txt").rename(self.a / "y.txt")
        self.put(self.b, "y.txt", "B has its own y")                 # the new name is already taken in B
        plan = self.plan(mode=TWO)
        self.assertFalse(any(a.kind == "move" for a in plan.actions))
        engine.apply_plan(plan)
        self.assertEqual((self.b / "y.txt").read_text(), "B has its own y")     # nothing lost
        self.assertEqual((self.a / "y.txt").read_text(), "same content for x")

    def test_one_way_rename_with_identical_files_elsewhere(self):
        self.put(self.a, "new name.txt", "same text"); self.put(self.a, "other.txt", "other")
        self.put(self.b, "old name.txt", "same text"); self.put(self.b, "other.txt", "other")
        self.put(self.b, "keep.txt", "same text and more")
        plan = self.plan(roots=str(self.tmp / "apps"))
        self.assertEqual(sorted(a.kind for a in plan.actions), ["move"])
        engine.apply_plan(plan)
        self.assertEqual(self.names(self.b), ["keep.txt", "new name.txt", "other.txt"])


class TestMultiFolderConflicts(SyncBase):
    def edit(self, folder, data, mtime):
        self.put(folder, "doc.txt", data, mtime)

    def test_an_unchanged_folder_never_wins_just_because_its_timestamp_is_newer(self):
        self.all3("doc.txt", "v1")
        self.go(mode=TWO)
        self.edit(self.a, "A2", 1_500_000_000)
        self.edit(self.b, "B2", 1_400_000_000)
        os.utime(self.c / "doc.txt", (2_000_000_000, 2_000_000_000))       # C is untouched but looks newest
        self.go(mode=TWO, conflicts=NEWEST)
        for f in (self.a, self.b, self.c):
            self.assertEqual((f / "doc.txt").read_text(), "A2", f)

    def test_times_within_two_seconds_are_a_conflict_not_a_guess(self):
        self.all3("doc.txt", "v1")
        self.go(mode=TWO)
        self.edit(self.a, "A2", 1_500_000_000)
        self.edit(self.b, "B2", 1_500_000_001)
        plan = self.plan(mode=TWO, conflicts=NEWEST)
        self.assertEqual(plan.actions, [])
        self.assertTrue(any("CONFLICT" in x for x in plan.report))
        self.assertEqual((self.c / "doc.txt").read_text(), "v1")

    def test_two_folders_with_the_same_new_text_count_as_one_version(self):
        self.all3("doc.txt", "v1")
        self.go(mode=TWO)
        self.edit(self.a, "same2", 1_500_000_000)
        self.edit(self.b, "same2", 1_500_000_000)                          # exact tie, but identical text
        self.edit(self.c, "C2", 1_500_000_100)                             # clearly the newest
        self.go(mode=TWO, conflicts=NEWEST)
        for f in (self.a, self.b, self.c):
            self.assertEqual((f / "doc.txt").read_text(), "C2")

    def test_reference_rule_needs_the_reference_to_have_changed(self):
        self.all3("doc.txt", "v1")
        self.go(mode=TWO)
        self.edit(self.b, "B2", 1_500_000_000)
        self.edit(self.c, "C2", 1_500_000_100)                             # A (the reference) is unchanged
        plan = self.plan(mode=TWO, conflicts=S.CONFLICT_RULES[2])
        self.assertEqual(plan.actions, [])
        self.assertEqual((self.b / "doc.txt").read_text(), "B2")

    def test_an_edit_beats_a_deletion(self):
        self.all3("doc.txt", "v1")
        self.go(mode=TWO)
        (self.a / "doc.txt").unlink()
        self.edit(self.b, "B2", 1_500_000_000)
        self.go(mode=TWO, conflicts=NEWEST, deletes=True)
        for f in (self.a, self.b, self.c):
            self.assertEqual((f / "doc.txt").read_text(), "B2")


class TestSyncSafetyNets(SyncBase):
    def test_a_wiped_folder_does_not_delete_everything_elsewhere(self):
        for i in range(3):
            self.all3(f"f{i}.txt", f"data {i}")
        self.go(mode=TWO)
        for p in self.a.iterdir():
            p.unlink()                                                    # A is emptied (or its drive is unplugged)
        plan = self.plan(mode=TWO, deletes=True)
        self.assertFalse([a for a in plan.actions if a.kind == "trash"])
        self.assertTrue(any("Deletions were switched off" in x for x in plan.report))
        engine.apply_plan(plan)
        self.assertEqual(len(self.names(self.b)), 3)

    def test_an_empty_reference_does_not_clear_the_others_in_one_way_mode(self):
        self.put(self.b, "a.txt", "1"); self.put(self.b, "b.txt", "2")
        plan = self.plan(roots=str(self.tmp / "apps"), deletes=True)
        self.assertEqual(plan.actions, [])
        self.assertEqual(len(self.names(self.b)), 2)

    def test_a_single_deleted_file_still_propagates(self):
        for i in range(3):
            self.all3(f"f{i}.txt", f"data {i}")
        self.go(mode=TWO)
        (self.a / "f0.txt").unlink()
        self.go(mode=TWO, deletes=True)
        self.assertFalse((self.b / "f0.txt").exists())
        self.assertEqual(len(self.names(self.b)), 2)

    def test_folders_that_start_identical_remember_it_so_later_deletions_are_seen(self):
        for i in range(2):
            self.put(self.a, f"f{i}.txt", f"d{i}", 1_000_000_000)
            self.put(self.b, f"f{i}.txt", f"d{i}", 1_000_000_000)
        plan = self.plan(mode=TWO, roots=str(self.tmp / "apps"))
        self.assertEqual(plan.actions, [])                                # nothing to do, yet the baseline is saved
        (self.a / "f0.txt").unlink()
        self.go(mode=TWO, roots=str(self.tmp / "apps"), deletes=True)
        self.assertFalse((self.b / "f0.txt").exists())                    # a deletion, not a "new file" to copy back
        self.assertFalse((self.a / "f0.txt").exists())

    def test_files_of_different_sizes_are_not_read_at_all(self):
        self.put(self.a, "big.txt", "aaaaaaaa"); self.put(self.b, "big.txt", "bb")
        with mock.patch.object(S.HashCache, "get", side_effect=AssertionError("hashed")):
            plan = self.plan(roots=str(self.tmp / "apps"))
        self.assertEqual([a.kind for a in plan.actions], ["overwrite"])
        with mock.patch.object(S.HashCache, "get", side_effect=AssertionError("hashed")):
            plan = self.plan(mode=TWO, roots=str(self.tmp / "apps"))
        self.assertEqual(plan.actions, [])
        self.assertTrue(any("CONFLICT" in x for x in plan.report))

    def test_a_stopped_run_does_not_record_a_baseline(self):
        self.put(self.a, "one.txt", "1"); self.put(self.a, "two.txt", "2")
        plan = self.plan(mode=TWO, roots=str(self.tmp / "apps"))
        engine.request_cancel()
        try:
            res = engine.apply_plan(plan)
        finally:
            engine.reset_cancel()
        self.assertEqual(res["done"], 0)
        self.assertNotIn(S.group_key([self.a, self.b]), S.load_state())
        self.go(mode=TWO, roots=str(self.tmp / "apps"))                   # a fresh run finishes the job
        self.assertEqual(self.names(self.b), ["one.txt", "two.txt"])


class TestWatcherState(SyncBase):
    def watcher(self, auto=True):
        v = vals(registry.get("watch"), folder=str(self.a), roots=str(self.tmp / "apps"))
        self.logs, self.pend = [], []
        return S.SyncWatcher(v, 1, auto, self.logs.append, self.pend.append, settle=0)

    def test_the_baseline_survives_and_repeat_rounds_do_nothing(self):
        self.put(self.a, "one.txt", "1"); self.put(self.b, "two.txt", "2")
        w = self.watcher()
        w.check_once()
        self.assertTrue(S.load_state())
        before = json.dumps(S.load_state(), sort_keys=True)
        for _ in range(3):
            self.assertIsNone(w.check_once())
        self.assertEqual(json.dumps(S.load_state(), sort_keys=True), before)

    def test_search_places_are_not_walked_again_every_round(self):
        self.put(self.a, "one.txt", "1"); self.put(self.b, "one.txt", "1")
        w = self.watcher()
        with mock.patch.object(S, "find_groups", wraps=S.find_groups) as spy:
            for _ in range(4):
                w.check_once()
            self.assertEqual(spy.call_count, 1)
            w._groups_time = 0                                            # long enough ago: look again
            w.check_once()
            self.assertEqual(spy.call_count, 2)

    def test_a_new_file_is_still_noticed_with_cached_groups(self):
        self.put(self.a, "one.txt", "1"); self.put(self.b, "one.txt", "1")
        w = self.watcher()
        w.check_once()
        self.put(self.b, "late.txt", "late")
        w.check_once()
        self.assertEqual((self.a / "late.txt").read_text(), "late")

    def test_renames_and_removals_of_just_written_files_also_wait(self):
        w = S.SyncWatcher({}, 1, True, lambda t: None, lambda p: None, settle=60)
        fresh = self.put(self.tmp, "fresh.txt", "x")
        for kind in ("move", "trash", "copy", "overwrite"):
            self.assertTrue(w._unsettled(Action(kind, "s", fresh, self.tmp / "z.txt"), time.time()), kind)
        old = self.put(self.tmp, "old.txt", "x", 1_000_000_000)
        self.assertFalse(w._unsettled(Action("move", "s", old, self.tmp / "z.txt"), time.time()))

    def test_file_changed_after_the_preview_is_refused_during_watch(self):
        self.put(self.a, "doc.txt", "v1", 1_000_000_000); self.put(self.b, "doc.txt", "v1", 1_000_000_000)
        S_state = self.plan(mode=TWO, roots=str(self.tmp / "apps"))
        self.assertEqual(S_state.actions, [])
        self.put(self.a, "doc.txt", "v2 longer", 1_100_000_000)
        plan = self.plan(mode=TWO, roots=str(self.tmp / "apps"))
        self.assertEqual([a.kind for a in plan.actions], ["overwrite"])
        self.put(self.a, "doc.txt", "v3 even longer than before", 1_200_000_000)    # changes after the preview
        res = engine.apply_plan(plan)
        self.assertEqual(res["done"], 0)
        self.assertTrue(any("changed after the preview" in p for p in res["problems"]))
        self.assertEqual((self.b / "doc.txt").read_text(), "v1")


# ==========================================================================
class TestAutoGuard(Base):
    def test_a_renamed_file_that_was_edited_after_the_preview_is_left_alone(self):
        p = self.put(self.tmp, "a.txt", "one")
        plan = self.job("rename", folder=str(self.tmp), mode=recipes.R_MODES[0], text="new_")
        self.put(self.tmp, "a.txt", "one, then edited")
        res = engine.apply_plan(plan)
        self.assertEqual(res["done"], 0)
        self.assertTrue(p.exists())
        self.assertTrue(any("changed after the preview" in x for x in res["problems"]))

    def test_find_replace_refuses_a_file_changed_after_the_preview(self):
        self.put(self.tmp, "a.txt", "cat")
        plan = self.job("replace", folder=str(self.tmp), find="cat", replace="dog")
        self.put(self.tmp, "a.txt", "cat cat cat")
        res = engine.apply_plan(plan)
        self.assertEqual(res["done"], 0)
        self.assertEqual((self.tmp / "a.txt").read_text(), "cat cat cat")

    def test_a_duplicate_is_not_removed_if_the_copy_that_stays_changed(self):
        keep = self.put(self.tmp, "a.txt", "same", 1_000_000_000)
        dup = self.put(self.tmp, "b.txt", "same", 1_100_000_000)
        plan = self.job("duplicates", folder=str(self.tmp))
        self.assertEqual(len(plan.actions), 1)
        self.put(self.tmp, "a.txt", "now different")                      # the keeper changes
        res = engine.apply_plan(plan)
        self.assertEqual(res["done"], 0)
        self.assertTrue(dup.exists() and keep.exists())

    def test_unchanged_files_still_apply(self):
        self.put(self.tmp, "a.txt", "cat"); self.put(self.tmp, "b.txt", "bird")
        plan = self.job("replace", folder=str(self.tmp), find="cat", replace="dog")
        self.assertEqual(engine.apply_plan(plan)["problems"], [])
        self.assertEqual((self.tmp / "a.txt").read_text(), "dog")

    @unittest.skipIf(os.name == "nt", "needs symbolic links")
    def test_a_file_swapped_for_a_link_is_refused(self):
        real = self.put(self.tmp, "secret.txt", "private data")
        victim = self.put(self.tmp, "a.txt", "hello")
        plan = self.job("rename", folder=str(self.tmp), mode=recipes.R_MODES[0], text="x_",
                        only_name="a.txt")
        victim.unlink()
        victim.symlink_to(real)                                           # same name, now a link elsewhere
        res = engine.apply_plan(plan)
        self.assertEqual(res["done"], 0)
        self.assertTrue(victim.is_symlink())

    def test_system_folders_are_never_changed_even_if_a_plan_asks(self):
        sysdir = Path("/etc") if os.name != "nt" else Path(os.environ.get("SystemRoot", r"C:\Windows"))
        plan = Plan("x")
        plan.actions.append(Action("write", "w", dst=sysdir / "filefixer_should_not_exist.txt",
                                   produce=lambda: ("x", "utf-8")))
        res = engine.apply_plan(plan)
        self.assertEqual(res["done"], 0)
        self.assertTrue(res["problems"])
        self.assertFalse((sysdir / "filefixer_should_not_exist.txt").exists())

    def test_replacing_the_original_is_refused_up_front_for_system_files(self):
        if os.name == "nt":
            self.skipTest("uses a Linux system path")
        with self.assertRaises(UserError):
            engine.check_editable(Path("/etc/hosts"))


# ==========================================================================
class TestAtomicWrites(Base):
    def leftovers(self):
        return [p for p in self.tmp.rglob("*") if p.name.endswith(".fftmp") or p.name.endswith(".tmp")]

    def test_a_failing_write_leaves_neither_half_file_nor_temp_file(self):
        dst = self.tmp / "out.txt"
        plan = Plan("w")
        plan.actions.append(Action("write", "w", dst=dst, produce=lambda: ("hello", "utf-8")))
        with mock.patch("os.replace", side_effect=OSError(5, "disk error")):
            res = engine.apply_plan(plan)
        self.assertEqual(res["done"], 0)
        self.assertTrue(res["problems"])
        self.assertFalse(dst.exists())
        self.assertEqual(self.leftovers(), [])

    def test_a_write_never_replaces_a_file_that_appeared_in_the_meantime(self):
        dst = self.tmp / "out.txt"
        plan = Plan("w")

        def produce():
            dst.write_text("someone else's file")                         # appears while the text is being made
            return "mine", "utf-8"
        plan.actions.append(Action("write", "w", dst=dst, produce=produce))
        res = engine.apply_plan(plan)
        self.assertEqual(res["done"], 0)
        self.assertEqual(dst.read_text(), "someone else's file")

    def test_a_good_write_works_and_undoes(self):
        dst = self.tmp / "sub" / "out.txt"
        plan = Plan("w")
        plan.actions.append(Action("write", "w", dst=dst, produce=lambda: ("hello", "utf-8")))
        self.assertEqual(engine.apply_plan(plan)["problems"], [])
        self.assertEqual(dst.read_text(), "hello")
        self.assertEqual(self.leftovers(), [])
        engine.undo_last()
        self.assertFalse(dst.exists())

    def test_an_interrupted_restore_never_leaves_a_half_written_original(self):
        f = self.put(self.tmp, "a.txt", "original text")
        plan = self.job("replace", folder=str(self.tmp), find="original", replace="changed")
        self.assertEqual(engine.apply_plan(plan)["problems"], [])
        self.assertEqual(f.read_text(), "changed text")
        real_replace = os.replace

        def flaky(src, dst):
            if str(dst) == str(f):
                raise OSError(5, "disk error")
            return real_replace(src, dst)
        with mock.patch("os.replace", side_effect=flaky):
            out = engine.undo_last()
        self.assertTrue(out["problems"] and out["partial"])
        self.assertEqual(f.read_text(), "changed text")                    # untouched, not half restored
        self.assertEqual(self.leftovers(), [])
        out = engine.undo_last()                                          # the retry works once the disk is fine
        self.assertEqual(out["problems"], [])
        self.assertEqual(f.read_text(), "original text")

    def test_a_failed_case_only_rename_does_not_strand_the_file(self):
        f = self.put(self.tmp, "report.txt", "data")
        real_rename = os.rename
        calls = []

        def flaky(src, dst, *a, **k):
            calls.append((str(src), str(dst)))
            if len(calls) == 2:                                           # the second step of the two-step rename
                raise OSError(5, "disk error")
            return real_rename(src, dst, *a, **k)
        target = self.tmp / "REPORT.txt"
        with mock.patch.object(engine.os, "rename", side_effect=flaky), \
                mock.patch.object(engine, "norm", side_effect=lambda p: str(p).lower()):
            with self.assertRaises(OSError):
                engine._move(f, target)
        self.assertEqual(self.names(), ["report.txt"])                    # back where it started, no *.fftmp left

    def test_app_data_is_saved_atomically(self):
        p = self.tmp / "data" / "x.json"
        engine.write_text_atomic(p, "one")
        with mock.patch("os.replace", side_effect=OSError(5, "disk error")):
            with self.assertRaises(OSError):
                engine.write_text_atomic(p, "two")
        self.assertEqual(p.read_text(), "one")
        self.assertEqual([x.name for x in p.parent.iterdir()], ["x.json"])

    def test_saving_workflows_and_scripts_is_safe_and_validated(self):
        W.save_workflow("Mine", [{"op": "trim", "params": {}}])
        with mock.patch("os.replace", side_effect=OSError(5, "disk error")):
            with self.assertRaises(UserError):
                W.save_workflow("Mine", [{"op": "reverse", "params": {}}])
        self.assertEqual(W.load_workflow("Mine"), [{"op": "trim", "params": {}}])
        with self.assertRaises(UserError):
            W.save_workflow("CON", [])
        with self.assertRaises(UserError):
            W.save_script("bad.", "print(1)")
        self.assertEqual(Path(W.save_script("ok", "print(1)")).name, "ok.py")


class TestJournal(Base):
    def test_history_is_saved_while_a_big_job_runs_so_a_crash_can_still_be_undone(self):
        for i in range(4):
            self.put(self.tmp, f"f{i}.txt", "x")
        plan = self.job("rename", folder=str(self.tmp), mode=recipes.R_MODES[0], text="n_")
        seen = []

        def spy(done, total):
            if done == 3:
                seen.append(engine.load_history())
                raise KeyboardInterrupt()                                 # the "crash"
        engine.reset_cancel()
        with mock.patch.object(engine.time, "time", side_effect=lambda r=iter(range(0, 10 ** 6, 5)): next(r)):
            with self.assertRaises(KeyboardInterrupt):
                engine.apply_plan(plan, spy)
        self.assertTrue(seen and seen[0], "nothing was written to history before the crash")
        self.assertGreaterEqual(len(seen[0][-1]["records"]), 2)
        out = engine.undo_last()                                          # undo restores what was done
        self.assertEqual(out["problems"], [])
        self.assertEqual(self.names(), [f"f{i}.txt" for i in range(4)])


# ==========================================================================
class TestWalkWarnings(Base):
    def test_unreadable_places_are_reported_not_hidden(self):
        self.put(self.tmp, "ok/a.txt", "x")
        self.put(self.tmp, "locked/b.txt", "x")
        real = os.scandir

        def deny(path):
            if str(path).endswith("locked"):
                raise PermissionError(13, "Access is denied", str(path))
            return real(path)
        with mock.patch.object(r_more.os, "scandir", side_effect=deny):
            plan = self.job("find", folder=str(self.tmp))
            rep = self.job("report", folder=str(self.tmp))
        self.assertIn("1 file", plan.summary)
        self.assertTrue(any("could not be read" in n for n in plan.notes), plan.notes)
        self.assertTrue(any("locked" in n for n in plan.notes))
        self.assertTrue(any("could not be read" in n for n in rep.notes))

    def test_entry_errors_are_reported_too(self):
        self.put(self.tmp, "a.txt", "x")
        problems = []
        list(r_more._walk(self.tmp, True, problems=problems))
        self.assertEqual(problems, [])
        fake = mock.MagicMock()
        fake.name, fake.path = "weird.txt", str(self.tmp / "weird.txt")
        fake.is_symlink.side_effect = PermissionError(13, "denied")
        fake_ctx = mock.MagicMock()
        fake_ctx.__enter__.return_value = iter([fake])
        with mock.patch.object(r_more.os, "scandir", return_value=fake_ctx):
            list(r_more._walk(self.tmp, True, problems=problems))
        self.assertEqual(len(problems), 1)
        self.assertIn("weird.txt", problems[0])

    def test_folder_mode_extract_and_combine_warn(self):
        self.put(self.tmp, "a.csv", "x,y\n1,2\n"); self.put(self.tmp, "b.csv", "x,y\n3,4\n")
        self.put(self.tmp, "locked/c.csv", "x,y\n5,6\n"); self.put(self.tmp, "a.txt", "mail me bob@example.com")
        real = os.scandir

        def deny(path):
            if str(path).endswith("locked"):
                raise PermissionError(13, "Access is denied", str(path))
            return real(path)
        with mock.patch.object(r_more.os, "scandir", side_effect=deny):
            comb = self.job("combinecsv", folder=str(self.tmp), sub=True)
            ext = self.job("emails", file="", folder=str(self.tmp), sub=True)
        self.assertTrue(any("could not be read" in n for n in comb.notes))
        self.assertTrue(any("could not be read" in n for n in ext.notes))

    def test_a_folder_of_only_unreadable_files_says_so(self):
        self.put(self.tmp, "broken.docx", b"not a zip file")
        plan = self.job("emails", file="", folder=str(self.tmp))
        self.assertIn("None of the files", plan.summary)


# ==========================================================================
class TestFolderChoice(Base):
    def test_the_whole_user_folder_is_refused_but_folders_inside_it_are_fine(self):
        with self.assertRaises(UserError) as cm:
            engine.check_folder(str(Path.home()))
        self.assertIn("whole user folder", str(cm.exception))
        self.assertEqual(engine.check_folder(str(self.tmp)), self.tmp)


class TestCommonJobsWarn(Base):
    def test_every_folder_job_warns_about_folders_it_could_not_open(self):
        self.put(self.tmp, "ok.txt", "a"); self.put(self.tmp, "ok2.txt", "b")
        out = Path(tempfile.mkdtemp(prefix="ff_out_")).resolve()
        real_walk = os.walk

        def walk(top, onerror=None, **k):
            if onerror:
                onerror(PermissionError(13, "Access is denied", str(self.tmp / "locked")))
            return real_walk(top, onerror=onerror, **k)
        for key, over in (("rename", {"text": "x_"}), ("organize", {}), ("replace", {"find": "a"}), ("duplicates", {}),
                          ("merge", {}), ("package", {"outdir": str(out)})):
            with mock.patch.object(engine.os, "walk", side_effect=walk):
                plan = self.job(key, folder=str(self.tmp), sub=True, **over)
            self.assertTrue(any("could not be read" in n for n in plan.notes), key)
            self.assertTrue(any("locked" in n for n in plan.notes), key)

    def test_a_folder_that_cannot_be_listed_is_explained(self):
        self.put(self.tmp, "ok.txt", "a")
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(UserError) as cm:
                self.job("rename", folder=str(self.tmp), mode=recipes.R_MODES[0], text="x")
        self.assertIn("can't open this folder", str(cm.exception))


class TestNamesEverywhere(Base):
    def test_organize_skips_extensions_that_would_make_reserved_folder_names(self):
        self.put(self.tmp, "weird.con", "x"); self.put(self.tmp, "pic.jpg", "x"); self.put(self.tmp, "odd.nul", "x")
        plan = self.job("organize", folder=str(self.tmp), how=recipes.O_MODES[3])
        self.assertEqual([a.dst.parent.name for a in plan.actions], ["JPG"])
        self.assertTrue(any("isn't allowed on Windows" in n for n in plan.notes))
        self.assertEqual(engine.apply_plan(plan)["problems"], [])

    def test_trailing_dot_space_and_reserved_names_across_jobs(self):
        self.put(self.tmp, "a.txt", "x"); self.put(self.tmp, "b.txt", "y")
        (self.tmp / "o").mkdir()
        self.put(self.tmp, "unsorted.txt", "b\na\n")
        for bad in ("CON", "aux.txt", "com5", "lpt1.log", "Nul.tar.gz"):
            with self.assertRaises(UserError, msg=bad):
                self.job("merge", folder=str(self.tmp), out=bad)
            with self.assertRaises(UserError, msg=bad):
                self.job("package", folder=str(self.tmp), name=bad, outdir=str(self.tmp / "o"))
            with self.assertRaises(UserError, msg=bad):
                self.job("lines", file=str(self.tmp / "unsorted.txt"), op=r_more.LINE_OPS[1], outname=bad)

    def test_unicode_and_long_names_work_in_rename_and_organize(self):
        name = "résumé_日本語_" + "x" * 100 + ".txt"
        self.put(self.tmp, name, "x")
        plan = self.job("rename", folder=str(self.tmp), mode=recipes.R_MODES[0], text="ñ_")
        self.assertEqual(engine.apply_plan(plan)["problems"], [])
        self.assertEqual(self.names(), ["ñ_" + name])
        plan = self.job("organize", folder=str(self.tmp))
        self.assertEqual(engine.apply_plan(plan)["problems"], [])
        self.assertEqual(self.names(), ["Documents/ñ_" + name])

    def test_unicode_names_sync(self):
        a, b = self.tmp / "x" / "données", self.tmp / "y" / "données"
        a.mkdir(parents=True); b.mkdir(parents=True)
        self.put(a, "日本語/ファイル.txt", "hello")
        plan = self.job("sync", folder=str(a), roots=str(self.tmp / "y"), review=False)
        self.assertEqual(engine.apply_plan(plan)["problems"], [])
        self.assertEqual((b / "日本語" / "ファイル.txt").read_text(), "hello")

    def test_package_skips_files_whose_tidy_name_is_reserved(self):
        self.put(self.tmp / "src", "a.txt", "1"); self.put(self.tmp / "src", "b.txt", "2")
        plan = self.job("package", folder=str(self.tmp / "src"), outdir=str(self.tmp), zip=False,
                        naming=recipes.P_NAMING[1])
        self.assertEqual(len([a for a in plan.actions if a.kind == "copy"]), 2)

    def test_workflow_output_names_are_checked(self):
        f = self.put(self.tmp, "a.txt", "x")
        with self.assertRaises(UserError):
            W.file_output_plan("t", f, "body", "", "CON.txt", "x.txt")
        plan = W.file_output_plan("t", f, "body", "", "v1.2", "x.txt")
        self.assertEqual(plan.actions[0].dst.name, "v1.2.txt")


class TestOutputNames(Base):
    def test_extension_handling(self):
        w = recipes.with_extension
        self.assertEqual(w("notes", ".txt"), "notes.txt")
        self.assertEqual(w("notes.csv", ".txt"), "notes.csv")
        self.assertEqual(w("v1.2", ".txt"), "v1.2.txt")
        self.assertEqual(w("Report. final", ".txt"), "Report. final.txt")
        self.assertEqual(w("archive.tar.gz", ".txt"), "archive.tar.gz")

    def test_job_output_name_with_a_dot_but_no_extension(self):
        f = self.put(self.tmp, "t.txt", "b\na\nb\n")
        plan = self.job("lines", file=str(f), op=r_more.LINE_OPS[0], outname="cleaned v1.2")
        self.assertEqual(plan.actions[0].dst.name, "cleaned v1.2.txt")

    def test_combine_csv_leaves_out_its_own_earlier_output(self):
        self.put(self.tmp, "a.csv", "x\n1\n"); self.put(self.tmp, "b.csv", "x\n2\n")
        plan = self.job("combinecsv", folder=str(self.tmp), outname="all")        # no extension typed
        engine.apply_plan(plan)
        self.assertTrue((self.tmp / "all.csv").exists())
        plan = self.job("combinecsv", folder=str(self.tmp), outname="all")
        text = "\n".join(plan.report)
        self.assertNotIn("all.csv", text)                                          # not merged into itself


class TestDuplicatesAcrossSubfolders(Base):
    def test_same_file_names_in_different_subfolders_never_overwrite_each_other(self):
        self.put(self.tmp, "sub1/report.pdf", "one"); self.put(self.tmp, "sub2/report.pdf", "two")
        self.put(self.tmp, "sub3/report.pdf", "three")
        plan = self.job("organize", folder=str(self.tmp), sub=True)
        engine.apply_plan(plan)
        got = sorted(p.read_text() for p in (self.tmp / "Documents").iterdir())
        self.assertEqual(got, ["one", "three", "two"])
        self.assertEqual(len(list((self.tmp / "Documents").iterdir())), 3)

    def test_rename_numbering_with_equal_names_in_subfolders(self):
        self.put(self.tmp, "sub1/report.pdf", "one"); self.put(self.tmp, "sub2/report.pdf", "two")
        plan = self.job("rename", folder=str(self.tmp), sub=True, mode=recipes.R_MODES[3], text="Doc")
        self.assertEqual(engine.apply_plan(plan)["problems"], [])
        self.assertEqual(self.names(), ["sub1/Doc 001.pdf", "sub2/Doc 002.pdf"])

    def test_package_keeps_equal_names_from_different_subfolders(self):
        self.put(self.tmp / "src", "a/report.pdf", "one"); self.put(self.tmp / "src", "b/report.pdf", "two")
        (self.tmp / "out").mkdir()
        plan = self.job("package", folder=str(self.tmp / "src"), outdir=str(self.tmp / "out"), zip=False)
        self.assertEqual(engine.apply_plan(plan)["problems"], [])
        files = [p.read_text() for p in (self.tmp / "out").rglob("*.pdf")]
        self.assertEqual(sorted(files), ["one", "two"])


class TestMissingThings(Base):
    def test_missing_source_and_destination_folders_are_explained(self):
        gone = str(self.tmp / "nope")
        for key, over in (("rename", {"folder": gone}), ("organize", {"folder": gone}), ("find", {"folder": gone}),
                          ("report", {"folder": gone}), ("duplicates", {"folder": gone}),
                          ("sync", {"folder": gone, "roots": str(self.tmp)}),
                          ("lines", {"file": gone + ".txt"}), ("extract", {"file": gone + ".txt"}),
                          ("csv", {"file": gone + ".csv"}), ("split", {"file": gone + ".txt"})):
            with self.assertRaises(UserError, msg=key):
                self.job(key, **over)
        f = self.put(self.tmp, "t.txt", "b\na\n")
        with self.assertRaises(UserError):
            self.job("lines", file=str(f), outdir=str(self.tmp / "missing dir"), op=r_more.LINE_OPS[1])
        with self.assertRaises(UserError):
            self.job("package", folder=str(self.tmp), outdir=str(self.tmp / "missing dir"))

    def test_a_source_that_vanishes_before_apply_is_a_reported_problem(self):
        f = self.put(self.tmp, "a.txt", "x")
        plan = self.job("rename", folder=str(self.tmp), mode=recipes.R_MODES[0], text="n_")
        f.unlink()
        res = engine.apply_plan(plan)
        self.assertEqual(res["done"], 0)
        self.assertTrue(res["problems"])

    def test_a_missing_folder_stops_the_watcher_calmly(self):
        a, b = self.tmp / "p" / "f", self.tmp / "q" / "f"
        a.mkdir(parents=True); b.mkdir(parents=True)
        v = vals(registry.get("watch"), folder=str(a), roots=str(self.tmp / "q"))
        logs = []
        w = S.SyncWatcher(v, 1, True, logs.append, lambda p: None, settle=0)
        import shutil
        shutil.rmtree(a)
        with self.assertRaises(UserError):
            w.check_once()


class TestSpreadsheets(Base):
    def test_ragged_csv_rows_are_handled(self):
        f = self.put(self.tmp, "t.csv", "a,b,c\n1,2\n3,4,5,6\n7\n")
        for op, col in ((r_more.CSV_OPS[0], "c"), (r_more.CSV_OPS[1], "a, c"), (r_more.CSV_OPS[2], ""),
                        (r_more.CSV_OPS[3], "c"), (r_more.CSV_OPS[4], "c"), (r_more.CSV_OPS[5], "")):
            plan = self.job("csv", file=str(f), op=op, column=col, value="5")
            self.assertTrue(plan.summary or plan.report, op)

    def test_excel_cells_with_line_breaks_and_extra_sheets(self):
        import zipfile
        import readers
        p = self.tmp / "t.xlsx"
        sheet1 = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                  '<row><c t="inlineStr"><is><t>name</t></is></c><c t="inlineStr"><is><t>note</t></is></c></row>'
                  '<row><c t="inlineStr"><is><t>Ann</t></is></c><c t="inlineStr"><is><t>two\nlines\tand tab</t></is></c></row>'
                  '<row><c t="inlineStr"><is><t>Bob</t></is></c></row>'
                  '</sheetData></worksheet>')
        sheet2 = sheet1.replace("Ann", "Zed")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("xl/worksheets/sheet1.xml", sheet1)
            z.writestr("xl/worksheets/sheet2.xml", sheet2)
        head, rows, _, _ = r_more.read_table(p)
        self.assertEqual(head, ["name", "note"])
        self.assertEqual(rows, [["Ann", "two\nlines\tand tab"], ["Bob", ""]])    # one sheet, a clean rectangle
        self.assertEqual(readers.read_document(p)[0].split("\n")[1], "Ann\ttwo lines and tab")   # text view stays one row per line


class TestCommandsOffTheWindowThread(Base):
    def test_understanding_a_request_is_pure_and_quick_on_a_big_tree(self):
        import nl
        root = self.tmp / "tree"
        for i in range(60):
            (root / f"d{i}" / "e").mkdir(parents=True)
        i = nl.interpret("sync all folders named zzz in both directions", [], [str(root)])
        self.assertTrue(i.missing)
        i = nl.interpret("find emails in the folder", [], [str(root)])
        self.assertEqual(i.key, "emails")
        i = nl.interpret('add "2024" to the start of every name', [], [str(root)])
        self.assertEqual((i.key, i.values["text"]), ("rename", "2024"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
