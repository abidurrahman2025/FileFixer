"""Advanced Tools window: script editor, regex builder, workflow builder, batch processing, history.

Everything here goes through the same safety layers as the main app: scripts run in the restricted
process from sandbox.py, and file results are written through engine plans (preview, backup, undo).
"""
from __future__ import annotations

import json
import os
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import engine
import readers
import sandbox
import workflow as W
from engine import Action, Namer, Plan, UserError, check_file, check_folder
from recipes import plural


def _mono(root) -> tkfont.Font:
    return tkfont.nametofont("TkFixedFont")


class AdvancedWindow:
    def __init__(self, app) -> None:
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("FileFixer - Advanced Tools")
        self.win.geometry("980x720")
        self.win.minsize(820, 560)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        top = ttk.Frame(self.win, padding=(16, 12, 16, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Advanced Tools", font=app.f_title).pack(anchor="w")
        ttk.Label(top, text="For people comfortable with scripts and patterns. Everything still runs inside FileFixer's safety "
                            "rules: previews, backups and undo.", style="Help.TLabel", wraplength=900).pack(anchor="w", pady=(0, 6))
        self.nb = ttk.Notebook(self.win)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.last_result = None
        self.script_inputs: list = []
        self.cancel_flag = threading.Event()
        self.steps: list = []
        self.batch_files: list = []
        self._script_tab()
        self._regex_tab()
        self._workflow_tab()
        self._batch_tab()
        self._history_tab()
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.refresh_history() if self.nb.index("current") == 4 else None)

    # ------------------------------------------------------------ plumbing
    def show(self) -> None:
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def close(self) -> None:
        if self.last_result:
            self.last_result.cleanup()
        self.app.advanced = None
        self.win.destroy()

    def bg(self, fn, done) -> None:
        box: dict = {}

        def work():
            try:
                box["r"] = fn()
            except BaseException as e:                     # noqa: BLE001
                box["e"] = e

        t = threading.Thread(target=work, daemon=True)
        t.start()

        def poll():
            if t.is_alive():
                self.win.after(100, poll)
            else:
                done(box.get("r"), box.get("e"))

        poll()

    def err(self, e: BaseException) -> None:
        if isinstance(e, UserError):
            messagebox.showwarning("FileFixer", str(e), parent=self.win)
        else:
            messagebox.showerror("Something went wrong", f"{e}\n\nNo harm done to your files.", parent=self.win)

    def text_box(self, parent, height=8, mono=True, **kw) -> tuple:
        frame = ttk.Frame(parent)
        t = tk.Text(frame, height=height, wrap="none" if mono else "word", relief="solid", bd=1, padx=6, pady=4,
                    font=_mono(self.win) if mono else None, undo=True, **kw)
        sb = ttk.Scrollbar(frame, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        return frame, t

    def apply_and_report(self, plan: Plan, kind: str, name: str, payload=None) -> None:
        """Confirm, apply through the engine (backup + undo) and log the run."""
        if not plan.actions:
            messagebox.showinfo("FileFixer", plan.summary or "Nothing to do.", parent=self.win)
            return
        lines = "\n".join(a.sentence for a in plan.actions[:12]) + (f"\n... and {len(plan.actions) - 12} more" if len(plan.actions) > 12 else "")
        notes = "\n".join(plan.notes)
        if not messagebox.askyesno("Ready to go?", f"{plan.summary}\n\n{lines}\n\n{notes}\n\nGo ahead? You can undo it afterwards.", parent=self.win):
            return
        res = engine.apply_plan(plan)
        status = "failed" if res["problems"] and not res["done"] else "ok"
        W.log_run(kind, name, status, f"{res['done']} change(s)\n" + "\n".join(res["problems"]), payload)
        engine.log_activity("failed" if status == "failed" else "done", name, f"{plural(res['done'], 'change')} made", res.get("open_path", ""))
        self.app.refresh_undo()
        msg = f"Done. {plural(res['done'], 'change')} made."
        if res["problems"]:
            msg += "\n\nProblems:\n" + "\n".join(res["problems"][:8])
        messagebox.showinfo("FileFixer", msg, parent=self.win)

    # ------------------------------------------------------------ 1. script
    def _script_tab(self) -> None:
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Python Script")
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        self.script_name = tk.StringVar()
        ttk.Label(bar, text="Saved scripts").pack(side="left")
        self.script_combo = ttk.Combobox(bar, textvariable=self.script_name, values=W.list_scripts(), width=24, state="readonly")
        self.script_combo.pack(side="left", padx=6)
        ttk.Button(bar, text="Open", command=self.script_open).pack(side="left")
        ttk.Button(bar, text="Save as...", command=self.script_save).pack(side="left", padx=6)
        ttk.Button(bar, text="Insert example", command=lambda: (self.script_ed.delete("1.0", "end"), self.script_ed.insert("1.0", sandbox.EXAMPLE))).pack(side="left")
        frame, self.script_ed = self.text_box(f, height=14)
        frame.pack(fill="both", expand=True, pady=(8, 6))
        self.script_ed.insert("1.0", sandbox.EXAMPLE)
        ttk.Label(f, text="Write or paste a script (for example one an AI assistant wrote for you). FileFixer does not generate scripts "
                          "itself, and it never runs one without your OK.", style="Help.TLabel", wraplength=900).pack(anchor="w")
        inp = ttk.Frame(f)
        inp.pack(fill="x", pady=(6, 2))
        ttk.Button(inp, text="Add input files...", command=self.script_add_inputs).pack(side="left")
        ttk.Button(inp, text="Clear", command=lambda: (self.script_inputs.clear(), self.script_show_inputs())).pack(side="left", padx=6)
        self.inputs_label = ttk.Label(inp, text="No input files. The script gets copies of the files you add (in INPUT_DIR).", style="Help.TLabel")
        self.inputs_label.pack(side="left", padx=6)
        run = ttk.Frame(f)
        run.pack(fill="x", pady=4)
        self.run_btn = ttk.Button(run, text="Run script", style="Big.TButton", command=self.script_run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(run, text="Stop", command=self.cancel_flag.set, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Label(run, text="Time limit (seconds)").pack(side="left", padx=(14, 4))
        self.timeout_var = tk.StringVar(value="20")
        ttk.Spinbox(run, from_=1, to=600, width=5, textvariable=self.timeout_var).pack(side="left")
        ttk.Label(run, text="Memory (MB)").pack(side="left", padx=(14, 4))
        self.mem_var = tk.StringVar(value="512")
        ttk.Spinbox(run, from_=64, to=8192, increment=64, width=6, textvariable=self.mem_var).pack(side="left")
        self.skip_consent = tk.BooleanVar(value=False)
        ttk.Checkbutton(run, text="Don't ask me again this session", variable=self.skip_consent).pack(side="left", padx=14)
        frame, self.script_out = self.text_box(f, height=6, mono=True)
        frame.pack(fill="both", expand=True, pady=(4, 4))
        self.script_out.configure(state="disabled")
        self.script_out.tag_configure("err", foreground="#b03a2e")
        self.script_out.tag_configure("ok", foreground="#1e8449")
        res = ttk.Frame(f)
        res.pack(fill="x")
        self.result_label = ttk.Label(res, text="Results appear here after a run. Nothing reaches your real folders until you save them.", style="Help.TLabel")
        self.result_label.pack(side="left")
        self.save_results_btn = ttk.Button(res, text="Save results to a folder...", command=self.script_save_results, state="disabled")
        self.save_results_btn.pack(side="right")

    def script_add_inputs(self) -> None:
        chosen = filedialog.askopenfilenames(parent=self.win, title="Choose input files")
        if chosen:
            self.script_inputs += [c for c in chosen if c not in self.script_inputs]
            self.script_show_inputs()

    def script_show_inputs(self) -> None:
        n = len(self.script_inputs)
        self.inputs_label.config(text=("; ".join(Path(p).name for p in self.script_inputs[:4]) + (f" ... (+{n - 4})" if n > 4 else "")) if n
                                 else "No input files. The script gets copies of the files you add (in INPUT_DIR).")

    def script_open(self) -> None:
        try:
            code = W.load_script(self.script_name.get())
        except UserError as e:
            return self.err(e)
        self.script_ed.delete("1.0", "end")
        self.script_ed.insert("1.0", code)

    def script_save(self) -> None:
        from tkinter import simpledialog
        name = simpledialog.askstring("Save script", "Name for this script:", parent=self.win)
        if not name:
            return
        try:
            p = W.save_script(name, self.script_ed.get("1.0", "end-1c"))
        except UserError as e:
            return self.err(e)
        self.script_combo.config(values=W.list_scripts())
        self.script_name.set(p.name)

    def script_run(self, code: str = None) -> None:
        code = code if code is not None else self.script_ed.get("1.0", "end-1c")
        if not code.strip():
            return messagebox.showinfo("FileFixer", "Write a script first.", parent=self.win)
        try:
            timeout, mem = float(self.timeout_var.get()), int(float(self.mem_var.get()))
        except ValueError:
            return messagebox.showwarning("FileFixer", "Time limit and memory need to be numbers.", parent=self.win)
        missing = [p for p in self.script_inputs if not Path(p).is_file()]
        if missing:
            return messagebox.showwarning("FileFixer", f"This input file is no longer there:\n{missing[0]}", parent=self.win)
        if not self.skip_consent.get():
            files = "\n".join(f"  • {Path(p).name}" for p in self.script_inputs[:8]) or "  (none)"
            if not messagebox.askyesno(
                    "Run this script?",
                    "The script will run in a restricted area:\n\n"
                    f"• It can read COPIES of these files:\n{files}\n"
                    "• It can write only into a temporary output folder\n"
                    "• No internet, no starting other programs\n"
                    f"• It stops after {timeout:g} seconds\n\n"
                    "Your real files are not touched. Only run scripts you understand or trust.\n\nRun it?", parent=self.win):
                return
        if self.last_result:
            self.last_result.cleanup()
            self.last_result = None
        self.cancel_flag.clear()
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.save_results_btn.config(state="disabled")
        self._set_out([("Running...\n", None)])
        inputs = list(self.script_inputs)
        self.bg(lambda: sandbox.run_script(code, inputs, timeout, mem, cancel=self.cancel_flag.is_set),
                lambda r, e: self.script_done(r, e, code, inputs))

    def _set_out(self, chunks: list) -> None:
        self.script_out.configure(state="normal")
        self.script_out.delete("1.0", "end")
        for text, tag in chunks:
            self.script_out.insert("end", text, tag or ())
        self.script_out.configure(state="disabled")

    def script_done(self, r, e, code, inputs) -> None:
        self.run_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        if e:
            return self.err(e)
        self.last_result = r
        chunks = []
        if r.stdout:
            chunks.append((r.stdout + ("\n" if not r.stdout.endswith("\n") else ""), None))
        if r.stderr:
            chunks.append((r.stderr, "err"))
        chunks.append((f"\n{'Finished' if r.ok else 'Did not finish'} in {r.seconds:.1f} seconds.", "ok" if r.ok else "err"))
        self._set_out(chunks)
        n = len(r.outputs)
        self.result_label.config(text=(f"The script made {plural(n, 'file')}: " + ", ".join(p.name for p in r.outputs[:5])) if n
                                 else "The script made no files in OUTPUT_DIR.")
        self.save_results_btn.config(state="normal" if n else "disabled")
        W.log_run("script", "Python script", "ok" if r.ok else "failed", (r.stdout + "\n" + r.stderr).strip(),
                  {"code": code, "inputs": inputs})
        engine.log_activity("done" if r.ok else "failed", "Python script", f"{r.seconds:.1f}s, {plural(n, 'result file')}")

    def script_save_results(self) -> None:
        r = self.last_result
        if not r or not r.outputs:
            return
        dest = filedialog.askdirectory(parent=self.win, title="Save the results in...", mustexist=True)
        if not dest:
            return
        try:
            folder = check_folder(dest)
        except UserError as e:
            return self.err(e)
        out_root = r.workdir / "output"
        namer, plan = Namer(), Plan("Save script results")
        for p in r.outputs:
            target = namer.reserve(folder / p.relative_to(out_root))
            plan.actions.append(Action("copy", f"{p.relative_to(out_root)}   →   {target}", p, target))
        plan.summary = f"{plural(len(plan.actions), 'file')} will be copied to {folder}."
        plan.notes.append("Existing files are never overwritten.")
        self.apply_and_report(plan, "script-results", "Save script results")

    # ------------------------------------------------------------ 2. regex
    def _regex_tab(self) -> None:
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Regex Builder")
        g = ttk.Frame(f)
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)
        self.rx_pat, self.rx_repl = tk.StringVar(), tk.StringVar()
        self.rx_mode = tk.StringVar(value=W.REGEX_MODES[0])
        self.rx_ic, self.rx_ml = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        ttk.Label(g, text="Pattern").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(g, textvariable=self.rx_pat, font=_mono(self.win)).grid(row=0, column=1, sticky="ew", pady=3, padx=6)
        ttk.Label(g, text="Action").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Combobox(g, textvariable=self.rx_mode, values=W.REGEX_MODES, state="readonly").grid(row=1, column=1, sticky="ew", pady=3, padx=6)
        ttk.Label(g, text="Replace with").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(g, textvariable=self.rx_repl, font=_mono(self.win)).grid(row=2, column=1, sticky="ew", pady=3, padx=6)
        opts = ttk.Frame(g)
        opts.grid(row=3, column=1, sticky="w", padx=6)
        ttk.Checkbutton(opts, text="Ignore capital letters", variable=self.rx_ic).pack(side="left")
        ttk.Checkbutton(opts, text="^ and $ match at each line", variable=self.rx_ml).pack(side="left", padx=12)
        for v in (self.rx_pat, self.rx_repl, self.rx_mode, self.rx_ic, self.rx_ml):
            v.trace_add("write", lambda *_: self.regex_live())
        ttk.Label(f, text="Sample text (paste some of your text to test on)").pack(anchor="w", pady=(8, 0))
        frame, self.rx_sample = self.text_box(f, height=7)
        frame.pack(fill="both", expand=True)
        self.rx_sample.tag_configure("hit", background="#ffe58f")
        self.rx_sample.insert("1.0", "itm_apple, regular_apple\nitm_banana category_fruit\nitm_orange")
        self.rx_sample.bind("<KeyRelease>", lambda e: self.regex_live())
        self.rx_status = ttk.Label(f, text="", foreground="#b03a2e")
        self.rx_status.pack(anchor="w")
        ttk.Label(f, text="Result").pack(anchor="w")
        frame, self.rx_result = self.text_box(f, height=6)
        frame.pack(fill="both", expand=True)
        self.rx_result.configure(state="disabled")
        ff = ttk.Frame(f)
        ff.pack(fill="x", pady=(8, 0))
        self.rx_file = tk.StringVar()
        self.rx_out = tk.StringVar()
        self.rx_replace_orig = tk.BooleanVar(value=False)
        ttk.Button(ff, text="Apply to a file...", command=self.regex_pick_file).pack(side="left")
        ttk.Entry(ff, textvariable=self.rx_file, width=40).pack(side="left", padx=6)
        ttk.Label(ff, text="New file name").pack(side="left")
        ttk.Entry(ff, textvariable=self.rx_out, width=18).pack(side="left", padx=6)
        ttk.Checkbutton(ff, text="Replace the original instead (backup kept)", variable=self.rx_replace_orig).pack(side="left", padx=6)
        ttk.Button(ff, text="Run on file", style="Big.TButton", command=self.regex_on_file).pack(side="right")
        self.regex_live()

    def regex_live(self) -> None:
        self.rx_sample.tag_remove("hit", "1.0", "end")
        pat = self.rx_pat.get()
        if not pat:
            self.rx_status.config(text="")
            return self._rx_show("")
        try:
            out, spans = W.regex_apply(pat, self.rx_sample.get("1.0", "end-1c"), self.rx_mode.get(), self.rx_repl.get(),
                                       self.rx_ic.get(), self.rx_ml.get())
        except UserError as e:
            self.rx_status.config(text=str(e))
            return self._rx_show("")
        self.rx_status.config(text="")
        for a, b in spans[:2000]:
            self.rx_sample.tag_add("hit", f"1.0+{a}c", f"1.0+{b}c")
        self._rx_show(out if out else "(no result)")
        self.rx_status.config(text=f"{plural(len(spans), 'match', 'matches')}", foreground="#1e8449")

    def _rx_show(self, text: str) -> None:
        self.rx_result.configure(state="normal")
        self.rx_result.delete("1.0", "end")
        self.rx_result.insert("1.0", text)
        self.rx_result.configure(state="disabled")

    def regex_pick_file(self) -> None:
        c = filedialog.askopenfilename(parent=self.win, title="Choose a file")
        if c:
            self.rx_file.set(os.path.normpath(c))

    def regex_on_file(self) -> None:
        from r_extract import RESULT
        import r_more
        try:
            src = check_file(self.rx_file.get())
            text, _ = readers.read_document(src)
            out, spans = W.regex_apply(self.rx_pat.get(), text, self.rx_mode.get(), self.rx_repl.get(), self.rx_ic.get(), self.rx_ml.get())
            if not spans:
                return messagebox.showinfo("FileFixer", "The pattern matched nothing in that file.", parent=self.win)
            plan = Plan("Regex on file")
            v = {"result": RESULT[1] if self.rx_replace_orig.get() else RESULT[0], "outname": self.rx_out.get(), "outdir": ""}
            r_more.deliver(plan, src, out, v, f"{src.stem}Regex.txt", what="result")
            plan.summary = f"{plural(len(spans), 'match', 'matches')} found in {src.name}."
        except UserError as e:
            return self.err(e)
        self.apply_and_report(plan, "regex", f"Regex on {src.name}", {"pattern": self.rx_pat.get(), "mode": self.rx_mode.get(),
                                                                       "repl": self.rx_repl.get(), "file": str(src)})

    # ------------------------------------------------------------ 3. workflow
    def _workflow_tab(self) -> None:
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Workflow Builder")
        ttk.Label(f, text="Chain steps: each step works on the result of the one before it.", style="Help.TLabel").pack(anchor="w")
        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True, pady=6)
        self.step_list = tk.Listbox(mid, height=9, activestyle="dotbox", exportselection=False)
        self.step_list.pack(side="left", fill="both", expand=True)
        btns = ttk.Frame(mid)
        btns.pack(side="left", padx=8, anchor="n")
        for text, cmd in (("Add step...", self.step_add), ("Edit...", self.step_edit), ("Remove", self.step_remove),
                          ("Move up", lambda: self.step_move(-1)), ("Move down", lambda: self.step_move(1)), ("Clear all", self.step_clear)):
            ttk.Button(btns, text=text, command=cmd).pack(fill="x", pady=2)
        sv = ttk.Frame(f)
        sv.pack(fill="x")
        self.wf_name = tk.StringVar()
        ttk.Label(sv, text="Workflow name").pack(side="left")
        ttk.Entry(sv, textvariable=self.wf_name, width=22).pack(side="left", padx=6)
        ttk.Button(sv, text="Save", command=self.wf_save).pack(side="left")
        ttk.Label(sv, text="   Saved").pack(side="left")
        self.wf_combo = ttk.Combobox(sv, values=W.list_workflows(), width=22, state="readonly")
        self.wf_combo.pack(side="left", padx=6)
        ttk.Button(sv, text="Open", command=self.wf_open).pack(side="left")
        ttk.Button(sv, text="Delete", command=self.wf_delete).pack(side="left", padx=6)
        ttk.Label(f, text="Try it on sample text").pack(anchor="w", pady=(8, 0))
        frame, self.wf_sample = self.text_box(f, height=4)
        frame.pack(fill="both", expand=True)
        self.wf_sample.insert("1.0", "itm_apple regular_apple itm_banana\ncategory_fruit itm_orange")
        ttk.Button(f, text="Run on sample", command=self.wf_test).pack(anchor="w", pady=4)
        frame, self.wf_result = self.text_box(f, height=4)
        frame.pack(fill="both", expand=True)
        self.wf_result.configure(state="disabled")
        ff = ttk.Frame(f)
        ff.pack(fill="x", pady=(6, 0))
        self.wf_file, self.wf_out = tk.StringVar(), tk.StringVar()
        ttk.Button(ff, text="Run on a file...", command=lambda: self.wf_file.set(os.path.normpath(filedialog.askopenfilename(parent=self.win) or self.wf_file.get() or ""))).pack(side="left")
        ttk.Entry(ff, textvariable=self.wf_file, width=40).pack(side="left", padx=6)
        ttk.Label(ff, text="New file name").pack(side="left")
        ttk.Entry(ff, textvariable=self.wf_out, width=18).pack(side="left", padx=6)
        ttk.Button(ff, text="Run and save", style="Big.TButton", command=self.wf_run_file).pack(side="right")

    def refresh_steps(self, select: int = None) -> None:
        self.step_list.delete(0, "end")
        for i, s in enumerate(self.steps, 1):
            self.step_list.insert("end", f"{i}.  {W.describe(s)}")
        if select is not None and 0 <= select < len(self.steps):
            self.step_list.selection_set(select)

    def step_add(self) -> None:
        s = StepDialog(self.win, None).show()
        if s:
            self.steps.append(s)
            self.refresh_steps(len(self.steps) - 1)

    def step_edit(self) -> None:
        sel = self.step_list.curselection()
        if sel:
            s = StepDialog(self.win, self.steps[sel[0]]).show()
            if s:
                self.steps[sel[0]] = s
                self.refresh_steps(sel[0])

    def step_remove(self) -> None:
        sel = self.step_list.curselection()
        if sel:
            del self.steps[sel[0]]
            self.refresh_steps(min(sel[0], len(self.steps) - 1))

    def step_move(self, d: int) -> None:
        sel = self.step_list.curselection()
        if sel and 0 <= sel[0] + d < len(self.steps):
            i = sel[0]
            self.steps[i], self.steps[i + d] = self.steps[i + d], self.steps[i]
            self.refresh_steps(i + d)

    def step_clear(self) -> None:
        if self.steps and messagebox.askyesno("FileFixer", "Remove all steps?", parent=self.win):
            self.steps.clear()
            self.refresh_steps()

    def wf_save(self) -> None:
        try:
            if not self.steps:
                raise UserError("Add at least one step first.")
            W.save_workflow(self.wf_name.get(), self.steps)
        except UserError as e:
            return self.err(e)
        self.refresh_workflow_lists()
        messagebox.showinfo("FileFixer", "Saved.", parent=self.win)

    def refresh_workflow_lists(self) -> None:
        names = W.list_workflows()
        self.wf_combo.config(values=names)
        self.batch_wf_combo.config(values=["(the steps in the Workflow Builder)"] + names)

    def wf_open(self) -> None:
        try:
            self.steps = W.load_workflow(self.wf_combo.get())
        except UserError as e:
            return self.err(e)
        self.wf_name.set(self.wf_combo.get())
        self.refresh_steps()

    def wf_delete(self) -> None:
        if self.wf_combo.get() and messagebox.askyesno("FileFixer", f"Delete the saved workflow “{self.wf_combo.get()}”?", parent=self.win):
            W.delete_workflow(self.wf_combo.get())
            self.wf_combo.set("")
            self.refresh_workflow_lists()

    def wf_test(self) -> None:
        try:
            out, log = W.run_steps(self.steps, self.wf_sample.get("1.0", "end-1c"))
        except UserError as e:
            return self.err(e)
        self.wf_result.configure(state="normal")
        self.wf_result.delete("1.0", "end")
        self.wf_result.insert("1.0", out if out else "(empty result)")
        self.wf_result.configure(state="disabled")

    def wf_run_file(self) -> None:
        try:
            src = check_file(self.wf_file.get())
            text, _ = readers.read_document(src)
            out, log = W.run_steps(self.steps, text)
            if not out.strip():
                return messagebox.showinfo("FileFixer", "The workflow produced nothing for that file.", parent=self.win)
            plan = W.file_output_plan("Workflow", src, out.replace("\n", "\r\n") if os.name == "nt" else out, "", self.wf_out.get(), f"{src.stem}Result.txt")
            plan.summary = f"{plural(len(out.splitlines()), 'line')} will be saved."
        except UserError as e:
            return self.err(e)
        self.apply_and_report(plan, "workflow", f"Workflow on {src.name}", {"steps": self.steps, "file": str(src)})

    # ------------------------------------------------------------ 4. batch
    def _batch_tab(self) -> None:
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Batch Processing")
        ttk.Label(f, text="Apply the same workflow to many files. Each file gets its own new result file; originals are never changed.",
                  style="Help.TLabel", wraplength=900).pack(anchor="w")
        g = ttk.Frame(f)
        g.pack(fill="x", pady=8)
        g.columnconfigure(1, weight=1)
        self.batch_wf = tk.StringVar(value="(the steps in the Workflow Builder)")
        self.batch_out, self.batch_suffix = tk.StringVar(), tk.StringVar(value="_result")
        self.batch_types = tk.StringVar(value="txt, md, csv, log")
        ttk.Label(g, text="Workflow").grid(row=0, column=0, sticky="w", pady=3)
        self.batch_wf_combo = ttk.Combobox(g, textvariable=self.batch_wf, values=["(the steps in the Workflow Builder)"] + W.list_workflows(), state="readonly")
        self.batch_wf_combo.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(g, text="Files").grid(row=1, column=0, sticky="w", pady=3)
        fb = ttk.Frame(g)
        fb.grid(row=1, column=1, sticky="w", padx=6)
        ttk.Button(fb, text="Choose files...", command=self.batch_pick_files).pack(side="left")
        ttk.Button(fb, text="Add a whole folder...", command=self.batch_pick_folder).pack(side="left", padx=6)
        ttk.Label(fb, text="file types for folders:").pack(side="left")
        ttk.Entry(fb, textvariable=self.batch_types, width=18).pack(side="left", padx=4)
        ttk.Label(g, text="Save results in").grid(row=2, column=0, sticky="w", pady=3)
        ob = ttk.Frame(g)
        ob.grid(row=2, column=1, sticky="ew", padx=6)
        ob.columnconfigure(0, weight=1)
        ttk.Entry(ob, textvariable=self.batch_out).grid(row=0, column=0, sticky="ew")
        ttk.Button(ob, text="Browse...", command=lambda: self.batch_out.set(os.path.normpath(filedialog.askdirectory(parent=self.win) or self.batch_out.get() or "."))).grid(row=0, column=1, padx=6)
        ttk.Label(g, text="Add to each name").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(g, textvariable=self.batch_suffix, width=18).grid(row=3, column=1, sticky="w", padx=6)
        self.batch_label = ttk.Label(f, text="No files chosen yet.", style="Help.TLabel")
        self.batch_label.pack(anchor="w")
        bb = ttk.Frame(f)
        bb.pack(fill="x", pady=6)
        ttk.Button(bb, text="Preview", command=self.batch_preview).pack(side="left")
        ttk.Button(bb, text="Run batch", style="Big.TButton", command=self.batch_run).pack(side="left", padx=8)
        ttk.Button(bb, text="Clear files", command=lambda: (self.batch_files.clear(), self.batch_show())).pack(side="left")
        frame, self.batch_text = self.text_box(f, height=12)
        frame.pack(fill="both", expand=True)
        self.batch_text.configure(state="disabled")

    def batch_pick_files(self) -> None:
        c = filedialog.askopenfilenames(parent=self.win, title="Choose files")
        if c:
            self.batch_files += [p for p in map(os.path.normpath, c) if p not in self.batch_files]
            self.batch_show()

    def batch_pick_folder(self) -> None:
        d = filedialog.askdirectory(parent=self.win, title="Choose a folder", mustexist=True)
        if not d:
            return
        try:
            folder = check_folder(d)
        except UserError as e:
            return self.err(e)
        from engine import list_files, parse_exts
        found = list_files(folder, True, parse_exts(self.batch_types.get()))
        self.batch_files += [str(p) for p in found if str(p) not in self.batch_files]
        self.batch_show()

    def batch_show(self) -> None:
        n = len(self.batch_files)
        self.batch_label.config(text=f"{plural(n, 'file')} chosen." if n else "No files chosen yet.")

    def batch_steps(self) -> list:
        name = self.batch_wf.get()
        return self.steps if name.startswith("(") else W.load_workflow(name)

    def batch_plan(self) -> Plan:
        if not self.batch_files:
            raise UserError("Choose some files first.")
        if not self.batch_out.get().strip():
            raise UserError("Choose where to save the results.")
        return W.batch_plan(self.batch_steps(), self.batch_files, self.batch_out.get(), self.batch_suffix.get())

    def batch_preview(self) -> None:
        try:
            plan = self.batch_plan()
        except UserError as e:
            return self.err(e)
        lines = [plan.summary, ""] + plan.notes + [""] + [a.sentence for a in plan.actions[:300]]
        self.batch_text.configure(state="normal")
        self.batch_text.delete("1.0", "end")
        self.batch_text.insert("1.0", "\n".join(lines))
        self.batch_text.configure(state="disabled")

    def batch_run(self) -> None:
        try:
            plan = self.batch_plan()
            steps = self.batch_steps()
        except UserError as e:
            return self.err(e)
        self.apply_and_report(plan, "batch", f"Batch of {len(self.batch_files)} files",
                              {"steps": steps, "files": list(self.batch_files), "outdir": self.batch_out.get(),
                               "suffix": self.batch_suffix.get()})

    # ------------------------------------------------------------ 5. history
    def _history_tab(self) -> None:
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="Execution History")
        cols = ("when", "kind", "name", "status")
        self.hist = ttk.Treeview(f, columns=cols, show="headings", height=9, selectmode="browse")
        for c, t, w in (("when", "When", 140), ("kind", "Type", 110), ("name", "What", 360), ("status", "Result", 100)):
            self.hist.heading(c, text=t)
            self.hist.column(c, width=w, stretch=(c == "name"))
        self.hist.pack(fill="x")
        self.hist.bind("<<TreeviewSelect>>", self.hist_select)
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=6)
        ttk.Button(bar, text="Refresh", command=self.refresh_history).pack(side="left")
        self.rerun_btn = ttk.Button(bar, text="Load and re-run...", command=self.hist_rerun, state="disabled")
        self.rerun_btn.pack(side="left", padx=8)
        ttk.Label(bar, text="Scripts, workflows, batches and regex runs can be re-run. File changes are undone with “Undo last change”.",
                  style="Help.TLabel").pack(side="left")
        frame, self.hist_detail = self.text_box(f, height=14)
        frame.pack(fill="both", expand=True)
        self.hist_detail.configure(state="disabled")
        self.hist_rows: dict = {}
        self.refresh_history()

    def refresh_history(self) -> None:
        self.hist.delete(*self.hist.get_children())
        self.hist_rows = {}
        rows = []
        for r in W.load_runs():
            rows.append((r["time"], {"script": "Script", "workflow": "Workflow", "batch": "Batch", "regex": "Regex",
                                     "script-results": "Saved results"}.get(r["kind"], r["kind"]), r["name"],
                         "Succeeded" if r["status"] == "ok" else "Failed", r))
        for b in engine.load_history():
            rows.append((b["time"], "File change", f"{b['title']} - {len(b['records'])} changes", "Undone" if b.get("undone") else "Applied",
                         {"kind": "change", "detail": "\n".join(f"{x['op']}: {x['dst']}" for x in b["records"][:200]), "payload": None}))
        for when, kind, name, status, rec in sorted(rows, key=lambda x: x[0], reverse=True)[:150]:
            try:
                w = datetime.fromisoformat(when).strftime("%b %d, %H:%M")
            except Exception:
                w = when
            iid = self.hist.insert("", "end", values=(w, kind, name, status))
            self.hist_rows[iid] = rec

    def hist_select(self, _e=None) -> None:
        sel = self.hist.selection()
        rec = self.hist_rows.get(sel[0]) if sel else None
        self.hist_detail.configure(state="normal")
        self.hist_detail.delete("1.0", "end")
        if rec:
            p = rec.get("payload")
            extra = ""
            if isinstance(p, dict) and "code" in p:
                extra = "\n\n--- script ---\n" + p["code"]
            elif isinstance(p, dict) and "steps" in p:
                extra = "\n\n--- steps ---\n" + "\n".join(W.describe(s) for s in p["steps"])
            self.hist_detail.insert("1.0", (rec.get("detail") or "(no details)") + extra)
        self.hist_detail.configure(state="disabled")
        ok = bool(rec) and isinstance(rec.get("payload"), dict) and rec.get("kind") in ("script", "workflow", "batch", "regex")
        self.rerun_btn.config(state="normal" if ok else "disabled")

    def hist_rerun(self) -> None:
        sel = self.hist.selection()
        rec = self.hist_rows.get(sel[0]) if sel else None
        if not rec:
            return
        p, kind = rec["payload"], rec["kind"]
        if kind == "script":
            self.script_ed.delete("1.0", "end")
            self.script_ed.insert("1.0", p["code"])
            self.script_inputs = [x for x in p.get("inputs", []) if Path(x).is_file()]
            self.script_show_inputs()
            self.nb.select(0)
            messagebox.showinfo("FileFixer", "The script is loaded. Check it, then click “Run script”.", parent=self.win)
        elif kind == "regex":
            self.rx_pat.set(p["pattern"]); self.rx_repl.set(p["repl"]); self.rx_mode.set(p["mode"]); self.rx_file.set(p["file"])
            self.nb.select(1)
        elif kind == "workflow":
            self.steps = list(p["steps"]); self.wf_file.set(p.get("file", ""))
            self.refresh_steps(); self.nb.select(2)
        elif kind == "batch":
            self.steps = list(p["steps"]); self.refresh_steps()
            self.batch_files = [x for x in p["files"] if Path(x).is_file()]
            self.batch_out.set(p["outdir"]); self.batch_suffix.set(p["suffix"]); self.batch_wf.set("(the steps in the Workflow Builder)")
            self.batch_show(); self.nb.select(3)


