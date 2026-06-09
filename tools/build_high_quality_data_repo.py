#!/usr/bin/env python3
"""Build a local high-quality data repository for Kigo mapgen.

The script is intended to run inside the mapgen worker image, because that
image already contains GDAL/OGR and 7zr.  It builds repository files accepted
by ``xcsoar.mapgen.downloader.Downloader``:

* ``manifest`` with OSM topology datasets,
* ``checksums`` with md5 sums,
* ``osm/<dataset>.7z`` archives containing shapefiles,
* ``dem1/*.tif`` Copernicus GLO-30 1 arc-second tiles,
* ``waterpolygons/*`` copied from an existing repository.
"""

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


COUNTRIES = {
    "poland": {
        "path": "europe/poland",
        "dataset": "pl",
        "bounds": [13.9, 24.4, 55.3, 48.8],
        "free_shapefile": False,
    },
    "slovakia": {
        "path": "europe/slovakia",
        "dataset": "sk",
        "bounds": [16.4, 22.9, 49.9, 47.5],
        "free_shapefile": True,
    },
    "czech-republic": {
        "path": "europe/czech-republic",
        "dataset": "cz",
        "bounds": [11.7, 19.1, 51.3, 48.4],
        "free_shapefile": False,
    },
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
        "name": "water_area_small",
        "level_of_detail": 4,
        "dataset": "osm",
        "layer": "water_area_small",
        "range": 1,
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
        "name": "roadsmall_line",
        "level_of_detail": 3,
        "dataset": "osm",
        "layer": "roadsmall_line",
        "range": 2,
        "color": "195,195,190",
    },
    {
        "name": "track_line",
        "level_of_detail": 4,
        "dataset": "osm",
        "layer": "track_line",
        "range": 1,
        "color": "150,150,145",
    },
    {
        "name": "path_line",
        "level_of_detail": 4,
        "dataset": "osm",
        "layer": "path_line",
        "range": 1,
        "color": "120,120,115",
    },
    {
        "name": "city_point",
        "level_of_detail": 1,
        "dataset": "osm",
        "layer": "city_point",
        "label": "name",
        "range": 15,
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
        "label_range": 8,
        "label_important_range": 3,
        "color": "223,223,0",
    },
    {
        "name": "suburb_point",
        "level_of_detail": 2,
        "dataset": "osm",
        "layer": "suburb_point",
        "label": "name",
        "range": 3,
        "label_range": 1,
        "color": "223,223,0",
    },
    {
        "name": "village_point",
        "level_of_detail": 3,
        "dataset": "osm",
        "layer": "village_point",
        "label": "name",
        "range": 3,
        "label_range": 2,
        "color": "223,223,0",
    },
    {
        "name": "hamlet_point",
        "level_of_detail": 4,
        "dataset": "osm",
        "layer": "hamlet_point",
        "label": "name",
        "range": 1,
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

ALL_LAYERS = [layer["layer"] for layer in MANIFEST_LAYERS]
AREA_NLT = "MULTIPOLYGON"
LINE_NLT = "MULTILINESTRING"
POINT_NLT = "POINT"
SHAPE_EXTENSIONS = (".shp", ".shx", ".dbf", ".prj", ".cpg")


@dataclass(frozen=True)
class OgrSource:
    input_name: str
    where: str
    nlt: str
    input_layer: Optional[str] = None
    required: bool = False


SHP_LAYER_SOURCES = {
    "city_area": [
        OgrSource(
            "gis_osm_landuse_a_free_1.shp",
            "fclass IN ('residential','commercial','industrial','retail')",
            AREA_NLT,
        ),
    ],
    "water_area_large": [
        OgrSource("gis_osm_water_a_free_1.shp", "1=1", AREA_NLT),
    ],
    "water_area_small": [
        OgrSource("gis_osm_water_a_free_1.shp", "1=1", AREA_NLT),
    ],
    "water_line": [
        OgrSource("gis_osm_waterways_free_1.shp", "fclass IN ('river','canal')", LINE_NLT),
    ],
    "stream_line": [
        OgrSource(
            "gis_osm_waterways_free_1.shp",
            "fclass IN ('stream','drain','ditch')",
            LINE_NLT,
        ),
    ],
    "forest_area": [
        OgrSource("gis_osm_landuse_a_free_1.shp", "fclass = 'forest'", AREA_NLT),
        OgrSource("gis_osm_natural_a_free_1.shp", "fclass IN ('wood','forest')", AREA_NLT),
    ],
    "roadbig_line": [
        OgrSource(
            "gis_osm_roads_free_1.shp",
            "fclass IN ('motorway','motorway_link','trunk','trunk_link','primary','primary_link')",
            LINE_NLT,
        ),
    ],
    "roadmedium_line": [
        OgrSource(
            "gis_osm_roads_free_1.shp",
            "fclass IN ('secondary','secondary_link','tertiary','tertiary_link','unclassified')",
            LINE_NLT,
        ),
    ],
    "roadsmall_line": [
        OgrSource(
            "gis_osm_roads_free_1.shp",
            "fclass IN ('residential','service','living_street','pedestrian','road')",
            LINE_NLT,
        ),
    ],
    "track_line": [
        OgrSource("gis_osm_roads_free_1.shp", "fclass = 'track'", LINE_NLT),
    ],
    "path_line": [
        OgrSource(
            "gis_osm_roads_free_1.shp",
            "fclass IN ('path','footway','cycleway','bridleway','steps')",
            LINE_NLT,
        ),
    ],
    "city_point": [
        OgrSource("gis_osm_places_free_1.shp", "fclass = 'city'", POINT_NLT),
    ],
    "town_point": [
        OgrSource("gis_osm_places_free_1.shp", "fclass = 'town'", POINT_NLT),
    ],
    "suburb_point": [
        OgrSource("gis_osm_places_free_1.shp", "fclass = 'suburb'", POINT_NLT),
    ],
    "village_point": [
        OgrSource("gis_osm_places_free_1.shp", "fclass = 'village'", POINT_NLT),
    ],
    "hamlet_point": [
        OgrSource("gis_osm_places_free_1.shp", "fclass = 'hamlet'", POINT_NLT),
    ],
    "airstrip_area": [
        OgrSource(
            "gis_osm_transport_a_free_1.shp",
            "fclass IN ('airport','airfield','apron','runway')",
            AREA_NLT,
        ),
        OgrSource("latest.osm.pbf", "aeroway IN ('aerodrome','apron')", AREA_NLT, "multipolygons"),
    ],
    "runway_area": [
        OgrSource("gis_osm_transport_a_free_1.shp", "fclass = 'runway'", AREA_NLT),
        OgrSource("latest.osm.pbf", "aeroway = 'runway'", AREA_NLT, "multipolygons"),
    ],
    "runway_line": [
        OgrSource("gis_osm_transport_free_1.shp", "fclass = 'runway'", LINE_NLT),
    ],
}

PBF_LAYER_SOURCES = {
    "city_area": [
        OgrSource(
            "latest.osm.pbf",
            "landuse IN ('residential','commercial','industrial','retail')",
            AREA_NLT,
            "multipolygons",
        ),
    ],
    "water_area_large": [
        OgrSource(
            "latest.osm.pbf",
            "natural = 'water' OR waterway = 'riverbank' OR landuse IN ('reservoir','basin')",
            AREA_NLT,
            "multipolygons",
        ),
    ],
    "water_area_small": [
        OgrSource(
            "latest.osm.pbf",
            "natural = 'water' OR waterway = 'riverbank' OR landuse IN ('reservoir','basin')",
            AREA_NLT,
            "multipolygons",
        ),
    ],
    "water_line": [
        OgrSource("latest.osm.pbf", "waterway IN ('river','canal')", LINE_NLT, "lines"),
    ],
    "stream_line": [
        OgrSource("latest.osm.pbf", "waterway IN ('stream','drain','ditch')", LINE_NLT, "lines"),
    ],
    "forest_area": [
        OgrSource(
            "latest.osm.pbf",
            "landuse = 'forest' OR natural IN ('wood','forest')",
            AREA_NLT,
            "multipolygons",
        ),
    ],
    "roadbig_line": [
        OgrSource(
            "latest.osm.pbf",
            "highway IN ('motorway','motorway_link','trunk','trunk_link','primary','primary_link')",
            LINE_NLT,
            "lines",
        ),
    ],
    "roadmedium_line": [
        OgrSource(
            "latest.osm.pbf",
            "highway IN ('secondary','secondary_link','tertiary','tertiary_link','unclassified')",
            LINE_NLT,
            "lines",
        ),
    ],
    "roadsmall_line": [
        OgrSource(
            "latest.osm.pbf",
            "highway IN ('residential','service','living_street','pedestrian','road')",
            LINE_NLT,
            "lines",
        ),
    ],
    "track_line": [
        OgrSource("latest.osm.pbf", "highway = 'track'", LINE_NLT, "lines"),
    ],
    "path_line": [
        OgrSource(
            "latest.osm.pbf",
            "highway IN ('path','footway','cycleway','bridleway','steps')",
            LINE_NLT,
            "lines",
        ),
    ],
    "city_point": [
        OgrSource("latest.osm.pbf", "place = 'city'", POINT_NLT, "points"),
    ],
    "town_point": [
        OgrSource("latest.osm.pbf", "place = 'town'", POINT_NLT, "points"),
    ],
    "suburb_point": [
        OgrSource("latest.osm.pbf", "place = 'suburb'", POINT_NLT, "points"),
    ],
    "village_point": [
        OgrSource("latest.osm.pbf", "place = 'village'", POINT_NLT, "points"),
    ],
    "hamlet_point": [
        OgrSource("latest.osm.pbf", "place = 'hamlet'", POINT_NLT, "points"),
    ],
    "airstrip_area": [
        OgrSource("latest.osm.pbf", "aeroway IN ('aerodrome','apron')", AREA_NLT, "multipolygons"),
    ],
    "runway_area": [
        OgrSource("latest.osm.pbf", "aeroway = 'runway'", AREA_NLT, "multipolygons"),
    ],
    "runway_line": [
        OgrSource("latest.osm.pbf", "aeroway = 'runway'", LINE_NLT, "lines"),
    ],
}

PBF_RUNWAY_SOURCES = {
    "airstrip_area": PBF_LAYER_SOURCES["airstrip_area"],
    "runway_area": PBF_LAYER_SOURCES["runway_area"],
    "runway_line": PBF_LAYER_SOURCES["runway_line"],
}


def run(cmd, *, cwd=None, quiet=False, check=True):
    if not quiet:
        print("+", " ".join(str(part) for part in cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, check=check)


def download(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"download exists {dest}", flush=True)
        return

    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    run(["wget", "-nv", "-O", str(tmp), url])
    tmp.rename(dest)


def remove_layer(dataset_dir, layer_name):
    for ext in SHAPE_EXTENSIONS:
        path = dataset_dir / f"{layer_name}{ext}"
        if path.exists():
            path.unlink()


def has_layer(dataset_dir, layer_name):
    return (dataset_dir / f"{layer_name}.shp").exists()


def ogr_count(path, layer_name):
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


def ogr_copy(source_path, source, dataset_dir, out_layer, append):
    if not source_path.exists():
        if source.required:
            raise FileNotFoundError(source_path)
        print(f"skip missing source {source_path}", flush=True)
        return False

    sql_layer = source.input_layer or source_path.stem
    sql = f"SELECT name FROM {sql_layer} WHERE {source.where}"

    cmd = ["ogr2ogr", "-f", "ESRI Shapefile"]
    if append and has_layer(dataset_dir, out_layer):
        cmd.extend(["-update", "-append"])
    cmd.extend(
        [
            str(dataset_dir),
            str(source_path),
        ]
    )
    cmd.extend(
        [
            "-dialect",
            "SQLite",
            "-sql",
            sql,
            "-nln",
            out_layer,
            "-nlt",
            source.nlt,
            "-lco",
            "ENCODING=UTF-8",
            "-skipfailures",
        ]
    )
    result = run(cmd, check=False)
    if result.returncode != 0:
        if source.required:
            raise RuntimeError(f"ogr2ogr failed for {source_path} -> {out_layer}")
        print(f"skip failed source {source_path} -> {out_layer}", flush=True)
        return False
    return True


def make_empty_from_template(template_dir, dataset_dir, layer_name):
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
            "-where",
            "1=0",
        ]
    )


