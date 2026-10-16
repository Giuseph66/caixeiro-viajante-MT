from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNERS_DIR = PROJECT_ROOT / "experiments/runners"
sys.path.insert(0, str(RUNNERS_DIR))

from memory_limit import enforce_memory_limit


def main() -> None:
    parser = argparse.ArgumentParser(description="Inicia a API dentro do limite seguro de RAM.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.reload:
        command.append("--reload")
    enforce_memory_limit(command=command, use_available_budget=False)


if __name__ == "__main__":
    main()
