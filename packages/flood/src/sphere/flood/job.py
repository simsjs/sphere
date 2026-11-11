"""Job contract for running SPHERE from another program (``sphere run job.json``).

A *job* is a small JSON file that describes one analysis: the buildings
parquet, the hazard rasters and the options the Marimo notebook asks for.
A *result* is the ``result.json`` manifest SPHERE writes into the job's
output folder when the run ends, whether it succeeded or not.

Both carry ``schema_version`` so a caller (for example an ArcGIS Pro
toolbox) can refuse to talk to a SPHERE build it does not understand.
See ``docs/job-contract.md`` for the full description.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: Version of the job and result file formats. Bump on any breaking change.
JOB_SCHEMA_VERSION = 1

METHODS = ("hazus1", "hazus2")
FLOOD_TYPES = ("R", "C")
COST_BASES = ("depreciated", "full_replacement")
HAZARD_UNITS = ("feet", "meters")

_KNOWN_KEYS = {
    "schema_version",
    "buildings",
    "depth_rasters",
    "velocity_raster",
    "duration_raster",
    "method",
    "flood_type",
    "cost_basis",
    "hazard_units",
    "output_dir",
    "keep_duckdb",
    "overwrite",
}


def sphere_version() -> str:
    """Installed SPHERE version, or ``"unknown"`` when running from an uninstalled checkout."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("sphere-flood")
    except PackageNotFoundError:
        return "unknown"


class OutputExistsError(FileExistsError):
    """The output folder already holds results and the job did not set ``overwrite``."""


