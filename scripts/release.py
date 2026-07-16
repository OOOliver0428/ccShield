"""Windows portable-release launcher for ccShield.

PyInstaller builds this file as ``ccShield.exe``. The executable configures a
per-user data directory, starts the bundled FastAPI + Vue application on an
available loopback port, opens the browser, and keeps a small text control menu
available for reopening the page, opening local data, or stopping cleanly.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Protocol

RELEASE_VERSION = "2.1.0"
DEFAULT_PORT = 8000
PORT_ATTEMPTS = 100


class ServerLike(Protocol):
    """Minimal uvicorn server surface used by the launcher and tests."""

    should_exit: bool

    def run(self) -> None: ...


class Arguments(argparse.Namespace):
    """Typed release-launcher arguments."""

    def __init__(self) -> None:
        super().__init__()
        self.no_browser: bool = False
        self.check: bool = False
        self.smoke_test: bool = False
        self.data_dir: Path | None = None
        self.port: int | None = None


def bundle_root() -> Path:
    """Return the read-only bundle root, or the repository root in source runs."""

    frozen_root = getattr(sys, "_MEIPASS", None)
    if isinstance(frozen_root, str) and frozen_root:
        return Path(frozen_root).resolve()
    return Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    """Use the standard per-user Windows application-data location."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data).expanduser().resolve() / "ccShield"
    return (Path.home() / "AppData" / "Local" / "ccShield").resolve()


def port_is_available(port: int) -> bool:
    """Return whether an IPv4 loopback listener can bind to ``port``."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def find_available_port(
    start: int = DEFAULT_PORT,
    attempts: int = PORT_ATTEMPTS,
) -> int:
    """Select a bounded, deterministic loopback port for the local UI."""

    for port in range(start, start + attempts):
        if port_is_available(port):
            return port
    raise OSError(f"no available port in range {start}-{start + attempts - 1}")


def configure_runtime(data_dir: Path, static_dir: Path, port: int) -> None:
    """Set release-only paths before importing any backend module."""

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    os.environ.update(
        {
            "CCSHIELD_RELEASE": "1",
            "CCSHIELD_DATA_DIR": str(data_dir),
            "CCSHIELD_STATIC_DIR": str(static_dir),
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "DEBUG": "false",
        }
    )


def wait_for_url(url: str, timeout: float = 30.0) -> bool:
    """Poll a local HTTP endpoint until it responds successfully."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if 200 <= response.status < 300:
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    return False


def fetch_bootstrap_token(app_url: str) -> str:
    """Verify the local login bootstrap endpoint and return its token."""

    with urllib.request.urlopen(f"{app_url}/api/auth/bootstrap", timeout=5.0) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("bootstrap response is not an object")
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        raise ValueError("bootstrap response is missing a token")
    return token


def websocket_url(app_url: str, token: str) -> str:
    """Build a same-origin room WebSocket URL for the packaged smoke test."""

    scheme = "wss" if app_url.startswith("https://") else "ws"
    host = app_url.split("://", maxsplit=1)[-1]
    encoded_token = urllib.parse.quote(token, safe="")
    return f"{scheme}://{host}/api/ws/rooms/0?token={encoded_token}"


def verify_local_websocket(app_url: str, token: str) -> bool:
    """Open the local room stream without starting or contacting a live room."""

    from websockets.sync.client import connect

    with connect(
        websocket_url(app_url, token),
        open_timeout=5,
        close_timeout=2,
    ) as websocket:
        payload = json.loads(websocket.recv(timeout=5))
    return isinstance(payload, dict) and payload.get("type") == "error"


def open_data_directory(path: Path) -> None:
    """Open the application-data directory in Windows Explorer."""

    _ = subprocess.Popen(["explorer.exe", str(path)])


def create_server(port: int) -> ServerLike:
    """Create uvicorn lazily, after release environment variables are ready."""

    import uvicorn

    from app.main import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
    )
    return uvicorn.Server(config)


