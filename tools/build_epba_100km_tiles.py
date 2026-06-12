#!/usr/bin/env python3
"""Build 3x3 high-quality source tiles around EPBA.

The output is a mapgen data repository made of source-data tiles, not XCM map
tiles.  Mapgen can then pick all source tiles intersecting a requested bbox and
generate a fresh high-quality map from them.

Run inside the mapgen worker image; it provides GDAL/OGR, 7zr and wget.
"""

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from osgeo import ogr, osr


EPBA_LAT = 49.805
EPBA_LON = 19.00201667
TILE_SIZE_M = 100_000
UTM_EPSG = 32634

AREA_NLT = "MULTIPOLYGON"
LINE_NLT = "MULTILINESTRING"
POINT_NLT = "POINT"
SHAPE_EXTENSIONS = (".shp", ".shx", ".dbf", ".prj", ".cpg")
POINT_SPACING_RULES_M = {}


@dataclass(frozen=True)
class SourcePackage:
    name: str
    url: str
    kind: str  # shp_zip or gpkg_zip
    pbf_url: str


@dataclass(frozen=True)
class LayerSource:
    source_layer: str
    where: str
    nlt: str
    source_file: str = "vector"


@dataclass(frozen=True)
class PreparedPackage:
    vector_path: Path
    pbf_places_path: Optional[Path]


@dataclass
class Tile:
    name: str
    ix: int
    iy: int
    bounds: List[float]  # [left, right, top, bottom]


SOURCES = [
    SourcePackage(
        "pl_slaskie",
        "https://download.geofabrik.de/europe/poland/slaskie-latest-free.shp.zip",
        "shp_zip",
        "https://download.geofabrik.de/europe/poland/slaskie-latest.osm.pbf",
    ),
    SourcePackage(
        "pl_malopolskie",
        "https://download.geofabrik.de/europe/poland/malopolskie-latest-free.shp.zip",
        "shp_zip",
        "https://download.geofabrik.de/europe/poland/malopolskie-latest.osm.pbf",
    ),
    SourcePackage(
        "pl_opolskie",
        "https://download.geofabrik.de/europe/poland/opolskie-latest-free.shp.zip",
        "shp_zip",
        "https://download.geofabrik.de/europe/poland/opolskie-latest.osm.pbf",
    ),
    SourcePackage(
        "pl_swietokrzyskie",
        "https://download.geofabrik.de/europe/poland/swietokrzyskie-latest-free.shp.zip",
        "shp_zip",
        "https://download.geofabrik.de/europe/poland/swietokrzyskie-latest.osm.pbf",
    ),
    SourcePackage(
        "pl_lodzkie",
        "https://download.geofabrik.de/europe/poland/lodzkie-latest-free.shp.zip",
        "shp_zip",
        "https://download.geofabrik.de/europe/poland/lodzkie-latest.osm.pbf",
    ),
    SourcePackage(
        "pl_podkarpackie",
        "https://download.geofabrik.de/europe/poland/podkarpackie-latest-free.shp.zip",
        "shp_zip",
        "https://download.geofabrik.de/europe/poland/podkarpackie-latest.osm.pbf",
    ),
    SourcePackage(
        "sk",
        "https://download.geofabrik.de/europe/slovakia-latest-free.shp.zip",
        "shp_zip",
        "https://download.geofabrik.de/europe/slovakia-latest.osm.pbf",
    ),
    SourcePackage(
        "cz_moravskoslezky",
        "https://download.geofabrik.de/europe/czech-republic/moravskoslezky-latest-free.gpkg.zip",
        "gpkg_zip",
        "https://download.geofabrik.de/europe/czech-republic/moravskoslezky-latest.osm.pbf",
    ),
    SourcePackage(
        "cz_olomoucky",
        "https://download.geofabrik.de/europe/czech-republic/olomoucky-latest-free.gpkg.zip",
        "gpkg_zip",
        "https://download.geofabrik.de/europe/czech-republic/olomoucky-latest.osm.pbf",
    ),
    SourcePackage(
        "cz_zlinsky",
        "https://download.geofabrik.de/europe/czech-republic/zlinsky-latest-free.gpkg.zip",
        "gpkg_zip",
        "https://download.geofabrik.de/europe/czech-republic/zlinsky-latest.osm.pbf",
    ),
]


