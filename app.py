"""FileFixer - point-and-click file and text-file tools.  Run:  python app.py"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from datetime import datetime
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import engine
import nl
import r_sync
import recipes
import registry
from engine import UserError

SIDEBAR_BG, SIDEBAR_FG, SIDEBAR_SEL, SIDEBAR_HOVER = "#17202a", "#ecf0f1", "#2e86c1", "#243342"
MAX_PREVIEW_ACTIONS = 400
SETTINGS_FILE = engine.APP_DIR / "settings.json"
APP_VERSION = "2.1"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_settings(s: dict) -> None:
    try:
        engine.write_text_atomic(SETTINGS_FILE, json.dumps(s))
    except OSError:
        pass


def open_in_explorer(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(str(path))                                   # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_path(p) -> None:
    p = Path(p)
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))                                  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except OSError as e:
        messagebox.showwarning("FileFixer", f"I couldn't open it:\n{p}\n\n{e}")


def wheel(widget, scroll_fn, everywhere: bool = False) -> None:
    """Make the mouse wheel call scroll_fn(units): -1 is up one step, 1 is down one step.

    Windows and macOS send <MouseWheel>; Linux (X11) sends Button-4 (up) and Button-5 (down).
    With everywhere=True the binding covers the whole app (bind_all), otherwise just this widget
    and the widgets inside it.
    """
    bind = widget.bind_all if everywhere else widget.bind
    bind("<MouseWheel>", lambda e: scroll_fn(-1 if e.delta > 0 else 1))
    bind("<Button-4>", lambda e: scroll_fn(-1))
    bind("<Button-5>", lambda e: scroll_fn(1))


class ReviewDialog:
    """Lets the user tick or untick each individual change before anything happens."""

    def __init__(self, parent, plan) -> None:
        self.plan, self.result = plan, None
        self.top = tk.Toplevel(parent)
        self.top.title("Review each change")
        self.top.geometry("820x560")
        self.top.transient(parent)
        self.top.grab_set()
        ttk.Label(self.top, text="Tick the changes you want. Unticked ones are skipped.",
                  padding=(16, 14, 16, 6)).pack(anchor="w")
        bar = ttk.Frame(self.top, padding=(16, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text="Select all", command=lambda: self.set_all(True)).pack(side="left")
        ttk.Button(bar, text="Select none", command=lambda: self.set_all(False)).pack(side="left", padx=8)
        self.count = ttk.Label(bar, text="")
        self.count.pack(side="right")
        box = ttk.Frame(self.top, padding=(16, 8))
        box.pack(fill="both", expand=True)
        canvas = tk.Canvas(box, highlightthickness=0)
        sb = ttk.Scrollbar(box, command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        # Wheel scrolling must NOT use bind_all: the binding would outlive this dialog
        # (calling yview_scroll on a destroyed canvas) and double-scroll the main window.
        # A binding on the Toplevel is in every child's bindtags and dies with the dialog.
        wheel(self.top, lambda units: canvas.yview_scroll(units, "units"))
        self.vars = []
        for a in plan.actions:
            v = tk.BooleanVar(value=True)
            v.trace_add("write", lambda *_: self.update_count())
            ttk.Checkbutton(inner, text=a.sentence, variable=v).pack(anchor="w", pady=1)
            self.vars.append(v)
        btns = ttk.Frame(self.top, padding=16)
        btns.pack(fill="x")
        ttk.Button(btns, text="Cancel", command=self.top.destroy).pack(side="right")
        ttk.Button(btns, text="Continue", style="Big.TButton", command=self.ok).pack(side="right", padx=8)
        self.update_count()

    def set_all(self, value: bool) -> None:
        for v in self.vars:
            v.set(value)

    def update_count(self) -> None:
        self.count.config(text=f"{sum(v.get() for v in self.vars)} of {len(self.vars)} selected")

    def ok(self) -> None:
        self.result = [a for a, v in zip(self.plan.actions, self.vars) if v.get()]
        if not self.result:
            messagebox.showinfo("FileFixer", "No changes are ticked.", parent=self.top)
            return
        self.top.destroy()

    def show(self):
        self.top.wait_window()
        return self.result


class TextBoxVar:
    """Makes a multi-line Text box look like a Tk variable (has .get())."""

    def __init__(self, widget: tk.Text) -> None:
        self.w = widget
        self.last = self.get()

    def get(self) -> str:
        return self.w.get("1.0", "end-1c")

    def set(self, text: str) -> None:
        self.w.delete("1.0", "end")
        self.w.insert("1.0", text)

    def changed(self) -> bool:
        now = self.get()
        was, self.last = self.last, now
        return now != was


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title(f"FileFixer {APP_VERSION}")
        screen_h = root.winfo_screenheight()
        height = min(860, screen_h - 90)
        root.geometry(f"1080x{height}")
        root.minsize(920, min(640, height))
        self.max_form_h = max(220, min(400, screen_h - 520))

        self.settings = load_settings()
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.advanced = None
        self.plan = None
        self.version = 0               # bumped whenever the form changes, so old previews can't be applied
        self.current = None
        self.vars: dict = {}
        self.side_buttons: dict = {}

        root.report_callback_exception = self._unexpected_error
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._style()
        self._build()
        wheel(root, self._scroll_under_pointer, everywhere=True)
        self.sel_files, self.sel_folders = [], []
        self.show_home()
        self.refresh_undo()
        self.poll()

    # ------------------------------------------------------- safety nets
    def _unexpected_error(self, exc, val, tb) -> None:
        """Any error inside a button handler: save the details and explain calmly instead of failing silently."""
        text = "".join(traceback.format_exception(exc, val, tb))
        log = engine.APP_DIR / "error.log"
        try:
            engine.APP_DIR.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\n{text}\n")
        except OSError:
            pass
        try:
            messagebox.showerror("Something went wrong",
                                 f"{val}\n\nNo harm done to your files. Technical details were saved to:\n{log}")
        except tk.TclError:
            pass

    def on_close(self) -> None:
        """Closing the window in the middle of work would leave a job half done, so stop it tidily first."""
        if self.busy:
            if not messagebox.askyesno("FileFixer is still working",
                                       "A job is still running. Stop it and close FileFixer?\n\n"
                                       "Changes already made can still be undone the next time you open it."):
                return
            engine.request_cancel()
            deadline = datetime.now().timestamp() + 8
            while self.busy_threads_alive() and datetime.now().timestamp() < deadline:
                self.root.update()
                self.root.after(50)
        w = getattr(self, "watcher", None)
        if w is not None:
            w.stop()
        try:
            r_sync.HashCache.flush()
        except Exception:                                        # noqa: BLE001
            pass
        self.root.destroy()

    def busy_threads_alive(self) -> bool:
        return any(t.name == "ff-worker" and t.is_alive() for t in threading.enumerate())

    # ------------------------------------------------------------------ look
    def _style(self) -> None:
        fam = "Segoe UI" if "Segoe UI" in tkfont.families() else tkfont.nametofont("TkDefaultFont").actual("family")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(name).configure(family=fam, size=10)
        self.f_title = tkfont.Font(family=fam, size=19, weight="bold")
        self.f_brand = tkfont.Font(family=fam, size=17, weight="bold")
        self.f_small = tkfont.Font(family=fam, size=9)
        self.f_bold = tkfont.Font(family=fam, size=11, weight="bold")
        style = ttk.Style()
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        style.configure("Big.TButton", padding=(16, 8), font=(fam, 10, "bold"))
        style.configure("Help.TLabel", foreground="#6b7785", font=self.f_small)

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        side = tk.Frame(self.root, bg=SIDEBAR_BG, width=250)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="FileFixer", bg=SIDEBAR_BG, fg="white", font=self.f_brand,
                 anchor="w").pack(fill="x", padx=20, pady=(22, 2))
        tk.Label(side, text="Pick a job", bg=SIDEBAR_BG, fg="#8fa3b5", anchor="w").pack(fill="x", padx=20, pady=(0, 14))
        bottom = tk.Frame(side, bg=SIDEBAR_BG)
        bottom.pack(side="bottom", fill="x")
        self.side_canvas = tk.Canvas(side, bg=SIDEBAR_BG, highlightthickness=0, bd=0)
        self.side_canvas.pack(side="top", fill="both", expand=True)
        lst = tk.Frame(self.side_canvas, bg=SIDEBAR_BG)
        win = self.side_canvas.create_window((0, 0), window=lst, anchor="nw")
        lst.bind("<Configure>", lambda e: self.side_canvas.configure(scrollregion=self.side_canvas.bbox("all")))
        self.side_canvas.bind("<Configure>", lambda e: self.side_canvas.itemconfigure(win, width=e.width))
        hb = tk.Button(lst, text="⌂  Home", anchor="w", relief="flat", bd=0, padx=20, pady=5, bg=SIDEBAR_BG,
                       fg=SIDEBAR_FG, activebackground=SIDEBAR_HOVER, activeforeground="white", cursor="hand2",
                       command=self.show_home)
        hb.pack(fill="x")
        self.side_buttons["home"] = hb
        for group, items in registry.GROUPS:
            tk.Label(lst, text=group.upper(), bg=SIDEBAR_BG, fg="#6f8397", font=self.f_small,
                     anchor="w").pack(fill="x", padx=20, pady=(10, 1))
            for r in items:
                b = tk.Button(lst, text=r.name, anchor="w", relief="flat", bd=0, padx=20, pady=5,
                              bg=SIDEBAR_BG, fg=SIDEBAR_FG, activebackground=SIDEBAR_HOVER,
                              activeforeground="white", cursor="hand2", takefocus=True,
                              command=lambda r=r: self.select(r))
                b.pack(fill="x")
                self.side_buttons[r.key] = b

        tk.Button(bottom, text="⚙  Advanced Tools", anchor="w", relief="flat", bd=0, padx=20, pady=8, bg="#1f2d3b",
                  fg=SIDEBAR_FG, activebackground=SIDEBAR_HOVER, activeforeground="white", cursor="hand2",
                  command=self.open_advanced).pack(fill="x", padx=14, pady=(8, 6))
        self.undo_btn = tk.Button(bottom, text="Undo last change", anchor="w", relief="flat", bd=0, padx=20, pady=10,
                                  bg="#34495e", fg="white", activebackground="#415b76", activeforeground="white",
                                  disabledforeground="#7f8c8d", cursor="hand2", command=self.on_undo)
        self.undo_btn.pack(fill="x", padx=14, pady=(0, 6))
        self.watch_label = tk.Label(bottom, text="", bg=SIDEBAR_BG, fg="#58d68d", font=self.f_small, anchor="w")
        self.watch_label.pack(fill="x", padx=20, pady=(0, 4))
        self.undo_label = tk.Label(bottom, text="", bg=SIDEBAR_BG, fg="#8fa3b5", font=self.f_small,
                                   wraplength=215, justify="left", anchor="w")
        self.undo_label.pack(fill="x", padx=20, pady=(0, 10))
        tk.Button(bottom, text="Open the Recycle folder", anchor="w", relief="flat", bd=0, padx=20, pady=8,
                  bg=SIDEBAR_BG, fg="#8fa3b5", activebackground=SIDEBAR_HOVER, activeforeground="white",
                  cursor="hand2", command=lambda: open_in_explorer(engine.RECYCLE_DIR)).pack(fill="x")
        self.recycle_label = tk.Label(bottom, text="", bg=SIDEBAR_BG, fg="#8fa3b5", font=self.f_small,
                                      wraplength=215, justify="left", anchor="w")
        self.recycle_label.pack(fill="x", padx=20)
        self.cleanup_btn = tk.Button(bottom, text="Clean up old backups...", anchor="w", relief="flat", bd=0, padx=20,
                                     pady=6, bg=SIDEBAR_BG, fg="#8fa3b5", activebackground=SIDEBAR_HOVER,
                                     activeforeground="white", disabledforeground="#586775", cursor="hand2",
                                     command=self.on_cleanup)
        self.cleanup_btn.pack(fill="x", pady=(0, 14))

        main = self.tool_frame = ttk.Frame(self.root, padding=(28, 22, 28, 12))
        self.title = ttk.Label(main, font=self.f_title)
        self.title.pack(anchor="w")
        self.blurb = ttk.Label(main, wraplength=700, justify="left")
        self.blurb.pack(anchor="w", pady=(4, 6), fill="x")
        self.understand = ttk.Label(main, wraplength=700, justify="left", foreground="#1f618d", font=self.f_bold)
        main.bind("<Configure>", lambda e: self.blurb.config(wraplength=max(300, e.width - 60)))

        # The form sits in a scrollable area so long forms (like the delivery package) fit small screens.
        holder = ttk.Frame(main)
        holder.pack(fill="x", pady=(4, 6))
        canvas_bg = ttk.Style().lookup("TFrame", "background") or self.root.cget("bg")
        self.fcanvas = tk.Canvas(holder, highlightthickness=0, borderwidth=0, height=10, bg=canvas_bg)
        fscroll = ttk.Scrollbar(holder, orient="vertical", command=self.fcanvas.yview)
        self.fcanvas.configure(yscrollcommand=fscroll.set)
        fscroll.pack(side="right", fill="y")
        self.fcanvas.pack(side="left", fill="x", expand=True)
        self.form = ttk.Frame(self.fcanvas)
        self.fcanvas.create_window((0, 0), window=self.form, anchor="nw", tags="form")
        self.form.columnconfigure(1, weight=1)
        self.form.bind("<Configure>", self._form_resized)
        self.fcanvas.bind("<Configure>", lambda e: self.fcanvas.itemconfigure("form", width=e.width))

        bar = ttk.Frame(main)
        bar.pack(fill="x", pady=(8, 6))
        self.preview_btn = ttk.Button(bar, text="1.  Show me what will happen", style="Big.TButton", command=self.on_preview)
        self.preview_btn.pack(side="left")
        self.do_btn = ttk.Button(bar, text="2.  Do it", style="Big.TButton", command=self.on_do, state="disabled")
        self.do_btn.pack(side="left", padx=(10, 0))
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=engine.request_cancel)
        self.pb = ttk.Progressbar(bar, length=180, mode="determinate")
        self.pb.pack(side="right")
        self.status = ttk.Label(bar, text="", style="Help.TLabel")
        self.status.pack(side="right", padx=10)

        self.summary = ttk.Label(main, text="", font=self.f_bold, wraplength=700, justify="left")
        self.summary.pack(anchor="w", pady=(6, 4), fill="x")
        box = ttk.Frame(main)
        box.pack(fill="both", expand=True)
        self.out = tk.Text(box, wrap="word", state="disabled", relief="solid", bd=1, padx=12, pady=10,
                           font=tkfont.nametofont("TkFixedFont"), bg="#fbfcfd", height=10)
        sb = ttk.Scrollbar(box, command=self.out.yview)
        self.out.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.out.pack(side="left", fill="both", expand=True)
        self.out.tag_configure("note", foreground="#6b7785")
        self.out.tag_configure("detail", foreground="#6b7785", lmargin1=36, lmargin2=36)
        self.out.tag_configure("good", foreground="#1e8449")
        self.out.tag_configure("bad", foreground="#b03a2e")
        self.out.tag_configure("head", font=self.f_bold)
        self._build_home()

    # ------------------------------------------------------------------ views
    def show_view(self, name: str) -> None:
        for f in (self.home_frame, self.tool_frame):
            f.pack_forget()
        (self.home_frame if name == "home" else self.tool_frame).pack(side="left", fill="both", expand=True)

    def set_understand(self, text: str) -> None:
        self.understand.pack_forget()
        if text:
            self.understand.config(text=text)
            self.understand.pack(anchor="w", pady=(0, 6), fill="x", after=self.blurb)

    def open_advanced(self) -> None:
        import advanced
        if self.advanced is None:
            self.advanced = advanced.AdvancedWindow(self)
        self.advanced.show()

    def show_home(self) -> None:
        if self.busy:
            return
        self.show_view("home")
        for key, b in self.side_buttons.items():
            b.config(bg=SIDEBAR_SEL if key == "home" else SIDEBAR_BG)
        self.refresh_activity()

    def _build_home(self) -> None:
        fam = self.f_small.actual("family")
        f = self.home_frame = ttk.Frame(self.root, padding=(28, 22, 28, 12))
        ttk.Label(f, text="What would you like to do with your files?", font=self.f_title).pack(anchor="w")
        ttk.Label(f, text="Describe it in your own words. FileFixer shows you exactly what will happen before it changes anything.",
                  style="Help.TLabel").pack(anchor="w", pady=(2, 10))
        self.placeholder = "Describe a file operation, such as finding all files containing a specific word..."
        self.cmd = tk.Text(f, height=3, wrap="word", relief="solid", bd=1, padx=10, pady=8, font=(fam, 11), fg="#6b7785")
        self.cmd.insert("1.0", self.placeholder)
        self.cmd.pack(fill="x")
        self.cmd.bind("<FocusIn>", self._cmd_focus_in)
        self.cmd.bind("<FocusOut>", self._cmd_focus_out)
        self.cmd.bind("<Control-Return>", lambda e: (self.on_command(), "break")[1])
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(8, 2))
        ttk.Button(row, text="Choose files...", command=self.pick_files).pack(side="left")
        ttk.Button(row, text="Choose folders...", command=self.pick_folders).pack(side="left", padx=6)
        ttk.Button(row, text="Clear", command=self.clear_selection).pack(side="left")
        ttk.Button(row, text="Go  ▸", style="Big.TButton", command=self.on_command).pack(side="right")
        self.sel_label = ttk.Label(f, text="Nothing chosen yet. Choose the files or folders your request is about.",
                                   style="Help.TLabel", wraplength=760, justify="left")
        self.sel_label.pack(anchor="w", pady=(0, 4), fill="x")
        self.cmd_msg = ttk.Label(f, text="", wraplength=760, justify="left", foreground="#b03a2e")
        self.cmd_msg.pack(anchor="w", fill="x")

        ttk.Label(f, text="Try one of these", font=self.f_bold).pack(anchor="w", pady=(10, 4))
        chips = ttk.Frame(f)
        chips.pack(fill="x")
        for i, s in enumerate(nl.SUGGESTIONS[:4]):
            b = tk.Button(chips, text=s if len(s) < 52 else s[:49] + "...", relief="flat", bd=0, bg="#e8f1f8", fg="#1f4e79",
                          activebackground="#d2e4f2", cursor="hand2", padx=10, pady=5, anchor="w",
                          command=lambda s=s: self.use_suggestion(s))
            b.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0, 8), pady=3)
            chips.columnconfigure(i % 2, weight=1)

        ttk.Label(f, text="Quick actions", font=self.f_bold).pack(anchor="w", pady=(12, 4))
        grid = ttk.Frame(f)
        grid.pack(fill="x")
        quick = [("🔍", "Find Files", "find"), ("⇄", "Compare Folders", "compare"), ("⟳", "Sync Folders", "sync"),
                 ("✂", "Extract Text", "extract"), ("✎", "Batch Rename", "rename"), ("⧉", "Find Duplicates", "duplicates"),
                 ("▦", "Organize Files", "organize"), ("☷", "Process Data", "csv")]
        for i, (icon, label, key) in enumerate(quick):
            b = tk.Button(grid, text=f"{icon}\n{label}", relief="flat", bd=0, bg="#f4f6f8", activebackground="#e3e9ee",
                          cursor="hand2", pady=10, command=lambda k=key: self.select(registry.get(k)))
            b.grid(row=i // 4, column=i % 4, sticky="ew", padx=4, pady=4)
            grid.columnconfigure(i % 4, weight=1, uniform="q")

        ttk.Label(f, text="Recent activity", font=self.f_bold).pack(anchor="w", pady=(12, 4))
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True)
        self.activity = ttk.Treeview(box, columns=("status", "when", "what"), show="headings", height=6, selectmode="browse")
        for col, text, w in (("status", "", 90), ("when", "When", 120), ("what", "What happened", 560)):
            self.activity.heading(col, text=text)
            self.activity.column(col, width=w, stretch=(col == "what"))
        sb = ttk.Scrollbar(box, command=self.activity.yview)
        self.activity.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.activity.pack(side="left", fill="both", expand=True)
        self.activity.bind("<Double-1>", self.open_activity)
        self.activity.bind("<Return>", self.open_activity)
        self.activity_paths: dict = {}
        self.activity_empty = ttk.Label(f, text="Nothing yet. Your finished, failed and waiting tasks will show up here.",
                                        style="Help.TLabel")

    def _cmd_focus_in(self, _e=None) -> None:
        if self.cmd.get("1.0", "end-1c") == self.placeholder:
            self.cmd.delete("1.0", "end")
            self.cmd.config(fg="#000000")

    def _cmd_focus_out(self, _e=None) -> None:
        if not self.cmd.get("1.0", "end-1c").strip():
            self.cmd.delete("1.0", "end")
            self.cmd.insert("1.0", self.placeholder)
            self.cmd.config(fg="#6b7785")

    def command_text(self) -> str:
        t = self.cmd.get("1.0", "end-1c").strip()
        return "" if t == self.placeholder else t

    def use_suggestion(self, s: str) -> None:
        self._cmd_focus_in()
        self.cmd.delete("1.0", "end")
        self.cmd.insert("1.0", s)
        self.cmd.config(fg="#000000")
        self.cmd_msg.config(text="")

    def pick_files(self) -> None:
        chosen = filedialog.askopenfilenames(initialdir=self.settings.get("last_folder") or str(Path.home()), title="Choose files")
        if chosen:
            self.sel_files = [os.path.normpath(c) for c in chosen]
            self.settings["last_folder"] = str(Path(chosen[0]).parent)
            self.update_selection()

    def pick_folders(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.settings.get("last_folder") or str(Path.home()),
                                         title="Choose a folder (use the button again to add more)", mustexist=True)
        if chosen:
            p = os.path.normpath(chosen)
            if p not in self.sel_folders:
                self.sel_folders.append(p)
            self.settings["last_folder"] = chosen
            self.update_selection()

    def clear_selection(self) -> None:
        self.sel_files, self.sel_folders = [], []
        self.update_selection()

    def update_selection(self) -> None:
        parts = [f"File: {p}" for p in self.sel_files[:3]] + [f"Folder: {p}" for p in self.sel_folders[:4]]
        more = len(self.sel_files) + len(self.sel_folders) - len(parts)
        self.sel_label.config(text="\n".join(parts) + (f"\n... and {more} more" if more > 0 else "") if parts else
                              "Nothing chosen yet. Choose the files or folders your request is about.")

    def on_command(self) -> None:
        text = self.command_text()
        self.cmd_msg.config(text="")
        if not text:
            self.cmd_msg.config(text="Type what you want to do first, or pick one of the examples below.")
            return
        if self.busy:
            return
        files, folders = list(self.sel_files), list(self.sel_folders)
        # Understanding a request may look through folders, so it never runs on the window's own thread.
        self.run_bg(lambda: nl.interpret(text, files, folders), self.after_command, "Reading your request...")

    def after_command(self, intent) -> None:
        if intent is None:
            self.cmd_msg.config(text=nl.HELP)
            return
        recipe = registry.get(intent.key)
        self.select(recipe)
        for k, v in intent.values.items():
            var = self.vars.get(k)
            if var is None:
                continue
            if isinstance(var, TextBoxVar):
                var.set(str(v))
            else:
                var.set(v)
        msg = intent.explain
        if intent.notes:
            msg += "\n" + "\n".join(intent.notes)
        if intent.missing:
            msg += "\nStill needed: " + "; ".join(intent.missing)
        self.set_understand("Here is what I understood:\n" + msg)
        if not intent.missing and not getattr(recipe, "live", False):
            self.on_preview()

    # -------------------------------------------------------------- activity
    def refresh_activity(self) -> None:
        self.activity.delete(*self.activity.get_children())
        self.activity_paths = {}
        icons = {"done": "✔ Done", "failed": "✖ Failed", "pending": "⏳ Waiting", "undone": "↶ Undone", "info": "•"}
        w = getattr(self, "watcher", None)
        rows = []
        if w is not None and getattr(w, "pending", None):
            rows.append(("pending", "now", f"{len(w.pending.actions)} synchronization changes are waiting for your approval", ""))
        for it in reversed(engine.load_activity()[-40:]):
            try:
                when = datetime.fromisoformat(it["time"]).strftime("%b %d, %H:%M")
            except Exception:
                when = it.get("time", "")
            detail = f" - {it['detail']}" if it.get("detail") else ""
            rows.append((it.get("status", "info"), when, f"{it.get('title', '')}{detail}"[:300], it.get("path", "")))
        for st, when, what, path in rows[:30]:
            iid = self.activity.insert("", "end", values=(icons.get(st, "•"), when, what))
            if path:
                self.activity_paths[iid] = path
            elif st == "pending":
                self.activity_paths[iid] = "watch"
        if rows:
            self.activity_empty.pack_forget()
        else:
            self.activity_empty.pack(anchor="w")

    def open_activity(self, _e=None) -> None:
        sel = self.activity.selection()
        if not sel:
            return
        p = self.activity_paths.get(sel[0])
        if p == "watch":
            self.select(registry.get("watch"))
        elif p and Path(p).exists():
            open_path(p)

    def _form_resized(self, _event=None) -> None:
        self.fcanvas.configure(scrollregion=self.fcanvas.bbox("all"),
                               height=min(self.form.winfo_reqheight(), self.max_form_h))

    def _scroll_under_pointer(self, units: int) -> None:
        """Mouse wheel scrolls the form when the pointer is over it (but not over a text box)."""
        try:
            w = self.root.winfo_containing(*self.root.winfo_pointerxy())
        except (KeyError, tk.TclError):
            return
        if isinstance(w, tk.Text):
            return
        while w is not None:
            if w is self.fcanvas or w is self.side_canvas:
                w.yview_scroll(units, "units")
                return
            w = w.master

    # ------------------------------------------------------------------ form
    def select(self, r) -> None:
        if self.busy:
            return
        self.show_view("tool")
        self.set_understand("")
        self.current, self.plan = r, None
        live = getattr(r, "live", False)
        self.preview_btn.config(text="Start watching" if live else "1.  Show me what will happen")
        self.do_btn.config(text="Stop watching" if live else "2.  Do it")
        self.show_review_button()
        self.version += 1
        for key, b in self.side_buttons.items():
            b.config(bg=SIDEBAR_SEL if key == r.key else SIDEBAR_BG)
        for w in self.form.winfo_children():
            w.destroy()
        self.vars = {}
        self.fcanvas.yview_moveto(0)
        self.title.config(text=r.name)
        self.blurb.config(text=r.blurb)
        self.summary.config(text="")
        self.set_output([("Fill in the form above, then click “Start watching”. Messages appear here.\n", "note")]
                        if live else
                        [("Fill in the form above, then click “Show me what will happen”.\n"
                          "Nothing changes on your computer until you click “Do it”.", "note")])
        self.do_btn.config(state="normal" if live and self.watcher_running() else "disabled")
        if live:
            self.show_watch_log()

        row = 0
        for f in r.fields:
            if f.kind == "check":
                var = tk.BooleanVar(value=bool(f.default))
                ttk.Checkbutton(self.form, text=f.label, variable=var).grid(row=row, column=1, sticky="w", pady=(8, 0))
            elif f.kind == "folderlist":
                ttk.Label(self.form, text=f.label).grid(row=row, column=0, sticky="nw", padx=(0, 14), pady=(10, 0))
                box = tk.Text(self.form, height=f.height, wrap="none", relief="solid", bd=1, padx=6, pady=4,
                              font=tkfont.nametofont("TkFixedFont"), undo=True)
                box.insert("1.0", str(f.default))
                box.grid(row=row, column=1, sticky="ew", pady=(8, 0))
                var = TextBoxVar(box)
                box.bind("<KeyRelease>", lambda e, v=var: self.invalidate() if v.changed() else None)
                ttk.Button(self.form, text="Add a folder...", command=lambda b=box, v=var: self.add_folder(b, v)).grid(
                    row=row, column=2, padx=(8, 0), pady=(8, 0), sticky="n")
            elif f.kind == "multiline":
                ttk.Label(self.form, text=f.label).grid(row=row, column=0, sticky="nw", padx=(0, 14), pady=(10, 0))
                box = tk.Text(self.form, height=f.height, wrap="none", relief="solid", bd=1, padx=6, pady=4,
                              font=tkfont.nametofont("TkFixedFont"), undo=True)
                box.insert("1.0", str(f.default))
                box.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(8, 0))
                var = TextBoxVar(box)
                box.bind("<KeyRelease>", lambda e, v=var: self.invalidate() if v.changed() else None)
            else:
                default = f.default
                if f.kind == "folder" and f.key == "folder":
                    default = self.settings.get("last_folder", "")
                var = tk.StringVar(value=str(default))
                ttk.Label(self.form, text=f.label).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=(8, 0))
                if f.kind == "choice":
                    w = ttk.Combobox(self.form, textvariable=var, values=f.choices, state="readonly")
                else:
                    w = ttk.Entry(self.form, textvariable=var)
                w.grid(row=row, column=1, sticky="ew", pady=(8, 0))
                if f.kind in ("folder", "file"):
                    pick = self.browse_file if f.kind == "file" else self.browse
                    ttk.Button(self.form, text="Browse...", command=lambda v=var, p=pick: p(v)).grid(
                        row=row, column=2, padx=(8, 0), pady=(8, 0))
            if hasattr(var, "trace_add"):
                var.trace_add("write", self.invalidate)
            self.vars[f.key] = var
            row += 1
            if f.help:
                ttk.Label(self.form, text=f.help, style="Help.TLabel", wraplength=430, justify="left").grid(
                    row=row, column=1, sticky="w")
                row += 1

    def browse(self, var: tk.StringVar) -> None:
        start = var.get() if Path(var.get() or "?").is_dir() else self.settings.get("last_folder") or str(Path.home())
        chosen = filedialog.askdirectory(initialdir=start, title="Choose a folder", mustexist=True)
        if chosen:
            var.set(os.path.normpath(chosen))

    def add_folder(self, box: tk.Text, var) -> None:
        chosen = filedialog.askdirectory(initialdir=self.settings.get("last_folder") or str(Path.home()),
                                         title="Add a folder", mustexist=True)
        if chosen:
            cur = box.get("1.0", "end-1c")
            box.insert("end", ("\n" if cur.strip() else "") + os.path.normpath(chosen))
            self.settings["last_folder"] = chosen
            if var.changed():
                self.invalidate()

    def browse_file(self, var: tk.StringVar) -> None:
        cur = Path(var.get()).parent if var.get().strip() else None
        start = str(cur) if cur and cur.is_dir() else self.settings.get("last_folder") or str(Path.home())
        chosen = filedialog.askopenfilename(initialdir=start, title="Choose a file")
        if chosen:
            var.set(os.path.normpath(chosen))
            self.settings["last_folder"] = str(Path(chosen).parent)

    def values(self) -> dict:
        return {k: v.get() for k, v in self.vars.items()}

    def invalidate(self, *_) -> None:
        self.version += 1
        if self.plan is not None:
            self.plan = None
            self.do_btn.config(state="disabled")
            self.summary.config(text="You changed something - click “Show me what will happen” again.")

    # ---------------------------------------------------------------- output
    def set_output(self, chunks: list) -> None:
        self.out.config(state="normal")
        self.out.delete("1.0", "end")
        for text, tag in chunks:
            self.out.insert("end", text, tag or ())
        self.out.config(state="disabled")
        self.out.yview_moveto(0)

    # --------------------------------------------------------- background work
    def set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.preview_btn.config(state=state)
        self.do_btn.config(state="disabled" if busy or self.plan is None or not self.plan.actions else "normal")
        self.undo_btn.config(state="disabled" if busy or not engine.last_undoable() else "normal")
        self.cleanup_btn.config(state=state)
        self.status.config(text=text)
        if busy:
            self.cancel_btn.pack(side="right", padx=(0, 8))
        else:
            self.cancel_btn.pack_forget()
        self.pb.stop()
        self.pb.config(mode="indeterminate" if busy else "determinate", value=0)
        if busy:
            self.pb.start(14)

    def run_bg(self, fn, done, status: str) -> None:
        engine.reset_cancel()
        self.set_busy(True, status)

        def work():
            try:
                res = ("ok", fn())
            except BaseException as e:                            # noqa: BLE001
                res = ("err", e, traceback.format_exc())
            self.q.put(("done", done, res))

        threading.Thread(target=work, daemon=True, name="ff-worker").start()

    def report_progress(self, i: int, total: int) -> None:
        self.q.put(("progress", i, total))

    def poll(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "watchlog":
                    stamp = datetime.now().strftime("%H:%M:%S")
                    self.watch_lines.append(f"{stamp}  {msg[1]}")
                    if msg[1].startswith("Applied"):
                        engine.log_activity("done", "Automatic sync", msg[1], "", "watch")
                    elif "Needs your decision" in msg[1]:
                        engine.log_activity("pending", "Sync conflict", msg[1][:200], "", "watch")
                    if getattr(self.current, "live", False):
                        self.show_watch_log()
                    self.watch_state()
                    self.refresh_undo()
                elif msg[0] == "watchpending":
                    self.show_review_button()
                    self.root.bell()
                elif msg[0] == "recycle":
                    self._recycle_scanning = False
                    self.recycle_label.config(text=(
                        f"Recycle folder: {recipes.plural(msg[1], 'batch', 'batches')}, "
                        f"{engine.size_text(msg[2])} on disk" if msg[1] else "Recycle folder: empty"))
                elif msg[0] == "progress":
                    _, i, total = msg
                    if str(self.pb.cget("mode")) != "determinate":
                        self.pb.stop()
                        self.pb.config(mode="determinate")
                    self.pb.config(maximum=max(total, 1), value=i)
                    self.status.config(text=f"{i} of {total}")
                else:
                    _, done, res = msg
                    self.set_busy(False)
                    self.finish(done, res)
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def finish(self, done, res) -> None:
        if res[0] == "err":
            e = res[1]
            if isinstance(e, engine.Cancelled):
                self.summary.config(text="Cancelled. Nothing was changed.")
                return
            if isinstance(e, UserError):
                messagebox.showwarning("FileFixer", str(e))
            else:
                log = engine.APP_DIR / "error.log"
                try:
                    engine.APP_DIR.mkdir(parents=True, exist_ok=True)
                    with open(log, "a", encoding="utf-8") as fh:
                        fh.write(res[2] + "\n")
                except OSError:
                    pass
                engine.log_activity("failed", getattr(self.current, "name", "Task"), str(e))
                messagebox.showerror("Something went wrong",
                                     f"{e}\n\nNo harm done to your other files. Technical details were saved to:\n{log}")
            return
        done(res[1])

    # --------------------------------------------------------------- actions
    # ------------------------------------------------------------- watching
    def watcher_running(self) -> bool:
        return bool(getattr(self, "watcher", None) and self.watcher.running)

    def show_watch_log(self) -> None:
        if getattr(self, "watch_lines", None):
            self.set_output([("\n".join(self.watch_lines[-200:]) + "\n", None)])
            self.out.see("end")

    def start_watch(self) -> None:
        if self.watcher_running():
            messagebox.showinfo("FileFixer", "Already watching. Click “Stop watching” first to change the settings.")
            return
        vals = self.values()
        try:
            planner = r_sync.SyncPlanner(vals)
            planner.references()
            planner.roots()
        except UserError as e:
            messagebox.showwarning("FileFixer", str(e))
            return
        self.watch_lines = []
        auto = vals["auto"] == r_sync.WATCH_ACTION[1]
        if auto and messagebox.askyesno("Apply automatically?",
                                        "Changes will be applied without asking each time. Replaced files are backed up "
                                        "and you can undo.\n\nStart watching in automatic mode?") is False:
            return
        self.watcher = r_sync.SyncWatcher(vals, r_sync.WATCH_INTERVALS[vals["interval"]], auto,
                                          lambda t: self.q.put(("watchlog", t)),
                                          lambda plan: self.q.put(("watchpending", plan)))
        self.watcher.start()
        self.do_btn.config(state="normal")
        self.summary.config(text="Watching your folders...")
        self.watch_state()

    def stop_watch(self) -> None:
        if self.watcher_running():
            self.watcher.stop()
        self.do_btn.config(state="disabled")
        self.summary.config(text="Stopped watching.")
        self.root.after(300, self.watch_state)

    def watch_state(self) -> None:
        on = self.watcher_running()
        self.watch_label.config(text="● Watching folders" if on else "")

    def show_review_button(self) -> None:
        w = getattr(self, "watcher", None)
        pend = getattr(w, "pending", None) if w else None
        show = bool(pend) and getattr(self.current, "live", False)
        if show and not getattr(self, "review_btn", None):
            self.review_btn = ttk.Button(self.status.master, text="Review waiting changes", command=self.review_pending)
            self.review_btn.pack(side="right", padx=(0, 10))
        elif not show and getattr(self, "review_btn", None):
            self.review_btn.destroy()
            self.review_btn = None

    def review_pending(self) -> None:
        w = getattr(self, "watcher", None)
        if not w or not w.pending:
            return
        from dataclasses import replace
        chosen = ReviewDialog(self.root, w.pending).show()
        if chosen is None:
            return
        plan = replace(w.pending, actions=chosen)
        self.run_bg(lambda: w.apply(plan), lambda res: (self.after_apply(res), self.show_review_button()), "Applying...")

    def on_preview(self) -> None:
        if getattr(self.current, "live", False):
            return self.start_watch()
        if self.busy:
            return
        r, vals, ver = self.current, self.values(), self.version
        self.plan = None
        self.summary.config(text="")

        def done(plan):
            if ver != self.version:                               # form changed while we were looking
                self.summary.config(text="You changed something - click “Show me what will happen” again.")
                return
            self.plan = plan
            self.settings["last_folder"] = vals.get("folder", "")
            save_settings(self.settings)
            self.show_plan(plan)

        self.run_bg(lambda: r.plan(vals), done, "Looking at your files...")

    def show_plan(self, plan) -> None:
        chunks = []
        for n in plan.notes:
            chunks.append((n + "\n", "note"))
        if plan.notes:
            chunks.append(("\n", None))
        for line in plan.report:
            chunks.append((line + "\n", None))
        if plan.report:
            chunks.append(("\n", None))
        for a in plan.actions[:MAX_PREVIEW_ACTIONS]:
            chunks.append((a.sentence + "\n", None))
            if a.detail:
                chunks.append((a.detail + "\n", "detail"))
        extra = len(plan.actions) - MAX_PREVIEW_ACTIONS
        if extra > 0:
            chunks.append((f"\n... and {extra} more (they will all be done).\n", "note"))
        self.summary.config(text=plan.summary)
        self.set_output(chunks or [("Nothing to do.", "note")])
        self.do_btn.config(state="normal" if plan.actions else "disabled")

    def on_do(self) -> None:
        if getattr(self.current, "live", False):
            return self.stop_watch()
        if self.busy or not self.plan or not self.plan.actions:
            return
        plan, n = self.plan, len(self.plan.actions)
        if plan.selectable:
            chosen = ReviewDialog(self.root, plan).show()
            if chosen is None:
                return
            if not chosen:
                self.summary.config(text="No changes were ticked, so nothing was done.")
                return
            from dataclasses import replace
            plan = replace(plan, actions=chosen, summary=f"{recipes.plural(len(chosen), 'change')} you approved will be made.")
        if not messagebox.askyesno(
                "Ready to go?",
                f"{plan.title}\n\n{plan.summary}\n\nNothing is permanently deleted, and you can undo this "
                f"afterwards with “Undo last change”.\n\nGo ahead?"):
            return
        self.plan = None
        self.do_btn.config(state="disabled")
        self.run_bg(lambda: engine.apply_plan(plan, self.report_progress), self.after_apply, "Working...")

    def after_apply(self, res: dict) -> None:
        name = getattr(self.current, "name", "Task")
        failed = bool(res["problems"]) and not res["done"]
        engine.log_activity("failed" if failed else "done", name,
                            f"{recipes.plural(res['done'], 'change')} made" +
                            (f", {len(res['problems'])} problem(s): {res['problems'][0]}" if res["problems"] else ""),
                            res.get("open_path", ""), getattr(self.current, "key", ""))
        chunks = [(f"Done!  {recipes.plural(res['done'], 'change')} made.\n", "good")]
        if res["done"]:
            chunks.append(("\nTo reverse this, click “Undo last change” on the left.\n", None))
            if res["recycle"]:
                chunks.append((f"Replaced or removed files are kept here:\n{res['recycle']}\n", "note"))
        if res["problems"]:
            chunks.append((f"\n{recipes.plural(len(res['problems']), 'file')} could not be changed:\n", "bad"))
            chunks += [(f"  {p}\n", None) for p in res["problems"][:100]]
        self.summary.config(text="Finished.")
        self.set_output(chunks)
        self.refresh_undo()
        self.show_open_button(res.get("open_path", ""))

    def show_open_button(self, path: str) -> None:
        if getattr(self, "open_btn", None):
            self.open_btn.destroy()
            self.open_btn = None
        if path:
            self.open_btn = ttk.Button(self.status.master, text="Open the result",
                                       command=lambda p=path: open_path(p))
            self.open_btn.pack(side="right", padx=(0, 10))

    def on_undo(self) -> None:
        b = engine.last_undoable()
        if self.busy or not b:
            return
        if not messagebox.askyesno("Undo", f"Undo this?\n\n{engine.describe_batch(b)}\n\n"
                                           "Your files go back to how they were before."):
            return
        self.plan = None
        self.do_btn.config(state="disabled")
        self.run_bg(lambda: engine.undo_last(self.report_progress), self.after_undo, "Undoing...")

    def after_undo(self, res: dict) -> None:
        engine.log_activity("undone", res["title"], f"{recipes.plural(res['done'], 'change')} reversed" +
                            (f", {len(res['problems'])} problem(s)" if res["problems"] else ""))
        chunks = [(f"Undone:  {res['title']}  ({recipes.plural(res['done'], 'change')} reversed).\n", "good")]
        if res["problems"]:
            chunks.append((f"\n{recipes.plural(len(res['problems']), 'item')} could not be put back:\n", "bad"))
            if res.get("partial"):
                chunks.append(("Everything else was restored. Fix the cause (for example, close the file in "
                               "another program) and click Undo again to retry the rest.\n", None))
            chunks += [(f"  {p}\n", None) for p in res["problems"][:100]]
        self.summary.config(text="Finished.")
        self.set_output(chunks)
        self.refresh_undo()

    def refresh_undo(self) -> None:
        b = engine.last_undoable()
        self.undo_btn.config(state="normal" if b and not self.busy else "disabled")
        self.undo_label.config(text=engine.describe_batch(b) if b else "Nothing to undo yet.")
        self.cleanup_btn.config(state="disabled" if self.busy else "normal")
        self.refresh_recycle()

    def refresh_recycle(self) -> None:
        """Measure the Recycle folder in the background (it can hold many files)."""
        if getattr(self, "_recycle_scanning", False):
            return
        self._recycle_scanning = True

        def work():
            try:
                n, size = engine.recycle_usage()
            except Exception:                                    # noqa: BLE001
                n, size = 0, 0
            self.q.put(("recycle", n, size))

        threading.Thread(target=work, daemon=True).start()

    def on_cleanup(self) -> None:
        if self.busy:
            return
        if not messagebox.askyesno(
                "Clean up old backups",
                "This deletes the saved copies that \"Undo last change\" can no longer use: "
                "changes you have already undone, and anything older than your last "
                f"{engine.MAX_HISTORY} changes.\n\n"
                "Changes you can still undo are kept. The files you are working on are not touched.\n\n"
                "Clean up now?"):
            return
        self.run_bg(engine.cleanup_recycle, self.after_cleanup, "Cleaning up...")

    def after_cleanup(self, res: dict) -> None:
        if res["removed"]:
            text = (f"Cleaned up {recipes.plural(res['removed'], 'old backup')} and freed "
                    f"{engine.size_text(res['freed'])}.\n")
        else:
            text = "There was nothing to clean up. Every backup is still needed for Undo.\n"
        chunks = [(text, "good")]
        if res["problems"]:
            chunks.append(("\nSome backups could not be deleted (they may be open in another program):\n", "bad"))
            chunks += [(f"  {p}\n", None) for p in res["problems"][:50]]
        self.summary.config(text="Finished.")
        self.set_output(chunks)
        engine.log_activity("done", "Clean up old backups", text.strip())
        self.refresh_undo()


def main() -> None:
    if "--sandbox-boot" in sys.argv:                      # the app re-launches itself to run scripts safely
        import sandbox
        sandbox.boot(sys.argv[sys.argv.index("--sandbox-boot") + 1:])
        return
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)        # crisp text on high-DPI screens
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
