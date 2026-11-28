"""Headless tests for the engine and recipes.  Run:  python tests.py"""
import os
import tempfile
import time
import unittest
from pathlib import Path

_home = tempfile.mkdtemp(prefix="ff_home_")
os.environ["FILEFIXER_HOME"] = _home          # keep tests away from the real history/recycle

import engine                                  # noqa: E402
import recipes                                 # noqa: E402
from engine import UserError                   # noqa: E402

R = {r.key: r for r in recipes.ALL}


def defaults(recipe, **over):
    v = {f.key: f.default for f in recipe.fields}
    v.update(over)
    return v


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ff_work_")).resolve()
        engine.save_history([])

    def make(self, rel, data="hello\n", mtime=None):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def names(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*") if p.is_file())

    def run_recipe(self, key, **over):
        plan = R[key].plan(defaults(R[key], folder=str(self.tmp), **over))
        res = engine.apply_plan(plan)
        self.assertEqual(res["problems"], [])
        return plan, res


class TestRename(Base):
    def test_prefix_and_undo(self):
        self.make("a.txt"); self.make("b.txt")
        before = self.names()
        plan, res = self.run_recipe("rename", mode=recipes.R_MODES[0], text="2026-")
        self.assertEqual(self.names(), ["2026-a.txt", "2026-b.txt"])
        out = engine.undo_last()
        self.assertEqual(out["problems"], [])
        self.assertEqual(self.names(), before)

    def test_number_in_natural_order(self):
        for n in ("pic10.jpg", "pic2.jpg", "pic1.jpg"):
            self.make(n, n)
        self.run_recipe("rename", mode=recipes.R_MODES[3], text="Trip")
        self.assertEqual((self.tmp / "Trip 001.jpg").read_text(), "pic1.jpg")
        self.assertEqual((self.tmp / "Trip 002.jpg").read_text(), "pic2.jpg")
        self.assertEqual((self.tmp / "Trip 003.jpg").read_text(), "pic10.jpg")

    def test_collision_gets_suffix(self):
        self.make("a x.txt", "1"); self.make("a_x.txt", "2")
        self.run_recipe("rename", mode=recipes.R_MODES[5])
        self.assertEqual(self.names(), ["a_x (2).txt", "a_x.txt"])

    def test_replace_and_filter(self):
        self.make("old report.txt"); self.make("old photo.jpg")
        self.run_recipe("rename", mode=recipes.R_MODES[2], text="old", text2="new", only="txt")
        self.assertEqual(self.names(), ["new report.txt", "old photo.jpg"])

    def test_bad_chars_and_empty(self):
        with self.assertRaises(UserError):
            R["rename"].plan(defaults(R["rename"], folder=str(self.tmp), text="a/b"))
        with self.assertRaises(UserError):
            R["rename"].plan(defaults(R["rename"], folder=str(self.tmp), text=""))

    def test_nothing_to_do(self):
        self.make("a.txt")
        plan = R["rename"].plan(defaults(R["rename"], folder=str(self.tmp), mode=recipes.R_MODES[6]))
        self.assertEqual(plan.actions, [])


class TestWindowsNames(Base):
    """Names Windows refuses are skipped one by one; they never block the valid renames."""

    def plan_replace(self, text, text2):
        return R["rename"].plan(defaults(R["rename"], folder=str(self.tmp), mode=recipes.R_MODES[2],
                                         text=text, text2=text2))

    def targets(self, plan):
        return sorted(a.dst.name for a in plan.actions)

    def test_trailing_dot_is_skipped(self):
        self.make("a.txt"); self.make("grtxt.md")
        plan = self.plan_replace("txt", "")
        self.assertEqual(self.targets(plan), ["gr.md"])
        self.assertTrue(any("1 file skipped because the new name isn't allowed on Windows" in n for n in plan.notes))

    def test_trailing_space_is_skipped(self):
        self.make("x y"); self.make("ay.md")
        plan = self.plan_replace("y", "")
        self.assertEqual(self.targets(plan), ["a.md"])
        self.assertEqual(len(plan.notes), 1)

    def test_reserved_device_names_are_skipped(self):
        self.make("Z.txt"); self.make("Q5"); self.make("Q5.log"); self.make("Zoo.txt")
        plan = self.plan_replace("Z", "CON")
        self.assertEqual(self.targets(plan), ["CONoo.txt"])
        self.assertTrue(any("1 file skipped" in n for n in plan.notes))
        plan = self.plan_replace("Q", "com")
        self.assertEqual(plan.actions, [])
        self.assertTrue(any("2 files skipped" in n for n in plan.notes))

    def test_valid_renames_still_apply(self):
        self.make("Z.txt"); self.make("Zoo.txt")
        self.run_recipe("rename", mode=recipes.R_MODES[2], text="Z", text2="CON")
        self.assertEqual(self.names(), ["CONoo.txt", "Z.txt"])

    def test_name_problem_helper(self):
        for bad in ("report.", "report ", "CON", "con.txt", "Nul.tar.gz", "com5", "LPT9.txt", "aux .txt"):
            self.assertIsNotNone(recipes.name_problem(bad), bad)
        for good in ("console.txt", "com10.txt", "CONX", "a.b", ".hidden", "lpt0.txt"):
            self.assertIsNone(recipes.name_problem(good), good)

    def test_typed_output_names_are_refused(self):
        self.make("a.txt"); self.make("b.txt")
        for bad in ("nul", "CON.txt", "aux.log"):
            with self.assertRaises(UserError):
                R["merge"].plan(defaults(R["merge"], folder=str(self.tmp), out=bad))
        with self.assertRaises(UserError):
            R["package"].plan(defaults(R["package"], folder=str(self.tmp), outdir=str(self.tmp.parent), name="aux"))

    def test_tidied_package_names_skip_bad_ones(self):
        out = Path(tempfile.mkdtemp(prefix="ff_out_")).resolve()
        self.make("CON.txt", "x"); self.make("fine.txt", "y")
        plan = R["package"].plan(defaults(R["package"], folder=str(self.tmp), outdir=str(out), zip=False,
                                          contents=False, naming=recipes.P_NAMING[1]))
        self.assertEqual([a.dst.name for a in plan.actions], ["fine.txt"])
        self.assertTrue(any("isn't allowed on Windows" in n for n in plan.notes))


class TestRecycleHygiene(Base):
    def batch_dirs(self):
        return sorted(p.name for p in engine.RECYCLE_DIR.iterdir()) if engine.RECYCLE_DIR.exists() else []

    def fake_batch(self, name, data=b"x" * 10, age_hours=48):
        d = engine.RECYCLE_DIR / name / "removed"
        d.mkdir(parents=True)
        (d / "f.txt").write_bytes(data)
        old = time.time() - age_hours * 3600
        os.utime(engine.RECYCLE_DIR / name, (old, old))

    def test_undo_removes_empty_batch_folder(self):
        self.make("a.txt"); self.make("b.txt")
        _, res = self.run_recipe("rename", mode=recipes.R_MODES[0], text="x_")
        engine.undo_last()
        self.assertFalse((engine.RECYCLE_DIR / res["batch"]).exists())

    def test_undo_of_removal_leaves_no_empty_folders(self):
        self.make("a.txt", "same"); self.make("b.txt", "same")
        _, res = self.run_recipe("duplicates")
        self.assertEqual(res["done"], 1)
        self.assertEqual(len(self.names()), 1)
        self.assertTrue((engine.RECYCLE_DIR / res["batch"]).exists())
        engine.undo_last()
        self.assertFalse((engine.RECYCLE_DIR / res["batch"]).exists())
        self.assertEqual(self.names(), ["a.txt", "b.txt"])

    def test_prune_keeps_folders_with_files(self):
        d = engine.RECYCLE_DIR / "keepme" / "undone"
        d.mkdir(parents=True)
        (d / "f").write_text("x")
        (engine.RECYCLE_DIR / "keepme" / "empty" / "deeper").mkdir(parents=True)
        engine.prune_empty_dirs(engine.RECYCLE_DIR / "keepme")
        self.assertTrue((d / "f").exists())
        self.assertFalse((engine.RECYCLE_DIR / "keepme" / "empty").exists())

    def test_usage_counts_batches_and_bytes(self):
        self.assertEqual(engine.recycle_usage()[0], len(self.batch_dirs()))
        before_n, before_b = engine.recycle_usage()
        self.fake_batch("zz-usage1", b"y" * 100); self.fake_batch("zz-usage2", b"y" * 50)
        n, b = engine.recycle_usage()
        self.assertEqual((n - before_n, b - before_b), (2, 150))

    def test_cleanup_keeps_referenced_batches_only(self):
        for n in self.batch_dirs():
            import shutil
            shutil.rmtree(engine.RECYCLE_DIR / n)
        for n in ("live", "undone1", "orphan", "newborn"):
            self.fake_batch(n, age_hours=0.001 if n == "newborn" else 48)
        rec = lambda: [{"op": "trash", "src": "a", "dst": "b"}]
        engine.save_history([
            {"id": "undone1", "title": "t", "time": "2026-01-01T00:00:00", "records": rec(), "undone": True},
            {"id": "live", "title": "t", "time": "2026-01-02T00:00:00", "records": rec(), "undone": False},
            {"id": "rename-only", "title": "t", "time": "2026-01-03T00:00:00", "records": rec(), "undone": False},
        ])
        out = engine.cleanup_recycle()
        self.assertEqual(self.batch_dirs(), ["live", "newborn"])      # newborn: too new to be sure it is finished
        self.assertEqual(out["removed"], 2)
        self.assertEqual(out["freed"], 20)
        self.assertEqual([b["id"] for b in engine.load_history()], ["live", "rename-only"])
        self.assertEqual(engine.cleanup_recycle()["removed"], 0)       # nothing more to do


class TestHonestUndo(Base):
    def renamed(self):
        self.make("a.txt", "A"); self.make("b.txt", "B")
        self.run_recipe("rename", mode=recipes.R_MODES[0], text="x_")

    def test_failed_restore_keeps_the_batch_and_retry_finishes_it(self):
        self.renamed()
        (self.tmp / "x_b.txt").unlink()                              # b can't be put back
        out = engine.undo_last()
        self.assertEqual((out["done"], len(out["problems"]), out["partial"]), (1, 1, True))
        self.assertEqual(self.names(), ["a.txt"])
        self.assertIsNotNone(engine.last_undoable())                 # not falsely reported as undone
        self.assertIn("still to put back", engine.describe_batch(engine.last_undoable()))
        (self.tmp / "x_b.txt").write_text("B")                       # cause fixed
        out = engine.undo_last()
        self.assertEqual((out["done"], out["problems"]), (1, []))
        self.assertEqual(self.names(), ["a.txt", "b.txt"])
        self.assertIsNone(engine.last_undoable())

    def test_hopeless_retry_gives_up_so_older_changes_are_not_blocked(self):
        self.make("old.txt"); self.run_recipe("rename", mode=recipes.R_MODES[0], text="o_")
        self.renamed()
        (self.tmp / "x_b.txt").unlink()
        engine.undo_last()                                           # first try: partial
        out = engine.undo_last()                                     # second try: no progress
        self.assertTrue(out["problems"] and "second try" in out["problems"][-1])
        self.assertEqual(engine.last_undoable()["title"], R["rename"].name)
        self.assertIn("o_old.txt", self.names())                     # the older batch is next in line


class TestOrganize(Base):
    def test_by_type_and_undo_removes_new_folders(self):
        self.make("p.JPG"); self.make("d.pdf"); self.make("weird.xyz"); self.make("song.mp3")
        before = self.names()
        self.run_recipe("organize")
        self.assertEqual(self.names(), ["Documents/d.pdf", "Music/song.mp3", "Other/weird.xyz", "Pictures/p.JPG"])
        engine.undo_last()
        self.assertEqual(self.names(), before)
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.is_dir()], [])

    def test_by_month_and_rerun_is_noop(self):
        t = time.mktime((2024, 3, 15, 12, 0, 0, 0, 0, -1))
        self.make("a.jpg", mtime=t)
        self.run_recipe("organize", how=recipes.O_MODES[1])
        self.assertEqual(self.names(), ["Pictures/2024-03/a.jpg"])
        plan = R["organize"].plan(defaults(R["organize"], folder=str(self.tmp), how=recipes.O_MODES[1], sub=True))
        self.assertEqual(plan.actions, [])

    def test_name_clash(self):
        self.make("a.jpg", "1"); self.make("Pictures/a.jpg", "2")
        self.run_recipe("organize")
        self.assertEqual(self.names(), ["Pictures/a (2).jpg", "Pictures/a.jpg"])


