"""Budget, run, and resume the pre-registered D9 experiment matrix."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_limit import MAX_MEMORY_BYTES, enforce_memory_limit

MEMORY_LIMIT = enforce_memory_limit()
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "experiments/configurations/d9-mt.json"
STAGE_FILES = {"d4": "d4-open-tsp.json", "d5": "d5-open-mtsp.json", "d6": "d6-overlap.json"}


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as source:
        return json.load(source)


def _mem_available_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _resource_snapshot() -> dict[str, int | None]:
    return {
        "logical_cpus": os.cpu_count() or 1,
        "memory_available_bytes": _mem_available_bytes(),
        "disk_free_bytes": shutil.disk_usage(ROOT).free,
    }


def _load_config(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    config = json.loads(raw)
    scenarios = config.get("scenarios", [])
    ids = [item.get("id") for item in scenarios]
    if len(scenarios) != 28 or ids != [f"E{index:02d}" for index in range(1, 29)]:
        raise ValueError("A configuração D9 deve listar E01–E28 em ordem")
    if config.get("memory_limit_bytes") != MAX_MEMORY_BYTES:
        raise ValueError("A configuração D9 precisa usar o teto rígido de 25 GB")
    lambda_values = [
        float(value)
        for value in [*config.get("lambda_sensitivity", []), config.get("lambda_main")]
    ]
    if any(not math.isfinite(value) or value < 0 for value in lambda_values):
        raise ValueError("Os lambdas da D9 devem ser valores finitos e não negativos")
    return config, _sha256_bytes(raw)


def _expected_inputs(config: dict[str, Any]) -> dict[str, Any]:
    graph = json.loads((ROOT / "data/manifests/road-network-mt.json").read_text())
    matrix = json.loads((ROOT / "data/manifests/travel-matrix-mt.json").read_text())
    expected = config["expected_inputs"]
    actual = {
        "graph_sha256": graph["graph_sha256"],
        "matrix_cache_key": matrix["cache_key"],
        "matrix_cache_sha256": matrix["cache_sha256"],
    }
    if actual != expected or matrix["graph_sha256"] != graph["graph_sha256"]:
        raise ValueError("Os dados atuais não correspondem ao snapshot congelado na D9")
    return actual


def _estimate(config: dict[str, Any], repetitions: int) -> dict[str, Any]:
    snapshot = _resource_snapshot()
    time_limits = config["time_limits_seconds"]
    workers = int(config["worker_processes"])
    d4_wall = 4 * float(time_limits["d4_per_scenario"])
    d5_wall = 430.778  # D5 medido: oito cenários com oito processos.
    d6_wall = 8 * (8 - 1) * float(time_limits["d6_per_variant"]) / workers
    wall_per_repeat = d4_wall + d5_wall + d6_wall
    wall_total = wall_per_repeat * repetitions
    cpu_seconds_per_repeat = (
        4 * float(time_limits["d4_per_scenario"])
        + 8 * float(time_limits["d5_per_scenario"])
        + 8 * (8 - 1) * float(time_limits["d6_per_variant"])
    )
    report_bytes = sum(
        (ROOT / "data/manifests" / name).stat().st_size
        for name in (
            "tsp-benchmarks-mt.json",
            "mtsp-benchmarks-mt.json",
            "overlap-benchmarks-mt.json",
        )
    )
    expected_storage = math.ceil(report_bytes * repetitions * 1.2)
    return {
        "repetitions": repetitions,
        "scenario_count": 28,
        "worker_processes": workers,
        "logical_cpus": snapshot["logical_cpus"],
        "memory_limit_bytes": int(config["memory_limit_bytes"]),
        "historical_peak_bytes": {
            "d4": 16_809_508_864,
            "d5": 15_284_240_384,
            "d6": 15_259_635_712,
        },
        "estimated_wall_seconds_before_margin": round(wall_total, 1),
        "estimated_wall_seconds_with_20_percent_margin": math.ceil(wall_total * 1.2),
        "estimated_wall_hours_with_20_percent_margin": round(wall_total * 1.2 / 3600, 2),
        "estimated_cpu_hours_upper_bound": round(
            cpu_seconds_per_repeat * repetitions / 3600, 2
        ),
        "estimated_report_bytes_before_compression": expected_storage,
        "disk_reserve_bytes": 2_000_000_000,
        "available_disk_bytes": snapshot["disk_free_bytes"],
        "memory_available_bytes_at_estimate": snapshot["memory_available_bytes"],
        "lambda_main": config["lambda_main"],
        "lambda_sensitivity": config["lambda_sensitivity"],
        "sensitivity_additional_searches": 0,
    }


def _budget_markdown(estimate: dict[str, Any], input_sha256: str) -> str:
    memory_gb = estimate["memory_limit_bytes"] / 1_000_000_000
    disk_gb = estimate["available_disk_bytes"] / 1_000_000_000
    reserve_gb = estimate["disk_reserve_bytes"] / 1_000_000_000
    return f"""# Orçamento experimental D9 — Mato Grosso

