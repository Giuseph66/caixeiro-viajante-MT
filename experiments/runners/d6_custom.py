from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from memory_limit import enforce_memory_limit

MEMORY_LIMIT = enforce_memory_limit()
ROOT = Path(__file__).resolve().parents[2]


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Gera um mTSP personalizado e avalia as políticas P1/P2/P3."
    )
    parser.add_argument("--time-limit-s", type=float, required=True)
    parser.add_argument("--variants", type=int, required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--vehicle-count", type=int, choices=(2, 3), required=True)
    parser.add_argument("--objective", choices=("distance", "time"), required=True)
    parser.add_argument("--origin-mode", choices=("auto", "fixed"), required=True)
    parser.add_argument("--origin-ibge-code", type=int)
    parser.add_argument("--preview-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lambdas", type=float, nargs="+", required=True)
    parser.add_argument("--repetition-policy", choices=("allow", "reduce", "forbid"), default="allow")
    args = parser.parse_args()

    base_report = args.output.with_name(f"{args.output.stem}.d5-base.json")
    common = [
        "--time-limit-s", str(args.time_limit_s * (0.75 if args.repetition_policy != "allow" else 1)),
        "--workers", str(args.workers),
        "--seed", str(args.seed),
        "--vehicle-count", str(args.vehicle_count),
        "--objective", args.objective,
        "--origin-mode", args.origin_mode,
        "--preview-path", str(args.preview_path),
    ]
    if args.origin_ibge_code is not None:
        common.extend(("--origin-ibge-code", str(args.origin_ibge_code)))

    try:
        print(
            f"Pré-validação de RAM aplicada: limite de "
            f"{MEMORY_LIMIT / 1_000_000_000:.2f} GB para esta etapa de cálculo.",
            flush=True,
        )
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "experiments/runners/d5_open_mtsp.py"),
                *common,
                "--output", str(base_report),
            ],
            cwd=ROOT,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "experiments/runners/d6_overlap.py"),
                "--time-limit-s", str(args.time_limit_s),
                "--variants", str(args.variants),
                "--workers", str(args.workers),
                "--seed", str(args.seed),
                "--lambdas", *(str(value) for value in args.lambdas),
                "--repetition-policy", args.repetition_policy,
                "--mtsp-report", str(base_report),
                "--preview-path", str(args.preview_path),
                "--output", str(args.output),
            ],
            cwd=ROOT,
            check=True,
        )
    finally:
        base_report.unlink(missing_ok=True)
        base_report.with_suffix(".checkpoint.json").unlink(missing_ok=True)


if __name__ == "__main__":
    _main()
