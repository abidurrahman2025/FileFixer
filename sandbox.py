"""Run a user-written Python script in a separate, restricted process.

What the guard does (best effort - see the note at the bottom):
  * The script works on COPIES of the files you give it, in a temporary folder:
        input/    read-only copies of your chosen files     (INPUT_DIR)
        output/   where results go                          (OUTPUT_DIR)
        scratch/  working space (the current folder)
  * File access outside those folders is refused (reading Python's own library is allowed).
  * Network access, starting programs, ctypes and similar are refused.
  * A time limit (default 20 s) and (Linux and macOS only) a memory limit are enforced.
  * Results are only copied to your real folders after you review and approve them.

Honest limits: this is a guard rail for scripts you write or have read, not a security boundary
against hostile code.  Do not run scripts you do not trust.  Memory limits need Linux or macOS.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

MAX_OUTPUT_BYTES = 200 * 1024          # shown to the user
MAX_STREAM_BYTES = 5 * 1024 * 1024     # the script may not print more than this
MAX_FILE_BYTES = 200 * 1024 * 1024     # biggest single file the script may create (Linux/macOS)
BOOT_FLAG = "--sandbox-boot"

EXAMPLE = '''# Your files are copies in INPUT_DIR. Write results into OUTPUT_DIR.
# Example: list every line that contains "itm_" with a number in front.
import os

lines = []
for name in sorted(os.listdir(INPUT_DIR)):
    with open(os.path.join(INPUT_DIR, name), encoding="utf-8", errors="replace") as f:
        lines += [ln.strip() for ln in f if "itm_" in ln]

with open(os.path.join(OUTPUT_DIR, "result.txt"), "w", encoding="utf-8") as out:
    for i, ln in enumerate(lines, 1):
        out.write(f"{i}. {ln}\\n")
print(f"Found {len(lines)} lines")
'''


@dataclass
class SandboxResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    timed_out: bool = False
    cancelled: bool = False
    seconds: float = 0.0
    workdir: Optional[Path] = None
    outputs: list = field(default_factory=list)      # paths of files the script produced

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled

    def cleanup(self) -> None:
        if self.workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)


def boot_command() -> list:
    if getattr(sys, "frozen", False):
        return [sys.executable, BOOT_FLAG]
    return [sys.executable, "-I", os.path.abspath(__file__), BOOT_FLAG]


def _read_capped(p: Path) -> str:
    try:
        with open(p, "rb") as fh:
            data = fh.read(MAX_OUTPUT_BYTES + 1)
    except OSError:
        return ""
    text = data[:MAX_OUTPUT_BYTES].decode("utf-8", "replace")
    return text + ("\n... (output cut off)" if len(data) > MAX_OUTPUT_BYTES else "")


def run_script(code: str, inputs=(), timeout: float = 20, mem_mb: int = 512,
               cancel: Optional[Callable[[], bool]] = None) -> SandboxResult:
    work = Path(tempfile.mkdtemp(prefix="ff_sandbox_")).resolve()
    for d in ("input", "output", "scratch"):
        (work / d).mkdir()
    used = set()
    for src in inputs:
        src = Path(src)
        name, n = src.name, 2
        while name.lower() in used:
            name = f"{src.stem} ({n}){src.suffix}"
            n += 1
        used.add(name.lower())
        shutil.copy2(src, work / "input" / name)
    script = work / "script.py"
    script.write_text(code, "utf-8")
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PATH": ""}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    res = SandboxResult(workdir=work)
    start = time.time()
    with open(work / "stdout.txt", "wb") as so, open(work / "stderr.txt", "wb") as se:
        proc = subprocess.Popen(boot_command() + [str(script), str(work), str(int(mem_mb)), str(int(timeout))],
                                cwd=work / "scratch", stdout=so, stderr=se, stdin=subprocess.DEVNULL, env=env)
        while True:
            try:
                proc.wait(timeout=0.15)
                break
            except subprocess.TimeoutExpired:
                if cancel and cancel():
                    res.cancelled = True
                elif time.time() - start > timeout:
                    res.timed_out = True
                if res.cancelled or res.timed_out:
                    proc.kill()
                    proc.wait()
                    break
    res.seconds = time.time() - start
    res.returncode = proc.returncode
    res.stdout = _read_capped(work / "stdout.txt")
    res.stderr = _read_capped(work / "stderr.txt")
    out = work / "output"
    res.outputs = sorted(p for p in out.rglob("*") if p.is_file())
    if res.timed_out:
        res.stderr += f"\nStopped: the script ran longer than {timeout:g} seconds."
    if res.cancelled:
        res.stderr += "\nStopped at your request."
    return res


# --------------------------------------------------------------------------
# Everything below runs inside the child process
# --------------------------------------------------------------------------
def boot(argv: list) -> None:
    script, work, mem_mb, timeout = Path(argv[0]), Path(argv[1]).resolve(), int(argv[2]), int(argv[3])
    try:
        import resource
        if mem_mb > 0:
            resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (timeout + 5,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES,) * 2)
    except (ImportError, ValueError, OSError):
        pass                                           # Windows: time limit only

    import sysconfig
    real = os.path.realpath
    stdlib = {real(p) for k, p in sysconfig.get_paths().items() if k in ("stdlib", "platstdlib", "purelib", "platlib")}
    for extra in (getattr(sys, "_MEIPASS", None), os.path.dirname(os.__file__)):
        if extra:
            stdlib.add(real(extra))
    zi = "/usr/share/zoneinfo"
    can_write = [real(work / "output"), real(work / "scratch")]
    can_read = can_write + [real(work / "input"), real(script)] + sorted(stdlib) + [zi]
    devices = {os.devnull, "/dev/urandom", "/dev/random", "/dev/zero", "/etc/localtime"}
    sep = os.sep

    def inside(p, roots) -> bool:
        try:
            rp = real(os.fspath(p))
        except (TypeError, ValueError):
            return True                                # not a path (e.g. a file descriptor)
        return any(rp == r or rp.startswith(r + sep) for r in roots)

    deny_events = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
                   "os.startfile", "webbrowser.open", "ctypes.dlopen", "ctypes.dlsym", "ctypes.cdata",
                   "ctypes.call_function", "os.kill", "os.killpg", "pty.spawn", "signal.pthread_kill")
    write_all = {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.truncate", "os.chmod", "os.chown", "os.utime",
                 "os.link", "os.symlink", "os.mkfifo", "os.mknod", "shutil.rmtree", "os.removedirs"}
    copy_like = {"shutil.copyfile", "shutil.copytree", "shutil.move", "shutil.copymode", "shutil.copystat"}
    blocked_imports = {"ctypes", "_ctypes", "winreg"}

    def refuse(what: str):
        raise PermissionError(f"FileFixer sandbox: {what} is not allowed in scripts.")

    def hook(event: str, args: tuple) -> None:
        if event == "open":
            path, _mode, flags = args
            if isinstance(path, int) or path in devices:
                return
            writing = bool(flags) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC))
            if writing and not inside(path, can_write):
                refuse(f"writing to {path!r} (write into OUTPUT_DIR instead)")
            if not writing and not inside(path, can_read):
                refuse(f"reading {path!r} (only the files in INPUT_DIR are available)")
        elif event in ("os.listdir", "os.scandir", "glob.glob", "os.walk"):
            p = args[0] if args else None
            if p is not None and not inside(p, can_read):
                refuse(f"looking inside {p!r}")
        elif event in write_all:
            for a in args[:2]:
                if isinstance(a, (str, bytes, os.PathLike)) and not inside(a, can_write):
                    refuse(f"changing {a!r}")
        elif event in copy_like:
            if isinstance(args[0], (str, bytes, os.PathLike)) and not inside(args[0], can_read):
                refuse(f"reading {args[0]!r}")
            if len(args) > 1 and isinstance(args[1], (str, bytes, os.PathLike)) and not inside(args[1], can_write):
                refuse(f"writing to {args[1]!r}")
        elif event.startswith("socket."):
            refuse("network access")
        elif event in deny_events or any(event.startswith(e + ".") for e in deny_events):
            refuse("starting programs or low-level system calls")
        elif event == "import" and args[0].split(".")[0] in blocked_imports:
            refuse(f"importing {args[0]}")

    class Capped:
        def __init__(self, stream) -> None:
            self.s, self.n = stream, 0

        def write(self, data):
            self.n += len(data)
            if self.n > MAX_STREAM_BYTES:
                raise RuntimeError("The script printed too much output and was stopped.")
            return self.s.write(data)

        def flush(self):
            self.s.flush()

        def __getattr__(self, name):
            return getattr(self.s, name)

    code = script.read_text("utf-8")
    sys.stdout, sys.stderr = Capped(sys.stdout), Capped(sys.stderr)
    glb = {"__name__": "__main__", "__file__": str(script), "INPUT_DIR": str(work / "input"),
           "OUTPUT_DIR": str(work / "output")}
    sys.addaudithook(hook)
    try:
        exec(compile(code, str(script), "exec"), glb)
    except SystemExit as e:
        sys.stdout.flush()
        os._exit(e.code if isinstance(e.code, int) else (0 if e.code is None else 1))
    except BaseException:
        import traceback
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__" and BOOT_FLAG in sys.argv:
    boot(sys.argv[sys.argv.index(BOOT_FLAG) + 1:])
