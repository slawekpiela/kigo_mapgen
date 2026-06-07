#!/usr/bin/env python3
"""Crop an XCSoar/Kigo .xcm map to a CUP task corridor.

This intentionally avoids GDAL/OGR so it can run on small VM/Pi setups that
only have Python, unzip-compatible zip files, and ImageMagick for JP2 crop.
Vector topography is filtered by shapefile record bounding boxes; crossing
features are kept whole instead of geometrically clipped.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import time
import zipfile


def parse_coord(value: str) -> float:
    value = value.strip()
    hemi = value[-1].upper()
    body = value[:-1]
    if hemi not in ("N", "S", "E", "W"):
        raise ValueError(f"invalid coordinate {value!r}")

    int_part, _, frac_part = body.partition(".")
    if len(int_part) < 3:
        raise ValueError(f"invalid coordinate {value!r}")

    deg_text = int_part[:-2]
    min_text = int_part[-2:] + (("." + frac_part) if frac_part else "")
    degrees = int(deg_text) + float(min_text) / 60.0
    if hemi in ("S", "W"):
        degrees = -degrees

    return degrees


def read_cup_task(path: Path, task_number: int):
    waypoints: dict[str, tuple[float, float]] = {}
    tasks: list[list[str]] = []

    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        in_tasks = False
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.lower() == "-----related tasks-----":
                in_tasks = True
                continue

            if in_tasks:
                if line.startswith('"') or line.startswith(","):
                    row = next(csv.reader([line]))
                    while row and row[-1] == "":
                        row.pop()
                    tasks.append(row)
                continue

            row = next(csv.reader([line]))
            if not row or row[0].lower() == "name" or len(row) < 5:
                continue
            try:
                lat = parse_coord(row[3])
                lon = parse_coord(row[4])
            except Exception:
                continue
            waypoints[row[0]] = (lat, lon)

    if task_number < 1 or task_number > len(tasks):
        raise ValueError(f"task {task_number} not found; CUP contains {len(tasks)} tasks")

    task = tasks[task_number - 1]
    if len(task) < 5:
        raise ValueError(f"task {task_number} has too few columns")

    names = task[2:-1]
    points = []
    missing = []
    for name in names:
        point = waypoints.get(name)
        if point is None:
            missing.append(name)
        else:
            points.append((name, point[0], point[1]))

    if missing:
        raise ValueError(f"task references missing waypoint(s): {missing}")
    if not points:
        raise ValueError("task has no resolvable waypoints")

    return points, len(tasks)


def buffered_bbox(points, margin_km: float):
    lats = [p[1] for p in points]
    lons = [p[2] for p in points]
    mean_lat = sum(lats) / len(lats)
    lat_margin = margin_km / 111.32
    lon_margin = margin_km / (111.32 * max(0.1, math.cos(math.radians(mean_lat))))

    return {
        "south": min(lats) - lat_margin,
        "north": max(lats) + lat_margin,
        "west": min(lons) - lon_margin,
        "east": max(lons) + lon_margin,
    }


def route_segments(points):
    """Return consecutive (lat, lon) segment pairs from task points list [(name, lat, lon), ...]."""
    if len(points) < 2:
        return []
    return [
        ((points[i][1], points[i][2]), (points[i + 1][1], points[i + 1][2]))
        for i in range(len(points) - 1)
    ]


def _dist_sq_point_seg_m(px, py, ax, ay, bx, by):
    """Squared distance in metres from point to segment (flat-earth coords)."""
    dx, dy = bx - ax, by - ay
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-10:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
    return (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2


def _bbox_in_corridor(record_bbox, segments, margin_km):
    """True if record_bbox (xmin=lon, ymin=lat, xmax=lon, ymax=lat) is within
    margin_km of ANY segment in *segments* list of ((lat1,lon1),(lat2,lon2)).

    Uses a flat-earth approximation — accurate to <0.3% at 55°N latitude.
    Checks 9 sample points on the bbox boundary/interior and 5 along the
    segment to catch all crossing configurations without exact geometry.
    """
    xmin, ymin, xmax, ymax = record_bbox
    margin_sq = (margin_km * 1000.0) ** 2

    for (lat1, lon1), (lat2, lon2) in segments:
        mean_lat = (lat1 + lat2 + ymin + ymax) / 4.0
        mlat = 111320.0
        mlon = mlat * max(0.01, math.cos(math.radians(mean_lat)))

        ax, ay = lon1 * mlon, lat1 * mlat
        bx, by = lon2 * mlon, lat2 * mlat
        mx, my = (xmin + xmax) / 2.0 * mlon, (ymin + ymax) / 2.0 * mlat

        # 9 sample points on/in the bbox
        for px, py in (
            (xmin * mlon, ymin * mlat),
            (xmin * mlon, ymax * mlat),
            (xmax * mlon, ymin * mlat),
            (xmax * mlon, ymax * mlat),
            ((xmin + xmax) / 2 * mlon, ymin * mlat),
            ((xmin + xmax) / 2 * mlon, ymax * mlat),
            (xmin * mlon, (ymin + ymax) / 2 * mlat),
            (xmax * mlon, (ymin + ymax) / 2 * mlat),
            (mx, my),
        ):
            if _dist_sq_point_seg_m(px, py, ax, ay, bx, by) <= margin_sq:
                return True

        # 5 sample points along the segment, projected onto bbox
        for t in (0.0, 0.25, 0.5, 0.75, 1.0):
            sx = ax + t * (bx - ax)
            sy = ay + t * (by - ay)
            cx = max(xmin * mlon, min(xmax * mlon, sx))
            cy = max(ymin * mlat, min(ymax * mlat, sy))
            if (sx - cx) ** 2 + (sy - cy) ** 2 <= margin_sq:
                return True

    return False


def _route_polygon(segments):
    """Extract ordered (lat, lon) vertex list from route segments.

    Deduplicates consecutive identical points so closed tasks (A→B→C→A)
    yield a proper polygon [A, B, C] suitable for point-in-polygon tests.
    Returns an empty list when fewer than 3 distinct vertices exist.
    """
    if not segments:
        return []
    poly = [segments[0][0]]
    for _, end_pt in segments:
        if end_pt != poly[-1]:
            poly.append(end_pt)
    # Drop the repeated closing vertex if it duplicates the first
    if len(poly) > 1 and poly[-1] == poly[0]:
        poly = poly[:-1]
    return poly if len(poly) >= 3 else []


def _point_in_poly(lat, lon, poly):
    """Ray-casting point-in-polygon test.  poly = [(lat, lon), ...]"""
    inside = False
    j = len(poly) - 1
    for i, (yi, xi) in enumerate(poly):
        yj, xj = poly[j]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def _bbox_overlaps_poly(xmin, ymin, xmax, ymax, poly):
    """True if the bbox (lon/lat) overlaps the polygon interior.

    Tests 9 sample points of the bbox and all polygon vertices to handle
    both "bbox inside polygon" and "polygon inside bbox" cases.
    """
    if not poly:
        return False
    # 9 sample points of bbox against polygon
    for lat in (ymin, (ymin + ymax) * 0.5, ymax):
        for lon in (xmin, (xmin + xmax) * 0.5, xmax):
            if _point_in_poly(lat, lon, poly):
                return True
    # Polygon vertices inside bbox
    for plat, plon in poly:
        if ymin <= plat <= ymax and xmin <= plon <= xmax:
            return True
    return False


def tool_path(name: str):
    base = os.environ.get("OPENJPEG_BIN")
    if base:
        candidate = Path(base) / name
        if candidate.exists():
            return str(candidate)

    found = shutil.which(name)
    if found:
        return found

    return None


def run_capture(args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def image_size(path: Path):
    opj_dump = tool_path("opj_dump")
    if opj_dump:
        out = run_capture([opj_dump, "-i", str(path)], stderr=subprocess.STDOUT)
        x1 = re.search(r"\bx1=(\d+),\s*y1=(\d+)", out)
        if x1:
            return int(x1.group(1)), int(x1.group(2))

    gdalinfo = tool_path("gdalinfo")
    if gdalinfo:
        out = run_capture([gdalinfo, str(path)], stderr=subprocess.STDOUT)
        size = re.search(r"\bSize is\s+(\d+),\s*(\d+)", out)
        if size:
            return int(size.group(1)), int(size.group(2))

    try:
        out = run_capture(["identify", "-format", "%w %h", str(path)])
    except Exception:
        out = run_capture(["convert", str(path), "-format", "%w %h", "info:"])
    w, h = out.split()[:2]
    return int(w), int(h)


def read_world(path: Path):
    values = [float(line.strip()) for line in path.read_text().splitlines() if line.strip()]
    if len(values) != 6:
        raise ValueError(f"{path} does not contain six world-file values")
    return values


def xcm_topology_bbox(xcm: Path):
    """Return the union bbox of all shapefile layers inside *xcm*, or None.

    Reads only the 100-byte shapefile header from each .shp entry (no full
    geometry load).  Returns None when the XCM contains no shapefile layers.
    """
    west = east = north = south = None
    with zipfile.ZipFile(xcm) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".shp"):
                continue
            with zf.open(name) as fh:
                header = fh.read(100)
            if len(header) < 68:
                continue
            xmin, ymin, xmax, ymax = struct.unpack_from("<dddd", header, 36)
            if xmin == 0.0 and ymin == 0.0 and xmax == 0.0 and ymax == 0.0:
                continue
            west = xmin if west is None else min(west, xmin)
            south = ymin if south is None else min(south, ymin)
            east = xmax if east is None else max(east, xmax)
            north = ymax if north is None else max(north, ymax)
    if west is None:
        return None
    return {"south": south, "west": west, "north": north, "east": east}


def crop_terrain(src_dir: Path, dst_dir: Path, bbox):
    src = src_dir / "terrain.jp2"
    world = read_world(src_dir / "terrain.j2w")
    a, d, b, e, c, f = world
    if abs(b) > 1e-12 or abs(d) > 1e-12 or a <= 0 or e >= 0:
        raise ValueError("only north-up terrain.j2w files are supported")

    width, height = image_size(src)
    left_edge = c - a / 2.0
    top_edge = f - e / 2.0
    pixel_h = -e

    x0 = max(0, math.floor((bbox["west"] - left_edge) / a))
    x1 = min(width, math.ceil((bbox["east"] - left_edge) / a))
    y0 = max(0, math.floor((top_edge - bbox["north"]) / pixel_h))
    y1 = min(height, math.ceil((top_edge - bbox["south"]) / pixel_h))
    if x1 <= x0 or y1 <= y0:
        raise ValueError("requested bbox does not overlap terrain")

    crop_w = x1 - x0
    crop_h = y1 - y0
    dst = dst_dir / "terrain.jp2"

    opj_decompress = tool_path("opj_decompress")
    opj_compress = tool_path("opj_compress")
    if opj_decompress and opj_compress:
        pgx = dst_dir / "terrain_crop.pgx"
        decode_area = f"{x0},{y0},{x1},{y1}"
        subprocess.check_call([
            opj_decompress, "-quiet", "-i", str(src), "-o", str(pgx),
            "-d", decode_area,
        ])
        actual_pgx = pgx
        if not actual_pgx.exists():
            indexed = pgx.with_name(pgx.stem + "_0" + pgx.suffix)
            if indexed.exists():
                actual_pgx = indexed
        subprocess.check_call([
            opj_compress, "-i", str(actual_pgx), "-o", str(dst),
            "-n", "2", "-t", "256,256",
        ])
        actual_pgx.unlink()
    else:
        gdal_translate = tool_path("gdal_translate")
        if gdal_translate:
            subprocess.check_call([
                gdal_translate,
                "-quiet",
                "-srcwin",
                str(x0),
                str(y0),
                str(crop_w),
                str(crop_h),
                "-of",
                "JP2OpenJPEG",
                str(src),
                str(dst),
            ])
        else:
            geometry = f"{crop_w}x{crop_h}+{x0}+{y0}"
            subprocess.check_call(["convert", str(src), "-crop", geometry, "+repage", str(dst)])

    new_c = c + a * x0 + b * y0
    new_f = f + d * x0 + e * y0
    (dst_dir / "terrain.j2w").write_text(
        f"{a:.10f}\n{d:.10f}\n{b:.10f}\n{e:.10f}\n{new_c:.10f}\n{new_f:.10f}\n"
    )

    return {
        "source_pixels": [width, height],
        "crop_pixels": [crop_w, crop_h],
        "crop_origin": [x0, y0],
    }


def shp_record_bbox(content: bytes):
    if len(content) < 4:
        return None
    shape_type = struct.unpack_from("<i", content, 0)[0]
    if shape_type == 0:
        return None
    if shape_type == 1:
        if len(content) < 20:
            return None
        x, y = struct.unpack_from("<dd", content, 4)
        return x, y, x, y
    if shape_type in (3, 5, 8):
        if len(content) < 36:
            return None
        return struct.unpack_from("<dddd", content, 4)
    return None


def intersects(record_bbox, bbox):
    xmin, ymin, xmax, ymax = record_bbox
    return not (
        xmax < bbox["west"]
        or xmin > bbox["east"]
        or ymax < bbox["south"]
        or ymin > bbox["north"]
    )


def update_shp_header(header: bytes, file_length_words: int, records):
    out = bytearray(header)
    struct.pack_into(">i", out, 24, file_length_words)
    if records:
        minx = min(r["bbox"][0] for r in records)
        miny = min(r["bbox"][1] for r in records)
        maxx = max(r["bbox"][2] for r in records)
        maxy = max(r["bbox"][3] for r in records)
        struct.pack_into("<dddd", out, 36, minx, miny, maxx, maxy)
    return bytes(out)


def crop_shapefile_group(src_dir: Path, dst_dir: Path, stem: str, bbox,
                        corridor_segments=None, corridor_margin_km: float = 0.0):
    shp_path = src_dir / f"{stem}.shp"
    dbf_path = src_dir / f"{stem}.dbf"
    if not shp_path.exists() or not dbf_path.exists():
        return None

    use_corridor = bool(corridor_segments and corridor_margin_km > 0)
    # Polygon formed by route waypoints — used to include features inside
    # the closed task area (e.g. the interior of a triangular task).
    _route_poly_cache = _route_polygon(corridor_segments) if use_corridor else []

    def _keep(rb):
        if rb is None:
            return False
        if use_corridor:
            # Keep if within 30 km of any route segment (exterior buffer)
            if _bbox_in_corridor(rb, corridor_segments, corridor_margin_km):
                return True
            # Also keep if inside the route polygon (triangle/polygon interior)
            if _route_poly_cache:
                return _bbox_overlaps_poly(rb[0], rb[1], rb[2], rb[3],
                                           _route_poly_cache)
            return False
        return intersects(rb, bbox)

    records = []
    total = 0
    with shp_path.open("rb") as shp:
        header = shp.read(100)
        while True:
            rec_header = shp.read(8)
            if not rec_header:
                break
            total += 1
            _, length_words = struct.unpack(">ii", rec_header)
            content = shp.read(length_words * 2)
            rb = shp_record_bbox(content)
            if _keep(rb):
                records.append({"content": content, "length_words": length_words, "bbox": rb})

    with dbf_path.open("rb") as dbf:
        dbf_header_prefix = bytearray(dbf.read(32))
        record_count = struct.unpack_from("<I", dbf_header_prefix, 4)[0]
        header_len = struct.unpack_from("<H", dbf_header_prefix, 8)[0]
        record_len = struct.unpack_from("<H", dbf_header_prefix, 10)[0]
        rest_header = dbf.read(header_len - 32)
        selected_dbf = []

        # The records list has lost original indices, so rescan SHP decisions
        # into a boolean list before selecting DBF rows.
        keep_flags = []
    with shp_path.open("rb") as shp:
        shp.seek(100)
        while True:
            rec_header = shp.read(8)
            if not rec_header:
                break
            _, length_words = struct.unpack(">ii", rec_header)
            content = shp.read(length_words * 2)
            rb = shp_record_bbox(content)
            keep_flags.append(_keep(rb))

    with dbf_path.open("rb") as dbf:
        dbf.seek(header_len)
        for i in range(record_count):
            record = dbf.read(record_len)
            if i < len(keep_flags) and keep_flags[i]:
                selected_dbf.append(record)

    shp_length_words = (100 + sum(8 + len(r["content"]) for r in records)) // 2
    shp_header = update_shp_header(header, shp_length_words, records)
    with (dst_dir / f"{stem}.shp").open("wb") as out:
        out.write(shp_header)
        for number, rec in enumerate(records, 1):
            out.write(struct.pack(">ii", number, rec["length_words"]))
            out.write(rec["content"])

    shx_length_words = (100 + len(records) * 8) // 2
    shx_header = update_shp_header(header, shx_length_words, records)
    with (dst_dir / f"{stem}.shx").open("wb") as out:
        out.write(shx_header)
        offset_words = 50
        for rec in records:
            out.write(struct.pack(">ii", offset_words, rec["length_words"]))
            offset_words += 4 + rec["length_words"]

    struct.pack_into("<I", dbf_header_prefix, 4, len(selected_dbf))
    with (dst_dir / f"{stem}.dbf").open("wb") as out:
        out.write(dbf_header_prefix)
        out.write(rest_header)
        for record in selected_dbf:
            out.write(record)
        out.write(b"\x1a")

    prj_path = src_dir / f"{stem}.prj"
    if prj_path.exists():
        shutil.copy2(prj_path, dst_dir / prj_path.name)

    return {"stem": stem, "kept": len(records), "total": total}


WGS84_PRJ = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
)


def shape_header(shape_type: int, bbox, file_length_words: int):
    header = bytearray(100)
    struct.pack_into(">i", header, 0, 9994)
    struct.pack_into(">i", header, 24, file_length_words)
    struct.pack_into("<i", header, 28, 1000)
    struct.pack_into("<i", header, 32, shape_type)
    struct.pack_into(
        "<dddd",
        header,
        36,
        bbox[0],
        bbox[1],
        bbox[2],
        bbox[3],
    )
    struct.pack_into("<dddd", header, 68, 0.0, 0.0, 0.0, 0.0)
    return bytes(header)


def write_dbf(path: Path, labels):
    field_name = b"ref"
    field_length = 80
    header_length = 32 + 32 + 1
    record_length = 1 + field_length
    now = time.localtime()

    header = bytearray(32)
    header[0] = 0x03
    header[1] = max(0, now.tm_year - 1900)
    header[2] = now.tm_mon
    header[3] = now.tm_mday
    struct.pack_into("<I", header, 4, len(labels))
    struct.pack_into("<H", header, 8, header_length)
    struct.pack_into("<H", header, 10, record_length)

    descriptor = bytearray(32)
    descriptor[: len(field_name)] = field_name
    descriptor[11] = ord("C")
    descriptor[16] = field_length

    with path.open("wb") as out:
        out.write(header)
        out.write(descriptor)
        out.write(b"\r")
        for label in labels:
            encoded = str(label or "").encode("latin1", errors="replace")
            encoded = encoded[:field_length].ljust(field_length, b" ")
            out.write(b" ")
            out.write(encoded)
        out.write(b"\x1a")


def normalise_polyline_feature(feature):
    if isinstance(feature, dict):
        raw_points = feature.get("points") or []
        label = feature.get("label") or ""
    else:
        raw_points = feature
        label = ""

    points = []
    for point in raw_points:
        try:
            lon, lat = point
        except Exception:
            continue
        lon = float(lon)
        lat = float(lat)
        if -180 <= lon <= 180 and -90 <= lat <= 90:
            points.append((lon, lat))

    if len(points) < 2:
        return None

    return {"points": points, "label": label}


def write_polyline_shapefile_group(dst_dir: Path, stem: str, features):
    normalised = []
    for feature in features or ():
        item = normalise_polyline_feature(feature)
        if item is not None:
            normalised.append(item)

    if not normalised:
        return None

    all_points = [point for item in normalised for point in item["points"]]
    bbox = (
        min(point[0] for point in all_points),
        min(point[1] for point in all_points),
        max(point[0] for point in all_points),
        max(point[1] for point in all_points),
    )

    records = []
    for item in normalised:
        points = item["points"]
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        content = bytearray()
        content += struct.pack("<i", 3)
        content += struct.pack("<dddd", min(xs), min(ys), max(xs), max(ys))
        content += struct.pack("<ii", 1, len(points))
        content += struct.pack("<i", 0)
        for lon, lat in points:
            content += struct.pack("<dd", lon, lat)
        records.append(
            {
                "content": bytes(content),
                "length_words": len(content) // 2,
                "label": item["label"],
            }
        )

    shp_length_words = (100 + sum(8 + len(record["content"]) for record in records)) // 2
    shp_header = shape_header(3, bbox, shp_length_words)
    with (dst_dir / f"{stem}.shp").open("wb") as out:
        out.write(shp_header)
        for number, record in enumerate(records, 1):
            out.write(struct.pack(">ii", number, record["length_words"]))
            out.write(record["content"])

    shx_length_words = (100 + len(records) * 8) // 2
    shx_header = shape_header(3, bbox, shx_length_words)
    with (dst_dir / f"{stem}.shx").open("wb") as out:
        out.write(shx_header)
        offset_words = 50
        for record in records:
            out.write(struct.pack(">ii", offset_words, record["length_words"]))
            offset_words += 4 + record["length_words"]

    write_dbf(dst_dir / f"{stem}.dbf", [record["label"] for record in records])
    (dst_dir / f"{stem}.prj").write_text(WGS84_PRJ + "\n")

    return {"stem": stem, "features": len(records)}


def normalise_polygon_feature(feature):
    item = normalise_polyline_feature(feature)
    if item is None or len(item["points"]) < 3:
        return None

    points = item["points"]
    if points[0] != points[-1]:
        points = points + [points[0]]

    if len(points) < 4:
        return None

    return {"points": points, "label": item["label"]}


def write_polygon_shapefile_group(dst_dir: Path, stem: str, features):
    normalised = []
    for feature in features or ():
        item = normalise_polygon_feature(feature)
        if item is not None:
            normalised.append(item)

    if not normalised:
        return None

    all_points = [point for item in normalised for point in item["points"]]
    bbox = (
        min(point[0] for point in all_points),
        min(point[1] for point in all_points),
        max(point[0] for point in all_points),
        max(point[1] for point in all_points),
    )

    records = []
    for item in normalised:
        points = item["points"]
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        content = bytearray()
        content += struct.pack("<i", 5)
        content += struct.pack("<dddd", min(xs), min(ys), max(xs), max(ys))
        content += struct.pack("<ii", 1, len(points))
        content += struct.pack("<i", 0)
        for lon, lat in points:
            content += struct.pack("<dd", lon, lat)
        records.append(
            {
                "content": bytes(content),
                "length_words": len(content) // 2,
                "label": item["label"],
            }
        )

    shp_length_words = (100 + sum(8 + len(record["content"]) for record in records)) // 2
    shp_header = shape_header(5, bbox, shp_length_words)
    with (dst_dir / f"{stem}.shp").open("wb") as out:
        out.write(shp_header)
        for number, record in enumerate(records, 1):
            out.write(struct.pack(">ii", number, record["length_words"]))
            out.write(record["content"])

    shx_length_words = (100 + len(records) * 8) // 2
    shx_header = shape_header(5, bbox, shx_length_words)
    with (dst_dir / f"{stem}.shx").open("wb") as out:
        out.write(shx_header)
        offset_words = 50
        for record in records:
            out.write(struct.pack(">ii", offset_words, record["length_words"]))
            offset_words += 4 + record["length_words"]

    write_dbf(dst_dir / f"{stem}.dbf", [record["label"] for record in records])
    (dst_dir / f"{stem}.prj").write_text(WGS84_PRJ + "\n")

    return {"stem": stem, "features": len(records)}


def write_extra_polyline_layers(dst_dir: Path, extra_polyline_layers=None):
    stats = []
    for stem, features in (extra_polyline_layers or {}).items():
        stem = str(stem).strip()
        if not stem:
            continue
        stat = write_polyline_shapefile_group(dst_dir, stem, features)
        if stat is not None:
            stats.append(stat)
    return stats


def write_extra_polygon_layers(dst_dir: Path, extra_polygon_layers=None):
    stats = []
    for stem, features in (extra_polygon_layers or {}).items():
        stem = str(stem).strip()
        if not stem:
            continue
        stat = write_polygon_shapefile_group(dst_dir, stem, features)
        if stat is not None:
            stats.append(stat)
    return stats


def copy_topology_template(
    src: Path,
    dst: Path,
    excluded_vector_stems,
    forced_topology_rows=None,
    available_stems=None,
):
    forced_rows = {
        stem.lower(): row
        for stem, row in (forced_topology_rows or {}).items()
    }
    available = (
        {stem.lower() for stem in available_stems}
        if available_stems is not None
        else None
    )
    written_forced = set()

    with src.open("r", encoding="utf-8", errors="replace", newline="") as f:
        lines = f.readlines()

    with dst.open("w", encoding="utf-8", newline="") as out:
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("*"):
                try:
                    row = next(csv.reader([line]))
                except Exception:
                    row = []

                if row:
                    stem = row[0].strip().lower()
                    if stem in excluded_vector_stems:
                        continue
                    if stem in forced_rows and (available is None or stem in available):
                        out.write(forced_rows[stem].rstrip() + "\n")
                        written_forced.add(stem)
                        continue

            out.write(line)

        for stem, row in forced_rows.items():
            if stem in excluded_vector_stems or stem in written_forced:
                continue
            if available is not None and stem not in available:
                continue
            out.write(row.rstrip() + "\n")


def copy_misc_files(
    src_dir: Path,
    dst_dir: Path,
    excluded_vector_stems=None,
    forced_topology_rows=None,
):
    excluded_stems = {stem.lower() for stem in (excluded_vector_stems or ())}
    forced_rows = {
        stem.lower(): row
        for stem, row in (forced_topology_rows or {}).items()
    }
    available_stems = {path.stem.lower() for path in src_dir.glob("*.shp")}
    shape_suffixes = {".shp", ".shx", ".dbf", ".prj", ".qix"}
    for path in src_dir.iterdir():
        if path.name in ("terrain.jp2", "terrain.j2w"):
            continue
        if path.suffix.lower() in shape_suffixes:
            continue
        if path.name == "topology.tpl" and (excluded_stems or forced_rows):
            copy_topology_template(
                path,
                dst_dir / path.name,
                excluded_stems,
                forced_rows,
                available_stems,
            )
            continue
        if path.is_file():
            shutil.copy2(path, dst_dir / path.name)


def zip_dir(src_dir: Path, output: Path):
    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(src_dir.iterdir()):
            if path.is_file():
                zf.write(path, path.name)


def format_size(path: Path):
    size = path.stat().st_size
    return size / (1024 * 1024)


def crop_xcm_to_bbox(
    xcm: Path,
    bbox,
    output: Path,
    workdir=None,
    keep_workdir: bool = False,
    excluded_vector_stems=None,
    forced_topology_rows=None,
    extra_polyline_layers=None,
    extra_polygon_layers=None,
    corridor_segments=None,
    corridor_margin_km: float = 0.0,
):
    if workdir is None and not keep_workdir:
        with tempfile.TemporaryDirectory(prefix="xcm_crop_") as tmp:
            return crop_xcm_to_bbox(
                xcm,
                bbox,
                output,
                workdir=Path(tmp),
                keep_workdir=False,
                excluded_vector_stems=excluded_vector_stems,
                forced_topology_rows=forced_topology_rows,
                extra_polyline_layers=extra_polyline_layers,
                extra_polygon_layers=extra_polygon_layers,
                corridor_segments=corridor_segments,
                corridor_margin_km=corridor_margin_km,
            )

    t0 = time.monotonic()
    temporary_workdir = workdir is None
    base_tmp = workdir or Path(tempfile.mkdtemp(prefix="xcm_crop_"))
    base_tmp.mkdir(parents=True, exist_ok=True)
    src_dir = base_tmp / "src"
    dst_dir = base_tmp / "dst"
    if src_dir.exists():
        shutil.rmtree(src_dir)
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    src_dir.mkdir()
    dst_dir.mkdir()

    t_extract = time.monotonic()
    with zipfile.ZipFile(xcm) as zf:
        zf.extractall(src_dir)

    injected_vector_stats = write_extra_polyline_layers(
        src_dir,
        extra_polyline_layers,
    )
    injected_polygon_stats = write_extra_polygon_layers(
        src_dir,
        extra_polygon_layers,
    )
    excluded_stems = {stem.lower() for stem in (excluded_vector_stems or ())}

    copy_misc_files(src_dir, dst_dir, excluded_stems, forced_topology_rows)
    terrain_info = crop_terrain(src_dir, dst_dir, bbox)

    stems = sorted({p.stem for p in src_dir.glob("*.shp")})
    vector_stats = []
    for stem in stems:
        if stem.lower() in excluded_stems:
            continue

        stat = crop_shapefile_group(
            src_dir, dst_dir, stem, bbox,
            corridor_segments=corridor_segments,
            corridor_margin_km=corridor_margin_km,
        )
        if stat is not None:
            vector_stats.append(stat)

    t_zip = time.monotonic()
    output.parent.mkdir(parents=True, exist_ok=True)
    zip_dir(dst_dir, output)
    t1 = time.monotonic()

    terrain_size = (dst_dir / "terrain.jp2").stat().st_size / (1024 * 1024)
    stats = {
        "terrain_info": terrain_info,
        "terrain_size_mb": terrain_size,
        "output_xcm_mb": format_size(output),
        "excluded_vector_stems": sorted(excluded_stems),
        "injected_vector_stats": injected_vector_stats,
        "injected_polygon_stats": injected_polygon_stats,
        "vector_stats": vector_stats,
        "time_extract_s": t_zip - t_extract,
        "time_crop_zip_s": t1 - t_zip,
        "time_total_s": t1 - t0,
    }

    if keep_workdir:
        stats["workdir"] = base_tmp
    elif temporary_workdir:
        shutil.rmtree(base_tmp, ignore_errors=True)

    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--xcm", required=True, type=Path)
    parser.add_argument("--cup", required=True, type=Path)
    parser.add_argument("--task", type=int, default=1)
    parser.add_argument("--margin-km", type=float, default=30.0)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args()

    points, task_count = read_cup_task(args.cup, args.task)
    bbox = buffered_bbox(points, args.margin_km)
    segments = route_segments(points)
    stats = crop_xcm_to_bbox(
        args.xcm,
        bbox,
        args.output,
        workdir=args.workdir,
        keep_workdir=args.keep_workdir,
        corridor_segments=segments,
        corridor_margin_km=args.margin_km,
    )
    print(f"task_count={task_count}")
    print(f"selected_task={args.task}")
    print("task_points=" + " | ".join(f"{name}:{lat:.5f},{lon:.5f}" for name, lat, lon in points))
    print(
        "bbox="
        f"{bbox['south']:.6f},{bbox['west']:.6f},{bbox['north']:.6f},{bbox['east']:.6f}"
    )
    terrain_info = stats["terrain_info"]
    print(f"terrain_source_pixels={terrain_info['source_pixels'][0]}x{terrain_info['source_pixels'][1]}")
    print(f"terrain_crop_pixels={terrain_info['crop_pixels'][0]}x{terrain_info['crop_pixels'][1]}")
    print(f"terrain_crop_origin={terrain_info['crop_origin'][0]},{terrain_info['crop_origin'][1]}")
    print(f"terrain_jp2_mb={stats['terrain_size_mb']:.2f}")
    print(f"output_xcm_mb={stats['output_xcm_mb']:.2f}")
    for stat in stats["vector_stats"]:
        print(f"vector_{stat['stem']}={stat['kept']}/{stat['total']}")
    print(f"time_extract_s={stats['time_extract_s']:.3f}")
    print(f"time_crop_zip_s={stats['time_crop_zip_s']:.3f}")
    print(f"time_total_s={stats['time_total_s']:.3f}")
    print(f"output={args.output}")
    if "workdir" in stats:
        print(f"workdir={stats['workdir']}")


if __name__ == "__main__":
    main()
