"""Tests for the upgrade stages.  Run:  python test_stages.py"""
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ["FILEFIXER_HOME"] = tempfile.mkdtemp(prefix="ff_home2_")

import engine                      # noqa: E402
import registry                    # noqa: E402
from engine import UserError       # noqa: E402
import r_extract as X              # noqa: E402


def vals(recipe, **over):
    v = {f.key: f.default for f in recipe.fields}
    v.update(over)
    return v


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ff_w2_")).resolve()
        engine.save_history([])

    def make(self, rel, data="x", mtime=None):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def run_job(self, key, **over):
        r = registry.get(key)
        plan = r.plan(vals(r, **over))
        res = engine.apply_plan(plan)
        self.assertEqual(res["problems"], [])
        return plan, res


GROCERY = "itm_apple\nitm_banana\nregular_apple\nitm_orange\nitm_grapes\ncategory_fruit\nitm_mango\nregular_banana\n"


class TestExtract(Base):
    def test_spec_example_new_file(self):
        src = self.make("GroceryItems.txt", GROCERY)
        plan, res = self.run_job("extract", file=str(src))
        out = self.tmp / "GroceryItemsList.txt"
        self.assertEqual(out.read_text().replace("\r\n", "\n"),
                         "1. itm_apple\n2. itm_banana\n3. itm_orange\n4. itm_grapes\n5. itm_mango")
        self.assertEqual(src.read_text(), GROCERY)                      # original untouched
        self.assertEqual(res["open_path"], str(out))
        engine.undo_last()
        self.assertFalse(out.exists())

    def test_modes(self):
        t = "itm_a xitm_b b_itm itm_ mid_itm_x ITM_Z"
        f = lambda how, p, **k: X.find_matches(t, [p], how, **k)
        self.assertEqual(f(X.HOW[0], "itm_"), ["itm_a", "itm_"])
        self.assertEqual(f(X.HOW[1], "itm"), ["b_itm"])
        self.assertEqual(f(X.HOW[2], "itm_"), ["itm_a", "xitm_b", "itm_", "mid_itm_x"])
        self.assertEqual(f(X.HOW[3], "itm_"), ["itm_"])
        self.assertEqual(f(X.HOW[4], r"^itm_\w+$"), ["itm_a"])
        self.assertEqual(f(X.HOW[0], "itm_", case=False), ["itm_a", "itm_", "ITM_Z"])

    def test_order_duplicates_multiple(self):
        t = "itm_b itm_a itm_b zed_1 itm_a"
        self.assertEqual(X.find_matches(t, ["itm_"]), ["itm_b", "itm_a", "itm_b", "itm_a"])
        self.assertEqual(X.find_matches(t, ["itm_"], dedupe=True), ["itm_b", "itm_a"])
        self.assertEqual(X.find_matches(t, ["zed_", "itm_a"]), ["itm_a", "zed_1", "itm_a"])

    def test_punctuation_never_silently_altered(self):
        t = "(itm_a), itm_b. 'itm_c' itm_d"
        self.assertEqual(X.find_matches(t, ["itm_"]), ["itm_b.", "itm_d"])          # kept attached, (itm_a) not a prefix match
        self.assertEqual(X.find_matches(t, ["itm_"], punct=X.PUNCT[1]), ["itm_a", "itm_b", "itm_c", "itm_d"])
        self.assertEqual(X.find_matches("#tag1 #tag2", ["#"], punct=X.PUNCT[1]), ["#tag1", "#tag2"])

    def test_unicode_and_tokens(self):
        t = "itm_café itm_café itm_中文 über"
        got = X.find_matches(t, ["itm_café"])
        self.assertEqual(got, ["itm_café", "itm_café"])                    # composed + decomposed both match, originals kept
        self.assertEqual(X.find_matches(t, ["itm_中"]), ["itm_中文"])

    def test_split_modes(self):
        t = "itm_a,itm_b; itm_c d\nitm_e"
        self.assertEqual(X.find_matches(t, ["itm_"], split=X.SPLIT[2], seps=",;"), ["itm_a", "itm_b", "itm_e"])
        self.assertEqual(X.find_matches("itm_x y\nz itm_q", ["itm_"], split=X.SPLIT[1]), ["itm_x y"])

    def test_empty_and_errors(self):
        src = self.make("e.txt", "")
        plan = registry.get("extract").plan(vals(registry.get("extract"), file=str(src)))
        self.assertEqual(plan.actions, [])
        for bad in ({"patterns": ""}, {"how": X.HOW[4], "patterns": "(["}, {"file": str(self.tmp / "none.txt")},
                    {"file": str(self.tmp)}):
            with self.assertRaises(UserError):
                r = registry.get("extract")
                r.plan(vals(r, **{"file": str(src), **bad}))
        b = self.make("b.bin", b"\x00\x01itm_a")
        with self.assertRaises(UserError):
            registry.get("extract").plan(vals(registry.get("extract"), file=str(b)))

    def test_custom_name_dir_and_no_overwrite(self):
        src = self.make("a.txt", "itm_1 itm_2")
        out = self.tmp / "out"; out.mkdir()
        self.run_job("extract", file=str(src), outname="mine", outdir=str(out), numbering=X.NUMBERING[1])
        self.assertEqual((out / "mine.txt").read_text().replace("\r\n", "\n"), "itm_1\nitm_2")
        plan, _ = self.run_job("extract", file=str(src), outname="mine", outdir=str(out))
        self.assertTrue((out / "mine (2).txt").exists())
        self.assertTrue(any("already exists" in n for n in plan.notes))
        r = registry.get("extract")                                     # same name as the original: never overwritten
        p2 = r.plan(vals(r, file=str(src), outname="a.txt"))
        self.assertEqual(p2.actions[0].dst.name, "a (2).txt")

    def test_replace_original_with_backup_and_undo(self):
        raw = b"itm_a x\r\nitm_b y\r\n"
        src = self.make("a.txt", raw)
        r = registry.get("extract")
        plan = r.plan(vals(r, file=str(src), result=X.RESULT[1]))
        res = engine.apply_plan(plan)
        self.assertEqual(src.read_bytes(), b"1. itm_a\r\n2. itm_b")
        self.assertTrue(res["recycle"])
        engine.undo_last()
        self.assertEqual(src.read_bytes(), raw)

    def test_large_file_streams(self):
        src = self.make("big.txt", ("itm_x filler words here\n" * 200_000))
        r = registry.get("extract")
        plan = r.plan(vals(r, file=str(src)))
        self.assertIn("200000", plan.summary)


