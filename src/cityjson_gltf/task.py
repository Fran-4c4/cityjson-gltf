#!/usr/bin/env python3
"""Container entrypoint: convert CityJSON into binary glTF (.glb) plus an
optional 3D Tiles `tileset.json`.

Thin environment wrapper around `cityjson_gltf.converter`, following the container
contract (progress NN%, output under /out/<task_id>). Touches no database.

The input CityJSON is read from the read-only host mount at /host-data.

Env vars:
  CJ_INPUT       path to the input .city.json (under /host-data)
  CJ_SRS_IN      EPSG of the input, overriding metadata.referenceSystem
  CJ_ATTRIBUTES  comma-separated CityObject attributes to expose as glTF
                 metadata (default measuredHeight)
  CJ_TILESET     '1' (default) also writes tileset.json next to the .glb
  TASK_RUN_ID    output subdir under /out
"""
import os
import subprocess
import sys

from task_runtime_base import (
    PathTranslator,
    env,
    env_bool,
    log_info,
    log_step,
    setup_out_dir,
)
from task_runtime_base.errors import TaskError
from task_runtime_base.progress import run_task


def main() -> None:
    out_dir = str(setup_out_dir())

    raw_source = env("CJ_INPUT")
    if not raw_source:
        raise TaskError("CJ_INPUT is not set: no input CityJSON given")

    # The form hands us a *host* path (/data/tenants/<t>/...), but that tree is
    # mounted read-only at /host-data inside the container. Without this
    # translation the file is genuinely not there, which is what made this task
    # fail on a path that exists perfectly well on the host.
    translator = PathTranslator.from_env()
    source = str(translator.to_container_existing(raw_source))

    base = os.path.basename(source)
    for suffix in (".city.json", ".json"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    out_file = os.path.join(out_dir, base + ".glb")

    cmd = ["python3", "-m", "cityjson_gltf.converter", source, out_file]

    srs_in = env("CJ_SRS_IN")
    if srs_in:
        cmd += ["--srs-in", srs_in]

    attributes = env("CJ_ATTRIBUTES", "measuredHeight")
    if attributes:
        cmd += ["--attributes", attributes]

    if env_bool("CJ_TILESET", True):
        cmd += ["--tileset", os.path.join(out_dir, "tileset.json")]

    log_step(20, "Converting CityJSON to glTF")
    result = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise TaskError("the glTF conversion failed")

    if not os.path.exists(out_file) or os.path.getsize(out_file) == 0:
        raise TaskError(
            "no output was produced: {0}".format(translator.display(out_file))
        )

    size_kb = os.path.getsize(out_file) // 1024
    log_info("{0} ({1} KB)".format(translator.display(out_file), size_kb))
    log_step(100, "Completed")


if __name__ == "__main__":
    run_task(main)
