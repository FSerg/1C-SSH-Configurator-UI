"""
Helper script to launch Streamlit UI and FastAPI API side by side.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from typing import List


COMMANDS: List[List[str]] = [
    ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"],
    ["streamlit", "run", "streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"],
]


def main() -> int:
    processes: List[subprocess.Popen[str]] = []

    def _shutdown(signum: int, _frame) -> None:
        for proc in processes:
            proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        sys.exit(0 if signum == signal.SIGTERM else 1)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _shutdown)

    for command in COMMANDS:
        processes.append(
            subprocess.Popen(command)
        )

    exit_code = 0
    try:
        while processes:
            for proc in list(processes):
                code = proc.poll()
                if code is not None:
                    exit_code = code
                    processes.remove(proc)
            if processes:
                time.sleep(0.5)
    finally:
        for proc in processes:
            proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
