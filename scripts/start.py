"""Cross-platform development launcher for ccShield.

This script is normally invoked through one of the repository-root launchers:
``start.cmd`` on Windows, ``start.sh`` on Linux, or ``start.command`` on macOS.
Those wrappers run it inside the backend's uv-managed Python environment.
"""

from __future__ import annotations

import argparse
import contextlib
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
BACKEND_PORT = 8000
FRONTEND_PORT_START = 5173
FRONTEND_PORT_ATTEMPTS = 100


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
        with contextlib.suppress(ProcessLookupError):
            _ = os.killpg(process.pid, signal.SIGKILL)


def _port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def _port_is_available(port: int) -> bool:
    """Return whether an IPv4 loopback listener can bind to ``port``."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _find_available_port(
    start: int = FRONTEND_PORT_START,
    attempts: int = FRONTEND_PORT_ATTEMPTS,
) -> int:
    """Select the first available frontend port starting at ``start``."""

    for port in range(start, start + attempts):
        if _port_is_available(port):
            return port
    raise OSError(
        f"no available frontend port in range {start}-{start + attempts - 1}"
    )


def _open_browser_when_ready(
    processes: tuple[subprocess.Popen[bytes], subprocess.Popen[bytes]],
    stop_event: threading.Event,
    frontend_port: int,
    app_url: str,
) -> None:
    """Open the UI only after both development servers are accepting traffic."""

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and not stop_event.wait(0.3):
        if any(process.poll() is not None for process in processes):
            return
        if _port_is_open(BACKEND_PORT) and _port_is_open(frontend_port):
            print(f"\n[start] Ready: {app_url}", flush=True)
            _ = webbrowser.open(app_url)
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

    try:
        frontend_port = _find_available_port()
    except OSError as exc:
        print(f"[start] Frontend port selection failed: {exc}", file=sys.stderr)
        return 1

    app_url = f"http://127.0.0.1:{frontend_port}"
    if frontend_port != FRONTEND_PORT_START:
        print(
            f"[start] Frontend port {FRONTEND_PORT_START} is occupied; "
            f"using {frontend_port}."
        )

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
    frontend_command = [
        *tool.run_command,
        "--",
        "--port",
        str(frontend_port),
        "--strictPort",
    ]

    print(f"[start] Backend: {_display_command(backend_command)}")
    print(f"[start] Frontend: {_display_command(frontend_command)}")
    print(f"[start] UI: {app_url}")
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
                args=((backend, frontend), stop_event, frontend_port, app_url),
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
