from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
import http.cookiejar
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


@dataclass
class TestResponse:
    status: int
    headers: Message
    body: str


class TestClient:
    __test__ = False
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
            _NoRedirect(),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> TestResponse:
        url = f"{self.base_url}{path}"
        body = None
        req_headers = headers.copy() if headers else {}
        if data is not None:
            encoded = urllib.parse.urlencode(data)
            body = encoded.encode("utf-8")
            req_headers.setdefault(
                "Content-Type", "application/x-www-form-urlencoded"
            )
        request = urllib.request.Request(
            url, data=body, headers=req_headers, method=method
        )
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        content = response.read().decode("utf-8", errors="replace")
        return TestResponse(status=response.code, headers=response.headers, body=content)

    def get_cookie(self, name: str) -> str | None:
        for cookie in self.cookie_jar:
            if cookie.name == name and not cookie.is_expired():
                return cookie.value
        return None


@dataclass
class ServerInfo:
    base_url: str
    db_path: Path


def _find_free_port() -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
    except PermissionError as exc:
        raise RuntimeError("Socket binding is not permitted in this environment.") from exc


def _wait_for_server(base_url: str, proc: subprocess.Popen[str]) -> None:
    deadline = time.time() + 10
    last_error: Exception | None = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Robyn server process exited early.")
        try:
            with urllib.request.urlopen(f"{base_url}/public", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # pragma: no cover - transient startup errors
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"Robyn server failed to start: {last_error}")


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> ServerInfo:
    if shutil.which("robyn") is None:
        pytest.skip("Robyn CLI is not available in this environment.")
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path_factory.mktemp("db") / "users.db"
    try:
        port = _find_free_port()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    env = os.environ.copy()
    env.update(
        {
            "ROBYN_HOST": "127.0.0.1",
            "ROBYN_PORT": str(port),
            "ROBYN_SECURE_COOKIES": "0",
            "TAGLENS_DB_PATH": str(db_path),
            "ROBYN_ENV": "test",
        }
    )
    proc = subprocess.Popen(
        ["robyn", "app.py", "--log-level", "ERROR"],
        cwd=repo_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        _wait_for_server(f"http://127.0.0.1:{port}", proc)
        yield ServerInfo(base_url=f"http://127.0.0.1:{port}", db_path=db_path)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - safety net
            proc.kill()