class TestFindReplace(Base):
    def test_basic_undo_restores_exact_bytes(self):
        raw = b"Cat sat. cat again.\r\nCATEGORY\r\n"
        p = self.make("n.txt", raw)
        self.run_recipe("replace", find="cat", replace="dog", whole=True)
        self.assertEqual(p.read_bytes(), b"dog sat. dog again.\r\nCATEGORY\r\n")      # CRLF kept, whole word
        engine.undo_last()
        self.assertEqual(p.read_bytes(), raw)

    def test_case_sensitive(self):
        p = self.make("n.txt", "Cat cat")
        self.run_recipe("replace", find="cat", replace="x", case=True)
        self.assertEqual(p.read_text(), "Cat x")

    def test_special_characters_are_literal(self):
        p = self.make("n.txt", "price (a+b) \\1")
        self.run_recipe("replace", find="(a+b)", replace="\\1 $x")
        self.assertEqual(p.read_text(), "price \\1 $x \\1")

    def test_keeps_encoding(self):
        p = self.make("w.txt", "caf\xe9 old".encode("cp1252"))
        self.run_recipe("replace", find="old", replace="new")
        self.assertEqual(p.read_bytes(), "caf\xe9 new".encode("cp1252"))
        b = self.make("bom.txt", b"\xef\xbb\xbfhello old")
        self.run_recipe("replace", find="old", replace="new")
        self.assertEqual(b.read_bytes(), b"\xef\xbb\xbfhello new")

    def test_skips_binary_and_types(self):
        self.make("a.bin", b"old\x00\x01old"); self.make("b.dat", "old")
        plan = R["replace"].plan(defaults(R["replace"], folder=str(self.tmp), types="", find="old", replace="n"))
        self.assertEqual([a.src.name for a in plan.actions], ["b.dat"])

    def test_unencodable_replacement_is_reported_not_corrupting(self):
        p = self.make("w.txt", "caf\xe9 old".encode("cp1252"))
        plan = R["replace"].plan(defaults(R["replace"], folder=str(self.tmp), find="old", replace="中"))
        res = engine.apply_plan(plan)
        self.assertEqual(len(res["problems"]), 1)
        self.assertEqual(p.read_bytes(), "caf\xe9 old".encode("cp1252"))

    def test_empty_find(self):
        with self.assertRaises(UserError):
            R["replace"].plan(defaults(R["replace"], folder=str(self.tmp)))


