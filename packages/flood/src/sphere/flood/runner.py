"""Run a SPHERE job end to end, the same way the Marimo notebook does.

``run_job`` reproduces the notebook's *Execute Analysis* and *Export Results*
cells (``examples/notebooks/run_sphere.py``) step for step, so a job run
from the command line gives exactly the numbers the notebook gives for the
same inputs. ``tests/test_job_runner.py`` checks that parity.

On top of the notebook's outputs it writes two files for programs that call
SPHERE: ``sphere_losses_by_raster.parquet`` (each raster's losses kept
separate) and ``result.json`` (what ran, what was written, totals, errors).
"""

from __future__ import annotations

import json
import os
import platform
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
from sphere.core.schemas.nsi_buildings_2026 import NsiBuildings2026
from sphere.flood.analysis.hazus_flood import HazusFloodAnalysis
from sphere.flood.analysis.hazus_flood2 import HazusFloodAnalysis2
from sphere.flood.default_vulnerability import DefaultFloodVulnerability
from sphere.flood.job import JOB_SCHEMA_VERSION, Job, OutputExistsError, sphere_version
from sphere.flood.single_value_reader import SingleValueRaster

#: Same constant as the notebook: exact conversion, 1 foot = 0.3048 m.
METERS_TO_FEET = 1.0 / 0.3048

WIDE_PARQUET = "sphere_results_wide.parquet"
BY_RASTER_PARQUET = "sphere_losses_by_raster.parquet"
RESULT_JSON = "result.json"
LOSS_COLUMNS = ["building_loss", "content_loss", "inventory_loss"]

#: ``progress(step, message, percent)``; percent runs 0-100.
ProgressFn = Callable[[str, str, float], None]



def duckdb_file_name(raster_stem: str) -> str:
    return f"sphere_analysis_{raster_stem}.duckdb"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)  # atomic: a reader never sees a half-written file


