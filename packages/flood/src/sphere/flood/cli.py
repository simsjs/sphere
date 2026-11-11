"""``sphere`` command line: run a job file and report progress as JSON lines.

    sphere run JOB.json        run the analysis; progress events on stdout
    sphere validate JOB.json   check a job file without running it
    sphere version             print SPHERE and job-schema versions as JSON

stdout carries only JSON objects, one per line, so a calling program can
parse it as the run goes. Anything the analysis libraries print is sent to
stderr instead. Callers should still skip any stdout line that is not JSON.

Exit codes: 0 success, 1 the run failed, 2 the job file or arguments are invalid.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Sequence
from typing import Any

EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_INVALID = 2


def _emit(event: str, **fields: Any) -> None:
    """Write one JSON event to the real stdout, even while stdout is redirected."""
    sys.__stdout__.write(json.dumps({"event": event, **fields}, default=str) + "\n")
    sys.__stdout__.flush()


def _cmd_version(_: argparse.Namespace) -> int:
    from sphere.flood.job import JOB_SCHEMA_VERSION, sphere_version

    _emit("version", sphere_version=sphere_version(), job_schema_version=JOB_SCHEMA_VERSION)
    return EXIT_OK


def _load_job(path: str):
    from sphere.flood.job import Job, JobValidationError

    try:
        job = Job.from_file(path)
    except JobValidationError as exc:
        _emit("invalid", errors=exc.errors)
        return None
    for w in job.warnings:
        _emit("warning", message=w)
    return job


def _cmd_validate(args: argparse.Namespace) -> int:
    job = _load_job(args.job)
    if job is None:
        return EXIT_INVALID
    _emit("valid", job=job.to_dict())
    return EXIT_OK


def _cmd_run(args: argparse.Namespace) -> int:
    job = _load_job(args.job)
    if job is None:
        return EXIT_INVALID

    from sphere.flood.job import OutputExistsError
    from sphere.flood.runner import RESULT_JSON, run_job

    def progress(step: str, message: str, percent: float) -> None:
        _emit("progress", step=step, message=message, percent=round(percent, 1))

    _emit("started", output_dir=job.output_dir)
    try:
        # Library code prints timings; keep stdout clean for the JSON stream.
        with contextlib.redirect_stdout(sys.stderr):
            manifest = run_job(job, progress=progress)
    except OutputExistsError as exc:
        _emit("invalid", errors=[str(exc)])
        return EXIT_INVALID
    except Exception as exc:  # noqa: BLE001 - reported to the caller, then exit non-zero
        result = f"{job.output_dir}/{RESULT_JSON}"
        _emit("failed", error_type=type(exc).__name__, message=str(exc), result=result)
        return EXIT_RUN_FAILED

    _emit(
        "succeeded",
        result=f"{job.output_dir}/{RESULT_JSON}",
        summary=manifest["summary"],
        warnings=manifest["warnings"],
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sphere", description="Run SPHERE flood loss analyses from a job file.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the analysis described by a job file.")
    p_run.add_argument("job", help="Path to the job JSON file.")
    p_run.set_defaults(func=_cmd_run)

    p_val = sub.add_parser("validate", help="Check a job file without running it.")
    p_val.add_argument("job", help="Path to the job JSON file.")
    p_val.set_defaults(func=_cmd_validate)

    p_ver = sub.add_parser("version", help="Print SPHERE and job-schema versions as JSON.")
    p_ver.set_defaults(func=_cmd_version)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits 2 on bad usage, 0 on --help
        return int(exc.code or 0)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
