# Running SPHERE from another program: the job contract

`sphere run job.json` runs one analysis from a small JSON file and writes its
results to a folder. It is how other programs, such as an ArcGIS Pro
toolbox, use SPHERE without loading it into their own Python. SPHERE runs as
a separate process: the caller writes a job file, starts the command, reads
progress from its output, then reads `result.json` and the result files.

The command produces exactly the numbers the Marimo notebook
(`examples/notebooks/run_sphere.py`) produces for the same inputs.
`tests/test_job_runner.py` replays the notebook's run and export cells and
fails if any number differs.

Contract version: **1** (`schema_version` in both files).

## Commands

| Command | What it does |
| --- | --- |
| `sphere run JOB.json` | Validates the job, runs it, writes results; progress on stdout |
| `sphere validate JOB.json` | Validates only; writes nothing |
| `sphere version` | Prints `{"event": "version", "sphere_version": …, "job_schema_version": 1}` |

`python -m sphere.flood.cli …` is equivalent when the `sphere` script is not on `PATH`.

## The job file

```json
{
  "schema_version": 1,
  "buildings": "C:/sphere/data/nsi2026_county.parquet",
  "depth_rasters": ["C:/sphere/data/rp100.tif", "C:/sphere/data/rp500.tif"],
  "velocity_raster": "C:/sphere/data/velocity.tif",
  "duration_raster": "C:/sphere/data/duration.tif",
  "method": "hazus2",
  "flood_type": "R",
  "cost_basis": "depreciated",
  "hazard_units": "feet",
  "output_dir": "C:/sphere/runs/2026-11-11_rp"
}
```

| Key | Required | Values | Notes |
| --- | --- | --- | --- |
| `schema_version` | yes | `1` | Any other value is refused |
| `buildings` | yes | path | NSI 2026 geoparquet |
| `depth_rasters` | yes | list of paths, at least one | One analysis per raster. File names must differ: results are keyed by file name without extension |
| `velocity_raster` | no | path or `null` | Method `hazus2` only; ignored with a warning for `hazus1` |
| `duration_raster` | no | path or `null` | Method `hazus2` only; ignored with a warning for `hazus1` |
| `method` | no, default `hazus1` | `hazus1`, `hazus2` | Original HAZUS, or the updated method |
| `flood_type` | no, default `R` | `R`, `C` | Riverine or coastal |
| `cost_basis` | no, default `depreciated` | `depreciated`, `full_replacement` | `val_struct`/`val_cont`, or `fullrep` |
| `hazard_units` | no, default `feet` | `feet`, `meters` | Applied to every raster, as the notebook does |
| `output_dir` | yes | path | Created if missing |
| `keep_duckdb` | no, default `true` | `true`, `false` | Keep one `.duckdb` file per raster for inspection |
| `overwrite` | no, default `false` | `true`, `false` | Without it, a folder that already holds results is refused |

- Relative paths resolve against the folder holding the job file.
- Write the file with a JSON library. On Windows, backslashes must be escaped
  (`"C:\\data\\rp100.tif"`); forward slashes also work.
- Rasters must be files on disk (GeoTIFF or another format GDAL reads). A
  raster stored inside a geodatabase should be exported to GeoTIFF first.
- Paths containing an apostrophe are refused for now.
- Unknown keys are ignored with a warning.

## What a run writes

| File | Contents |
| --- | --- |
| `result.json` | The manifest below. Written when the run starts (`"status": "running"`) and rewritten when it ends, including on failure |
| `sphere_results_wide.parquet` | Same file the notebook writes: every building in the study area, its input columns (no geometry), and `building_loss`, `content_loss`, `inventory_loss`, `total_loss` **summed across all depth rasters**; 0 for buildings that stayed dry |
| `sphere_losses_by_raster.parquet` | Each raster's losses kept separate: `raster`, `fd_id`, `building_loss`, `content_loss`, `inventory_loss`, one row per flooded building per raster |
| `sphere_analysis_<raster>.duckdb` | Every intermediate table for that raster (when `keep_duckdb` is true) |