class TestSync(Base):
    def setUp(self):
        super().setUp()
        self.a = self.tmp / "portfolio" / "folder1"
        self.b = self.tmp / "apps" / "folder1"
        self.c = self.tmp / "backup" / "folder1"
        for p in (self.a, self.b, self.c):
            p.mkdir(parents=True)

    def put(self, folder, rel, data="x", mtime=None):
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def plan(self, key="sync", **over):
        r = registry.get(key)
        base = dict(folder=str(self.a), roots=f"{self.tmp/'apps'}\n{self.tmp/'backup'}", review=False)
        base.update(over)
        return r.plan(vals(r, **base))

    def go(self, **over):
        plan = self.plan(**over)
        res = engine.apply_plan(plan)
        self.assertEqual(res["problems"], [])
        return plan, res

    def tree(self, f):
        return sorted(str(p.relative_to(f)).replace(os.sep, "/") for p in f.rglob("*"))

    def test_finds_matching_names_and_verifies_content(self):
        self.put(self.a, "a.txt", "1"); self.put(self.b, "a.txt", "1")
        self.put(self.c, "zzz.txt", "unrelated")
        plan = self.plan("compare")
        text = "\n".join(plan.report)
        self.assertIn("found in 3 places", text)
        self.assertIn("SAME NAME ONLY", self.plan("compare", roots=str(self.tmp / "backup")).report[0])
        self.assertEqual(plan.actions, [])

    def test_one_way_new_modified_nested_and_undo(self):
        self.put(self.a, "new.txt", "n"); self.put(self.a, "deep/er/f.txt", "deep"); self.put(self.a, "mod.txt", "NEW", 2_000_000_000)
        self.put(self.b, "mod.txt", "old", 1_000_000_000); (self.a / "emptydir").mkdir()
        before_b, before_c = self.tree(self.b), self.tree(self.c)
        self.go()
        for d in (self.b, self.c):
            self.assertEqual((d / "deep/er/f.txt").read_text(), "deep")
            self.assertEqual((d / "mod.txt").read_text(), "NEW")
            self.assertTrue((d / "emptydir").is_dir())
        engine.undo_last()
        self.assertEqual((self.b / "mod.txt").read_text(), "old")
        self.assertEqual(self.tree(self.b), before_b); self.assertEqual(self.tree(self.c), before_c)

    def test_deletes_are_opt_in_and_go_to_recycle(self):
        self.put(self.a, "keep.txt", "k"); self.put(self.b, "keep.txt", "k"); self.put(self.b, "extra.txt", "e")
        plan = self.plan(roots=str(self.tmp / "apps"))
        self.assertEqual(plan.actions, [])
        self.assertTrue(any("Not removed" in x for x in plan.report))
        self.go(roots=str(self.tmp / "apps"), deletes=True)
        self.assertFalse((self.b / "extra.txt").exists())
        engine.undo_last()
        self.assertEqual((self.b / "extra.txt").read_text(), "e")

    def test_rename_detected_one_way(self):
        self.put(self.a, "renamed.txt", "same content here"); self.put(self.b, "old name.txt", "same content here")
        plan = self.plan(roots=str(self.tmp / "apps"))
        self.assertEqual([a.kind for a in plan.actions], ["move"])
        engine.apply_plan(plan)
        self.assertEqual(self.tree(self.b), ["renamed.txt"])

    def test_two_way_changes_flow_both_ways_then_conflict(self):
        for f in (self.a, self.b):
            self.put(f, "shared.txt", "v1", 1_000_000_000); self.put(f, "other.txt", "o1", 1_000_000_000)
        self.go(mode=__import__("r_sync").SYNC_MODES[1], roots=str(self.tmp / "apps"))     # first sync records the baseline
        self.put(self.a, "shared.txt", "edited in A", 1_500_000_000)                        # change in A
        self.put(self.b, "fresh.txt", "made in B")                                          # new in B
        self.go(mode=__import__("r_sync").SYNC_MODES[1], roots=str(self.tmp / "apps"))
        self.assertEqual((self.b / "shared.txt").read_text(), "edited in A")
        self.assertEqual((self.a / "fresh.txt").read_text(), "made in B")
        self.put(self.a, "other.txt", "A version", 1_600_000_000); self.put(self.b, "other.txt", "B version", 1_700_000_000)
        plan = self.plan(mode=__import__("r_sync").SYNC_MODES[1], roots=str(self.tmp / "apps"))
        self.assertEqual(plan.actions, [])
        self.assertTrue(any("CONFLICT" in x for x in plan.report))
        self.assertEqual((self.a / "other.txt").read_text(), "A version")                   # nothing silently overwritten
        plan, _ = self.go(mode=__import__("r_sync").SYNC_MODES[1], roots=str(self.tmp / "apps"),
                          conflicts=__import__("r_sync").CONFLICT_RULES[1])
        self.assertEqual((self.a / "other.txt").read_text(), "B version")                   # newest wins by rule

    def test_two_way_deletion_propagates_only_when_allowed(self):
        S = __import__("r_sync")
        for f in (self.a, self.b):
            self.put(f, "gone.txt", "g")
        self.go(mode=S.SYNC_MODES[1], roots=str(self.tmp / "apps"))
        (self.a / "gone.txt").unlink()
        plan = self.plan(mode=S.SYNC_MODES[1], roots=str(self.tmp / "apps"))
        self.assertEqual(plan.actions, [])
        self.go(mode=S.SYNC_MODES[1], roots=str(self.tmp / "apps"), deletes=True)
        self.assertFalse((self.b / "gone.txt").exists())

    def test_never_with_itself_or_nested_and_links(self):
        self.put(self.a, "f.txt", "1")
        plan = self.plan(roots=str(self.tmp / "portfolio"))                    # only itself found
        self.assertTrue(any("no other folder" in x for x in plan.report))
        nested = self.a / "inner" / "folder1"; nested.mkdir(parents=True)
        plan = self.plan(roots=str(self.a))
        self.assertTrue(any("no other folder" in x for x in plan.report))
        if hasattr(os, "symlink"):
            os.symlink(self.a / "f.txt", self.b / "link.txt")
            plan = self.plan(roots=str(self.tmp / "apps"))
            self.assertTrue(any("link" in n for n in plan.notes))
            self.assertFalse(any("link.txt" in a.sentence for a in plan.actions))

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "needs a non-root POSIX user")
    def test_permission_error_reported(self):
        self.put(self.a, "f.txt", "1"); (self.b / "locked").mkdir(); os.chmod(self.b / "locked", 0)
        try:
            plan = self.plan(roots=str(self.tmp / "apps"))
            self.assertTrue(any("Couldn't open" in n for n in plan.notes))
        finally:
            os.chmod(self.b / "locked", 0o755)

    def test_permission_errors_are_reported_not_fatal(self):
        from unittest import mock
        self.put(self.a, "f.txt", "1"); (self.b / "locked").mkdir()
        real = os.scandir
        def fake(p):
            if str(p).endswith("locked"):
                raise PermissionError(13, "Permission denied")
            return real(p)
        with mock.patch("os.scandir", fake):
            plan = self.plan(roots=str(self.tmp / "apps"))
        self.assertTrue(any("Couldn't open" in n and "Permission denied" in n for n in plan.notes))
        self.assertEqual(len(plan.actions), 1)                                   # the rest still works
        with mock.patch("shutil.copy2", side_effect=PermissionError(13, "denied")):
            res = engine.apply_plan(plan)
        self.assertEqual(res["done"], 0)
        self.assertIn("not allowed", res["problems"][0])
        self.assertFalse((self.b / "f.txt").exists())                            # nothing half-written

    def test_apply_failure_is_reported_not_silent(self):
        self.put(self.a, "f.txt", "1")
        plan = self.plan(roots=str(self.tmp / "apps"))
        (self.b / "f.txt").write_text("appeared meanwhile")                    # destination changes after preview
        res = engine.apply_plan(plan)
        self.assertEqual(len(res["problems"]) >= 1, True)
        self.assertEqual((self.b / "f.txt").read_text(), "appeared meanwhile")

    def test_large_folder(self):
        for i in range(1500):
            self.put(self.a, f"d{i % 30}/f{i}.txt", f"content {i}")
        self.go(roots=str(self.tmp / "apps"))
        self.assertEqual(len(list(self.b.rglob("*.txt"))), 1500)
        self.assertEqual(self.plan(roots=str(self.tmp / "apps")).actions, [])    # second run: already in step

    def test_manual_review_flag_and_cancel(self):
        self.put(self.a, "f.txt", "1")
        self.assertTrue(registry.get("sync").plan(vals(registry.get("sync"), folder=str(self.a),
                        roots=str(self.tmp / "apps"), review=True)).selectable)
        engine.request_cancel()
        try:
            with self.assertRaises(engine.Cancelled):
                self.plan()
        finally:
            engine.reset_cancel()