def extract_zip(zip_path, extract_dir):
    marker = extract_dir / ".done"
    if marker.exists():
        return
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)
    print(f"+ python unzip {zip_path} -> {extract_dir}", flush=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_dir)
    marker.write_text("ok\n", encoding="utf-8")


def extract_template(old_repo, work_dir):
    archive = old_repo / "osm" / "nowa_mapa2.7z"
    if not archive.exists():
        raise RuntimeError(f"Template archive not found: {archive}")
    template_root = work_dir / "template"
    template_dir = template_root / "nowa_mapa2"
    if template_dir.exists():
        return template_dir
    template_root.mkdir(parents=True, exist_ok=True)
    run(["7zr", "x", "-y", f"-o{template_root}", str(archive)])
    return template_dir


def country_urls(country):
    meta = COUNTRIES[country]
    base = f"https://download.geofabrik.de/{meta['path']}-latest"
    return base + "-free.shp.zip", base + ".osm.pbf"


def build_country(country, args, template_dir):
    meta = COUNTRIES[country]
    dataset_name = meta["dataset"]
    use_shapefile = meta.get("free_shapefile", True)
    source_dir = args.work / "source" / country
    extract_dir = args.work / "extract" / country
    dataset_parent = args.work / "datasets"
    dataset_dir = dataset_parent / dataset_name
    source_dir.mkdir(parents=True, exist_ok=True)
    dataset_parent.mkdir(parents=True, exist_ok=True)

    shp_url, pbf_url = country_urls(country)
    shp_zip = source_dir / f"{country}-latest-free.shp.zip"
    pbf = source_dir / "latest.osm.pbf"

    if use_shapefile:
        print(f"== {country}: download shapefile extract", flush=True)
        download(shp_url, shp_zip)
        extract_zip(shp_zip, extract_dir)
    if args.with_pbf_runways or not use_shapefile:
        purpose = "aeroway layers" if use_shapefile else "all topology layers"
        print(f"== {country}: download OSM PBF for {purpose}", flush=True)
        download(pbf_url, pbf)

    if dataset_dir.exists():
        shutil.rmtree(dataset_dir)
    dataset_dir.mkdir(parents=True)

    for layer in ALL_LAYERS:
        print(f"== {country}: layer {layer}", flush=True)
        remove_layer(dataset_dir, layer)
        wrote_any = False
        sources = []
        if use_shapefile:
            sources.extend(SHP_LAYER_SOURCES[layer])
            if args.with_pbf_runways:
                sources.extend(PBF_RUNWAY_SOURCES.get(layer, ()))
        else:
            sources.extend(PBF_LAYER_SOURCES[layer])
        for source in sources:
            input_path = pbf if source.input_name.endswith(".osm.pbf") else extract_dir / source.input_name
            wrote_any = ogr_copy(input_path, source, dataset_dir, layer, wrote_any) or wrote_any
        if not has_layer(dataset_dir, layer):
            make_empty_from_template(template_dir, dataset_dir, layer)
        count = ogr_count(dataset_dir / f"{layer}.shp", layer)
        print(f"== {country}: {layer} features={count}", flush=True)

    repo_osm = args.repo / "osm"
    repo_osm.mkdir(parents=True, exist_ok=True)
    archive = repo_osm / f"{dataset_name}.7z"
    if archive.exists():
        archive.unlink()
    run(["7zr", "a", "-t7z", "-mx=5", str(archive), dataset_name], cwd=dataset_parent)
    return {
        "name": f"osm/{dataset_name}",
        "bounds": meta["bounds"],
    }