Buildings are points: `x` and `y` are longitude and latitude in EPSG:4326.
`fd_id` is the building key in both parquet files. Losses are US dollars.

## result.json

```json
{
  "schema_version": 1,
  "status": "succeeded",
  "sphere_version": "0.3.0",
  "python_version": "3.10.20",
  "started_at": "2026-11-11T13:45:31+00:00",
  "finished_at": "2026-11-11T13:45:32+00:00",
  "elapsed_s": 1.072,
  "job": { "...": "the job, normalised: absolute paths, defaults filled in" },
  "warnings": [],
  "outputs": {
    "wide_parquet": "sphere_results_wide.parquet",
    "losses_by_raster_parquet": "sphere_losses_by_raster.parquet",
    "duckdb": {"rp100": "sphere_analysis_rp100.duckdb", "rp500": "sphere_analysis_rp500.duckdb"}
  },
  "fields": {
    "id": "fd_id", "x": "x", "y": "y", "crs": "EPSG:4326",
    "losses": ["building_loss", "content_loss", "inventory_loss", "total_loss"],
    "currency": "USD"
  },
  "summary": {
    "buildings_in_study_area": 214,
    "buildings_with_loss": 161,
    "rasters": [
      {"name": "rp100", "depth_raster": "C:/sphere/demo/rp100.tif", "flooded_buildings": 182,
       "building_loss": 25329334.55, "content_loss": 10275436.43, "inventory_loss": 0.0, "elapsed_s": 0.525},
      {"name": "rp500", "depth_raster": "C:/sphere/demo/rp500.tif", "flooded_buildings": 214,
       "building_loss": 47693991.57, "content_loss": 11857875.29, "inventory_loss": 0.0, "elapsed_s": 0.498}
    ],
    "sum_across_rasters": {"building_loss": 73023326.12, "content_loss": 22133311.72,
                           "inventory_loss": 0.0, "total_loss": 95156637.84}
  },
  "error": null
}
```

- `status` is `running`, `succeeded` or `failed`.
- File names in `outputs` are relative to `output_dir`.
- On failure, `summary` is `null` and `error` holds `type`, `message` and `traceback`.

## Progress and exit codes

While running, stdout carries one JSON object per line:

| `event` | Fields | When |
| --- | --- | --- |
| `warning` | `message` | A non-fatal note about the job |
| `started` | `output_dir` | The job is valid and the run begins |
| `progress` | `step`, `message`, `percent` (0–100, never decreasing) | Steps: `load_rasters`, `load_buildings`, `analyze` (once per raster), `export`, `done` |
| `succeeded` | `result`, `summary`, `warnings` | Last line of a successful run |
| `failed` | `error_type`, `message`, `result` | Last line when the run fails |
| `invalid` | `errors` (list of strings) | Last line when the job is invalid or the output folder already holds results |
| `valid` | `job` | Last line of `sphere validate` |

Anything the analysis libraries print goes to stderr. Callers should still skip
any stdout line that does not parse as JSON.

| Exit code | Meaning |
| --- | --- |
| `0` | Succeeded |
| `1` | The run failed; see `result.json` → `error` |
| `2` | The job file or arguments are invalid; nothing was run |

To cancel a run, end the process. `result.json` then stays at `"status": "running"`;
treat that as cancelled.

## Behaviour carried over from the notebook

This contract reproduces the notebook's results as they are today. These known
issues are therefore present in its output and are tracked separately:

- `hazard_units: "meters"` converts the duration raster as if it were a length.
- `sphere_results_wide.parquet` sums losses across depth rasters, which
  overstates totals when the rasters are return periods for one area. Use
  `sphere_losses_by_raster.parquet` to keep them apart.
- `inventory_loss` is always 0 for NSI 2026 buildings.
- Method `hazus2` gives no content loss for coastal runs, maps foundation
  codes 5, 6 and 7 to a blend of all foundation types, and finds no curve for
  content lookups that point to curves 1363–1407.