class TestWatch(TestSync):
    def watcher(self, auto, **over):
        S = __import__("r_sync")
        v = vals(registry.get("watch"), folder=str(self.a), roots=str(self.tmp / "apps"), **over)
        self.logs, self.pend = [], []
        return S.SyncWatcher(v, 1, auto, self.logs.append, self.pend.append, settle=0)

    def test_auto_mode_converges_and_does_not_loop(self):
        self.put(self.a, "one.txt", "1"); self.put(self.b, "two.txt", "2")
        w = self.watcher(True)
        w.check_once()
        self.assertEqual((self.b / "one.txt").read_text(), "1"); self.assertEqual((self.a / "two.txt").read_text(), "2")
        self.assertIsNone(w.check_once())                                    # nothing left: no ping-pong
        self.put(self.b, "three.txt", "3")
        w.check_once()
        self.assertEqual((self.a / "three.txt").read_text(), "3")
        self.assertIsNone(w.check_once())
        self.assertTrue(engine.last_undoable())                              # watcher changes are undoable

    def test_ask_mode_waits_for_approval(self):
        self.put(self.a, "one.txt", "1"); self.put(self.b, "two.txt", "2")
        w = self.watcher(False)
        w.check_once()
        self.assertEqual(len(self.pend), 1)
        self.assertFalse((self.b / "one.txt").exists())                      # not applied yet
        w.check_once()
        self.assertEqual(len(self.pend), 1)                                  # no repeat nagging
        w.apply(w.pending)
        self.assertTrue((self.b / "one.txt").exists())

    def test_recent_files_wait_and_thread_stops(self):
        S = __import__("r_sync")
        self.put(self.a, "now.txt", "1")
        v = vals(registry.get("watch"), folder=str(self.a), roots=str(self.tmp / "apps"))
        w = S.SyncWatcher(v, 0.05, True, lambda t: None, lambda p: None, settle=3600)
        w.start(); import time; time.sleep(0.3); w.stop(); time.sleep(0.2)
        self.assertFalse(w.running)
        self.assertFalse((self.b / "now.txt").exists())                      # too fresh: left for later