def build_wide_results(buildings_gdf: pd.DataFrame, losses_by_stem: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """The notebook's *Export Results* cell: sum each building's losses across rasters.

    Kept line-for-line with the notebook so both write identical parquet files.
    """
    _bdf = buildings_gdf.drop(columns=["geometry"], errors="ignore")

    _all_losses = []
    for _losses_df in losses_by_stem.values():
        _all_losses.append(_losses_df[["id", "building_loss", "content_loss", "inventory_loss"]])

    if _all_losses:
        _combined = pd.concat(_all_losses)
        _agg = (
            _combined
            .groupby("id", as_index=False)[["building_loss", "content_loss", "inventory_loss"]]
            .sum()
        )
        _agg["total_loss"] = _agg["building_loss"] + _agg["content_loss"] + _agg["inventory_loss"]
    else:
        _agg = pd.DataFrame(columns=["id", "building_loss", "content_loss", "inventory_loss", "total_loss"])

    wide_df = _bdf.merge(_agg, left_on="fd_id", right_on="id", how="left").drop(columns=["id"], errors="ignore")
    for _col in ["building_loss", "content_loss", "inventory_loss", "total_loss"]:
        wide_df[_col] = wide_df[_col].fillna(0)
    return wide_df


def _check_output_dir(job: Job) -> Path:
    out = Path(job.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    if not job.overwrite:
        existing = [n for n in (RESULT_JSON, WIDE_PARQUET, BY_RASTER_PARQUET) if (out / n).exists()]
        if existing:
            raise OutputExistsError(
                f"Output folder already holds results ({', '.join(existing)}). "
                "Choose an empty folder or set \"overwrite\": true."
            )
    return out


def run_job(job: Job, progress: ProgressFn | None = None) -> dict[str, Any]:
    """Run ``job`` and return the ``result.json`` manifest (also written to disk).

    Raises whatever the analysis raises, after writing a ``failed`` manifest.
    """
    report = progress or (lambda step, message, percent: None)

    out = _check_output_dir(job)
    result_path = out / RESULT_JSON
    started_wall = _now()
    t0 = time.perf_counter()

    manifest: dict[str, Any] = {
        "schema_version": JOB_SCHEMA_VERSION,
        "status": "running",
        "sphere_version": sphere_version(),
        "python_version": platform.python_version(),
        "started_at": started_wall,
        "finished_at": None,
        "elapsed_s": None,
        "job": job.to_dict(),
        "warnings": list(job.warnings),
        "outputs": {},
        "fields": {
            "id": "fd_id",
            "x": "x",
            "y": "y",
            "crs": "EPSG:4326",
            "losses": LOSS_COLUMNS + ["total_loss"],
            "currency": "USD",
        },
        "summary": None,
        "error": None,
    }
    _write_json(result_path, manifest)

    try:
        # --- Notebook "Execute Analysis" cell -----------------------------------
        conversion_factor = METERS_TO_FEET if job.hazard_units == "meters" else 1.0

        report("load_rasters", f"Opening {len(job.depth_rasters)} depth raster(s)", 2)
        depth_grids = [SingleValueRaster(f, conversion_factor=conversion_factor) for f in job.depth_rasters]
        velocity_grid = (
            SingleValueRaster(job.velocity_raster, conversion_factor=conversion_factor) if job.velocity_raster else None
        )
        duration_grid = (
            SingleValueRaster(job.duration_raster, conversion_factor=conversion_factor) if job.duration_raster else None
        )
        all_rasters = depth_grids + [r for r in [velocity_grid, duration_grid] if r is not None]

        report("load_buildings", "Loading buildings inside the rasters' footprint", 5)
        buildings = NsiBuildings2026(job.buildings, rasters=all_rasters, cost_basis=job.cost_basis)
        n_buildings = len(buildings.gdf)
        report("load_buildings", f"{n_buildings:,} buildings in the study area", 15)

        losses_by_stem: dict[str, pd.DataFrame] = {}
        raster_summaries: list[dict[str, Any]] = []
        duckdb_files: dict[str, str] = {}
        n = len(depth_grids)
        for i, depth_grid in enumerate(depth_grids):
            stem = Path(depth_grid.data_source).stem
            report("analyze", f"Raster {i + 1} of {n}: {stem}", 15 + 75 * i / n)
            db_start = time.perf_counter()
            if job.keep_duckdb:
                db_path = out / duckdb_file_name(stem)
                db_path.unlink(missing_ok=True)
                conn = duckdb.connect(str(db_path))
                duckdb_files[stem] = db_path.name
            else:
                conn = duckdb.connect(":memory:")
            try:
                if job.method == "hazus1":
                    vuln = DefaultFloodVulnerability(buildings, flood_type=job.flood_type)
                    analyzer = HazusFloodAnalysis(buildings=buildings, vulnerability_func=vuln, depth_grid=depth_grid)
                else:
                    analyzer = HazusFloodAnalysis2(
                        buildings=buildings,
                        depth_grid=depth_grid,
                        flood_type=job.flood_type,
                        velocity_grid=velocity_grid,
                        duration_grid=duration_grid,
                    )
                losses = analyzer.calculate_losses_duckdb(conn)
            finally:
                conn.close()
            elapsed = time.perf_counter() - db_start
            losses_by_stem[stem] = losses
            raster_summaries.append(
                {
                    "name": stem,
                    "depth_raster": depth_grid.data_source,
                    "flooded_buildings": len(losses),
                    **{c: float(losses[c].sum()) for c in LOSS_COLUMNS},
                    "elapsed_s": round(elapsed, 3),
                }
            )

        # --- Notebook "Export Results" cell -------------------------------------
        report("export", "Writing results", 92)
        wide_df = build_wide_results(buildings.gdf, losses_by_stem)
        wide_df.to_parquet(out / WIDE_PARQUET, compression="zstd", index=False)

        # --- Extra output for callers: each raster's losses, not summed ---------
        by_raster = (
            pd.concat([df.assign(raster=stem) for stem, df in losses_by_stem.items()], ignore_index=True)
            if losses_by_stem
            else pd.DataFrame(columns=["id", *LOSS_COLUMNS, "raster"])
        )
        # Same building key as the wide file, so callers join on one name.
        by_raster = by_raster.rename(columns={"id": "fd_id"})
        by_raster = by_raster[["raster"] + [c for c in by_raster.columns if c != "raster"]]
        by_raster.to_parquet(out / BY_RASTER_PARQUET, compression="zstd", index=False)

        manifest["outputs"] = {
            "wide_parquet": WIDE_PARQUET,
            "losses_by_raster_parquet": BY_RASTER_PARQUET,
            "duckdb": duckdb_files,
        }
        manifest["summary"] = {
            "buildings_in_study_area": int(n_buildings),
            "buildings_with_loss": int((wide_df["total_loss"] > 0).sum()),
            "rasters": raster_summaries,
            "sum_across_rasters": {
                **{c: float(wide_df[c].sum()) for c in LOSS_COLUMNS},
                "total_loss": float(wide_df["total_loss"].sum()),
            },
        }
        manifest["status"] = "succeeded"
        report("done", "Finished", 100)
        return manifest
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        raise
    finally:
        manifest["finished_at"] = _now()
        manifest["elapsed_s"] = round(time.perf_counter() - t0, 3)
        _write_json(result_path, manifest)
