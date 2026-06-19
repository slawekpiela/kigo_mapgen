#!/usr/bin/env python3
"""Anton-side OpenAir package assembler for Kigo Nav.

The phone sends country selections and optional task points.  Anton finds
prepared country OpenAir packages, applies lightweight request filters, merges
the result and returns one ready-to-activate OpenAir text file.
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import zipfile


DEFAULT_PACKAGE_DIRS = (
    "/maps/airspaces",
    "/opt/mapgen/airspaces",
    "/home/slawek/kigo-airspaces",
)
DEFAULT_CACHE_DIR = "/tmp/kigo_airspace_cache"
SUPPORTED_COUNTRIES = frozenset(("AT", "CZ", "DE", "FR", "PL", "SI", "SK", "US"))
TASK_FILTER_RADIUS_KM = float(os.environ.get("KIGO_AIRSPACE_TASK_RADIUS_KM", "1000"))


class AirspaceRequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class CountryRequest:
    code: str
    controlled_airspace_base: str


@dataclass(frozen=True)
class AirspacePackage:
    code: str
    path: Path
    text: str
    source_id: str


@dataclass(frozen=True)
class AirspaceBuildResult:
    data: bytes
    filename: str
    countries: tuple[str, ...]
    sources: tuple[str, ...]
    cache_status: str


def package_dirs() -> tuple[Path, ...]:
    configured = os.environ.get("KIGO_AIRSPACE_PACKAGE_DIRS", "")
    if configured:
        parts = configured.replace(";", ",").split(",")
        return tuple(Path(part.strip()) for part in parts if part.strip())

    return tuple(Path(path) for path in DEFAULT_PACKAGE_DIRS)


def cache_dir() -> Path:
    return Path(os.environ.get("KIGO_AIRSPACE_CACHE_DIR", DEFAULT_CACHE_DIR))


def upper_ascii(value: str) -> str:
    return "".join(chr(ord(ch) - 32) if "a" <= ch <= "z" else ch for ch in value)


def normalize_country_code(value) -> str:
    code = upper_ascii(str(value or "").strip())
    if code == "USA":
        code = "US"

    if not re.fullmatch(r"[A-Z]{2}", code):
        raise AirspaceRequestError(400, f"invalid country code: {value!r}")
    if code not in SUPPORTED_COUNTRIES:
        raise AirspaceRequestError(400, f"unsupported country code: {code}")

    return code


def normalize_controlled_base(value) -> str:
    text = upper_ascii(str(value or "").strip())
    if not text:
        return ""
    if text.isdigit():
        text = "FL" + text
    if not re.fullmatch(r"FL[0-9]{1,3}", text):
        raise AirspaceRequestError(400, f"invalid controlled_airspace_base: {value!r}")
    return text


def parse_airspace_request(payload: dict) -> tuple[tuple[CountryRequest, ...], tuple[tuple[float, float], ...]]:
    countries = payload.get("countries")
    if not isinstance(countries, list) or not countries:
        raise AirspaceRequestError(400, "countries must be a non-empty list")
    if len(countries) > 16:
        raise AirspaceRequestError(400, "too many countries")

    parsed: list[CountryRequest] = []
    seen: set[str] = set()
    for item in countries:
        if not isinstance(item, dict):
            raise AirspaceRequestError(400, "country entries must be objects")

        code = normalize_country_code(item.get("code") or item.get("country_code"))
        if code in seen:
            continue

        parsed.append(
            CountryRequest(
                code=code,
                controlled_airspace_base=normalize_controlled_base(
                    item.get("controlled_airspace_base")
                ),
            )
        )
        seen.add(code)

    task_points = parse_task_points(payload.get("task"))
    return tuple(parsed), task_points


def parse_task_points(task) -> tuple[tuple[float, float], ...]:
    if not isinstance(task, dict):
        return ()

    points = task.get("points")
    if not isinstance(points, list):
        return ()

    parsed: list[tuple[float, float]] = []
    for point in points[:64]:
        if not isinstance(point, dict):
            continue
        try:
            lat = float(point["lat"])
            lon = float(point["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            parsed.append((lat, lon))

    return tuple(parsed)


def country_file_names(code: str) -> tuple[str, ...]:
    variants = (code, "USA") if code == "US" else (code,)
    suffixes = (".txt", ".air", ".openair", ".zip")
    names: list[str] = []

    for variant in variants:
        lower = variant.lower()
        for stem in (
            variant,
            lower,
            f"kigo_{variant}",
            f"kigo_{lower}",
            f"airspace_{variant}",
            f"airspace_{lower}",
        ):
            names.extend(stem + suffix for suffix in suffixes)

    if code == "US":
        names.extend(("usa-faa-sua-today.txt", "USA-faa-sua-today.txt"))

    return tuple(dict.fromkeys(names))


def find_country_package_path(code: str) -> Path:
    for directory in package_dirs():
        for name in country_file_names(code):
            path = directory / name
            if path.is_file():
                return path

    searched = ", ".join(str(path) for path in package_dirs())
    raise AirspaceRequestError(
        404,
        f"airspace package for {code} is not prepared on Anton; searched {searched}",
    )


def read_package_text(path: Path) -> tuple[str, str]:
    stat = path.stat()
    source_id = f"{path}:{stat.st_mtime_ns}:{stat.st_size}"

    if path.suffix.lower() != ".zip":
        return path.read_text(encoding="utf-8", errors="replace"), source_id

    chunks: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = [
            name
            for name in sorted(zf.namelist())
            if name.lower().endswith((".txt", ".air", ".openair"))
            and not name.endswith("/")
        ]
        for name in names:
            chunks.append(zf.read(name).decode("utf-8", errors="replace"))

    if not chunks:
        raise AirspaceRequestError(500, f"zip package has no OpenAir text files: {path}")

    return "\n\n".join(chunks), source_id


def load_country_package(country: CountryRequest) -> AirspacePackage:
    path = find_country_package_path(country.code)
    text, source_id = read_package_text(path)
    return AirspacePackage(country.code, path, text, source_id)


def parse_fl_base(value: str) -> int | None:
    match = re.fullmatch(r"FL([0-9]{1,3})", value or "")
    if not match:
        return None
    return int(match.group(1)) * 100


def parse_altitude_feet(value: str) -> float | None:
    text = upper_ascii(value).replace(",", ".")
    if "UNL" in text or "UNLIMITED" in text:
        return 1_000_000.0
    if "SFC" in text or "GND" in text or "GROUND" in text:
        return 0.0

    fl = re.search(r"\bFL\s*([0-9]{1,3})\b", text)
    if fl:
        return float(fl.group(1)) * 100.0

    feet = re.search(r"(-?[0-9]+(?:\.[0-9]+)?)\s*(?:FT|F|AMSL|MSL)\b", text)
    if feet:
        return float(feet.group(1))

    meters = re.search(r"(-?[0-9]+(?:\.[0-9]+)?)\s*M\b", text)
    if meters:
        return float(meters.group(1)) * 3.28084

    return None


def openair_field(line: str, prefix: str) -> str | None:
    stripped = line.strip()
    if upper_ascii(stripped).startswith(prefix + " "):
        return stripped[len(prefix):].strip()
    return None


def paragraph_fields(paragraph: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in paragraph.splitlines():
        for prefix in ("AC", "AN", "AL", "AH"):
            value = openair_field(line, prefix)
            if value is not None and prefix not in fields:
                fields[prefix] = value
    return fields


def split_openair_paragraphs(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return [part.strip("\n") for part in re.split(r"\n\s*\n", normalized) if part.strip()]


def is_openair_area(paragraph: str) -> bool:
    return "AC" in paragraph_fields(paragraph)


def apply_controlled_base(paragraph: str, controlled_base: str) -> str | None:
    threshold_feet = parse_fl_base(controlled_base)
    if threshold_feet is None or not is_openair_area(paragraph):
        return paragraph

    fields = paragraph_fields(paragraph)
    lower_feet = parse_altitude_feet(fields.get("AL", ""))
    if lower_feet is not None and lower_feet >= threshold_feet:
        return None

    upper_feet = parse_altitude_feet(fields.get("AH", ""))
    if upper_feet is None or upper_feet <= threshold_feet:
        return paragraph

    lines = []
    replaced = False
    for line in paragraph.splitlines():
        if openair_field(line, "AH") is not None and not replaced:
            lines.append(f"AH {controlled_base}")
            replaced = True
        else:
            lines.append(line)

    return "\n".join(lines)


COORD_RE = re.compile(
    r"(\d{1,3})(?::(\d{1,2}))?(?::(\d{1,2}(?:\.\d+)?))?\s*([NS])"
    r"\s+"
    r"(\d{1,3})(?::(\d{1,2}))?(?::(\d{1,2}(?:\.\d+)?))?\s*([EW])",
    re.IGNORECASE,
)


def dms_to_degrees(degrees: str, minutes: str | None, seconds: str | None, hemi: str) -> float:
    value = float(degrees)
    if minutes is not None:
        value += float(minutes) / 60.0
    if seconds is not None:
        value += float(seconds) / 3600.0
    if upper_ascii(hemi) in ("S", "W"):
        value = -value
    return value


def paragraph_points(paragraph: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for match in COORD_RE.finditer(paragraph):
        lat = dms_to_degrees(match.group(1), match.group(2), match.group(3), match.group(4))
        lon = dms_to_degrees(match.group(5), match.group(6), match.group(7), match.group(8))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            points.append((lat, lon))
    return points


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    )
    return 6371.0 * 2.0 * math.atan2(math.sqrt(h), math.sqrt(max(0.0, 1.0 - h)))


def point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    lat, lon = point
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        lati, loni = polygon[i]
        latj, lonj = polygon[j]
        intersects = ((loni > lon) != (lonj > lon)) and (
            lat < (latj - lati) * (lon - loni) / ((lonj - loni) or 1e-12) + lati
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def task_filter_allows(paragraph: str, task_points: tuple[tuple[float, float], ...]) -> bool:
    if not task_points or not is_openair_area(paragraph):
        return True

    points = paragraph_points(paragraph)
    if not points:
        return True

    for task_point in task_points:
        for point in points:
            if distance_km(task_point, point) <= TASK_FILTER_RADIUS_KM:
                return True

        if len(points) >= 3 and point_in_polygon(task_point, points):
            return True

    return False


def filter_package_text(
    package: AirspacePackage,
    country: CountryRequest,
    task_points: tuple[tuple[float, float], ...],
) -> str:
    output: list[str] = []
    for paragraph in split_openair_paragraphs(package.text):
        limited = apply_controlled_base(paragraph, country.controlled_airspace_base)
        if limited is None:
            continue
        if not task_filter_allows(limited, task_points):
            continue
        output.append(limited)

    return "\n\n".join(output).strip() + "\n"


def request_cache_key(
    countries: tuple[CountryRequest, ...],
    task_points: tuple[tuple[float, float], ...],
    packages: tuple[AirspacePackage, ...],
) -> str:
    digest = hashlib.sha256()
    digest.update(b"kigo-airspace-v1\n")
    digest.update(json.dumps([country.__dict__ for country in countries], sort_keys=True).encode())
    digest.update(json.dumps(task_points, sort_keys=True).encode())
    for package in packages:
        digest.update(package.source_id.encode("utf-8", errors="replace"))
        digest.update(b"\n")
    return digest.hexdigest()


def response_filename(countries: tuple[CountryRequest, ...]) -> str:
    if len(countries) == 1:
        stem = f"kigo_{countries[0].code}"
    else:
        stem = "kigo_merged_" + "_".join(country.code for country in countries)
    return stem + "_" + time.strftime("%d_%m_%Y", time.localtime()) + ".txt"


def write_cache(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def build_airspace_response(payload: dict) -> AirspaceBuildResult:
    countries, task_points = parse_airspace_request(payload)
    packages = tuple(load_country_package(country) for country in countries)
    key = request_cache_key(countries, task_points, packages)
    cached_path = cache_dir() / (key + ".txt")
    filename = response_filename(countries)

    if cached_path.is_file():
        return AirspaceBuildResult(
            data=cached_path.read_bytes(),
            filename=filename,
            countries=tuple(country.code for country in countries),
            sources=tuple(str(package.path) for package in packages),
            cache_status="hit",
        )

    header = [
        "* KIGO Anton airspace export",
        "* generated_utc=" + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "* countries=" + ",".join(country.code for country in countries),
        "* task_filter_radius_km=" + str(int(TASK_FILTER_RADIUS_KM)),
    ]

    chunks: list[str] = ["\n".join(header) + "\n"]
    for country, package in zip(countries, packages):
        chunks.append(f"* package_country={country.code}")
        chunks.append(f"* package_source={package.path}")
        if country.controlled_airspace_base:
            chunks.append(f"* kigo_controlled_airspace_base={country.code}:{country.controlled_airspace_base}")
        chunks.append(filter_package_text(package, country, task_points))

    text = "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip()) + "\n"
    data = text.encode("utf-8", errors="replace")
    write_cache(cached_path, data)

    return AirspaceBuildResult(
        data=data,
        filename=filename,
        countries=tuple(country.code for country in countries),
        sources=tuple(str(package.path) for package in packages),
        cache_status="miss-filled",
    )


def read_http_json(handler: BaseHTTPRequestHandler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise AirspaceRequestError(400, "invalid Content-Length") from exc

    if length <= 0 or length > 1024 * 1024:
        raise AirspaceRequestError(413, "invalid request size")

    try:
        payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    except Exception as exc:
        raise AirspaceRequestError(400, f"bad JSON request: {exc}") from exc

    if not isinstance(payload, dict):
        raise AirspaceRequestError(400, "payload must be a JSON object")

    return payload


def send_http_text(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    body = (message.rstrip() + "\n").encode("utf-8", errors="replace")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_airspace_post(handler: BaseHTTPRequestHandler) -> None:
    try:
        payload = read_http_json(handler)
        result = build_airspace_response(payload)
    except AirspaceRequestError as exc:
        send_http_text(handler, exc.status, exc.message)
        return
    except Exception as exc:
        send_http_text(handler, 500, f"airspace update failed: {exc}")
        return

    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header(
        "Content-Disposition",
        f"attachment; filename={result.filename}",
    )
    handler.send_header("Content-Length", str(len(result.data)))
    handler.send_header("X-Kigo-Airspace-Countries", ",".join(result.countries))
    handler.send_header("X-Kigo-Airspace-Sources", ",".join(result.sources)[:800])
    handler.send_header("X-Kigo-Cache", result.cache_status)
    handler.end_headers()
    handler.wfile.write(result.data)
    handler.log_message(
        "airspace generated countries=%s cache=%s bytes=%d sources=%s",
        ",".join(result.countries),
        result.cache_status,
        len(result.data),
        ",".join(result.sources),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    payload = json.loads(args.request.read_text(encoding="utf-8"))
    result = build_airspace_response(payload)
    args.output.write_bytes(result.data)
    print(
        f"wrote {args.output} countries={','.join(result.countries)} "
        f"bytes={len(result.data)} cache={result.cache_status}"
    )


if __name__ == "__main__":
    main()