def make_docx(path, paragraphs):
    import zipfile
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)


def make_xlsx(path, rows):
    import zipfile
    strings = []
    def cell(r, c, v):
        ref = "ABCDEFGH"[c] + str(r + 1)
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"><v>{v}</v></c>'
        if v not in strings:
            strings.append(v)
        return f'<c r="{ref}" t="s"><v>{strings.index(v)}</v></c>'
    sheet = "".join(f'<row r="{r+1}">' + "".join(cell(r, c, v) for c, v in enumerate(row)) + "</row>" for r, row in enumerate(rows))
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData>{sheet}</sheetData></worksheet>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{ns}">' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")


def make_pdf(path, text):
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"]
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
    objs += [f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += f"{i} 0 obj\n{o}\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offs)
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF"
    Path(path).write_bytes(out.encode("latin-1"))


class TestMore(Base):
    def out_text(self, name):
        return (self.tmp / name).read_text().replace("\r\n", "\n")

    def test_find_files_filters(self):
        now = __import__("time").time()
        self.make("a/draft one.txt", "x" * 10, mtime=now - 5 * 86400)
        self.make("a/old draft.txt", "x" * 10, mtime=now - 100 * 86400)
        self.make("big.bin", b"\0" * 2_000_000); (self.tmp / "emptyone").mkdir(); (self.tmp / "full").mkdir(); self.make("full/f.txt")
        r = registry.get("find")
        p = r.plan(vals(r, folder=str(self.tmp), name_has="DRAFT"))
        self.assertIn("2 files", p.summary)
        p = r.plan(vals(r, folder=str(self.tmp), name_has="draft", days="30"))
        self.assertIn("1 file", p.summary)
        p = r.plan(vals(r, folder=str(self.tmp), min_mb="1"))
        self.assertTrue(p.report[0].startswith("big.bin"))
        p = r.plan(vals(r, folder=str(self.tmp), what="Empty folders"))
        self.assertEqual(p.report, ["emptyone"])
        p = r.plan(vals(r, folder=str(self.tmp), types="txt", save="found.txt"))
        engine.apply_plan(p)
        self.assertEqual(len(self.out_text("found.txt").splitlines()), 3)
        with self.assertRaises(UserError):
            r.plan(vals(r, folder=str(self.tmp), days="abc"))

    def test_emails_urls_and_documents(self):
        txt = self.make("c.txt", "Mail bob@x.com, or (alice.b@mail.co.uk). Visit https://a.org/p?q=1. bob@x.com")
        r = registry.get("emails")
        self.assertEqual(r.plan(vals(r, file=str(txt))).report, ["bob@x.com", "alice.b@mail.co.uk"])
        p = r.plan(vals(r, file=str(txt), what=r.fields[3].choices[1], dedupe=False))
        self.assertEqual(p.report, ["https://a.org/p?q=1"])
        d = self.tmp / "c.docx"; make_docx(d, ["hello carl@z.io", "x"])
        self.assertEqual(r.plan(vals(r, file=str(d))).report, ["carl@z.io"])
        engine.apply_plan(r.plan(vals(r, file=str(d))))
        self.assertTrue((self.tmp / "Extracted.txt").exists())
        with self.assertRaises(UserError):
            r.plan(vals(r, file=str(d), result=X.RESULT[1]))               # never replaces a Word file with text
        x = self.tmp / "s.xlsx"; make_xlsx(x, [["name", "mail"], ["Ann", "ann@q.com"], ["Bo", 7]])
        self.assertEqual(r.plan(vals(r, file=str(x))).report, ["ann@q.com"])
        f = self.tmp / "folderrun"; f.mkdir(); (f / "a.txt").write_text("u@v.org"); (f / "b.txt").write_text("w@v.org")
        p = r.plan(vals(r, file="", folder=str(f)))
        self.assertEqual(p.report, ["u@v.org", "w@v.org"])

    def test_pdf_and_missing_pdf_support(self):
        try:
            import pypdf  # noqa: F401
        except ImportError:
            self.skipTest("pypdf not installed")
        pdf = self.tmp / "d.pdf"; make_pdf(pdf, "stock itm_pdf1 and itm_pdf2")
        r = registry.get("extract")
        p = r.plan(vals(r, file=str(pdf)))
        self.assertEqual(p.report, ["1. itm_pdf1", "2. itm_pdf2"])

    def test_extract_words_from_docx_and_xlsx(self):
        d = self.tmp / "w.docx"; make_docx(d, ["itm_one other", "itm_two"])
        r = registry.get("extract")
        self.assertEqual(r.plan(vals(r, file=str(d))).report, ["1. itm_one", "2. itm_two"])

    def test_line_tools(self):
        f = self.make("l.txt", "b\nA\nb\n\n a \n10\n9\n")
        r = registry.get("lines")
        run = lambda op, **k: r.plan(vals(r, file=str(f), op=op, **k)).report
        import r_more as M
        self.assertEqual(run(M.LINE_OPS[0]), ["b", "A", "", " a ", "10", "9"])
        self.assertEqual(run(M.LINE_OPS[4]), ["b", "A", "b", " a ", "10", "9"])
        self.assertEqual(run(M.LINE_OPS[7], text="a"), ["A", " a "])
        self.assertEqual(run(M.LINE_OPS[8], text="b"), ["A", "", " a ", "10", "9"])
        self.assertEqual(run(M.LINE_OPS[3])[:2], ["10", "9"][::-1])
        self.run_job("lines", file=str(f), op=M.LINE_OPS[4])
        self.assertTrue((self.tmp / "lCleanx.txt").exists() or (self.tmp / "lClean.txt").exists())
        self.assertEqual(f.read_text(), "b\nA\nb\n\n a \n10\n9\n")

    def test_split_text(self):
        f = self.make("big.txt", "".join(f"line {i}\n" for i in range(25)))
        self.run_job("split", file=str(f), n="10")
        parts = sorted((self.tmp / "big parts").glob("*.txt"))
        self.assertEqual([len(p.read_text().splitlines()) for p in parts], [10, 10, 5])
        self.assertEqual("".join(p.read_text() for p in parts), f.read_text())
        engine.undo_last()
        self.assertFalse((self.tmp / "big parts").exists())

    def test_csv_ops(self):
        f = self.make("d.csv", "name,qty,city\nAnn,5,Oslo\nBo,12,Rome\nCy,7,Oslo\nAnn,5,Oslo\n")
        r = registry.get("csv")
        import r_more as M
        rd = lambda name: (self.tmp / name).read_text().replace("\r\n", "\n")
        self.run_job("csv", file=str(f), op=M.CSV_OPS[0], column="city", value="oslo")
        self.assertEqual(rd("dResult.csv"), "name,qty,city\nAnn,5,Oslo\nCy,7,Oslo\nAnn,5,Oslo")
        self.run_job("csv", file=str(f), op=M.CSV_OPS[0], column="qty", cond="is greater than", value="6", outname="big.csv")
        self.assertEqual(rd("big.csv"), "name,qty,city\nBo,12,Rome\nCy,7,Oslo")
        self.run_job("csv", file=str(f), op=M.CSV_OPS[1], column="city, name", outname="cols.csv")
        self.assertEqual(rd("cols.csv").splitlines()[0], "city,name")
        self.run_job("csv", file=str(f), op=M.CSV_OPS[2], outname="dedupe.csv")
        self.assertEqual(len(rd("dedupe.csv").splitlines()), 4)
        self.run_job("csv", file=str(f), op=M.CSV_OPS[3], column="qty", desc=True, outname="sorted.csv")
        self.assertEqual(rd("sorted.csv").splitlines()[1], "Bo,12,Rome")
        p = r.plan(vals(r, file=str(f), op=M.CSV_OPS[4], column="qty"))
        self.assertIn("Total: 29", p.report); self.assertIn("Average: 7.25", p.report)
        self.run_job("csv", file=str(f), op=M.CSV_OPS[5], fmt=M.FORMATS[1], outname="out.json")
        self.assertEqual(json.loads((self.tmp / "out.json").read_text())[1]["city"], "Rome")
        self.run_job("csv", file=str(self.tmp / "out.json"), op=M.CSV_OPS[5], fmt=M.FORMATS[2], outname="back.csv")
        self.assertEqual(rd("back.csv").splitlines()[0], "name,qty,city")
        with self.assertRaises(UserError):
            r.plan(vals(r, file=str(f), op=M.CSV_OPS[0], column="nope", value="x"))
        self.assertEqual(f.read_text().count("Ann"), 2)                       # original untouched

    def test_combine_csv_and_report(self):
        self.make("1.csv", "a,b\n1,2\n"); self.make("2.csv", "b,c\n3,4\n")
        self.run_job("combinecsv", folder=str(self.tmp))
        got = (self.tmp / "combined.csv").read_text().replace("\r\n", "\n")
        self.assertEqual(got, "Source file,a,b,c\n1.csv,1,2,\n2.csv,,3,4")
        p = registry.get("report").plan(vals(registry.get("report"), folder=str(self.tmp), save="rep.txt"))
        self.assertIn("Files: 3", p.report)

    def test_rename_name_filter_organize_ext_days_and_multi_dup(self):
        self.make("Draft a.txt", "1"); self.make("final.txt", "2")
        self.run_job("rename", folder=str(self.tmp), mode="Replace some text in each name", only_name="draft", text="Draft", text2="Done")
        self.assertTrue((self.tmp / "Done a.txt").exists() and (self.tmp / "final.txt").exists())
        now = __import__("time").time()
        o = self.tmp / "org"; o.mkdir()
        (o / "new.pdf").write_text("p"); (o / "old.pdf").write_text("q"); (o / "n.jpg").write_text("j")
        os.utime(o / "old.pdf", (now - 90 * 86400,) * 2)
        import recipes as R_
        self.run_job("organize", folder=str(o), how=R_.O_MODES[3], days="30")
        self.assertEqual(sorted(str(p.relative_to(o)).replace(os.sep, "/") for p in o.rglob("*") if p.is_file()),
                         ["JPG/n.jpg", "PDF/new.pdf", "old.pdf"])
        a, b = self.tmp / "da", self.tmp / "db"
        a.mkdir(); b.mkdir(); (a / "x.dat").write_text("same"); (b / "y.dat").write_text("same")
        self.run_job("duplicates", folder=str(a), more=str(b))
        self.assertEqual(len(list(a.glob("*.dat"))) + len(list(b.glob("*.dat"))), 1)


class TestNL(Base):
    def test_spec_requests(self):
        import nl
        g = self.make("GroceryItems.txt", GROCERY)
        (self.tmp / "Projects").mkdir(); (self.tmp / "a" / "Projects").mkdir(parents=True)
        i = nl.interpret("Find all words that start with itm_ in GroceryItems.txt and number them.", [], [self.tmp])
        self.assertEqual((i.key, i.values["patterns"], i.values["how"], i.values["file"], i.missing), ("extract", "itm_", X.HOW[0], str(g), []))
        i = nl.interpret("Find all words that start with itm_. Keep the complete words intact, remove everything else, and put the results "
                         "into a numbered list in the same order they originally appeared.", [g], [])
        self.assertEqual((i.key, i.values["patterns"]), ("extract", "itm_"))
        i = nl.interpret('find words ending with "_x" in this file, case-insensitive, no numbers', [g], [])
        self.assertEqual((i.values["how"], i.values["patterns"], i.values["case"], i.values["numbering"]),
                         (X.HOW[1], "_x", False, X.NUMBERING[1]))
        i = nl.interpret("Rename all files containing the word Draft to Final", [], [self.tmp])
        self.assertEqual((i.key, i.values["only_name"], i.values["text2"], i.missing), ("rename", "Draft", "Final", []))
        self.assertTrue(nl.interpret("Rename all files containing the word Draft", [], [self.tmp]).missing)
        i = nl.interpret("Find all files modified in the last 30 days and organize them by extension.", [], [self.tmp])
        self.assertEqual((i.key, i.values["days"]), ("organize", "30"))
        self.assertEqual(nl.interpret("Find duplicate files across my computer.", [], [self.tmp]).key, "duplicates")
        self.assertEqual(nl.interpret("Extract all email addresses from these documents.", [g], []).key, "emails")
        i = nl.interpret("Find all folders named Projects and synchronize their contents.", [], [self.tmp])
        self.assertEqual((i.key, i.values["folder"]), ("sync", str(self.tmp / "Projects")))
        i = nl.interpret("Whenever I make changes to one folder, automatically apply those same changes to all other "
                         "folders with the same name.", [], [self.tmp / "Projects", self.tmp / "a" / "Projects"])
        self.assertEqual((i.key, i.values["mode"]), ("watch", __import__("r_sync").SYNC_MODES[1]))
        self.assertIsNone(nl.interpret("what is the weather like", [], []))

    def test_intent_runs_end_to_end(self):
        import nl
        g = self.make("GroceryItems.txt", GROCERY)
        i = nl.interpret("Find all words that start with itm_ in this file and number them", [g], [])
        r = registry.get(i.key)
        plan = r.plan(vals(r, **i.values))
        engine.apply_plan(plan)
        self.assertTrue((self.tmp / "GroceryItemsList.txt").read_text().startswith("1. itm_apple"))


class TestSandbox(Base):
    def sb(self, code, inputs=(), **k):
        import sandbox
        res = sandbox.run_script(code, inputs, **k)
        self.addCleanup(res.cleanup)
        return res

    def test_example_script_works_on_copies(self):
        import sandbox
        g = self.make("g.txt", GROCERY)
        r = self.sb(sandbox.EXAMPLE, [g])
        self.assertTrue(r.ok, r.stderr)
        self.assertEqual([p.name for p in r.outputs], ["result.txt"])
        self.assertTrue(r.outputs[0].read_text().startswith("1. itm_apple"))
        self.assertIn("Found 5 lines", r.stdout)
        self.assertEqual(g.read_text(), GROCERY)

    def test_input_copies_cannot_modify_originals_and_errors_are_reported(self):
        g = self.make("g.txt", "keep me")
        r = self.sb("open(INPUT_DIR + '/g.txt', 'w').write('hacked')", [g])
        self.assertFalse(r.ok); self.assertIn("sandbox", r.stderr)
        self.assertEqual(g.read_text(), "keep me")
        r = self.sb("print(undefined_name)")
        self.assertFalse(r.ok); self.assertIn("NameError", r.stderr)
        r = self.sb("def (:")
        self.assertFalse(r.ok); self.assertIn("SyntaxError", r.stderr)

    def test_blocked_actions(self):
        secret = self.make("secret.txt", "private")
        cases = {
            "read outside": f"print(open({str(secret)!r}).read())",
            "write outside": f"open({str(self.tmp / 'evil.txt')!r}, 'w').write('x')",
            "list outside": f"import os; print(os.listdir({str(self.tmp)!r}))",
            "delete outside": f"import os; os.remove({str(secret)!r})",
            "network": "import socket; socket.create_connection(('example.com', 80), 2)",
            "subprocess": "import subprocess; subprocess.run(['echo', 'hi'])",
            "os.system": "import os; os.system('echo hi')",
            "ctypes": "import ctypes",
            "move outside": f"import shutil; shutil.copy({str(secret)!r}, OUTPUT_DIR + '/x')",
            "etc passwd": "print(open('/etc/passwd').read())",
        }
        for name, code in cases.items():
            r = self.sb(code)
            self.assertFalse(r.ok, name)
            self.assertIn("sandbox", r.stderr, name)
        self.assertFalse((self.tmp / "evil.txt").exists())
        self.assertEqual(secret.read_text(), "private")

    def test_timeout_cancel_and_output_flood(self):
        r = self.sb("while True: pass", timeout=1)
        self.assertTrue(r.timed_out and not r.ok)
        import threading, time
        flag = threading.Event(); threading.Timer(0.6, flag.set).start()
        r = self.sb("import time; time.sleep(30)", cancel=flag.is_set)
        self.assertTrue(r.cancelled)
        r = self.sb("while True: print('x' * 1000)", timeout=10)
        self.assertFalse(r.ok); self.assertLess(len(r.stdout), 300_000)

    @unittest.skipIf(os.name == "nt", "memory limits need Linux or macOS")
    def test_memory_limit(self):
        r = self.sb("x = bytearray(2 * 1024 * 1024 * 1024)", mem_mb=256)
        self.assertFalse(r.ok); self.assertIn("MemoryError", r.stderr)

    def test_allowed_normal_work(self):
        r = self.sb("import json, re, csv, datetime, collections, math\n"
                     "open(OUTPUT_DIR + '/a.json', 'w').write(json.dumps({'ok': math.sqrt(16)}))\n"
                     "import os; os.makedirs(OUTPUT_DIR + '/sub'); open(OUTPUT_DIR + '/sub/b.txt', 'w').write('b')")
        self.assertTrue(r.ok, r.stderr)
        self.assertEqual(sorted(p.name for p in r.outputs), ["a.json", "b.txt"])


class TestWorkflow(Base):
    def test_chain_steps(self):
        import workflow as W
        steps = [{"op": "extract", "params": {"how": X.HOW[0], "patterns": "itm_"}},
                 {"op": "sort_lines", "params": {}}, {"op": "number", "params": {}}]
        out, log = W.run_steps(steps, GROCERY)
        self.assertEqual(out, "1. itm_apple\n2. itm_banana\n3. itm_grapes\n4. itm_mango\n5. itm_orange")
        self.assertEqual([n for _, n in log], [5, 5, 5])
        self.assertEqual(W.run_steps([{"op": "regex_replace", "params": {"pattern": r"(\w+)_(\w+)", "repl": r"\2-\1"}}], "itm_a x_y")[0], "a-itm y-x")
        self.assertEqual(W.run_steps([{"op": "keep_lines", "params": {"text": "A"}}, {"op": "uppercase"}], "a1\nb\nA2")[0], "A1\nA2")
        for bad in ([], [{"op": "nope"}], [{"op": "regex_extract", "params": {"pattern": "(["}}], [{"op": "replace", "params": {}}]):
            with self.assertRaises(UserError):
                W.run_steps(bad, "x")

    def test_save_load_and_log(self):
        import workflow as W
        steps = [{"op": "trim", "params": {}}]
        W.save_workflow("My flow", steps)
        self.assertIn("My flow", W.list_workflows())
        self.assertEqual(W.load_workflow("My flow"), steps)
        with self.assertRaises(UserError):
            W.save_workflow("a/b", steps)
        with self.assertRaises(UserError):
            W.load_workflow("missing")
        W.save_script("s1", "print(1)"); self.assertEqual(W.load_script("s1.py"), "print(1)")
        W.log_run("script", "s1", "ok", "out"); self.assertEqual(W.load_runs()[-1]["name"], "s1")

    def test_regex_builder_and_file_plans(self):
        import workflow as W
        text = "a1 b22 c333"
        self.assertEqual(W.regex_apply(r"\d+", text, W.REGEX_MODES[0])[0], "1. 1\n2. 22\n3. 333")
        self.assertEqual(W.regex_apply(r"\d+", text, W.REGEX_MODES[3])[0], "a b c")
        self.assertEqual(W.regex_apply(r"([a-z])(\d+)", text, W.REGEX_MODES[2], r"\2\1")[0], "1a 22b 333c")
        self.assertEqual(W.regex_apply("B", text, W.REGEX_MODES[1], ignorecase=True)[1], [(3, 4)])
        with self.assertRaises(UserError):
            W.regex_apply("(", text, W.REGEX_MODES[0])
        src = self.make("s.txt", text)
        plan = W.file_output_plan("Regex", src, "out", "", "res", "d.txt")
        engine.apply_plan(plan)
        self.assertEqual((self.tmp / "res.txt").read_text().replace("\r\n", "\n"), "out")

    def test_batch(self):
        import workflow as W
        self.make("a.txt", "itm_1 x\nitm_2"); self.make("b.txt", "none here")
        out = self.tmp / "out"; out.mkdir()
        steps = [{"op": "extract", "params": {"how": X.HOW[0], "patterns": "itm_"}}, {"op": "number", "params": {}}]
        plan = W.batch_plan(steps, [self.tmp / "a.txt", self.tmp / "b.txt"], str(out), "_list")
        self.assertEqual(len(plan.actions), 2)
        engine.apply_plan(plan)
        self.assertEqual((out / "a_list.txt").read_text().replace("\r\n", "\n"), "1. itm_1\n2. itm_2")
        self.assertEqual((out / "b_list.txt").read_text(), "")
        self.assertEqual((self.tmp / "a.txt").read_text(), "itm_1 x\nitm_2")


if __name__ == "__main__":
    unittest.main(verbosity=1)
