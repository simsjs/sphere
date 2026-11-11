"""Tests for the job contract, the runner and the ``sphere`` command.

The parity tests replay the Marimo notebook's *Execute Analysis* and
*Export Results* cells (examples/notebooks/run_sphere.py) and require the
runner to produce identical files. The runner must not change any number.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_origin
from sphere.core.schemas.nsi_buildings_2026 import NsiBuildings2026
from sphere.flood.analysis.hazus_flood import HazusFloodAnalysis
from sphere.flood.analysis.hazus_flood2 import HazusFloodAnalysis2
from sphere.flood.default_vulnerability import DefaultFloodVulnerability
from sphere.flood.job import (
    JOB_SCHEMA_VERSION,
    Job,
    JobValidationError,
    OutputExistsError,
)
from sphere.flood.runner import (
    BY_RASTER_PARQUET,
    RESULT_JSON,
    WIDE_PARQUET,
    run_job,
)
from sphere.flood.single_value_reader import SingleValueRaster

OCCUPANCIES = ["RES1", "RES2", "RES3A", "RES4", "COM1", "COM4", "IND2", "EDU1", "GOV1", "REL1"]


# ---------------------------------------------------------------------------
# Synthetic inputs
# ---------------------------------------------------------------------------
def _write_raster(path: Path, arr: np.ndarray) -> str:
    with rasterio.open(
        path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1,
        dtype="float32", crs="EPSG:4326", transform=from_origin(-90.0, 30.0, 0.001, 0.001), nodata=-9999,
    ) as dst:
        dst.write(arr.astype("float32"), 1)
    return str(path)


@pytest.fixture(scope="module")
def inputs(tmp_path_factory) -> dict[str, str]:
    """A 250-building NSI 2026 parquet and four 60x60 rasters over the same area."""
    d = tmp_path_factory.mktemp("inputs")
    rng = np.random.default_rng(42)
    n = 250
    df = pd.DataFrame(
        {
            "fd_id": np.arange(1, n + 1),
            "occtype": [f"{o}-1SNB" for o in rng.choice(OCCUPANCIES, n)],
            "found_ht": rng.uniform(0, 4, n),
            "found_type": rng.choice(list("CSBPWIF"), n),
            "num_story": rng.integers(1, 5, n),
            "sqft": rng.integers(600, 12000, n),
            "val_struct": rng.uniform(5e4, 1.5e6, n),
            "val_cont": rng.uniform(2e4, 7e5, n),
            "fullrep": rng.uniform(8e4, 2e6, n),
            "bldgtype": rng.choice(["W", "M", "C", "S", "H"], n),
            # Some buildings fall outside the rasters (x > -89.94) and must be filtered out.
            "x": rng.uniform(-90.0, -89.93, n),
            "y": rng.uniform(29.94, 30.0, n),
        }
    )
    gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.x, df.y), crs="EPSG:4326")
    gdf["shape"] = gdf.geometry.to_wkb()
    parquet = d / "nsi2026.parquet"
    gdf.drop(columns="geometry").to_parquet(parquet)

    return {
        "buildings": str(parquet),
        "rp100": _write_raster(d / "rp100.tif", rng.uniform(-2, 10, (60, 60))),
        "rp500": _write_raster(d / "rp500.tif", rng.uniform(0, 16, (60, 60))),
        "velocity": _write_raster(d / "velocity.tif", rng.uniform(0, 8, (60, 60))),
        "duration": _write_raster(d / "duration.tif", rng.uniform(0, 100, (60, 60))),
    }


def _job_dict(inputs: dict[str, str], output_dir: Path, **overrides) -> dict:
    job = {
        "schema_version": JOB_SCHEMA_VERSION,
        "buildings": inputs["buildings"],
        "depth_rasters": [inputs["rp100"], inputs["rp500"]],
        "method": "hazus1",
        "flood_type": "R",
        "cost_basis": "depreciated",
        "hazard_units": "feet",
        "output_dir": str(output_dir),
    }
    job.update(overrides)
    return job


# ---------------------------------------------------------------------------
# The notebook, replayed (examples/notebooks/run_sphere.py, run + export cells)
# ---------------------------------------------------------------------------
def _run_like_notebook(
    run_output_dir: Path,
    building_file: str,
    depth_raster_files: list[str],
    velocity_raster_file: str | None,
    duration_raster_file: str | None,
    analysis_method: str,
    flood_type: str,
    hazard_units: str,
    cost_basis: str,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    _METERS_TO_FEET = 1.0 / 0.3048
    conversion_factor = _METERS_TO_FEET if hazard_units == "meters" else 1.0
    if analysis_method != "hazus2":  # validation cell only keeps these for hazus2
        velocity_raster_file = duration_raster_file = None
    os.makedirs(run_output_dir, exist_ok=True)

    depth_grids = [SingleValueRaster(f, conversion_factor=conversion_factor) for f in depth_raster_files]
    velocity_grid = SingleValueRaster(velocity_raster_file, conversion_factor=conversion_factor) if velocity_raster_file else None
    duration_grid = SingleValueRaster(duration_raster_file, conversion_factor=conversion_factor) if duration_raster_file else None
    _all_rasters = depth_grids + [r for r in [velocity_grid, duration_grid] if r is not None]

    buildings = NsiBuildings2026(building_file, rasters=_all_rasters, cost_basis=cost_basis)

    results_by_raster = {}
    for _depth_grid in depth_grids:
        _stem = Path(_depth_grid.data_source).stem
        _duckdb_path = os.path.join(run_output_dir, f"sphere_analysis_{_stem}.duckdb")
        Path(_duckdb_path).unlink(missing_ok=True)
        _conn = duckdb.connect(_duckdb_path)
        if analysis_method == "hazus1":
            _vuln = DefaultFloodVulnerability(buildings, flood_type=flood_type)
            _analyzer = HazusFloodAnalysis(buildings=buildings, vulnerability_func=_vuln, depth_grid=_depth_grid)
        else:
            _analyzer = HazusFloodAnalysis2(
                buildings=buildings,
                depth_grid=_depth_grid,
                flood_type=flood_type,
                velocity_grid=velocity_grid,
                duration_grid=duration_grid,
            )
        _losses = _analyzer.calculate_losses_duckdb(_conn)
        _conn.close()
        results_by_raster[_stem] = _losses

    # Export Results cell
    _bdf = buildings.gdf.drop(columns=["geometry"], errors="ignore")
    _all_losses = [ldf[["id", "building_loss", "content_loss", "inventory_loss"]] for ldf in results_by_raster.values()]
    _combined = pd.concat(_all_losses)
    _agg = _combined.groupby("id", as_index=False)[["building_loss", "content_loss", "inventory_loss"]].sum()
    _agg["total_loss"] = _agg["building_loss"] + _agg["content_loss"] + _agg["inventory_loss"]
    wide_df = _bdf.merge(_agg, left_on="fd_id", right_on="id", how="left").drop(columns=["id"], errors="ignore")
    for _col in ["building_loss", "content_loss", "inventory_loss", "total_loss"]:
        wide_df[_col] = wide_df[_col].fillna(0)
    wide_df.to_parquet(os.path.join(run_output_dir, "sphere_results_wide.parquet"), compression="zstd", index=False)
    return wide_df, results_by_raster


# ---------------------------------------------------------------------------
# Parity: the runner reproduces the notebook exactly
# ---------------------------------------------------------------------------
SCENARIOS = [
    pytest.param("hazus1", "R", "feet", "depreciated", False, id="m1-riverine-feet-depreciated"),
    pytest.param("hazus1", "C", "meters", "full_replacement", False, id="m1-coastal-meters-fullrep"),
    pytest.param("hazus2", "R", "meters", "depreciated", True, id="m2-riverine-meters-vel-dur"),
    pytest.param("hazus2", "C", "feet", "full_replacement", True, id="m2-coastal-feet-fullrep-vel-dur"),
    pytest.param("hazus2", "R", "feet", "depreciated", False, id="m2-riverine-feet-no-vel-dur"),
]


@pytest.mark.parametrize("method,flood_type,units,cost_basis,use_vel_dur", SCENARIOS)
def test_runner_matches_notebook(tmp_path, inputs, method, flood_type, units, cost_basis, use_vel_dur):
    vel = inputs["velocity"] if use_vel_dur else None
    dur = inputs["duration"] if use_vel_dur else None

    _nb_wide, nb_by_raster = _run_like_notebook(
        tmp_path / "notebook", inputs["buildings"], [inputs["rp100"], inputs["rp500"]],
        vel, dur, method, flood_type, units, cost_basis,
    )

    job = Job.from_dict(
        _job_dict(
            inputs, tmp_path / "runner", method=method, flood_type=flood_type, hazard_units=units,
            cost_basis=cost_basis, velocity_raster=vel, duration_raster=dur,
        )
    )
    manifest = run_job(job)
    assert manifest["status"] == "succeeded"

    # The wide parquet must be byte-for-byte the same table the notebook writes.
    runner_wide = pd.read_parquet(tmp_path / "runner" / WIDE_PARQUET)
    notebook_wide = pd.read_parquet(tmp_path / "notebook" / WIDE_PARQUET)
    pd.testing.assert_frame_equal(runner_wide, notebook_wide, check_exact=True)
    assert (runner_wide["total_loss"] > 0).any(), "scenario should produce some losses"

    # Each raster's losses, kept separate, match the notebook's per-raster DataFrames.
    by_raster = pd.read_parquet(tmp_path / "runner" / BY_RASTER_PARQUET)
    assert set(by_raster["raster"]) == set(nb_by_raster)
    for stem, nb_losses in nb_by_raster.items():
        got = (
            by_raster[by_raster["raster"] == stem]
            .drop(columns="raster")
            .rename(columns={"fd_id": "id"})
            .reset_index(drop=True)
        )
        pd.testing.assert_frame_equal(got, nb_losses.reset_index(drop=True), check_exact=True, check_dtype=False)

    # Manifest totals agree with the files.
    totals = manifest["summary"]["sum_across_rasters"]
    assert totals["total_loss"] == pytest.approx(float(runner_wide["total_loss"].sum()), rel=1e-12)
    assert manifest["summary"]["buildings_in_study_area"] == len(runner_wide)
    assert set(manifest["outputs"]["duckdb"]) == {"rp100", "rp500"}
    for name in manifest["outputs"]["duckdb"].values():
        assert (tmp_path / "runner" / name).is_file()


def test_result_json_on_disk_matches_return_value(tmp_path, inputs):
    manifest = run_job(Job.from_dict(_job_dict(inputs, tmp_path / "out")))
    on_disk = json.loads((tmp_path / "out" / RESULT_JSON).read_text())
    assert on_disk["status"] == "succeeded"
    assert on_disk["summary"] == json.loads(json.dumps(manifest["summary"]))
    assert on_disk["fields"]["id"] == "fd_id"
    assert on_disk["schema_version"] == JOB_SCHEMA_VERSION


def test_keep_duckdb_false_writes_no_database_and_same_numbers(tmp_path, inputs):
    run_job(Job.from_dict(_job_dict(inputs, tmp_path / "with_db")))
    manifest = run_job(Job.from_dict(_job_dict(inputs, tmp_path / "no_db", keep_duckdb=False)))
    assert manifest["outputs"]["duckdb"] == {}
    assert not list((tmp_path / "no_db").glob("*.duckdb"))
    pd.testing.assert_frame_equal(
        pd.read_parquet(tmp_path / "no_db" / WIDE_PARQUET),
        pd.read_parquet(tmp_path / "with_db" / WIDE_PARQUET),
        check_exact=True,
    )


def test_refuses_to_overwrite_previous_results(tmp_path, inputs):
    run_job(Job.from_dict(_job_dict(inputs, tmp_path / "out")))
    with pytest.raises(OutputExistsError):
        run_job(Job.from_dict(_job_dict(inputs, tmp_path / "out")))
    manifest = run_job(Job.from_dict(_job_dict(inputs, tmp_path / "out", overwrite=True)))
    assert manifest["status"] == "succeeded"


def test_failed_run_writes_failed_manifest(tmp_path, inputs):
    bad = tmp_path / "broken.tif"
    bad.write_bytes(b"not a raster")
    job = Job.from_dict(_job_dict(inputs, tmp_path / "out", depth_rasters=[str(bad)]))
    with pytest.raises(rasterio.errors.RasterioIOError):
        run_job(job)
    manifest = json.loads((tmp_path / "out" / RESULT_JSON).read_text())
    assert manifest["status"] == "failed"
    assert manifest["error"]["type"]
    assert manifest["finished_at"] is not None


# ---------------------------------------------------------------------------
# Job validation
# ---------------------------------------------------------------------------
def test_validation_reports_every_problem_at_once(tmp_path):
    with pytest.raises(JobValidationError) as exc:
        Job.from_dict({"schema_version": 1, "method": "hazus9", "flood_type": "X", "depth_rasters": []})
    errors = " | ".join(exc.value.errors)
    for expected in ("'method'", "'flood_type'", "'depth_rasters'", "'buildings' is required", "'output_dir' is required"):
        assert expected in errors


def test_validation_rejects_wrong_schema_version(tmp_path, inputs):
    with pytest.raises(JobValidationError, match="schema_version"):
        Job.from_dict(_job_dict(inputs, tmp_path, schema_version=99))


def test_validation_rejects_boolean_schema_version(tmp_path, inputs):
    with pytest.raises(JobValidationError, match="schema_version"):
        Job.from_dict(_job_dict(inputs, tmp_path, schema_version=True))


def test_validation_rejects_missing_files(tmp_path, inputs):
    with pytest.raises(JobValidationError, match="not found"):
        Job.from_dict(_job_dict(inputs, tmp_path, depth_rasters=[str(tmp_path / "nope.tif")]))


def test_validation_rejects_rasters_with_the_same_file_name(tmp_path, inputs):
    other = tmp_path / "other"
    other.mkdir()
    copy = other / "rp100.tif"
    copy.write_bytes(Path(inputs["rp100"]).read_bytes())
    with pytest.raises(JobValidationError, match="distinct file names"):
        Job.from_dict(_job_dict(inputs, tmp_path / "out", depth_rasters=[inputs["rp100"], str(copy)]))


def test_hazus1_ignores_velocity_and_duration_with_a_warning(tmp_path, inputs):
    job = Job.from_dict(
        _job_dict(inputs, tmp_path, velocity_raster=inputs["velocity"], duration_raster=inputs["duration"])
    )
    assert job.velocity_raster is None and job.duration_raster is None
    assert any("only used by method 'hazus2'" in w for w in job.warnings)


def test_relative_paths_resolve_against_the_job_file(tmp_path, inputs):
    folder = Path(inputs["buildings"]).parent
    job_file = folder / "job.json"
    job_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "buildings": "nsi2026.parquet",
                "depth_rasters": ["rp100.tif"],
                "output_dir": "out_relative",
            }
        )
    )
    job = Job.from_file(job_file)
    assert job.buildings == str((folder / "nsi2026.parquet").resolve())
    assert job.output_dir == str((folder / "out_relative").resolve())


# ---------------------------------------------------------------------------
# The command, run as a separate process (how ArcGIS Pro will call it)
# ---------------------------------------------------------------------------
def _sphere(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "sphere.flood.cli", *args], check=False, capture_output=True, text=True, timeout=600
    )


def _events(stdout: str) -> list[dict]:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]  # every stdout line must be JSON


def test_cli_run_streams_json_and_succeeds(tmp_path, inputs):
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps(_job_dict(inputs, tmp_path / "out", method="hazus2")))
    proc = _sphere("run", str(job_file))
    assert proc.returncode == 0, proc.stderr
    events = _events(proc.stdout)
    assert events[0]["event"] == "started"
    assert any(e["event"] == "progress" for e in events)
    percents = [e["percent"] for e in events if e["event"] == "progress"]
    assert percents == sorted(percents) and percents[-1] == 100
    assert events[-1]["event"] == "succeeded"
    assert Path(events[-1]["result"]).is_file()


def test_cli_invalid_job_exits_2(tmp_path):
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps({"schema_version": 1}))
    proc = _sphere("run", str(job_file))
    assert proc.returncode == 2
    events = _events(proc.stdout)
    assert events[-1]["event"] == "invalid" and events[-1]["errors"]


def test_cli_failed_run_exits_1(tmp_path, inputs):
    bad = tmp_path / "broken.tif"
    bad.write_bytes(b"not a raster")
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps(_job_dict(inputs, tmp_path / "out", depth_rasters=[str(bad)])))
    proc = _sphere("run", str(job_file))
    assert proc.returncode == 1
    assert _events(proc.stdout)[-1]["event"] == "failed"
    assert json.loads((tmp_path / "out" / RESULT_JSON).read_text())["status"] == "failed"


def test_cli_validate_and_version(tmp_path, inputs):
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps(_job_dict(inputs, tmp_path / "out")))
    proc = _sphere("validate", str(job_file))
    assert proc.returncode == 0
    assert _events(proc.stdout)[-1]["event"] == "valid"
    assert not (tmp_path / "out").exists(), "validate must not create outputs"

    proc = _sphere("version")
    ev = _events(proc.stdout)[-1]
    assert ev["event"] == "version" and ev["job_schema_version"] == JOB_SCHEMA_VERSION