class JobValidationError(ValueError):
    """Raised when a job file is invalid. ``errors`` lists every problem found."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class Job:
    """One SPHERE analysis, as described by a job file.

    Field names and allowed values mirror the Marimo notebook
    (``examples/notebooks/run_sphere.py``) so both produce the same results.
    """

    buildings: str
    depth_rasters: list[str]
    output_dir: str
    method: str = "hazus1"
    flood_type: str = "R"
    cost_basis: str = "depreciated"
    hazard_units: str = "feet"
    velocity_raster: str | None = None
    duration_raster: str | None = None
    keep_duckdb: bool = True
    overwrite: bool = False
    schema_version: int = JOB_SCHEMA_VERSION
    #: Non-fatal notes found while validating (for example ignored inputs).
    warnings: list[str] = field(default_factory=list, compare=False)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_file(cls, path: str | os.PathLike) -> Job:
        """Read and validate a job file. Relative paths resolve against the file's folder."""
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise JobValidationError([f"Job file not found: {path}"]) from None
        except json.JSONDecodeError as exc:
            raise JobValidationError([f"Job file is not valid JSON: {exc}"]) from None
        return cls.from_dict(data, base_dir=path.parent)

    @classmethod
    def from_dict(cls, data: Any, base_dir: str | os.PathLike | None = None) -> Job:
        """Validate a job dictionary, collecting every problem before raising."""
        if not isinstance(data, dict):
            raise JobValidationError(["Job must be a JSON object."])

        errors: list[str] = []
        warnings: list[str] = []
        base = Path(base_dir) if base_dir is not None else Path.cwd()

        def resolve(p: Any, label: str) -> str | None:
            if p is None or p == "":
                return None
            if not isinstance(p, str):
                errors.append(f"'{label}' must be a path string.")
                return None
            resolved = Path(p)
            if not resolved.is_absolute():
                resolved = base / resolved
            resolved_str = str(resolved.resolve())
            if "'" in resolved_str:
                # Paths are interpolated into DuckDB SQL by the loaders.
                errors.append(f"'{label}' path contains an apostrophe, which SPHERE cannot read yet: {resolved_str}")
            return resolved_str

        def must_exist(p: str | None, label: str) -> None:
            if p is not None and not os.path.isfile(p):
                errors.append(f"'{label}' file not found: {p}")

        unknown = sorted(set(data) - _KNOWN_KEYS)
        if unknown:
            warnings.append(f"Ignored unknown keys: {', '.join(unknown)}")

        version = data.get("schema_version")
        if version is None:
            errors.append("'schema_version' is required.")
        elif isinstance(version, bool) or version != JOB_SCHEMA_VERSION:
            errors.append(
                f"'schema_version' {version!r} is not supported; this SPHERE build reads version {JOB_SCHEMA_VERSION}."
            )

        method = data.get("method", "hazus1")
        if method not in METHODS:
            errors.append(f"'method' must be one of {', '.join(METHODS)}; got {method!r}.")
        flood_type = data.get("flood_type", "R")
        if flood_type not in FLOOD_TYPES:
            errors.append(f"'flood_type' must be one of {', '.join(FLOOD_TYPES)}; got {flood_type!r}.")
        cost_basis = data.get("cost_basis", "depreciated")
        if cost_basis not in COST_BASES:
            errors.append(f"'cost_basis' must be one of {', '.join(COST_BASES)}; got {cost_basis!r}.")
        hazard_units = data.get("hazard_units", "feet")
        if hazard_units not in HAZARD_UNITS:
            errors.append(f"'hazard_units' must be one of {', '.join(HAZARD_UNITS)}; got {hazard_units!r}.")

        for flag in ("keep_duckdb", "overwrite"):
            if flag in data and not isinstance(data[flag], bool):
                errors.append(f"'{flag}' must be true or false.")

        buildings = resolve(data.get("buildings"), "buildings")
        if buildings is None:
            errors.append("'buildings' is required.")
        must_exist(buildings, "buildings")

        raw_depths = data.get("depth_rasters")
        depth_rasters: list[str] = []
        if not isinstance(raw_depths, list) or not raw_depths:
            errors.append("'depth_rasters' must be a list with at least one raster path.")
        else:
            for i, p in enumerate(raw_depths):
                r = resolve(p, f"depth_rasters[{i}]")
                must_exist(r, f"depth_rasters[{i}]")
                if r is not None:
                    depth_rasters.append(r)
            stems = [Path(p).stem for p in depth_rasters]
            dupes = sorted({s for s in stems if stems.count(s) > 1})
            if dupes:
                # Results and DuckDB files are keyed by file name; duplicates would overwrite each other.
                errors.append(f"Depth rasters must have distinct file names; repeated: {', '.join(dupes)}.")

        velocity = resolve(data.get("velocity_raster"), "velocity_raster")
        duration = resolve(data.get("duration_raster"), "duration_raster")
        if method == "hazus1" and (velocity or duration):
            # The notebook ignores these for the original method; do the same.
            warnings.append("velocity_raster and duration_raster are only used by method 'hazus2'; ignored.")
            velocity = duration = None
        must_exist(velocity, "velocity_raster")
        must_exist(duration, "duration_raster")

        output_dir = resolve(data.get("output_dir"), "output_dir")
        if output_dir is None:
            errors.append("'output_dir' is required.")
        elif os.path.isfile(output_dir):
            errors.append(f"'output_dir' is a file, not a folder: {output_dir}")

        if errors:
            raise JobValidationError(errors)

        return cls(
            schema_version=version,
            buildings=buildings,  # type: ignore[arg-type]
            depth_rasters=depth_rasters,
            output_dir=output_dir,  # type: ignore[arg-type]
            method=method,
            flood_type=flood_type,
            cost_basis=cost_basis,
            hazard_units=hazard_units,
            velocity_raster=velocity,
            duration_raster=duration,
            keep_duckdb=data.get("keep_duckdb", True),
            overwrite=data.get("overwrite", False),
            warnings=warnings,
        )

    def to_dict(self) -> dict[str, Any]:
        """The normalised job, as echoed into ``result.json``."""
        d = asdict(self)
        d.pop("warnings")
        return d
