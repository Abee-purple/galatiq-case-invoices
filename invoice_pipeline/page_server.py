"""Runs a Streamlit page as a local server and opens it in the browser."""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

PREFERRED_PORT = 8501  # Streamlit's usual port, when it is free


class PageServerError(Exception):
    """The page could not be started."""


class PageServer:
    """A running Streamlit server, stopped by `stop()` or by Ctrl+C in the terminal."""

    def __init__(self, process: subprocess.Popen[bytes], url: str) -> None:
        self._process = process
        self.url = url

    def wait(self) -> None:
        """Block until the server stops. Ctrl+C raises KeyboardInterrupt here."""
        while True:
            try:
                self._process.wait(timeout=0.5)  # a short timeout lets Ctrl+C through on Windows
                return
            except subprocess.TimeoutExpired:
                continue

    def stop(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()


def start_page_server(
    script: Path,
    script_args: list[str],
    *,
    log_file: Path,
    env: dict[str, str] | None = None,
    timeout: float = 60,
) -> PageServer:
    """Serve `script` on localhost only, wait until it answers, then open the browser.

    `env` adds to the environment the page runs in. Streamlit's own output goes to
    `log_file`, so the caller's terminal output stays last.
    Raises ModuleNotFoundError when Streamlit isn't installed, and PageServerError when
    the server doesn't come up.
    """
    if importlib.util.find_spec("streamlit") is None:
        raise ModuleNotFoundError("No module named 'streamlit'", name="streamlit")
    port = _free_port()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(script),
                f"--server.port={port}",
                "--server.address=localhost",  # the page shows payment data; keep it off the LAN
                "--server.headless=true",  # we open the browser once the server is up
                "--browser.gatherUsageStats=false",  # Grok is the only network dependency
                "--",
                *script_args,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, **(env or {})},
        )
    server = PageServer(process, f"http://localhost:{port}")
    try:
        deadline = time.monotonic() + timeout
        while not _answers(port):
            if process.poll() is not None:
                raise PageServerError(f"The results page stopped while starting; see {log_file}")
            if time.monotonic() > deadline:
                raise PageServerError(
                    f"The results page did not start within {timeout:g}s; see {log_file}"
                )
            time.sleep(0.2)
    except BaseException:  # including Ctrl+C: never leave the server running unseen
        server.stop()
        raise
    webbrowser.open(server.url)
    return server


def _free_port() -> int:
    for port in (PREFERRED_PORT, 0):  # 0: any free port the OS picks
        with socket.socket() as s:
            try:
                s.bind(("localhost", port))
            except OSError:
                continue
            return int(s.getsockname()[1])
    raise PageServerError("No free port for the results page")


def _answers(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=0.5):
            return True
    except OSError:
        return False