LAYER_SOURCES: Dict[str, List[LayerSource]] = {
    "city_area": [
        LayerSource(
            "gis_osm_landuse_a_free_1",
            "fclass IN ('residential','commercial','industrial','retail')",
            AREA_NLT,
        ),
    ],
    "water_area_large": [
        LayerSource(
            "gis_osm_water_a_free_1",
            "fclass IN ('water','reservoir')",
            AREA_NLT,
        ),
    ],
    "water_line": [
        LayerSource("gis_osm_waterways_free_1", "fclass IN ('river','canal')", LINE_NLT),
    ],
    "stream_line": [
        LayerSource(
            "gis_osm_waterways_free_1",
            "fclass IN ('stream','drain','ditch')",
            LINE_NLT,
        ),
    ],
    "forest_area": [
        LayerSource("gis_osm_landuse_a_free_1", "fclass = 'forest'", AREA_NLT),
        LayerSource("gis_osm_natural_a_free_1", "fclass IN ('wood','forest')", AREA_NLT),
    ],
    "roadbig_line": [
        LayerSource(
            "gis_osm_roads_free_1",
            "fclass IN ('motorway','motorway_link','trunk','trunk_link','primary','primary_link')",
            LINE_NLT,
        ),
    ],
    "roadmedium_line": [
        LayerSource(
            "gis_osm_roads_free_1",
            "fclass IN ('secondary','secondary_link','tertiary','tertiary_link','unclassified')",
            LINE_NLT,
        ),
    ],
    "city_point": [
        LayerSource("pbf_place_point", "fclass = 'city'", POINT_NLT, "pbf_places"),
    ],
    "town_point": [
        LayerSource("pbf_place_point", "fclass = 'town'", POINT_NLT, "pbf_places"),
    ],
    "suburb_point": [
        LayerSource("pbf_place_point", "fclass = 'suburb'", POINT_NLT, "pbf_places"),
    ],
    "village_point": [
        LayerSource(
            "pbf_place_point",
            "fclass = 'village'",
            POINT_NLT,
            "pbf_places",
        ),
    ],
    "airstrip_area": [
        LayerSource(
            "gis_osm_transport_a_free_1",
            "fclass IN ('airport','airfield','apron','runway')",
            AREA_NLT,
        ),
    ],
    "runway_area": [
        LayerSource("gis_osm_transport_a_free_1", "fclass = 'runway'", AREA_NLT),
    ],
    "runway_line": [
        LayerSource("gis_osm_transport_free_1", "fclass = 'runway'", LINE_NLT),
    ],
}


MANIFEST_LAYERS = [
    {
        "name": "city_area",
        "level_of_detail": 1,
        "dataset": "osm",
        "layer": "city_area",
        "range": 10,
        "color": "223,223,0",
        "alpha": 80,
    },
    {
        "name": "water_area_large",
        "level_of_detail": 1,
        "dataset": "osm",
        "layer": "water_area_large",
        "label": "name",
        "range": 5,
        "color": "98,157,251",
    },
    {
        "name": "water_line",
        "level_of_detail": 1,
        "dataset": "osm",
        "layer": "water_line",
        "label": "name",
        "range": 5,
        "color": "98,157,251",
        "pen_width": 1,
    },
    {
        "name": "stream_line",
        "level_of_detail": 4,
        "dataset": "osm",
        "layer": "stream_line",
        "range": 1,
        "color": "118,180,245",
        "alpha": 120,
    },
    {
        "name": "forest_area",
        "level_of_detail": 4,
        "dataset": "osm",
        "layer": "forest_area",
        "range": 2,
        "color": "157,190,137",
        "alpha": 120,
    },
    {
        "name": "roadbig_line",
        "level_of_detail": 1,
        "dataset": "osm",
        "layer": "roadbig_line",
        "range": 15,
        "color": "218,109,130",
        "pen_width": 3,
    },
    {
        "name": "roadmedium_line",
        "level_of_detail": 2,
        "dataset": "osm",
        "layer": "roadmedium_line",
        "range": 8,
        "color": "229,156,44",
        "pen_width": 2,
    },
    {
        "name": "city_point",
        "level_of_detail": 1,
        "dataset": "osm",
        "layer": "city_point",
        "label": "name",
        "range": 15,
        "label_range": 15,
        "label_important_range": 10,
        "color": "223,223,0",
    },
    {
        "name": "town_point",
        "level_of_detail": 1,
        "dataset": "osm",
        "layer": "town_point",
        "label": "name",
        "range": 10,
        "label_range": 10,
        "label_important_range": 3,
        "color": "223,223,0",
    },
    {
        "name": "suburb_major_point",
        "level_of_detail": 4,
        "dataset": "osm",
        "layer": "suburb_point",
        "where": "population >= 5000",
        "label": "name",
        "range": 3,
        "label_range": 3,
        "color": "223,223,0",
    },
    {
        "name": "suburb_medium_point",
        "level_of_detail": 4,
        "dataset": "osm",
        "layer": "suburb_point",
        "where": "population >= 1000 AND population < 5000",
        "label": "name",
        "range": 3,
        "label_range": 3,
        "color": "223,223,0",
    },
    {
        "name": "suburb_local_point",
        "level_of_detail": 4,
        "dataset": "osm",
        "layer": "suburb_point",
        "where": "population < 1000",
        "label": "name",
        "range": 3,
        "label_range": 3,
        "color": "223,223,0",
    },
    {
        "name": "village_major_point",
        "level_of_detail": 3,
        "dataset": "osm",
        "layer": "village_point",
        "where": "population >= 5000",
        "label": "name",
        "range": 3,
        "label_range": 3,
        "color": "223,223,0",
    },
    {
        "name": "village_medium_point",
        "level_of_detail": 3,
        "dataset": "osm",
        "layer": "village_point",
        "where": "population >= 1000 AND population < 5000",
        "label": "name",
        "range": 3,
        "label_range": 3,
        "color": "223,223,0",
    },
    {
        "name": "village_local_point",
        "level_of_detail": 3,
        "dataset": "osm",
        "layer": "village_point",
        "where": "population < 1000",
        "label": "name",
        "range": 3,
        "label_range": 3,
        "color": "223,223,0",
    },
    {
        "name": "airstrip_area",
        "level_of_detail": 1,
        "dataset": "osm",
        "layer": "airstrip_area",
        "label": "name",
        "label_range": 1,
        "label_important_range": 1,
        "range": 10,
        "color": "187,187,204",
    },
    {
        "name": "runway_area",
        "level_of_detail": 1,
        "dataset": "osm",
        "layer": "runway_area",
        "range": 10,
        "color": "255,255,255",
        "alpha": 255,
    },
    {
        "name": "runway_line",
        "level_of_detail": 1,
        "dataset": "osm",
        "layer": "runway_line",
        "range": 10,
        "color": "20,20,20",
        "pen_width": 5,
    },
]

