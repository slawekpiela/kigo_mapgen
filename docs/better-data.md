# Using higher-detail map data

Mapgen can use a custom data repository instead of the public default at
`https://mapgen-data.sigkill.ch/`.  Set `MAPGEN_DATA_URL` for the worker or CLI
process:

```sh
MAPGEN_DATA_URL=https://example.com/mapgen-data/ ./bin/mapgen ...
```

When running with Docker Compose, the worker reads the same variable from the
host environment:

```sh
MAPGEN_DATA_URL=https://example.com/mapgen-data/ docker-compose up -d --build
```

## Data repository layout

The repository must expose the same files that `Downloader` expects:

```text
checksums
manifest
dem1/n49e019.hgt or dem1/n49e019.tif
dem1/n49e020.hgt or dem1/n49e020.tif
waterpolygons/water_polygons.shp
waterpolygons/water_polygons.shx
waterpolygons/water_polygons.dbf
waterpolygons/water_polygons.cpg
osm/planet.7z
```

`checksums` is an md5 list with repository-relative paths:

```text
81483da7a63eedc6898f629e0ad9d2a1  dem1/n49e019.hgt
```

## Disabled higher-resolution terrain

1 arc-second terrain generation is disabled in the web frontend, worker, CLI,
and `Generator.add_terrain()`.  Kigo/TaskMap uses standard mapgen topology with
3 arc-second terrain (`highres=on`) plus the runway post-process.

## Higher-detail topology

Topology detail is controlled by the `manifest` file.  The CLI and web UI now
accept only `level_of_detail` 1-3.  Layers marked with `"level_of_detail": 4`
are treated as disabled high-quality topology and are not generated through the
supported entry points.

Very detailed shapefiles can make `.xcm` files large and can slow down
rendering in XCSoar, so TaskMap generation stays on the standard topology path.

## Disabled web high-quality mode

The web high-quality mode is disabled.  The frontend no longer shows it, and
manual POST requests with `high_quality`, `terrain_plus`, `ultrahighres`, or
`level_of_detail=4` are rejected before a job is queued.
