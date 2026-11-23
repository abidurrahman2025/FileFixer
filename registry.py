"""Every job the app offers, grouped for the sidebar.  Add new jobs here."""
import recipes
from r_extract import ExtractWords
from r_more import FindFiles, ExtractPatterns, LineTools, SplitText, CsvTools, CombineCsv, FolderReport
from r_sync import CompareFolders, SyncFolders, WatchFolders

_by_key = {r.key: r for r in recipes.ALL}

GROUPS = [
    ("Files", [FindFiles(), _by_key["rename"], _by_key["organize"], _by_key["duplicates"], _by_key["package"]]),
    ("Folders", [CompareFolders(), SyncFolders(), WatchFolders()]),
    ("Text", [ExtractWords(), ExtractPatterns(), _by_key["replace"], LineTools(), _by_key["merge"], SplitText()]),
    ("Data", [CsvTools(), CombineCsv(), FolderReport()]),
]


def all_recipes() -> list:
    return [r for _, rs in GROUPS for r in rs]


def get(key: str):
    return next((r for r in all_recipes() if r.key == key), None)