class TestDuplicates(Base):
    def test_keep_oldest_and_undo(self):
        self.make("orig.txt", "same data", mtime=1_000_000_000)
        self.make("copy.txt", "same data", mtime=1_500_000_000)
        self.make("sub/copy2.txt", "same data", mtime=1_600_000_000)
        self.make("other.txt", "different")
        self.make("e1.txt", ""); self.make("e2.txt", "")          # empty files are ignored
        before = self.names()
        self.run_recipe("duplicates")
        self.assertEqual(self.names(), ["e1.txt", "e2.txt", "orig.txt", "other.txt"])
        engine.undo_last()
        self.assertEqual(self.names(), before)

    def test_same_size_different_content(self):
        self.make("a.txt", "abcd"); self.make("b.txt", "abce")
        plan = R["duplicates"].plan(defaults(R["duplicates"], folder=str(self.tmp)))
        self.assertEqual(plan.actions, [])

    def test_keep_newest(self):
        self.make("a.txt", "zz", mtime=1_000_000_000); self.make("b.txt", "zz", mtime=1_600_000_000)
        self.run_recipe("duplicates", keep=recipes.D_KEEP[1])
        self.assertEqual(self.names(), ["b.txt"])


class TestMerge(Base):
    def test_merge_order_headings_and_undo(self):
        self.make("part10.txt", "ten"); self.make("part2.txt", "two\n"); self.make("part1.txt", "one")
        self.make("pic.txt", b"\x00\x01binary")
        plan, res = self.run_recipe("merge")
        out = (self.tmp / "combined.txt").read_text()
        self.assertEqual(out.replace("\r\n", "\n"),
                         "===== part1.txt =====\none\n\n===== part2.txt =====\ntwo\n\n===== part10.txt =====\nten\n")
        self.assertTrue(any("aren't plain text" in n for n in plan.notes))
        self.assertTrue((self.tmp / "part1.txt").exists())       # originals untouched
        engine.undo_last()
        self.assertFalse((self.tmp / "combined.txt").exists())

    def test_rerun_does_not_merge_its_own_output_and_never_overwrites(self):
        self.make("a.txt", "A"); self.make("b.txt", "B")
        self.run_recipe("merge", headings=False)
        self.run_recipe("merge", headings=False)
        self.assertEqual(self.names(), ["a.txt", "b.txt", "combined (2).txt", "combined.txt"])
        self.assertEqual((self.tmp / "combined (2).txt").read_text().replace("\r\n", "\n"), "A\n\nB\n")

    def test_output_name_is_left_out_in_subfolders_too(self):
        self.make("a.txt", "A"); self.make("sub/b.txt", "B"); self.make("sub/combined.txt", "OLD")
        plan, _ = self.run_recipe("merge", sub=True, headings=False)
        self.assertNotIn("OLD", (self.tmp / "combined.txt").read_text())
        self.assertEqual((self.tmp / "combined.txt").read_text().replace("\r\n", "\n"), "A\n\nB\n")
        self.assertTrue((self.tmp / "sub" / "combined.txt").exists())      # and the old file is untouched

    def test_needs_two(self):
        self.make("a.txt")
        plan = R["merge"].plan(defaults(R["merge"], folder=str(self.tmp)))
        self.assertEqual(plan.actions, [])


