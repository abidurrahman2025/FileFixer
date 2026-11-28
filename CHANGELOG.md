# Changelog

## 2.1
Safety
- Every rename, removal and edit now remembers how its file looked in the preview and is skipped if the file changed since (duplicate removal also guards the copy that stays).
- Changes are refused for files swapped for links and for anything inside Windows or program folders.
- "Write" results and the restore step of Undo are written beside the target and swapped in, so an interrupted save never leaves a half file; a failed case-only rename no longer strands a `.fftmp` file.
- The undo list is saved while a job runs, so a crash or closed window can still be undone for what was done. Closing the window during a job asks first and stops it tidily.
- Settings, saved workflows, scripts and run logs are saved atomically.

Sync
- "Keep the newest version" only considers folders that really changed; a folder holding the old content can no longer win with a newer timestamp. Identical new versions count as one.
- A folder that lost all its files no longer causes deletions everywhere else.
- Folders that already match are remembered so later deletions are recognised. Rename bookkeeping is consistent for 3+ folders.
- Files of different sizes are no longer read just to find out they differ; the watcher looks for new same-named folders every two minutes instead of every round, and waits on renames and removals of just-changed files too.

Jobs
- Unreadable folders and files are reported in the preview (find, report, rename, organize, find & replace, duplicates, combine, package, emails, CSV).
- Organize by extension skips extensions that would create Windows-reserved folder names (CON, NUL ...).
- Spreadsheet jobs read the first Excel sheet only; cells with line breaks survive; short CSV rows no longer crash sorting.
- Output names like `v1.2` get `.txt` added; "Combine spreadsheet files" no longer merges its own earlier output when no extension was typed.
- Typing a request no longer freezes the window on big folder trees.
- Any unexpected error in the window is logged to `error.log` and explained, instead of failing silently.