def copernicus_url(lat, lon):
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    lat_abs = abs(lat)
    lon_abs = abs(lon)
    tile = f"{ns}{lat_abs:02d}_00_{ew}{lon_abs:03d}_00"
    name = f"Copernicus_DSM_COG_10_{tile}_DEM"
    return f"https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"


def dem_name(lat, lon):
    ns = "n" if lat >= 0 else "s"
    ew = "e" if lon >= 0 else "w"
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}.tif"


def build_dem(args):
    dem_dir = args.repo / "dem1"
    dem_dir.mkdir(parents=True, exist_ok=True)
    left = math.floor(args.dem_left)
    right = math.ceil(args.dem_right)
    bottom = math.floor(args.dem_bottom)
    top = math.ceil(args.dem_top)
    for lat in range(bottom, top):
        for lon in range(left, right):
            dest = dem_dir / dem_name(lat, lon)
            if args.old_repo:
                old = args.old_repo / "dem1" / dest.name
                if old.exists() and not dest.exists():
                    print(f"copy DEM {old.name}", flush=True)
                    shutil.copyfile(old, dest)
            if dest.exists() and dest.stat().st_size > 0:
                continue
            print(f"download DEM {dest.name}", flush=True)
            download(copernicus_url(lat, lon), dest)


def copy_waterpolygons(args):
    if not args.old_repo:
        raise RuntimeError("waterpolygons require --old-repo")
    source = args.old_repo / "waterpolygons"
    if not source.exists():
        raise RuntimeError(f"Missing source waterpolygons: {source}")
    dest = args.repo / "waterpolygons"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)