ATTRIBUTION = [
    "Map topography contains OpenStreetMap data (c) OpenStreetMap contributors, licensed under the Open Data Commons Open Database License (ODbL) 1.0. See https://www.openstreetmap.org/copyright and https://opendatacommons.org/licenses/odbl/1-0/.",
    "The OpenStreetMap extract used for this map was processed from Geofabrik regional download data. See https://download.geofabrik.de/ and https://www.geofabrik.de/en/data/.",
    "Terrain data uses Copernicus DEM GLO-30 / Copernicus WorldDEM-30. Copernicus DEM GLO-30 is provided under COPERNICUS by the European Union and ESA; original WorldDEM production by DLR e.V. 2010-2014 and Airbus Defence and Space GmbH 2014-2018. See https://dataspace.copernicus.eu/.",
    "This XCM file is a generated map package for XCSoar/TopHat. If redistributed, keep this attribution notice with the map package.",
]


def run(cmd: List[str], *, cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(str(part) for part in cmd), flush=True)
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check)


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"download exists {dest} ({dest.stat().st_size} bytes)", flush=True)
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    run(["wget", "-nv", "-O", str(tmp), url])
    tmp.rename(dest)


def extract_zip(zip_path: Path, extract_dir: Path) -> None:
    marker = extract_dir / ".done"
    if marker.exists():
        return
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    print(f"+ python unzip {zip_path} -> {extract_dir}", flush=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_dir)
    marker.write_text("ok\n", encoding="utf-8")


