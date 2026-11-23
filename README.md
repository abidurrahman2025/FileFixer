# FileFixer 2

Describe what you want in plain English, or click a job. FileFixer shows exactly what will happen, asks
before changing anything, backs up whatever it replaces, and can undo it. Needs only Python 3.13 (nothing to install;
PDF reading needs the free add-on `pip install pypdf`).

**Start:** double-click `Start FileFixer.bat` (or `python app.py`). `build_exe.bat` makes a single `FileFixer.exe`.

## Home
Type a request, optionally click **Choose files / Choose folders**, then **Go**. FileFixer explains what it understood,
fills in the matching job and shows the preview. Examples it understands:
- Find every word starting with itm_ in this file and put them into a numbered list
- Find duplicate files in this folder / Extract all email addresses from this file
- Find all files modified in the last 30 days / Organize these files by extension
- Rename all files containing the word Draft to Final
- Synchronize all folders named folder1 in both directions / Whenever I change one folder, update the others
- Remove duplicate lines, sort lines, split a file into parts, keep rows where a column is..., sum a column

The plain-English reader is built in (rule-based, no internet, no account). If it can't understand a request it says so and
shows examples. Every request becomes one of the jobs below with visible settings, so nothing runs that you can't see.

## Jobs (left sidebar)
| Group | Job |
|---|---|
| Files | Find files · Rename many files · Organize a messy folder (by type, month or extension; optional "last N days") · Find duplicate files (several folders) · Build a delivery package |
| Folders | Compare same-named folders · Sync same-named folders (one-way / two-way) · Watch and sync automatically |
| Text | Extract words from a file · Extract emails and links · Find and replace · Clean up lines · Combine text files · Split a large text file |
| Data | Work with a spreadsheet file (filter, columns, duplicates, sort, summarize, convert) · Combine spreadsheet files · Folder report |

Readable inputs: text files (any encoding), Word `.docx`, Excel `.xlsx`, PDF `.pdf` (with pypdf), `.csv/.tsv/.json`.

### Extract words
Starts with / ends with / contains / is exactly / regular expression; several patterns at once; capitals matter or not;
each word once or every time; **original order kept and complete words kept**. Punctuation is never altered unless you choose
"Ignore punctuation at the edges". Results go to a **new file you name** (default `<name>List.txt`, original untouched), or
you may replace the original (a backup is saved first).

### Sync same-named folders
A matching name never means matching content: every group is verified by comparing file contents and labelled
*Identical*, *Similar* or *Same name only*. Detects new, changed, deleted, renamed (when unambiguous) files, missing
subfolders and conflicts. Conflicts are never decided silently (skip / keep newest / keep reference). Replaced files are backed up;
**deletions are off unless you tick the box** and go to the Recycle folder. Symbolic links aren't followed; a folder never syncs
with itself or one inside it. Modes: one-time, manual (tick each change), or continuous watching (asks first, or applies
automatically). Continuous mode works while FileFixer is open and checks on a timer; files still being written are left for the next round.
The first two-way sync can't know about earlier deletions (it has no history yet), so it only copies.

## Advanced Tools (bottom of the sidebar)
Python Script editor (restricted sandbox) · Regex Builder (live matches; extract/replace/remove on a file) · Workflow Builder
(chain steps, save and reuse) · Batch Processing (a workflow over many files) · Execution History (logs, re-run).

**Script sandbox, honestly:** scripts run in a separate process on *copies* of the files you choose, may write only to a temporary
output folder, and cannot use the network or start programs. They stop after a time limit (and a memory limit on Linux/macOS).
Results reach your folders only after you approve. This is a guard rail for scripts you wrote or read, **not** protection
against hostile code. Memory limits are not available on Windows. FileFixer does not write scripts for you; paste one from any source.

## Safety
Preview first, confirm, atomic writes, backups to `C:\Users\<you>\FileFixer\Recycle`, undo for the last 50 batches, nothing
permanently deleted, system folders refused, one failure never stops the rest, Cancel button during long work.

## For developers
`engine.py` (safe apply/undo/log) · `recipes.py`, `r_extract.py`, `r_sync.py`, `r_more.py` (jobs) · `registry.py` (grouping) ·
`nl.py` (plain-English reader) · `workflow.py`, `sandbox.py`, `advanced.py` · `app.py` (window).
Add a job: subclass `Recipe`, give it `fields` and `plan()`, add it to `registry.GROUPS`.
Tests: `python tests.py`, `python test_stages.py`, `python test_gui.py` (needs Tk and a display; skips itself otherwise).
`PROGRESS.md` records what is built.
