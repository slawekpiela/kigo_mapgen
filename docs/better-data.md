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

## Higher-resolution terrain

The new ultra terrain option uses `1.0` arcsecond per pixel.  For that
resolution, mapgen now requests DEM tiles from `dem1/` by default.  The files
must use the same 1x1 degree tile naming scheme as the current `dem3/` tiles.
Ultra terrain tries `.hgt` first and then `.tif`, for example `n49e019.hgt`
or `n49e019.tif`.

If you want to use a different compatible DEM directory or extension list, set
`MAPGEN_DEM_DATASET` and optionally `MAPGEN_DEM_EXTENSIONS`:

```sh
MAPGEN_DATA_URL=https://example.com/mapgen-data/ \
MAPGEN_DEM_DATASET=copernicus30 \
MAPGEN_DEM_EXTENSIONS=tif \
./bin/mapgen -r 1 -l 4 -b 18 22 51 49 poland-ultra.xcm
```

Good source candidates are SRTM 1 arc-second HGT tiles, or Copernicus GLO-30
converted into 1x1 degree HGT tiles.  Do not only resample existing `dem3` data
to 1 arcsecond, because that creates larger files without real extra detail.

## Higher-detail topology

Topology detail is controlled by the `manifest` file.  The CLI and web UI now
accept `level_of_detail = 4`.  To make that useful, add extra OSM-derived
layers to your custom manifest with `"level_of_detail": 4`, for example:

```json
{
  "name": "track_line",
  "level_of_detail": 4,
  "dataset": "osm",
  "layer": "track_line",
  "range": 1,
  "color": "150,150,145"
}
```

Recommended additional OSM layers for gliding maps:

- `track_line` and `path_line`, shown only at close range.
- `forest_area` and other landcover polygons, with conservative styling.
- `hamlet_point` or `isolated_dwelling_point`, with close-range labels only.
- More detailed water, river, and stream layers.
- Selected power lines or towers if useful for visual navigation.

Keep ranges small for dense layers.  Very detailed shapefiles can make `.xcm`
files large and can slow down rendering in XCSoar.

## Web high-quality mode

The web frontend exposes a single "high quality" option for the map style used
by Kigo test maps around EPBA/Bielsko.  It reproduces the manual generation
path:

```sh
MAPGEN_DATA_URL=file:///opt/mapgen/high-quality-data/ \
MAPGEN_DEM_EXTENSIONS=tif \
./bin/mapgen -r 1 -l 4 -b <left> <right> <top> <bottom> output.xcm
```

When this option is selected, the server stores the job with:

- terrain resolution `1.0` arcsecond per pixel,
- topology level of detail `4`,
- uncompressed topology, matching the manual path,
- the data repository from `MAPGEN_HIGH_QUALITY_DATA_URL`.

For Docker Compose, mount the prepared repository and start the worker with:

```sh
MAPGEN_HIGH_QUALITY_DATA_DIR=/home/slawek/mapgen-data-nowa_mapa2/repo docker compose up -d --force-recreate mapgen-worker
```

The generated `.xcm` includes `ATTRIBUTION.txt`.  Its contents come from the
custom repository `manifest` `attribution` field, which must list the real data
sources, for example OpenStreetMap/Geofabrik and Copernicus DEM GLO-30.