**Estado:** estimativa registrada antes do lote.
**Configuração SHA-256:** `{input_sha256}`
**Repetições:** {estimate['repetitions']} × {estimate['scenario_count']} configurações.
**Hardware observado:** {estimate['logical_cpus']} CPUs lógicas; {estimate['memory_available_bytes_at_estimate'] / 1_000_000_000:.1f} GB disponíveis no momento da estimativa.

| Recurso | Orçamento |
|---|---:|
| RAM por cálculo | teto rígido de {memory_gb:.0f} GB em unidade systemd própria |
| Picos anteriores medidos | D4 {estimate['historical_peak_bytes']['d4'] / 1e9:.2f} GB; D5 {estimate['historical_peak_bytes']['d5'] / 1e9:.2f} GB; D6 {estimate['historical_peak_bytes']['d6'] / 1e9:.2f} GB |
| CPU | até {estimate['worker_processes']} processos; limite superior estimado de {estimate['estimated_cpu_hours_upper_bound']:.1f} CPU-h |
| Parede | {estimate['estimated_wall_hours_with_20_percent_margin']:.2f} h com 20% de margem |
| Armazenamento | reserva de {reserve_gb:.0f} GB; estimativa sem compressão {estimate['estimated_report_bytes_before_compression'] / 1e9:.2f} GB |
| Disco disponível | {disk_gb:.1f} GB |

O limite de tempo D4 é 300 s por cenário. D5 usa 300 s por cenário e referência medida de 430,778 s para os oito cenários. D6 usa 60 s por variante; sete variantes adicionais em cada um dos oito cenários dão um teto estimado de 420 s por repetição com oito workers. A análise λ=0/0,25/0,5/1/2 seleciona sobre o mesmo conjunto D6 e não inicia buscas adicionais.

