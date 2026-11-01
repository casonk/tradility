"""The trader service's side of the Fidelity browser: starts the worker and talks to it in JSON lines.

The worker needs Playwright, which the service's own environment does not have, so it runs under the
system Python (``TRADILITY_FIDELITY_PYTHON``). It is started the first time it is needed and replaced if it
dies. A reply that does not arrive in time is reported as an unknown outcome, never retried.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import threading
from pathlib import Path
from typing import Any

REPLY_SECONDS = {"stage": 120, "submit": 90}


class WorkerClient:
    def __init__(
        self, python: str | None = None, headed: bool = False, argv: list[str] | None = None
    ) -> None:
        self.python = python or os.environ.get("TRADILITY_FIDELITY_PYTHON", "/usr/bin/python3")
        self.headed = headed
        self.argv = argv  # a replacement command, for tests
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    def _start(self) -> subprocess.Popen[str]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        src = str(Path(__file__).resolve().parent.parent)
        env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
        argv = self.argv or [
            self.python,
            "-m",
            "tradility.fidelity_worker",
            *(["--headed"] if self.headed else []),
        ]
        self._proc = subprocess.Popen(  # noqa: S603
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )
        return self._proc

    def _call(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            proc = self._start()
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            ready, _, _ = select.select([proc.stdout], [], [], REPLY_SECONDS[request["cmd"]])
            if not ready:
                proc.kill()
                raise OSError("the Fidelity browser did not answer in time")
            line = proc.stdout.readline()
            if not line:
                raise OSError("the Fidelity browser closed")
            return json.loads(line)

    def stage(self, order: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._call({"cmd": "stage", "order": order})
        except (OSError, ValueError) as exc:
            return {
                "ok": False,
                "kind": "error",
                "error": f"the Fidelity browser is not available ({type(exc).__name__})",
            }

    def submit(self) -> dict[str, Any]:
        # a submit whose reply is lost is an unknown outcome: the OSError propagates so the trader records it
        return self._call({"cmd": "submit"})

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                self._proc.stdin.flush()
            except OSError:
                self._proc.kill()