class StepDialog:
    """Add or edit one workflow step."""

    def __init__(self, parent, step) -> None:
        self.result = None
        self.top = tk.Toplevel(parent)
        self.top.title("Workflow step")
        self.top.transient(parent)
        self.top.grab_set()
        self.top.geometry("560x420")
        self.labels = {v[0]: k for k, v in W.OPS.items()}
        self.op = tk.StringVar(value=W.OPS[step["op"]][0] if step else list(self.labels)[0])
        ttk.Label(self.top, text="What should this step do?", padding=(14, 12, 14, 4)).pack(anchor="w")
        cb = ttk.Combobox(self.top, textvariable=self.op, values=list(self.labels), state="readonly")
        cb.pack(fill="x", padx=14)
        cb.bind("<<ComboboxSelected>>", lambda e: self.build({}))
        self.form = ttk.Frame(self.top, padding=14)
        self.form.pack(fill="both", expand=True)
        self.vars: dict = {}
        self.build(step.get("params", {}) if step else {})
        bb = ttk.Frame(self.top, padding=14)
        bb.pack(fill="x")
        ttk.Button(bb, text="Cancel", command=self.top.destroy).pack(side="right")
        ttk.Button(bb, text="OK", style="Big.TButton", command=self.ok).pack(side="right", padx=8)

    def key(self) -> str:
        return self.labels[self.op.get()]

    def build(self, params: dict) -> None:
        for w in self.form.winfo_children():
            w.destroy()
        self.vars = {}
        self.form.columnconfigure(1, weight=1)
        for i, (k, kind, default, choices, label) in enumerate(W.OPS[self.key()][1]):
            val = params.get(k, default)
            if kind == "check":
                v = tk.BooleanVar(value=bool(val))
                ttk.Checkbutton(self.form, text=label, variable=v).grid(row=i, column=1, sticky="w", pady=4)
            else:
                v = tk.StringVar(value=str(val))
                ttk.Label(self.form, text=label).grid(row=i, column=0, sticky="w", padx=(0, 10), pady=4)
                w = ttk.Combobox(self.form, textvariable=v, values=choices, state="readonly") if kind == "choice" else ttk.Entry(self.form, textvariable=v)
                w.grid(row=i, column=1, sticky="ew", pady=4)
            self.vars[k] = v
        if not self.vars:
            ttk.Label(self.form, text="This step has no settings.", style="Help.TLabel").grid(row=0, column=0)

    def ok(self) -> None:
        self.result = {"op": self.key(), "params": {k: v.get() for k, v in self.vars.items()}}
        self.top.destroy()

    def show(self):
        self.top.wait_window()
        return self.result
