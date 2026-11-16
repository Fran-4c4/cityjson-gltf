# cityjson-gltf

Convert CityJSON building geometry into a binary glTF (`.glb`) and an optional
3D Tiles `tileset.json`. The generated glTF uses local metre coordinates,
Y-up orientation, and feature metadata suitable for picking and styling.

## Requirements

- Docker with BuildKit/Buildx
- The `task_runtime` source repository, either as a sibling checkout or as a
  public Git repository used directly by Docker

## Build with sibling repositories

Clone both autonomous repositories into the same parent directory:

```text
workspace/
├── cityjson-gltf/
└── task_runtime/
```

Then build from `cityjson-gltf`:

```bash
docker buildx bake --allow=fs.read=../task_runtime cityjson-gltf
```

The included `docker-bake.hcl` uses `../task_runtime` by default. The equivalent
command without Bake is:

```bash
docker buildx build \
  --build-context task_runtime=../task_runtime \
  -t cityjson-gltf:local .
```

## Build with task_runtime from GitHub

Docker can fetch the shared source without a local checkout. Pin a release tag
and its commit checksum so the build remains reproducible:

```bash
docker buildx build \
  --build-context 'task_runtime=https://github.com/Fran-4c4/task_runtime.git?tag=v0.2.0&checksum=8be7970d3e8c4f8ed67130ab502ae89e53fe5226' \
  -t cityjson-gltf:1.0.0 .
```

With Bake, override the context variable:

```bash
TASK_RUNTIME_CONTEXT='https://github.com/Fran-4c4/task_runtime.git?tag=v0.2.0&checksum=8be7970d3e8c4f8ed67130ab502ae89e53fe5226' \
  docker buildx bake cityjson-gltf
```

PowerShell:

```powershell
$env:TASK_RUNTIME_CONTEXT = 'https://github.com/Fran-4c4/task_runtime.git?tag=v0.2.0&checksum=8be7970d3e8c4f8ed67130ab502ae89e53fe5226'
docker buildx bake cityjson-gltf
```

## Run the container

The task wrapper expects these mounts:

| Host data | Container path | Access |
|---|---|---|
| Input directory | `/host-data` | read-only |
| Output directory | `/out` | read-write |

Required environment variables:

| Variable | Description |
|---|---|
| `CJ_INPUT` | Input CityJSON path under the host data prefix |
| `TASK_RUN_ID` | Output subdirectory name under `/out` |

Optional environment variables:

| Variable | Default | Description |
|---|---|---|
| `CJ_SRS_IN` | CityJSON metadata | EPSG code overriding `metadata.referenceSystem` |
| `CJ_ATTRIBUTES` | `measuredHeight` | Comma-separated attributes exported as metadata |
| `CJ_TILESET` | `1` | Write `tileset.json` beside the `.glb` |
| `HOST_DATA_PREFIX` | `/data` | Host-side prefix used in `CJ_INPUT` |
| `HOST_DATA_ROOT` | `/host-data` | Matching path inside the container |

Example:

```bash
docker run --rm \
  -e TASK_RUN_ID=example \
  -e CJ_INPUT=/data/buildings.city.json \
  -v "$PWD/data:/host-data:ro" \
  -v "$PWD/output:/out" \
  cityjson-gltf:local
```

Output:

```text
output/example/
├── buildings.glb
└── tileset.json
```

## Run the converter directly

The converter itself does not depend on `task_runtime_base`:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install 'pyproj==3.7.*' 'mapbox-earcut==1.0.*'
PYTHONPATH=src python -m cityjson_gltf.converter input.city.json output.glb \
  --tileset tileset.json
```

## Tests

```bash
python -m pip install 'pyproj==3.7.*' 'mapbox-earcut==1.0.*' 'pytest==8.*'
python -m pytest tests -q
```

Or run the tests in the built image:

```bash
docker run --rm --entrypoint python cityjson-gltf:local -m pytest /app/tests -q
```

## Limitations

- A tileset contains one content node; no LOD hierarchy is generated.
- Draco and meshopt compression are not applied.

## License

MIT. See [LICENSE](LICENSE).

## Author

[Francisco Rafael Moreno Santana](https://github.com/Fran-4c4)
