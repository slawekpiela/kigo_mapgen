#!/usr/bin/env python3
"""Build broad 100 km task-map base XCM tile sets via mapgen HTTP jobs."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile


LAT_STEP_DEG = 100.0 / 111.32
REQUIRED_XCM_FILES = {"terrain.jp2", "terrain.j2w", "topology.tpl"}

EUROPE_ISO3 = {
    "ALB", "AND", "AUT", "BEL", "BIH", "BGR", "HRV", "CYP", "CZE",
    "DNK", "EST", "FIN", "FRA", "DEU", "GRC", "HUN", "ISL", "IRL",
    "ITA", "XKX", "LVA", "LIE", "LTU", "LUX", "MLT", "MDA", "MCO",
    "MNE", "NLD", "MKD", "NOR", "POL", "PRT", "ROU", "SMR", "SRB",
    "SVK", "SVN", "ESP", "SWE", "CHE", "TUR", "UKR", "GBR", "VAT",
}

TARGETS = {
    "europe_no_ru_by": {
        "iso3": EUROPE_ISO3,
        "clip": {"west": -31.5, "east": 45.0, "south": 34.0, "north": 72.0},
        "exclude_iso3": {"RUS", "BLR"},
    },
    "usa": {
        "iso3": {"USA"},
        "clip": {"west": -180.0, "east": -65.0, "south": 18.0, "north": 72.0},
    },
    "canada": {
        "iso3": {"CAN"},
        "clip": {"west": -142.0, "east": -52.0, "south": 41.0, "north": 84.0},
    },
    "australia": {
        "iso3": {"AUS"},
        "clip": {"west": 112.0, "east": 154.5, "south": -44.5, "north": -9.0},
    },
    "japan": {
        "iso3": {"JPN"},
        "clip": {"west": 122.0, "east": 146.5, "south": 24.0, "north": 46.5},
    },
    "namibia": {
        "iso3": {"NAM"},
        "clip": {"west": 11.0, "east": 26.0, "south": -29.5, "north": -16.5},
    },
    "south_africa": {
        "iso3": {"ZAF"},
        "clip": {"west": 16.0, "east": 33.5, "south": -35.0, "north": -22.0},
    },
}

FALLBACK_BBOXES = {
    "ALB": (19.2, 21.1, 39.6, 42.7), "AND": (1.4, 1.8, 42.4, 42.7),
    "AUT": (9.5, 17.2, 46.3, 49.1), "BEL": (2.5, 6.5, 49.5, 51.5),
    "BIH": (15.7, 19.7, 42.5, 45.3), "BGR": (22.3, 28.7, 41.2, 44.3),
    "HRV": (13.4, 19.5, 42.4, 46.6), "CYP": (32.2, 34.7, 34.5, 35.8),
    "CZE": (12.1, 18.9, 48.5, 51.1), "DNK": (8.0, 15.2, 54.5, 57.8),
    "EST": (21.7, 28.2, 57.5, 59.8), "FIN": (20.5, 31.6, 59.8, 70.1),
    "FRA": (-5.2, 9.8, 41.2, 51.2), "DEU": (5.8, 15.1, 47.2, 55.1),
    "GRC": (19.3, 29.7, 34.6, 41.8), "HUN": (16.1, 22.9, 45.7, 48.7),
    "ISL": (-24.7, -13.4, 63.2, 66.6), "IRL": (-10.7, -5.9, 51.4, 55.4),
    "ITA": (6.6, 18.6, 35.5, 47.1), "XKX": (20.0, 21.9, 41.8, 43.3),
    "LVA": (20.9, 28.3, 55.7, 58.1), "LIE": (9.4, 9.7, 47.0, 47.3),
    "LTU": (20.9, 26.9, 53.9, 56.5), "LUX": (5.7, 6.6, 49.4, 50.2),
    "MLT": (14.2, 14.6, 35.8, 36.1), "MDA": (26.6, 30.2, 45.4, 48.5),
    "MCO": (7.3, 7.5, 43.6, 43.8), "MNE": (18.4, 20.4, 41.8, 43.6),
    "NLD": (3.3, 7.3, 50.7, 53.7), "MKD": (20.4, 23.1, 40.8, 42.4),
    "NOR": (4.5, 31.2, 57.8, 71.2), "POL": (13.9, 24.4, 48.8, 55.0),
    "PRT": (-31.5, -6.1, 32.3, 42.2), "ROU": (20.2, 29.7, 43.6, 48.3),
    "SMR": (12.3, 12.6, 43.8, 44.0), "SRB": (18.8, 23.1, 42.2, 46.2),
    "SVK": (16.8, 22.6, 47.7, 49.7), "SVN": (13.4, 16.7, 45.4, 46.9),
    "ESP": (-18.3, 4.4, 27.6, 43.8), "SWE": (11.0, 24.2, 55.0, 69.1),
    "CHE": (5.9, 10.5, 45.8, 47.9), "TUR": (25.6, 44.9, 35.8, 42.2),
    "UKR": (22.0, 40.3, 44.2, 52.4), "GBR": (-8.7, 1.8, 49.8, 60.9),
    "VAT": (12.4, 12.5, 41.8, 42.0), "USA": (-179.5, -66.8, 18.8, 71.5),
    "CAN": (-141.0, -52.6, 41.7, 83.2), "AUS": (112.9, 153.7, -43.8, -10.7),
    "JPN": (122.9, 145.9, 24.0, 45.6), "NAM": (11.7, 25.3, -28.9, -16.9),
    "ZAF": (16.5, 32.9, -34.9, -22.1), "RUS": (19.6, 180.0, 41.2, 82.0),
    "BLR": (23.1, 32.8, 51.2, 56.2),
}


def bbox_intersects(a, b):
    return not (
        a["east"] <= b["west"]
        or a["west"] >= b["east"]
        or a["north"] <= b["south"]
        or a["south"] >= b["north"]
    )


def bbox_clip(a, b):
    out = {
        "west": max(a["west"], b["west"]),
        "east": min(a["east"], b["east"]),
        "south": max(a["south"], b["south"]),
        "north": min(a["north"], b["north"]),
    }
    if out["west"] >= out["east"] or out["south"] >= out["north"]:
        return None
    return out


def ring_bbox(ring):
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    return {"west": min(xs), "east": max(xs), "south": min(ys), "north": max(ys)}


def point_in_ring(lon, lat, ring):
    inside = False
    j = len(ring) - 1
    for i, point in enumerate(ring):
        xi, yi = point[:2]
        xj, yj = ring[j][:2]
        if (yi > lat) != (yj > lat):
            x = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
            if lon < x:
                inside = not inside
        j = i
    return inside


def point_in_polygon(lon, lat, polygon):
    if not polygon or not point_in_ring(lon, lat, polygon[0]):
        return False
    return not any(point_in_ring(lon, lat, hole) for hole in polygon[1:])


def polygon_intersects_bbox(polygon, tile):
    if not polygon or not bbox_intersects(ring_bbox(polygon[0]), tile):
        return False
    for lon, lat in (
        (tile["west"], tile["south"]),
        (tile["west"], tile["north"]),
        (tile["east"], tile["south"]),
        (tile["east"], tile["north"]),
        ((tile["west"] + tile["east"]) / 2.0, (tile["south"] + tile["north"]) / 2.0),
    ):
        if point_in_polygon(lon, lat, polygon):
            return True
    for lon, lat, *_ in polygon[0]:
        if tile["west"] <= lon <= tile["east"] and tile["south"] <= lat <= tile["north"]:
            return True
    return True


def iso3_from_properties(props):
    for key in ("ISO_A3", "iso_a3", "ADM0_A3", "ISO3166-1-Alpha-3", "alpha-3"):
        value = props.get(key)
        if value and value != "-99":
            return str(value).upper()
    aliases = {
        "Kosovo": "XKX",
        "United Kingdom": "GBR",
        "United States of America": "USA",
        "Czechia": "CZE",
        "North Macedonia": "MKD",
        "South Africa": "ZAF",
    }
    return aliases.get(str(props.get("ADMIN") or props.get("name") or props.get("Name") or ""), "")


def load_country_polygons(path):
    if not path or not Path(path).exists():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {}
    for feature in data.get("features", []):
        iso3 = iso3_from_properties(feature.get("properties") or {})
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") == "Polygon":
            polygons = [coords]
        elif geom.get("type") == "MultiPolygon":
            polygons = coords
        else:
            polygons = []
        if iso3 and polygons:
            out.setdefault(iso3, []).extend(polygons)
    return out


def fallback_polygons(iso3_set):
    out = {}
    for iso3 in iso3_set:
        bbox = FALLBACK_BBOXES.get(iso3)
        if not bbox:
            continue
        west, east, south, north = bbox
        out[iso3] = [[[[west, south], [west, north], [east, north], [east, south], [west, south]]]]
    return out


def lon_step_for_lat(lat):
    return 100.0 / (111.32 * max(0.15, math.cos(math.radians(lat))))


def iter_grid(clip):
    row = math.floor(clip["south"] / LAT_STEP_DEG)
    south = row * LAT_STEP_DEG
    while south < clip["north"]:
        north = south + LAT_STEP_DEG
        lon_step = lon_step_for_lat((south + north) / 2.0)
        col = math.floor(clip["west"] / lon_step)
        west = col * lon_step
        while west < clip["east"]:
            yield {
                "west": west,
                "east": west + lon_step,
                "south": south,
                "north": north,
                "row": row,
                "col": col,
            }
            west += lon_step
            col += 1
        south += LAT_STEP_DEG
        row += 1


def tile_matches(tile, polygons_by_iso, target):
    clipped = bbox_clip(tile, target["clip"])
    if clipped is None:
        return []
    center_lon = (clipped["west"] + clipped["east"]) / 2.0
    center_lat = (clipped["south"] + clipped["north"]) / 2.0
    for iso3 in target.get("exclude_iso3", set()):
        if any(point_in_polygon(center_lon, center_lat, polygon) for polygon in polygons_by_iso.get(iso3, [])):
            return []
    matches = []
    for iso3 in sorted(target["iso3"]):
        if any(polygon_intersects_bbox(polygon, clipped) for polygon in polygons_by_iso.get(iso3, [])):
            matches.append(iso3)
    return matches


def build_plan(target_names, polygons_by_iso):
    tiles = {}
    for target_name in target_names:
        target = TARGETS[target_name]
        for raw in iter_grid(target["clip"]):
            clipped = bbox_clip(raw, target["clip"])
            if clipped is None:
                continue
            countries = tile_matches(clipped, polygons_by_iso, target)
            if not countries:
                continue
            key = (
                round(clipped["west"], 6),
                round(clipped["east"], 6),
                round(clipped["south"], 6),
                round(clipped["north"], 6),
            )
            entry = tiles.setdefault(
                key,
                {"bbox": clipped, "targets": set(), "countries": set(), "row": raw["row"], "col": raw["col"]},
            )
            entry["targets"].add(target_name)
            entry["countries"].update(countries)

    plan = []
    for index, entry in enumerate(
        sorted(tiles.values(), key=lambda item: (min(item["targets"]), item["bbox"]["south"], item["bbox"]["west"])),
        1,
    ):
        prefix = "_".join(sorted(entry["targets"]))
        plan.append(
            {
                "name": f"{prefix}_100km_{index:05d}",
                "bbox": entry["bbox"],
                "targets": sorted(entry["targets"]),
                "countries": sorted(entry["countries"]),
                "grid": {"row": entry["row"], "col": entry["col"]},
            }
        )
    return plan


class StopRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def submit_job(mapgen_url, bbox, name):
    form = {
        "name": name[:80],
        "mail": "",
        "highres": "on",
        "level_of_detail": "3",
        "selection": "bounds",
        "left": f"{bbox['west']:.6f}",
        "right": f"{bbox['east']:.6f}",
        "top": f"{bbox['north']:.6f}",
        "bottom": f"{bbox['south']:.6f}",
    }
    req = urllib.request.Request(
        mapgen_url.rstrip("/") + "/",
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener = urllib.request.build_opener(StopRedirect())
    try:
        opener.open(req, timeout=30)
        raise RuntimeError("mapgen did not redirect after submit")
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise
        location = exc.headers.get("Location", "")
    if "uuid=" not in location:
        raise RuntimeError(f"unexpected mapgen redirect: {location!r}")
    return location.split("uuid=", 1)[1].strip()


def poll_job(mapgen_url, uuid, timeout_s, poll_s):
    deadline = time.monotonic() + timeout_s
    status_url = f"{mapgen_url.rstrip('/')}/status?uuid={uuid}"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(status_url, timeout=20) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            if "is ready to download" in html:
                return "done"
            if "Generation failed" in html or "<title>Error" in html:
                return "error"
        except Exception:
            pass
        time.sleep(poll_s)
    return "timeout"


def download_job(mapgen_url, uuid):
    with urllib.request.urlopen(f"{mapgen_url.rstrip('/')}/download?uuid={uuid}", timeout=180) as resp:
        return resp.read()


def xcm_complete(path):
    if not path.exists() or path.stat().st_size <= 0:
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            return REQUIRED_XCM_FILES.issubset(set(zf.namelist()))
    except Exception:
        return False


def write_manifest(output_dir, plan, results):
    payload = {
        "created_epoch": time.time(),
        "tile_size_km": 100,
        "lat_step_deg": LAT_STEP_DEG,
        "targets": {
            name: {
                "clip": spec["clip"],
                "iso3": sorted(spec["iso3"]),
                "exclude_iso3": sorted(spec.get("exclude_iso3", set())),
            }
            for name, spec in TARGETS.items()
        },
        "tiles_planned": len(plan),
        "tiles": results,
    }
    tmp = output_dir / "manifest-global-100km.json.tmp"
    final = output_dir / "manifest-global-100km.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, final)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--geojson", type=Path)
    parser.add_argument("--mapgen-url", default="http://127.0.0.1:9091")
    parser.add_argument("--targets", default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-tiles", type=int, default=0)
    parser.add_argument("--timeout-s", type=int, default=900)
    parser.add_argument("--poll-s", type=float, default=10.0)
    args = parser.parse_args(argv)

    target_names = list(TARGETS) if args.targets == "all" else [item.strip() for item in args.targets.split(",") if item.strip()]
    unknown = [name for name in target_names if name not in TARGETS]
    if unknown:
        raise SystemExit(f"unknown targets: {', '.join(unknown)}")

    needed_iso = set()
    for name in target_names:
        needed_iso.update(TARGETS[name]["iso3"])
        needed_iso.update(TARGETS[name].get("exclude_iso3", set()))

    polygons = load_country_polygons(args.geojson)
    if not polygons:
        polygons = fallback_polygons(needed_iso)
    for iso3, fallback in fallback_polygons(needed_iso).items():
        polygons.setdefault(iso3, fallback)

    plan = build_plan(target_names, polygons)
    if args.max_tiles > 0:
        plan = plan[:args.max_tiles]

    args.output.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"planned_tiles": len(plan), "output": str(args.output), "targets": target_names}), flush=True)
    if args.dry_run:
        write_manifest(args.output, plan, [])
        return 0

    results = []
    state_path = args.output / "state.jsonl"
    for index, tile in enumerate(plan, 1):
        path = args.output / f"{tile['name']}.xcm"
        started = time.time()
        status = "unknown"
        error = ""
        uuid = ""
        if xcm_complete(path):
            status = "skipped-existing"
        else:
            try:
                uuid = submit_job(args.mapgen_url, tile["bbox"], tile["name"])
                status = poll_job(args.mapgen_url, uuid, args.timeout_s, args.poll_s)
                if status == "done":
                    data = download_job(args.mapgen_url, uuid)
                    tmp = path.with_suffix(".xcm.tmp")
                    tmp.write_bytes(data)
                    os.replace(tmp, path)
                    if not xcm_complete(path):
                        status = "incomplete"
                        error = "downloaded XCM missing required files"
                else:
                    error = status
            except Exception as exc:
                status = "error"
                error = str(exc)
        result = {
            **tile,
            "path": str(path),
            "bytes": path.stat().st_size if path.exists() else 0,
            "status": status,
            "error": error,
            "uuid": uuid,
            "seconds": round(time.time() - started, 3),
            "index": index,
            "total": len(plan),
        }
        results.append(result)
        with state_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, ensure_ascii=True, sort_keys=True) + "\n")
        write_manifest(args.output, plan, results)
        print(f"{status.upper()} {index}/{len(plan)} {tile['name']} {result['bytes']} bytes {result['seconds']}s {error}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
