"""Cross-platform development launcher for ccShield.

This script is normally invoked through one of the repository-root launchers:
``start.cmd`` on Windows, ``start.sh`` on Linux, or ``start.command`` on macOS.
Those wrappers run it inside the backend's uv-managed Python environment.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"
APP_URL = "http://127.0.0.1:5173"
BACKEND_PORT = 8000
FRONTEND_PORT = 5173


@dataclass(frozen=True)
class FrontendTool:
    """Commands needed for one supported frontend package manager."""

    name: str
    executable: str
    install_args: tuple[str, ...]
    run_args: tuple[str, ...]

    @property
    def install_command(self) -> list[str]:
        return [self.executable, *self.install_args]

    @property
    def run_command(self) -> list[str]:
        return [self.executable, *self.run_args]


class Arguments(argparse.Namespace):
    """Typed command-line options returned by argparse."""

    def __init__(self) -> None:
        super().__init__()
        self.no_browser: bool = False
        self.check: bool = False


def _find_executable(name: str) -> str | None:
    """Find a tool on PATH or in common per-user installation locations."""

    found = shutil.which(name)
    if found:
        return found

    executable = f"{name}.exe" if os.name == "nt" else name
    candidates = [
        Path.home() / ".local" / "bin" / executable,
        Path.home() / ".cargo" / "bin" / executable,
        Path.home() / ".bun" / "bin" / executable,
        Path("/opt/homebrew/bin") / executable,
        Path("/usr/local/bin") / executable,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _frontend_tool() -> FrontendTool | None:
    """Select the lockfile's package manager, with portable fallbacks."""

    configs = {
        "bun": FrontendTool("bun", "", ("install", "--frozen-lockfile"), ("run", "dev")),
        "npm": FrontendTool("npm", "", ("install", "--no-package-lock"), ("run", "dev")),
        "pnpm": FrontendTool("pnpm", "", ("install", "--lockfile=false"), ("run", "dev")),
        "yarn": FrontendTool("yarn", "", ("install", "--no-lockfile"), ("run", "dev")),
    }
    lockfile_order = (
        ("bun", ("bun.lock", "bun.lockb")),
        ("npm", ("package-lock.json",)),
        ("pnpm", ("pnpm-lock.yaml",)),
        ("yarn", ("yarn.lock",)),
    )

    preferred: list[str] = []
    for name, lockfiles in lockfile_order:
        if any((FRONTEND_DIR / lockfile).exists() for lockfile in lockfiles):
            preferred.append(name)
    preferred.extend(name for name in configs if name not in preferred)

    for name in preferred:
        executable = _find_executable(name)
        if executable:
            config = configs[name]
            return FrontendTool(
                config.name,
                executable,
                config.install_args,
                config.run_args,
            )
    return None


def _frontend_dependencies_exist() -> bool:
    bin_dir = FRONTEND_DIR / "node_modules" / ".bin"
    return (bin_dir / "vite").exists() or (bin_dir / "vite.cmd").exists()


def _display_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def _popen(command: list[str], cwd: Path) -> subprocess.Popen[bytes]:
    if os.name == "nt":
        return subprocess.Popen(
            command,
            cwd=cwd,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(command, cwd=cwd, start_new_session=True)


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Stop a server and all children spawned by its reload/package runner."""

    if process.poll() is not None:
        return

    if os.name == "nt":
        _ = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return

    try:
        _ = os.killpg(process.pid, signal.SIGTERM)
        _ = process.wait(timeout=5)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            _ = os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def _open_browser_when_ready(
    processes: tuple[subprocess.Popen[bytes], subprocess.Popen[bytes]],
    stop_event: threading.Event,
) -> None:
    """Open the UI only after both development servers are accepting traffic."""

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and not stop_event.wait(0.3):
        if any(process.poll() is not None for process in processes):
            return
        if _port_is_open(BACKEND_PORT) and _port_is_open(FRONTEND_PORT):
            print(f"\n[start] Ready: {APP_URL}", flush=True)
            _ = webbrowser.open(APP_URL)
            return


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start ccShield backend and frontend")
    _ = parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open the web UI automatically",
    )
    _ = parser.add_argument(
        "--check",
        action="store_true",
        help="check the local toolchain without starting servers",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv, namespace=Arguments())
    tool = _frontend_tool()
    if tool is None:
        print(
            "[start] No supported frontend runtime found. Install Node.js/npm or Bun first.",
            file=sys.stderr,
        )
        return 2

    if importlib.util.find_spec("uvicorn") is None:
        print(
            "[start] Backend environment is not ready. Run this through start.cmd/start.sh.",
            file=sys.stderr,
        )
        return 2

    dependencies_ready = _frontend_dependencies_exist()
    print(f"[start] Project: {ROOT}")
    print(f"[start] Python: {sys.executable}")
    print(f"[start] Frontend: {tool.name} ({tool.executable})")
    print(
        "[start] Frontend dependencies: "
        + ("ready" if dependencies_ready else "will be installed")
    )

    if args.check:
        print("[start] Toolchain check passed.")
        return 0

    if not dependencies_ready:
        command = tool.install_command
        print(f"[start] Installing frontend dependencies: {_display_command(command)}")
        result = subprocess.run(command, cwd=FRONTEND_DIR, check=False)
        if result.returncode != 0:
            print("[start] Frontend dependency installation failed.", file=sys.stderr)
            return result.returncode

    backend_command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--reload",
        "--host",
        "127.0.0.1",
        "--port",
        str(BACKEND_PORT),
    ]
    frontend_command = tool.run_command

    print(f"[start] Backend: {_display_command(backend_command)}")
    print(f"[start] Frontend: {_display_command(frontend_command)}")
    print(f"[start] UI: {APP_URL}")
    print("[start] Press Ctrl+C to stop both servers.\n")

    processes: list[subprocess.Popen[bytes]] = []
    stop_event = threading.Event()
    interrupted = False
    exit_code = 1
    try:
        backend = _popen(backend_command, BACKEND_DIR)
        processes.append(backend)
        frontend = _popen(frontend_command, FRONTEND_DIR)
        processes.append(frontend)

        if not args.no_browser:
            threading.Thread(
                target=_open_browser_when_ready,
                args=((backend, frontend), stop_event),
                daemon=True,
            ).start()

        while True:
            for label, process in (("backend", backend), ("frontend", frontend)):
                return_code = process.poll()
                if return_code is not None:
                    print(f"\n[start] {label} exited with code {return_code}.")
                    exit_code = return_code or 1
                    return exit_code
            time.sleep(0.25)
    except KeyboardInterrupt:
        interrupted = True
        print("\n[start] Stopping ccShield...")
        return 0
    except OSError as exc:
        print(f"\n[start] Failed to start: {exc}", file=sys.stderr)
        return 1
    finally:
        stop_event.set()
        for process in reversed(processes):
            _terminate_process_tree(process)
        if interrupted:
            print("[start] Stopped.")


if __name__ == "__main__":
    raise SystemExit(main())
