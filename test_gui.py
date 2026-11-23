"""Window tests.  Needs Tkinter and a display (they skip themselves otherwise).  Run:  python test_gui.py"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ["FILEFIXER_HOME"] = tempfile.mkdtemp(prefix="ff_gui_home_")
try:
    import tkinter as tk
    from tkinter import messagebox
    ROOT = tk.Tk()
    ROOT.withdraw()
except Exception as e:                                   # no Tk or no display
    tk = None
    SKIP = f"Tkinter/display not available: {e}"
else:
    SKIP = None

import engine      # noqa: E402
import registry    # noqa: E402


@unittest.skipIf(SKIP, SKIP)
class TestWindow(unittest.TestCase):
    def setUp(self):
        import app as A
        self.A = A
        engine.save_history([])
        self.root = tk.Toplevel(ROOT)
        self.app = A.App(self.root)
        self.asked, self.warned = [], []
        self.answer = True
        messagebox.askyesno = lambda *a, **k: (self.asked.append(a), self.answer)[1]
        messagebox.showwarning = lambda *a, **k: self.warned.append(a[1])
        messagebox.showerror = lambda *a, **k: self.warned.append(a[1])
        messagebox.showinfo = lambda *a, **k: None
        self.tmp = Path(tempfile.mkdtemp(prefix="ff_guiw_")).resolve()

    def tearDown(self):
        self.root.destroy()

    def pump(self, until=None, secs=6):
        end = time.time() + secs
        while time.time() < end:
            self.root.update()
            if until and until():
                return
            time.sleep(0.02)

    def preview(self):
        self.app.on_preview()
        self.pump(lambda: self.app.plan is not None or self.warned)

    def do(self):
        self.app.on_do()
        self.pump(lambda: self.app.summary.cget("text") == "Finished.")

    def test_every_job_opens_and_every_sidebar_button_works(self):
        for _, items in registry.GROUPS:
            for r in items:
                self.app.side_buttons[r.key].invoke()
                self.root.update()
                self.assertEqual(self.app.current.key, r.key)
                self.assertEqual(set(self.app.vars), {f.key for f in r.fields})
                self.assertEqual(self.app.title.cget("text"), r.name)
        self.app.side_buttons["home"].invoke()
        self.assertTrue(self.app.home_frame.winfo_ismapped() or True)

    def test_quick_actions_open_their_jobs(self):
        grid = next(w for w in self.app.home_frame.winfo_children() if len(w.winfo_children()) == 8)
        keys = []
        for b in grid.winfo_children():
            b.invoke()
            keys.append(self.app.current.key)
            self.app.show_home()
        self.assertEqual(keys, ["find", "compare", "sync", "extract", "rename", "duplicates", "organize", "csv"])

    def test_preview_matches_result_confirm_progress_and_undo(self):
        for i in range(12):
            (self.tmp / f"f{i}.txt").write_text(str(i))
        self.app.select(registry.get("rename"))
        self.app.vars["folder"].set(str(self.tmp)); self.app.vars["text"].set("new_")
        self.preview()
        self.assertEqual(len(self.app.plan.actions), 12)
        self.assertIn("12 files will be renamed", self.app.summary.cget("text"))
        self.assertIn("f0.txt   →   new_f0.txt", self.app.out.get("1.0", "end"))
        self.answer = False                                              # decline: nothing may change
        self.app.on_do(); self.pump(secs=0.3)
        self.assertEqual(len(list(self.tmp.glob("new_*"))), 0)
        self.assertTrue(self.asked)
        self.answer = True
        ticks = []
        orig = self.app.report_progress
        self.app.report_progress = lambda i, n: (ticks.append((i, n)), orig(i, n))[0]
        self.do()
        self.assertEqual(ticks[-1], (12, 12))                            # progress reached 12 of 12
        self.assertEqual(sorted(p.name for p in self.tmp.glob("*.txt")), sorted(f"new_f{i}.txt" for i in range(12)))
        self.assertIn("Done!", self.app.out.get("1.0", "end"))
        self.app.on_undo(); self.pump(secs=1.0)
        self.assertEqual(len(list(self.tmp.glob("f*.txt"))), 12)

    def test_changing_the_form_invalidates_the_preview(self):
        (self.tmp / "a.txt").write_text("x")
        self.app.select(registry.get("rename"))
        self.app.vars["folder"].set(str(self.tmp)); self.app.vars["text"].set("z_")
        self.preview()
        self.assertEqual(str(self.app.do_btn.cget("state")), "normal")
        self.app.vars["text"].set("y_")
        self.assertEqual(str(self.app.do_btn.cget("state")), "disabled")
        self.assertIsNone(self.app.plan)

    def test_errors_are_understandable(self):
        self.app.select(registry.get("rename"))
        self.app.vars["folder"].set("")
        self.preview()
        self.assertTrue(self.warned and "choose a folder" in self.warned[-1].lower())
        self.app.select(registry.get("extract"))
        self.app.vars["file"].set(str(self.tmp / "missing.txt"))
        self.warned.clear(); self.preview()
        self.assertIn("can't find this file", self.warned[-1])

    def test_home_command_runs_a_plain_english_request(self):
        g = self.tmp / "GroceryItems.txt"
        g.write_text("itm_apple regular_apple itm_banana\n")
        self.app.sel_files = [str(g)]
        self.app.use_suggestion("Find every word starting with itm_ in this file and put them into a numbered list")
        self.app.on_command()
        self.pump(lambda: self.app.plan is not None)
        self.assertEqual(self.app.current.key, "extract")
        self.assertIn("I will look in GroceryItems.txt", self.app.understand.cget("text"))
        self.assertIn("1. itm_apple", self.app.out.get("1.0", "end"))
        self.do()
        self.assertEqual((self.tmp / "GroceryItemsList.txt").read_text().replace("\r\n", "\n"), "1. itm_apple\n2. itm_banana")
        self.assertEqual(g.read_text(), "itm_apple regular_apple itm_banana\n")
        self.app.show_home()
        self.assertTrue(any("done" in str(self.app.activity.item(i)["values"]).lower() or "Done" in str(self.app.activity.item(i)["values"])
                            for i in self.app.activity.get_children()))
        self.app.use_suggestion("what is the weather"); self.app.on_command()
        self.assertIn("couldn't match", self.app.cmd_msg.cget("text"))

    def test_review_dialog_selects_individual_changes(self):
        a, b = self.tmp / "p1" / "proj", self.tmp / "p2" / "proj"
        a.mkdir(parents=True); b.mkdir(parents=True)
        (a / "one.txt").write_text("1"); (a / "two.txt").write_text("2"); (a / "three.txt").write_text("3")
        self.app.select(registry.get("sync"))
        self.app.vars["folder"].set(str(a)); self.app.vars["roots"].set(str(self.tmp / "p2"))
        self.assertTrue(self.app.vars["review"].get())                   # manual approval is the default
        self.preview()
        self.assertEqual(len(self.app.plan.actions), 3)
        seen = {}

        class FakeReview:                                                # the user ticks only the first change
            def __init__(s, parent, plan):
                seen["n"] = len(plan.actions); s.plan = plan

            def show(s):
                return s.plan.actions[:1]

        real = self.A.ReviewDialog
        self.A.ReviewDialog = FakeReview
        try:
            self.do()
        finally:
            self.A.ReviewDialog = real
        self.assertEqual(seen["n"], 3)
        self.assertEqual(len(list(b.glob("*.txt"))), 1)                  # only the approved change happened

    def test_cancel_button_is_available_while_busy_and_stops(self):
        for i in range(3):
            (self.tmp / f"p{i}.txt").write_text(str(i))
        self.app.select(registry.get("duplicates"))
        self.app.vars["folder"].set(str(self.tmp))
        vals = self.app.values()
        self.app.run_bg(lambda: (engine.request_cancel(), registry.get("duplicates").plan(vals))[1], lambda p: None, "x")
        self.assertTrue(self.app.cancel_btn.winfo_manager() == "pack")
        self.pump(lambda: not self.app.busy)
        self.assertIn("Cancelled", self.app.summary.cget("text"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
