# FileFixer upgrade: plan and checkpoint

Test GUI headless: `xvfb-run -a /root/.local/share/uv/python/cpython-3.13-linux-x86_64-gnu/bin/python3.13 ...` (has Tk; run `uv python install 3.13` if missing)

Resume rule: read this file, find the first unchecked stage, finish it, tick it, then continue.
Each stage leaves the app working and `python tests.py` + `python test_stages.py` passing.

Architecture (keep): `engine.py` (safe apply/undo), `recipes.py` (v1 jobs), `app.py` (Tkinter).
New modules: `registry.py` (all jobs, grouped), `r_extract.py`, `r_sync.py`, `r_more.py`,
`nl.py` (plain-English command parser, rule-based, no network), `sandbox.py` (script runner),
`advanced.py` (Advanced Tools window), `test_stages.py` (new tests).

- [x] S1 Text extraction job (prefix/suffix/substring/exact/regex, multi-pattern, new file or replace-with-backup, open result)
- [x] S2 Folder compare + sync (one-time, manual review, two-way with conflicts, opt-in deletes)
- [x] S3 Continuous sync (watch mode with approvals)
- [x] S4 More jobs: find files, extract emails/URLs, line tools, split, CSV tools, docx text
- [x] S5 Home dashboard: plain-English command box, quick actions, recent activity
- [x] S6 Advanced Tools: regex builder, script editor + sandbox, workflow builder, batch, history
- [x] S7 Tests (tests.py, test_stages.py, test_gui.py), README, zip, deliver

Notes for resuming: S5 = `nl.py` (rule-based parser -> Intent(key, values, explain, missing)), Home view in `app.py`
(command box, Choose files/folders, quick actions, recent activity from `engine.log_activity` -> activity.json).
S6 = `sandbox.py` (python -I subprocess + audit-hook guard, scratch dir, timeout, rlimits), `advanced.py` (window with tabs).

- [x] S8 Hardening pass (v2.1): auto preview guards on every change, atomic write/undo, crash-safe undo journal, sync conflict/rename/baseline fixes, unreadable-folder warnings, background request parsing, `test_hardening.py`