class TestSafety(Base):
    def test_folder_checks(self):
        for bad in ("", str(self.tmp / "nope"), "/", str(self.make("f.txt"))):
            with self.assertRaises(UserError):
                engine.check_folder(bad)
        with self.assertRaises(UserError):
            engine.check_folder(engine.APP_DIR)

    def test_one_failure_does_not_stop_the_rest(self):
        a, b = self.make("a.txt"), self.make("b.txt")
        plan = R["rename"].plan(defaults(R["rename"], folder=str(self.tmp), text="x_"))
        a.unlink()                                                # vanishes before Do
        res = engine.apply_plan(plan)
        self.assertEqual(res["done"], 1)
        self.assertEqual(len(res["problems"]), 1)
        self.assertTrue((self.tmp / "x_b.txt").exists())

    def test_undo_twice_and_empty(self):
        self.make("a.txt")
        self.run_recipe("rename", text="z")
        engine.undo_last()
        with self.assertRaises(UserError):
            engine.undo_last()

    def test_undo_conflict_is_reported(self):
        self.make("a.txt")
        self.run_recipe("rename", text="z")
        self.make("a.txt", "new thing in the old place")
        out = engine.undo_last()
        self.assertEqual(len(out["problems"]), 1)
        self.assertTrue((self.tmp / "za.txt").exists())

    def test_case_only_rename(self):
        self.make("Hello.TXT")
        self.run_recipe("rename", mode=recipes.R_MODES[6])
        self.assertEqual(self.names(), ["hello.txt"])