def run_smoke_test(server: ServerLike, app_url: str) -> int:
    """Verify packaged HTTP, login bootstrap and local WebSocket, then stop.

    The room id is deliberately zero and no room start endpoint is called, so
    this check never contacts Bilibili or performs a moderation operation.
    """

    thread = threading.Thread(target=server.run, name="ccshield-server")
    thread.start()
    try:
        if not wait_for_url(f"{app_url}/health"):
            print("[release] Smoke test failed: health endpoint not ready.", file=sys.stderr)
            return 1
        if not wait_for_url(app_url):
            print("[release] Smoke test failed: frontend not ready.", file=sys.stderr)
            return 1
        try:
            token = fetch_bootstrap_token(app_url)
            websocket_ok = verify_local_websocket(app_url, token)
        except (OSError, ValueError, TimeoutError) as exc:
            print(f"[release] Smoke test failed: {exc}", file=sys.stderr)
            return 1
        if not websocket_ok:
            print(
                "[release] Smoke test failed: unexpected WebSocket response.",
                file=sys.stderr,
            )
            return 1
        print(
            "[release] Smoke test passed: health + frontend + bootstrap + WebSocket."
        )
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def run_interactive(
    server: ServerLike,
    app_url: str,
    data_dir: Path,
    *,
    no_browser: bool,
) -> int:
    """Run the local server and expose a small console control menu."""

    thread = threading.Thread(target=server.run, name="ccshield-server")
    thread.start()
    try:
        if not wait_for_url(f"{app_url}/health"):
            print("[release] Startup failed: local service did not become ready.")
            return 1

        print(f"\nccShield v{RELEASE_VERSION} 已启动")
        print(f"页面: {app_url}")
        print(f"数据: {data_dir}")
        print("\n操作: [O] 打开页面  [D] 打开数据目录  [Q] 停止并退出")
        if not no_browser:
            _ = webbrowser.open(app_url)

        while thread.is_alive():
            try:
                command = input("> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                command = "q"
            if command in {"q", "quit", "exit"}:
                return 0
            if command in {"o", "open"}:
                _ = webbrowser.open(app_url)
            elif command in {"d", "data"}:
                open_data_directory(data_dir)
            elif command:
                print("请输入 O、D 或 Q。")
        return 1
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Start ccShield portable release")
    _ = result.add_argument("--no-browser", action="store_true")
    _ = result.add_argument("--check", action="store_true")
    _ = result.add_argument("--smoke-test", action="store_true")
    _ = result.add_argument("--data-dir", type=Path)
    _ = result.add_argument("--port", type=int)
    _ = result.add_argument("--version", action="version", version=RELEASE_VERSION)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv, namespace=Arguments())
    static_dir = bundle_root() / "frontend" / "dist"
    data_dir = (args.data_dir or default_data_dir()).expanduser().resolve()

    if args.port is not None:
        if not 1 <= args.port <= 65535:
            print("[release] Port must be between 1 and 65535.", file=sys.stderr)
            return 2
        if not port_is_available(args.port):
            print(f"[release] Port {args.port} is already in use.", file=sys.stderr)
            return 1
        port = args.port
    else:
        try:
            port = find_available_port()
        except OSError as exc:
            print(f"[release] {exc}", file=sys.stderr)
            return 1

    configure_runtime(data_dir, static_dir, port)
    if not (static_dir / "index.html").is_file():
        print(f"[release] Bundled frontend is missing: {static_dir}", file=sys.stderr)
        return 1

    print(f"[release] ccShield v{RELEASE_VERSION}")
    print(f"[release] Bundle: {bundle_root()}")
    print(f"[release] Data: {data_dir}")
    print(f"[release] Static: {static_dir}")
    print(f"[release] Port: {port}")
    if args.check:
        print("[release] Packaged runtime check passed.")
        return 0

    from loguru import logger

    logger.add(
        data_dir / "logs" / "ccshield.log",
        rotation="5 MB",
        retention="7 days",
        encoding="utf-8",
        enqueue=True,
    )
    server = create_server(port)
    app_url = f"http://127.0.0.1:{port}"
    if args.smoke_test:
        return run_smoke_test(server, app_url)
    return run_interactive(server, app_url, data_dir, no_browser=args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