def write_manifest(args, datasets):
    manifest = {
        "topology": {
            "datasets": {
                "osm": datasets,
            },
            "layers": MANIFEST_LAYERS,
        },
        "attribution": ATTRIBUTION,
    }
    args.repo.mkdir(parents=True, exist_ok=True)
    (args.repo / "manifest").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def md5(path):
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksums(repo):
    rows = []
    for path in sorted(repo.rglob("*")):
        if path.is_file() and path.name != "checksums" and not path.name.endswith(".md5"):
            rel = path.relative_to(repo).as_posix()
            rows.append(f"{md5(path)}  {rel}")
    (repo / "checksums").write_text("\n".join(rows) + "\n", encoding="utf-8")


def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--old-repo", type=Path, required=True)
    parser.add_argument(
        "--country",
        action="append",
        choices=sorted(COUNTRIES),
        dest="countries",
        help="Country to include. May be repeated. Defaults to PL/SK/CZ.",
    )
    parser.add_argument("--with-pbf-runways", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dem-left", type=float, default=11.0)
    parser.add_argument("--dem-right", type=float, default=25.0)
    parser.add_argument("--dem-bottom", type=float, default=47.0)
    parser.add_argument("--dem-top", type=float, default=56.0)
    parser.add_argument("--skip-dem", action="store_true")
    parser.add_argument("--skip-topology", action="store_true")
    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    args.repo = args.repo.resolve()
    args.work = args.work.resolve()
    args.old_repo = args.old_repo.resolve()
    args.countries = args.countries or ["poland", "slovakia", "czech-republic"]

    args.repo.mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)
    copy_waterpolygons(args)

    datasets = []
    if not args.skip_topology:
        template_dir = extract_template(args.old_repo, args.work)
        for country in args.countries:
            datasets.append(build_country(country, args, template_dir))
    else:
        datasets = [
            {"name": f"osm/{COUNTRIES[country]['dataset']}", "bounds": COUNTRIES[country]["bounds"]}
            for country in args.countries
        ]

    write_manifest(args, datasets)
    if not args.skip_dem:
        build_dem(args)
    write_checksums(args.repo)
    print(f"repository ready: {args.repo}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