class TestPackage(Base):
    def setUp(self):
        super().setUp()
        self.out = Path(tempfile.mkdtemp(prefix="ff_out_")).resolve()

    def messy(self):
        self.make("Service Agreement.docx", "contract", mtime=1_600_000_000)
        self.make("Q3 report.xlsx", "numbers", mtime=1_600_000_000)          # 'report' beats .xlsx (listed first)
        self.make("sales.csv", "a,b", mtime=1_500_000_000)
        self.make("sub/sales copy.csv", "a,b", mtime=1_600_000_000)          # duplicate of sales.csv
        self.make("photo one.JPG", "img")
        self.make("meeting.txt", "m")
        self.make("odd.weird", "w")
        self.make("download.part", "junk")
        self.make("~$lock.docx", "lock")

    def build(self, **over):
        return self.run_recipe("package", outdir=str(self.out), **over)

    def pkg_files(self):
        return sorted(str(p.relative_to(self.out)).replace(os.sep, "/")
                      for p in self.out.rglob("*") if p.is_file())

    def test_full_package_and_undo(self):
        self.messy()
        before = self.names()
        plan, _ = self.build()
        self.assertEqual(self.pkg_files(), [
            "Delivery Package.zip",
            "Delivery Package/1_Contracts/Service Agreement.docx",
            "Delivery Package/2_Reports/Q3 report.xlsx",
            "Delivery Package/3_Data/sales.csv",
            "Delivery Package/4_Images/photo one.JPG",
            "Delivery Package/5_Notes/meeting.txt",
            "Delivery Package/9_Other/odd.weird",
            "Delivery Package/CONTENTS.txt"])
        self.assertEqual(self.names(), before)                              # originals untouched
        contents = (self.out / "Delivery Package" / "CONTENTS.txt").read_text()
        self.assertIn("=== 3_Data  (1 file) ===", contents)
        import zipfile
        with zipfile.ZipFile(self.out / "Delivery Package.zip") as z:
            self.assertIn("Delivery Package/3_Data/sales.csv", z.namelist())
            self.assertIn("Delivery Package/CONTENTS.txt", z.namelist())
        self.assertTrue(any("duplicate" in n for n in plan.notes))
        self.assertTrue(any("junk" in n for n in plan.notes))
        out = engine.undo_last()
        self.assertEqual(out["problems"], [])
        self.assertEqual(self.pkg_files(), [])
        self.assertEqual([p.name for p in self.out.iterdir()], [])          # no leftover folders
        self.assertEqual(self.names(), before)

    def test_tidy_names_with_date(self):
        self.make("My  Big report (final).PDF", "x", mtime=time.mktime((2024, 3, 15, 12, 0, 0, 0, 0, -1)))
        self.build(naming=recipes.P_NAMING[2], zip=False, contents=False)
        self.assertEqual(self.pkg_files(), ["Delivery Package/2_Reports/2024-03-15_My_Big_report_(final).pdf"])

    def test_same_names_in_different_subfolders_do_not_clash(self):
        self.make("a/notes.txt", "1"); self.make("b/notes.txt", "2")
        self.build(zip=False, contents=False)
        self.assertEqual(self.pkg_files(), ["Delivery Package/5_Notes/notes (2).txt",
                                            "Delivery Package/5_Notes/notes.txt"])

    def test_rerun_never_overwrites(self):
        self.make("a.txt", "1")
        self.build(); self.build()
        self.assertTrue((self.out / "Delivery Package (2).zip").exists())
        self.assertTrue((self.out / "Delivery Package" / "5_Notes" / "a.txt").exists())

    def test_custom_rules(self):
        self.make("invoice 12.pdf", "pdf data"); self.make("photo.png", "png data")
        self.build(rules="Money = invoice, receipt\nPictures = .png\n", other="Misc", zip=False, contents=False)
        self.assertEqual(self.pkg_files(), ["Delivery Package/Money/invoice 12.pdf",
                                            "Delivery Package/Pictures/photo.png"])

    def test_bad_rules_and_locations(self):
        self.make("a.txt")
        for rules in ("no equals sign here", "= .txt", "Folder =", "# only comments\n"):
            with self.assertRaises(UserError):
                R["package"].plan(defaults(R["package"], folder=str(self.tmp), outdir=str(self.out), rules=rules))
        with self.assertRaises(UserError):                                  # package inside the source
            R["package"].plan(defaults(R["package"], folder=str(self.tmp), outdir=str(self.tmp)))
        with self.assertRaises(UserError):
            R["package"].plan(defaults(R["package"], folder=str(self.tmp), outdir=str(self.out), name="a/b"))

    def test_nothing_to_package(self):
        self.make("only.tmp")
        plan = R["package"].plan(defaults(R["package"], folder=str(self.tmp), outdir=str(self.out)))
        self.assertEqual(plan.actions, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