Os ativos grandes já estão congelados pelos hashes da configuração. Cada repetição grava relatórios próprios compactados; os runners preservam checkpoints para retomada. O teto interrompe o cálculo antes de ultrapassar 25 GB.
"""


def _validate_report(stage: str, report: dict[str, Any], config: dict[str, Any], seed: int) -> None:
    limits = config["time_limits_seconds"]
    fingerprint = report.get("fingerprint", {})
    if stage == "d4":
        valid = (
            fingerprint.get("seed") == seed
            and fingerprint.get("time_limit_s_per_scenario") == limits["d4_per_scenario"]
            and len(report.get("scenarios", [])) == 4
        )
    elif stage == "d5":
        valid = (
            fingerprint.get("seed") == seed
            and fingerprint.get("time_limit_s_per_scenario") == limits["d5_per_scenario"]
            and fingerprint.get("parallel_solver_processes") == config["worker_processes"]
            and len(report.get("scenarios", [])) == 8
        )
    else:
        expected_lambdas = [
            *config["lambda_sensitivity"],
            config["lambda_main"],
        ]
        expected_lambdas = sorted(set(float(value) for value in expected_lambdas))
        valid = (
            fingerprint.get("seed") == seed
            and fingerprint.get("time_limit_s_per_variant") == limits["d6_per_variant"]
            and fingerprint.get("parallel_solver_processes") == config["worker_processes"]
            and fingerprint.get("lambdas") == expected_lambdas
            and len(report.get("scenarios", [])) == 8
        )
    if not valid:
        raise ValueError(f"Relatório {stage.upper()} existe, mas diverge da configuração D9")


def _ensure_report(
    *,
    stage: str,
    script: Path,
    arguments: list[str],
    plain_path: Path,
    archive_path: Path,
    config: dict[str, Any],
    seed: int,
) -> dict[str, Any]:
    if archive_path.is_file() and not plain_path.exists():
        with gzip.open(archive_path, "rb") as source, plain_path.open("wb") as target:
            shutil.copyfileobj(source, target)
    if plain_path.is_file():
        report = _load_json(plain_path)
        _validate_report(stage, report, config, seed)
        return report

    subprocess.run([sys.executable, str(script), *arguments], cwd=ROOT, check=True)
    report = _load_json(plain_path)
    _validate_report(stage, report, config, seed)
    return report


def _archive_report(plain_path: Path) -> Path:
    archive_path = plain_path.with_suffix(plain_path.suffix + ".gz")
    if not archive_path.exists():
        temporary = archive_path.with_name(archive_path.name + ".tmp")
        with plain_path.open("rb") as source, gzip.open(temporary, "wb", compresslevel=6) as target:
            shutil.copyfileobj(source, target)
        temporary.replace(archive_path)
    plain_path.unlink(missing_ok=True)
    return archive_path


def _git_revision() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _run(config: dict[str, Any], config_sha256: str, repetitions: int) -> None:
    inputs = _expected_inputs(config)
    output_dir = ROOT / config["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / "index.json"
    run_fingerprint = _sha256_bytes(
        json.dumps(
            {"config_sha256": config_sha256, "repetitions": repetitions},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    if index_path.exists():
        index = json.loads(index_path.read_text())
        if index.get("run_fingerprint") != run_fingerprint:
            raise ValueError("O diretório D9 já pertence a outra configuração; não será sobrescrito")
    else:
        if any(output_dir.iterdir()):
            raise FileExistsError(f"Diretório de resultados já contém arquivos: {output_dir}")
        index = {
            "study_id": config["study_id"],
            "run_fingerprint": run_fingerprint,
            "config_sha256": config_sha256,
            "input_hashes": inputs,
            "git_revision": _git_revision(),
            "runtime_environment": {
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "logical_cpus": os.cpu_count(),
                "ortools_version": __import__("ortools").__version__,
                "memory_limit_bytes": config["memory_limit_bytes"],
                "effective_memory_limit_bytes": MEMORY_LIMIT,
            },
            "repetitions": [],
            "status": "running",
        }

    scripts = {
        "d4": ROOT / "experiments/runners/d4_open_tsp.py",
        "d5": ROOT / "experiments/runners/d5_open_mtsp.py",
        "d6": ROOT / "experiments/runners/d6_overlap.py",
    }
    lambda_values = sorted(
        set(float(value) for value in [*config["lambda_sensitivity"], config["lambda_main"]])
    )
    stage_records: list[dict[str, Any]] = []
    _write_json(index_path, index)
    for repetition in range(1, repetitions + 1):
        seed_start = int(config["seed_base"]) + (repetition - 1) * int(config["seed_stride"])
        seeds = {"d4": seed_start, "d5": seed_start + 1, "d6": seed_start + 2}
        repeat_dir = output_dir / f"repeat-{repetition:02d}"
        repeat_dir.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "repetition": repetition,
            "status": "running",
            "seeds": seeds,
            "artifacts": {},
        }
        stage_records.append(record)
        index["repetitions"] = stage_records
        _write_json(index_path, index)

        limits = config["time_limits_seconds"]
        d4_plain = repeat_dir / STAGE_FILES["d4"]
        d4_archive = Path(str(d4_plain) + ".gz")
        _ensure_report(
            stage="d4",
            script=scripts["d4"],
            arguments=[
                "--time-limit-s",
                str(limits["d4_per_scenario"]),
                "--seed",
                str(seeds["d4"]),
                "--output",
                str(d4_plain),
            ],
            plain_path=d4_plain,
            archive_path=d4_archive,
            config=config,
            seed=seeds["d4"],
        )
        record["artifacts"]["d4"] = _sha256_bytes(d4_plain.read_bytes())
        _write_json(index_path, index)

        d5_plain = repeat_dir / STAGE_FILES["d5"]
        d5_archive = Path(str(d5_plain) + ".gz")
        _ensure_report(
            stage="d5",
            script=scripts["d5"],
            arguments=[
                "--time-limit-s",
                str(limits["d5_per_scenario"]),
                "--seed",
                str(seeds["d5"]),
                "--workers",
                str(config["worker_processes"]),
                "--output",
                str(d5_plain),
            ],
            plain_path=d5_plain,
            archive_path=d5_archive,
            config=config,
            seed=seeds["d5"],
        )
        record["artifacts"]["d5"] = _sha256_bytes(d5_plain.read_bytes())
        _write_json(index_path, index)

        d6_plain = repeat_dir / STAGE_FILES["d6"]
        d6_archive = Path(str(d6_plain) + ".gz")
        _ensure_report(
            stage="d6",
            script=scripts["d6"],
            arguments=[
                "--time-limit-s",
                str(limits["d6_per_variant"]),
                "--variants",
                "8",
                "--workers",
                str(config["worker_processes"]),
                "--seed",
                str(seeds["d6"]),
                "--reference-speed-kph",
                str(config["reference_speed_kph"]),
                "--mtsp-report",
                str(d5_plain),
                "--output",
                str(d6_plain),
                "--lambdas",
                *(str(value) for value in lambda_values),
            ],
            plain_path=d6_plain,
            archive_path=d6_archive,
            config=config,
            seed=seeds["d6"],
        )
        record["artifacts"]["d6"] = _sha256_bytes(d6_plain.read_bytes())

        for plain in (d4_plain, d5_plain, d6_plain):
            archive = _archive_report(plain)
            record["artifacts"][archive.name] = _sha256_bytes(archive.read_bytes())
        record["status"] = "complete"
        record["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        index["repetitions"] = stage_records
        _write_json(index_path, index)
        print(f"D9 repetição {repetition}/{repetitions} concluída.", flush=True)

    index["status"] = "complete"
    index["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    _write_json(index_path, index)
    print(f"Lote D9 concluído: {output_dir}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Orçamento e execução retomável da D9.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--run", action="store_true", help="Executa o lote; sem esta opção, apenas estima.")
    args = parser.parse_args()
    config_path = args.config.resolve()
    config, config_sha256 = _load_config(config_path)
    repetitions = args.repetitions or int(config["repetitions"])
    if not 1 <= repetitions <= 100:
        parser.error("--repetitions deve estar entre 1 e 100")

    estimate = _estimate(config, repetitions)
    budget_path = ROOT / "experiments/analysis/d9-budget-mt-2026-10-10.md"
    budget_path.parent.mkdir(parents=True, exist_ok=True)
    budget_path.write_text(_budget_markdown(estimate, config_sha256), encoding="utf-8")
    print(_budget_markdown(estimate, config_sha256), flush=True)
    if not args.run:
        print(f"Estimativa gravada em {budget_path}", flush=True)
        return

    resources = _resource_snapshot()
    minimum_available = int(config["memory_limit_bytes"]) + 2_000_000_000
    if resources["memory_available_bytes"] is not None and resources["memory_available_bytes"] < minimum_available:
        raise RuntimeError("RAM disponível abaixo do teto de cálculo mais 2 GB de margem")
    if int(resources["disk_free_bytes"] or 0) < estimate["disk_reserve_bytes"]:
        raise RuntimeError("Espaço livre abaixo da reserva D9 de 2 GB")
    _run(config, config_sha256, repetitions)


if __name__ == "__main__":
    main()
