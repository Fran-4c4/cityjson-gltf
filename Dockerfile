# syntax=docker/dockerfile:1
#
# cityjson-gltf — convert a CityJSON into a binary glTF (.glb) plus a
# 3D Tiles tileset.json, ready for publish_buildings.py.
#
# Base: python:3.12-slim, the default pattern for tasks with no GIS CLI needs.
# This task reads a JSON file and writes a binary one; the only heavy lifting is
# the CRS transform, which pyproj does in-process. No PDAL, no GDAL, no database.

FROM python:3.12-slim

LABEL org.opencontainers.image.title="cityjson-gltf"
LABEL org.opencontainers.image.description="Convert CityJSON to binary glTF (.glb) and a 3D Tiles tileset"

# pyproj ships the PROJ data it needs in the wheel, so there is no system PROJ to
# keep in sync — which is why this task does not need the GDAL base image.
# pytest ships in the image so run-tests.sh can exercise the suite the same way
# it does for the other tasks, without a build step of its own.
# mapbox_earcut tessellates surfaces with interior rings — buildings with a
# courtyard. Without it a patio would be filled in, inflating areas and casting
# shadows onto open ground.
RUN pip install --no-cache-dir "pyproj==3.7.*" "mapbox-earcut==1.0.*" "pytest==8.*"

WORKDIR /app

ENV PYTHONPATH=/app/src:/opt/task_runtime

COPY --from=task_runtime src/task_runtime_base /opt/task_runtime/task_runtime_base
COPY src /app/src

COPY tests /app/tests

CMD ["python3", "-m", "cityjson_gltf.task"]