def size_text(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def projection() -> Tuple[osr.CoordinateTransformation, osr.CoordinateTransformation]:
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    utm = osr.SpatialReference()
    utm.ImportFromEPSG(UTM_EPSG)
    if hasattr(wgs, "SetAxisMappingStrategy"):
        wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        utm.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return osr.CoordinateTransformation(wgs, utm), osr.CoordinateTransformation(utm, wgs)


def compute_tiles() -> List[Tile]:
    to_utm, to_wgs = projection()
    center_x, center_y, _ = to_utm.TransformPoint(EPBA_LON, EPBA_LAT)
    half = TILE_SIZE_M / 2
    tiles = []
    labels = {
        (-1, 1): "nw",
        (0, 1): "n",
        (1, 1): "ne",
        (-1, 0): "w",
        (0, 0): "epba",
        (1, 0): "e",
        (-1, -1): "sw",
        (0, -1): "s",
        (1, -1): "se",
    }
    for iy in [1, 0, -1]:
        for ix in [-1, 0, 1]:
            min_x = center_x + ix * TILE_SIZE_M - half
            max_x = center_x + ix * TILE_SIZE_M + half
            min_y = center_y + iy * TILE_SIZE_M - half
            max_y = center_y + iy * TILE_SIZE_M + half
            corners = [
                to_wgs.TransformPoint(min_x, min_y),
                to_wgs.TransformPoint(min_x, max_y),
                to_wgs.TransformPoint(max_x, min_y),
                to_wgs.TransformPoint(max_x, max_y),
            ]
            lons = [point[0] for point in corners]
            lats = [point[1] for point in corners]
            tiles.append(
                Tile(
                    name=f"epba100_{labels[(ix, iy)]}",
                    ix=ix,
                    iy=iy,
                    bounds=[min(lons), max(lons), max(lats), min(lats)],
                )
            )
    return tiles


def remove_layer(dataset_dir: Path, layer_name: str) -> None:
    for ext in SHAPE_EXTENSIONS:
        path = dataset_dir / f"{layer_name}{ext}"
        if path.exists():
            path.unlink()


def has_layer(dataset_dir: Path, layer_name: str) -> bool:
    return (dataset_dir / f"{layer_name}.shp").exists()


def ogr_count(path: Path, layer_name: str) -> Optional[int]:
    result = subprocess.run(
        ["ogrinfo", "-so", str(path), layer_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("Feature Count:"):
            return int(line.split(":", 1)[1].strip())
    return None


def source_dataset_path(extract_dir: Path, package: SourcePackage) -> Path:
    if package.kind == "gpkg_zip":
        gpkg = sorted(extract_dir.rglob("*.gpkg"))
        if not gpkg:
            raise RuntimeError(f"No GPKG found in {extract_dir}")
        return gpkg[0]
    return extract_dir


def prepare_pbf_places(pbf: Path, extract_dir: Path) -> Path:
    out_dir = extract_dir / "pbf_places"
    marker = out_dir / ".done"
    layer = out_dir / "pbf_place_point.shp"
    if marker.exists() and layer.exists():
        return out_dir
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sql = (
        "SELECT osm_id, name, place AS fclass, geometry, "
        "CAST(COALESCE(hstore_get_value(other_tags, 'population'), '0') AS INTEGER) "
        "AS population FROM points "
        "WHERE place IN ('city','town','suburb','village') AND name IS NOT NULL"
    )
    run(
        [
            "ogr2ogr",
            "-f",
            "ESRI Shapefile",
            str(out_dir),
            str(pbf),
            "-dialect",
            "SQLite",
            "-sql",
            sql,
            "-nln",
            "pbf_place_point",
            "-nlt",
            POINT_NLT,
            "-lco",
            "ENCODING=UTF-8",
            "-skipfailures",
        ]
    )
    marker.write_text("ok\n", encoding="utf-8")
    return out_dir


def source_layer_path_or_name(
    prepared: PreparedPackage,
    package: SourcePackage,
    layer_source: LayerSource,
) -> Optional[Tuple[Path, str]]:
    layer_name = layer_source.source_layer
    if layer_source.source_file == "pbf_places":
        if prepared.pbf_places_path is None or not prepared.pbf_places_path.exists():
            return None
        path = prepared.pbf_places_path / f"{layer_name}.shp"
        if path.exists():
            return path, layer_name
        return None

    source_root = prepared.vector_path
    if package.kind == "gpkg_zip":
        return source_root, layer_name[:-2] if layer_name.endswith("_1") else layer_name
    path = source_root / f"{layer_name}.shp"
    if path.exists():
        return path, layer_name
    return None


def append_source(
    package: SourcePackage,
    prepared: PreparedPackage,
    layer_source: LayerSource,
    tile: Tile,
    dataset_dir: Path,
    out_layer: str,
    append: bool,
) -> bool:
    source = source_layer_path_or_name(prepared, package, layer_source)
    if source is None:
        return False
    source_path, source_layer = source
    sql = f"SELECT * FROM {source_layer} WHERE {layer_source.where}"

    cmd = ["ogr2ogr", "-f", "ESRI Shapefile"]
    if append and has_layer(dataset_dir, out_layer):
        cmd.extend(["-update", "-append"])
    cmd.extend(
        [
            str(dataset_dir),
            str(source_path),
            "-dialect",
            "SQLite",
            "-sql",
            sql,
            "-spat",
            str(tile.bounds[0]),
            str(tile.bounds[3]),
            str(tile.bounds[1]),
            str(tile.bounds[2]),
            "-clipsrc",
            str(tile.bounds[0]),
            str(tile.bounds[3]),
            str(tile.bounds[1]),
            str(tile.bounds[2]),
            "-nln",
            out_layer,
            "-nlt",
            layer_source.nlt,
            "-lco",
            "ENCODING=UTF-8",
            "-skipfailures",
        ]
    )
    result = run(cmd, check=False)
    if result.returncode != 0:
        print(f"skip failed {package.name}:{layer_source.source_layer} -> {out_layer}", flush=True)
        return False
    return True


def extract_template(old_repo: Path, work_dir: Path) -> Path:
    archive = old_repo / "osm" / "nowa_mapa2.7z"
    if not archive.exists():
        candidates = sorted((old_repo / "osm").glob("*.7z"))
        if not candidates:
            raise RuntimeError(f"Template archive not found: {archive}")
        archive = candidates[0]
    template_root = work_dir / "template"
    template_dir = template_root / archive.stem
    if template_dir.exists():
        return template_dir
    template_root.mkdir(parents=True, exist_ok=True)
    run(["7zr", "x", "-y", f"-o{template_root}", str(archive)])
    return template_dir


def make_empty_from_template(template_dir: Path, dataset_dir: Path, layer_name: str) -> None:
    template = template_dir / f"{layer_name}.shp"
    if not template.exists():
        raise RuntimeError(f"Missing template layer {template}")
    remove_layer(dataset_dir, layer_name)
    run(
        [
            "ogr2ogr",
            "-f",
            "ESRI Shapefile",
            str(dataset_dir),
            str(template),
            "-nln",
            layer_name,
            "-lco",
            "ENCODING=UTF-8",
            "-where",
            "1=0",
        ]
    )


def field_index(layer_defn: ogr.FeatureDefn, field_name: str) -> int:
    wanted = field_name.lower()
    for index in range(layer_defn.GetFieldCount()):
        if layer_defn.GetFieldDefn(index).GetName().lower() == wanted:
            return index
    return -1


def field_text(feature: ogr.Feature, index: int) -> str:
    if index < 0 or not feature.IsFieldSetAndNotNull(index):
        return ""
    value = feature.GetField(index)
    return str(value).strip().casefold()


def dedupe_key(feature: ogr.Feature, layer_defn: ogr.FeatureDefn) -> Tuple[object, ...]:
    osm_id = field_text(feature, field_index(layer_defn, "osm_id"))
    if osm_id and osm_id != "0":
        return ("osm_id", osm_id)

    name = field_text(feature, field_index(layer_defn, "name"))
    fclass = field_text(feature, field_index(layer_defn, "fclass"))
    geom = feature.GetGeometryRef()
    if geom is None:
        return ("nogeom", fclass, name)

    clone = geom.Clone()
    clone.FlattenTo2D()
    envelope = tuple(round(value, 7) for value in clone.GetEnvelope())
    geom_hash = hashlib.md5(bytes(clone.ExportToWkb())).hexdigest()
    return ("geometry_name", fclass, name, envelope, geom_hash)


def clone_field_defn(field: ogr.FieldDefn) -> ogr.FieldDefn:
    cloned = ogr.FieldDefn(field.GetName(), field.GetType())
    cloned.SetWidth(field.GetWidth())
    cloned.SetPrecision(field.GetPrecision())
    if hasattr(cloned, "SetSubType") and hasattr(field, "GetSubType"):
        cloned.SetSubType(field.GetSubType())
    if hasattr(cloned, "SetNullable") and hasattr(field, "IsNullable"):
        cloned.SetNullable(field.IsNullable())
    if hasattr(cloned, "SetDefault") and hasattr(field, "GetDefault"):
        cloned.SetDefault(field.GetDefault())
    return cloned


def dedupe_layer(dataset_dir: Path, layer_name: str) -> int:
    path = dataset_dir / f"{layer_name}.shp"
    if not path.exists():
        return 0

    driver = ogr.GetDriverByName("ESRI Shapefile")
    source_ds = ogr.Open(str(path))
    if source_ds is None:
        return 0
    source_layer = source_ds.GetLayer(0)
    if source_layer is None:
        source_ds = None
        return 0

    source_defn = source_layer.GetLayerDefn()
    tmp_dir = dataset_dir / f".dedupe_{layer_name}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    tmp_ds = driver.CreateDataSource(str(tmp_dir))
    tmp_layer = tmp_ds.CreateLayer(
        layer_name,
        source_layer.GetSpatialRef(),
        source_layer.GetGeomType(),
        options=["ENCODING=UTF-8"],
    )
    for index in range(source_defn.GetFieldCount()):
        tmp_layer.CreateField(clone_field_defn(source_defn.GetFieldDefn(index)))
    tmp_defn = tmp_layer.GetLayerDefn()

    seen = set()
    skipped = 0
    source_layer.ResetReading()
    for source_feature in source_layer:
        key = dedupe_key(source_feature, source_defn)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)

        out_feature = ogr.Feature(tmp_defn)
        for index in range(source_defn.GetFieldCount()):
            if source_feature.IsFieldSetAndNotNull(index):
                out_feature.SetField(index, source_feature.GetField(index))
        geom = source_feature.GetGeometryRef()
        if geom is not None:
            out_feature.SetGeometry(geom.Clone())
        tmp_layer.CreateFeature(out_feature)
        out_feature = None

    tmp_layer.SyncToDisk()
    tmp_ds = None
    source_ds = None

    remove_layer(dataset_dir, layer_name)
    for ext in SHAPE_EXTENSIONS:
        src = tmp_dir / f"{layer_name}{ext}"
        if src.exists():
            shutil.move(str(src), dataset_dir / src.name)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return skipped


def feature_population(feature: ogr.Feature, layer_defn: ogr.FeatureDefn) -> int:
    index = field_index(layer_defn, "population")
    if index < 0 or not feature.IsFieldSetAndNotNull(index):
        return 0
    try:
        return int(feature.GetField(index) or 0)
    except (TypeError, ValueError):
        return 0


def copy_feature(feature: ogr.Feature, source_defn: ogr.FeatureDefn,
                 target_defn: ogr.FeatureDefn) -> ogr.Feature:
    out_feature = ogr.Feature(target_defn)
    for index in range(source_defn.GetFieldCount()):
        if feature.IsFieldSetAndNotNull(index):
            out_feature.SetField(index, feature.GetField(index))
    geom = feature.GetGeometryRef()
    if geom is not None:
        out_feature.SetGeometry(geom.Clone())
    return out_feature


def thin_point_layer(dataset_dir: Path, layer_name: str, min_distance_m: float) -> int:
    path = dataset_dir / f"{layer_name}.shp"
    if not path.exists():
        return 0

    driver = ogr.GetDriverByName("ESRI Shapefile")
    source_ds = ogr.Open(str(path))
    if source_ds is None:
        return 0
    source_layer = source_ds.GetLayer(0)
    if source_layer is None:
        source_ds = None
        return 0

    source_defn = source_layer.GetLayerDefn()
    to_utm, _ = projection()
    rows = []
    source_layer.ResetReading()
    for feature in source_layer:
        geom = feature.GetGeometryRef()
        if geom is None or geom.GetGeometryType() not in (ogr.wkbPoint, ogr.wkbPoint25D):
            continue
        lon = geom.GetX()
        lat = geom.GetY()
        x, y, _ = to_utm.TransformPoint(lon, lat)
        rows.append(
            {
                "feature": feature.Clone(),
                "x": x,
                "y": y,
                "population": feature_population(feature, source_defn),
                "name": field_text(feature, field_index(source_defn, "name")),
                "fid": feature.GetFID(),
            }
        )

    if not rows:
        source_ds = None
        return 0

    rows.sort(key=lambda row: (-row["population"], row["name"], row["fid"]))
    accepted = []
    min_distance_sq = min_distance_m * min_distance_m
    skipped = 0
    for row in rows:
        too_close = any(
            (row["x"] - kept["x"]) ** 2 + (row["y"] - kept["y"]) ** 2 < min_distance_sq
            for kept in accepted
        )
        if too_close:
            skipped += 1
            continue
        accepted.append(row)

    if skipped == 0:
        source_ds = None
        return 0

    tmp_dir = dataset_dir / f".thin_{layer_name}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    tmp_ds = driver.CreateDataSource(str(tmp_dir))
    tmp_layer = tmp_ds.CreateLayer(
        layer_name,
        source_layer.GetSpatialRef(),
        source_layer.GetGeomType(),
        options=["ENCODING=UTF-8"],
    )
    for index in range(source_defn.GetFieldCount()):
        tmp_layer.CreateField(clone_field_defn(source_defn.GetFieldDefn(index)))
    tmp_defn = tmp_layer.GetLayerDefn()

    for row in accepted:
        out_feature = copy_feature(row["feature"], source_defn, tmp_defn)
        tmp_layer.CreateFeature(out_feature)
        out_feature = None

    tmp_layer.SyncToDisk()
    tmp_ds = None
    source_ds = None

    remove_layer(dataset_dir, layer_name)
    for ext in SHAPE_EXTENSIONS:
        src = tmp_dir / f"{layer_name}{ext}"
        if src.exists():
            shutil.move(str(src), dataset_dir / src.name)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return skipped


def copernicus_url(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    tile = f"{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00"
    name = f"Copernicus_DSM_COG_10_{tile}_DEM"
    return f"https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"


def dem_name(lat: int, lon: int) -> str:
    ns = "n" if lat >= 0 else "s"
    ew = "e" if lon >= 0 else "w"
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}.tif"


def build_dem(repo: Path, old_repo: Path, tiles: Iterable[Tile]) -> List[Dict[str, object]]:
    left = math.floor(min(tile.bounds[0] for tile in tiles))
    right = math.ceil(max(tile.bounds[1] for tile in tiles))
    bottom = math.floor(min(tile.bounds[3] for tile in tiles))
    top = math.ceil(max(tile.bounds[2] for tile in tiles))
    dem_dir = repo / "dem1"
    dem_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for lat in range(bottom, top):
        for lon in range(left, right):
            dest = dem_dir / dem_name(lat, lon)
            old = old_repo / "dem1" / dest.name
            source = "downloaded"
            if old.exists() and not dest.exists():
                shutil.copyfile(old, dest)
                source = "copied-existing"
            if not dest.exists():
                download(copernicus_url(lat, lon), dest)
            rows.append({"file": f"dem1/{dest.name}", "size": dest.stat().st_size, "source": source})
    return rows


def md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksums(repo: Path) -> None:
    rows = []
    for path in sorted(repo.rglob("*")):
        if path.is_file() and path.name != "checksums" and not path.name.endswith(".md5"):
            rows.append(f"{md5(path)}  {path.relative_to(repo).as_posix()}")
    (repo / "checksums").write_text("\n".join(rows) + "\n", encoding="utf-8")


def build_tile(
    tile: Tile,
    package_roots: Dict[str, PreparedPackage],
    template_dir: Path,
    work: Path,
    repo: Path,
) -> Tuple[Dict[str, object], Dict[str, object]]:
    print(f"== tile {tile.name} bounds={tile.bounds}", flush=True)
    dataset_parent = work / "datasets"
    dataset_dir = dataset_parent / tile.name
    if dataset_dir.exists():
        shutil.rmtree(dataset_dir)
    dataset_dir.mkdir(parents=True, exist_ok=True)

    counts: Dict[str, int] = {}
    duplicate_counts: Dict[str, int] = {}
    spacing_counts: Dict[str, int] = {}
    for layer_name, layer_sources in LAYER_SOURCES.items():
        wrote_any = False
        remove_layer(dataset_dir, layer_name)
        for package in SOURCES:
            prepared = package_roots[package.name]
            for layer_source in layer_sources:
                wrote_any = (
                    append_source(
                        package,
                        prepared,
                        layer_source,
                        tile,
                        dataset_dir,
                        layer_name,
                        wrote_any,
                    )
                    or wrote_any
                )
        if not has_layer(dataset_dir, layer_name):
            make_empty_from_template(template_dir, dataset_dir, layer_name)
        duplicate_counts[layer_name] = dedupe_layer(dataset_dir, layer_name)
        spacing_counts[layer_name] = thin_point_layer(
            dataset_dir,
            layer_name,
            POINT_SPACING_RULES_M.get(layer_name, 0.0),
        ) if layer_name in POINT_SPACING_RULES_M else 0
        counts[layer_name] = ogr_count(dataset_dir / f"{layer_name}.shp", layer_name) or 0
        print(
            f"== tile {tile.name}: {layer_name} features={counts[layer_name]} "
            f"duplicates_removed={duplicate_counts[layer_name]} "
            f"spacing_removed={spacing_counts[layer_name]}",
            flush=True,
        )

    archive = repo / "osm" / f"{tile.name}.7z"
    if archive.exists():
        archive.unlink()
    run(["7zr", "a", "-t7z", "-mx=5", str(archive), tile.name], cwd=dataset_parent)

    dataset = {"name": f"osm/{tile.name}", "bounds": tile.bounds}
    report = {
        "name": tile.name,
        "bounds": tile.bounds,
        "feature_counts": counts,
        "duplicates_removed": duplicate_counts,
        "spacing_removed": spacing_counts,
        "archive": f"osm/{tile.name}.7z",
        "archive_size": archive.stat().st_size,
        "archive_size_text": size_text(archive.stat().st_size),
    }
    return dataset, report


def build(args: argparse.Namespace) -> None:
    started = time.time()
    args.repo.mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)
    args.sources.mkdir(parents=True, exist_ok=True)

    template_dir = extract_template(args.old_repo, args.work)
    package_roots: Dict[str, PreparedPackage] = {}
    source_report = []
    for package in SOURCES:
        archive = args.sources / f"{package.name}.zip"
        print(f"== download source {package.name}", flush=True)
        download(package.url, archive)
        pbf = args.sources / f"{package.name}.osm.pbf"
        print(f"== download PBF source {package.name}", flush=True)
        download(package.pbf_url, pbf)
        extract_dir = args.work / "extract" / package.name
        extract_zip(archive, extract_dir)
        root = source_dataset_path(extract_dir, package)
        pbf_places = prepare_pbf_places(pbf, extract_dir)
        package_roots[package.name] = PreparedPackage(root, pbf_places)
        source_report.append(
            {
                "name": package.name,
                "kind": package.kind,
                "url": package.url,
                "pbf_url": package.pbf_url,
                "size": archive.stat().st_size,
                "size_text": size_text(archive.stat().st_size),
                "pbf_size": pbf.stat().st_size,
                "pbf_size_text": size_text(pbf.stat().st_size),
            }
        )

    tiles = compute_tiles()
    repo_osm = args.repo / "osm"
    repo_osm.mkdir(parents=True, exist_ok=True)
    jobs = args.jobs if args.jobs and args.jobs > 0 else min(len(tiles), os.cpu_count() or 1)
    print(f"== building {len(tiles)} tiles with jobs={jobs}", flush=True)
    results: Dict[str, Tuple[Dict[str, object], Dict[str, object]]] = {}
    if jobs == 1:
        for tile in tiles:
            results[tile.name] = build_tile(tile, package_roots, template_dir, args.work, args.repo)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as executor:
            futures = {
                executor.submit(
                    build_tile,
                    tile,
                    package_roots,
                    template_dir,
                    args.work,
                    args.repo,
                ): tile
                for tile in tiles
            }
            for future in as_completed(futures):
                tile = futures[future]
                results[tile.name] = future.result()
                print(f"== tile {tile.name} complete", flush=True)

    datasets = [results[tile.name][0] for tile in tiles]
    tile_report = [results[tile.name][1] for tile in tiles]

    water_src = args.old_repo / "waterpolygons"
    water_dst = args.repo / "waterpolygons"
    if water_dst.exists():
        shutil.rmtree(water_dst)
    shutil.copytree(water_src, water_dst)

    dem_report = build_dem(args.repo, args.old_repo, tiles)
    manifest = {
        "topology": {
            "datasets": {"osm": datasets},
            "layers": MANIFEST_LAYERS,
        },
        "attribution": ATTRIBUTION,
    }
    (args.repo / "manifest").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_checksums(args.repo)

    report = {
        "started_epoch": started,
        "finished_epoch": time.time(),
        "duration_seconds": time.time() - started,
        "epba": {"lat": EPBA_LAT, "lon": EPBA_LON},
        "tile_size_m": TILE_SIZE_M,
        "utm_epsg": UTM_EPSG,
        "sources": source_report,
        "tiles": tile_report,
        "dem": dem_report,
        "repo_size_bytes": sum(path.stat().st_size for path in args.repo.rglob("*") if path.is_file()),
    }
    args.reports.mkdir(parents=True, exist_ok=True)
    (args.reports / "epba_100km_tiles_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("== repository ready", args.repo, flush=True)
    print("== report", args.reports / "epba_100km_tiles_report.json", flush=True)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, default=Path("/work"))
    parser.add_argument("--old-repo", type=Path, default=Path("/old"))
    parser.add_argument(
        "--jobs",
        type=int,
        default=int(os.environ.get("MAPGEN_TILE_BUILD_JOBS", "0")),
        help="number of source tiles to build in parallel; default uses all useful CPUs",
    )
    args = parser.parse_args(argv)
    args.sources = args.base / "sources"
    args.work = args.base / "work"
    args.repo = args.base / "repo"
    args.reports = args.base / "reports"
    args.old_repo = args.old_repo.resolve()
    return args


def main(argv: List[str]) -> None:
    build(parse_args(argv))


if __name__ == "__main__":
    main(sys.argv[1:])
