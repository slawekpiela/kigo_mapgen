# -*- coding: utf-8 -*-
import os
import math
import json
import subprocess
from zipfile import ZipFile, BadZipfile

from xcsoar.mapgen.georect import GeoRect
from xcsoar.mapgen.filelist import FileList

__cmd_gdalwarp = "gdalwarp"
__use_world_file = True


def __gdal_env():
    env = os.environ.copy()
    env.setdefault("GDAL_NUM_THREADS", "ALL_CPUS")
    env.setdefault("OMP_NUM_THREADS", str(os.cpu_count() or 1))
    return env


"""
 1) Retrieve tiles
"""


def __get_tile_name(lat, lon):
    print("lat: {}, lon: {}".format(lat, lon))
    if lat >= 0:
        ns = "n"
    else:
        ns = "s"
        lat = -lat

    if lon >= 0:
        ew = "e"
    else:
        ew = "w"
        lon = -lon

    return "" + ns + "{0:02}".format(lat) + ew + "{0:03}".format(lon)


def __retrieve_tile(downloader, dir_temp, lat, lon, dataset, extensions):
    filename = __get_tile_name(lat, lon)
    last_error = None
    for extension in extensions:
        try:
            dem_file = downloader.retrieve("{}/{}.{}".format(dataset, filename, extension))
            print(("Tile {}.{} found in {}.".format(filename, extension, dataset)))
            return dem_file
        except Exception as e:
            last_error = e
    raise last_error


def __retrieve_tiles(downloader, dir_temp, bounds, dataset, extensions):
    """
    Makes sure the terrain tiles are available at a certain location.
    @param downloader: Downloader
    @param dir_temp: Temporary path
    @param bounds: Bounding box (GeoRect)
    @return: The list of tile files
    """
    if not isinstance(bounds, GeoRect):
        raise TypeError

    print("Retrieving terrain tiles...")

    # Calculate rounded bounds
    lat_start = int(math.floor(bounds.bottom)) - 1
    lon_start = int(math.floor(bounds.left)) - 1
    lat_end = int(math.ceil(bounds.top)) + 1
    lon_end = int(math.ceil(bounds.right)) + 1

    tiles = []
    # Iterate through latitude and longitude in 1 degree interval
    for lat in range(lat_start, lat_end, 1):
        for lon in range(lon_start, lon_end, 1):
            try:
                tiles.append(
                    __retrieve_tile(downloader, dir_temp, lat, lon, dataset, extensions)
                )
            except Exception as e:
                print(
                    (
                        "Failed to retrieve tile for {0:02}/{1:02}: {2}".format(
                            lat, lon, e
                        )
                    )
                )

    # Return list of available tile files
    return tiles


def __retrieve_waterpolygons(downloader, dir_temp):
    """
    Retrieve water polygons from the OSM coastline data
    @param download: Downloader
    @param dir_temp: Temporary path
    """
    print("Retrieving water polygons...")
    water_file1 = downloader.retrieve("waterpolygons/water_polygons.dbf")
    water_file2 = downloader.retrieve("waterpolygons/water_polygons.cpg")
    water_file3 = downloader.retrieve("waterpolygons/water_polygons.shx")
    water_file = downloader.retrieve("waterpolygons/water_polygons.shp")
    return water_file


"""
 1b) Build a task-corridor polygon for non-rectangular terrain masking
"""


def __destination(lat, lon, distance_km, bearing_degrees):
    angular_distance = distance_km / 6371.0088
    bearing = math.radians(bearing_degrees)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)

    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular_distance)
        + math.cos(lat1) * math.sin(angular_distance) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(lat1),
        math.cos(angular_distance) - math.sin(lat1) * math.sin(lat2),
    )

    lon2 = (lon2 + math.pi) % (2 * math.pi) - math.pi
    return math.degrees(lon2), math.degrees(lat2)


def __initial_bearing(start, end):
    lon1, lat1 = start
    lon2, lat2 = end
    lat1 = math.radians(lat1)
    lat2 = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)

    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(
        dlon
    )
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def __circle_polygon(point, distance_km, steps=24):
    lon, lat = point
    ring = [
        list(__destination(lat, lon, distance_km, bearing))
        for bearing in [i * 360.0 / steps for i in range(steps)]
    ]
    ring.append(ring[0])
    return ring


def __segment_polygon(start, end, distance_km):
    lon1, lat1 = start
    lon2, lat2 = end
    if lon1 == lon2 and lat1 == lat2:
        return None

    bearing = __initial_bearing(start, end)
    ring = [
        list(__destination(lat1, lon1, distance_km, bearing - 90.0)),
        list(__destination(lat2, lon2, distance_km, bearing - 90.0)),
        list(__destination(lat2, lon2, distance_km, bearing + 90.0)),
        list(__destination(lat1, lon1, distance_km, bearing + 90.0)),
    ]
    ring.append(ring[0])
    return ring


def __normalise_task_route(route):
    points = []
    for point in route:
        if len(point) < 2:
            continue
        try:
            points.append((float(point[0]), float(point[1])))
        except Exception:
            continue
    return points


def __create_task_corridor_file(dir_temp, task_routes, distance_km):
    if not task_routes:
        return None

    polygons = []
    for route in task_routes:
        points = __normalise_task_route(route)
        if len(points) < 1:
            continue

        for point in points:
            polygons.append([__circle_polygon(point, distance_km)])

        for i in range(0, len(points) - 1):
            ring = __segment_polygon(points[i], points[i + 1], distance_km)
            if ring is not None:
                polygons.append([ring])

    if len(polygons) < 1:
        return None

    path = os.path.join(dir_temp, "terrain_task_corridor.geojson")
    feature = {
        "type": "Feature",
        "properties": {},
        "geometry": {"type": "MultiPolygon", "coordinates": polygons},
    }
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": [feature]}, f)
    return path


"""
 2) Merge tiles into big tif, Resample and Crop merged image
    gdalwarp
    -r cubic
        (Resampling method to use. Cubic resampling.)
    -tr $degrees_per_pixel $degrees_per_pixel
        (set output file resolution (in target georeferenced units))
    -wt Int16
        (Working pixel data type. The data type of pixels in the source
         image and destination image buffers.)
    -dstnodata -31744
        (Set nodata values for output bands (different values can be supplied
         for each band). If more than one value is supplied all values should
         be quoted to keep them together as a single operating system argument.
         New files will be initialized to this value and if possible the
         nodata value will be recorded in the output file.)
    -te $left $bottom $right $top
        (set georeferenced extents of output file to be created (in target SRS))
    a.tif b.tif c.tif ...
        (Input files)
    terrain.tif
        (Output file)
"""


def __create(dir_temp, tiles, arcseconds_per_pixel, bounds):
    print("Resampling terrain...")
    output_file = os.path.join(dir_temp, "terrain.tif")
    degree_per_pixel = float(arcseconds_per_pixel) / 3600.0

    args = [
        __cmd_gdalwarp,
        "-wo",
        "NUM_THREADS=ALL_CPUS",
        "-r",
        "cubic",
        "-tr",
        str(degree_per_pixel),
        str(degree_per_pixel),
        "-wt",
        "Int16",
        "-ot",
        "Int16",
        "-dstnodata",
        "-31744",
        "-multi",
    ]

    if __use_world_file == True:
        args.extend(["-co", "TFW=YES"])

    args.extend(
        [
            "-te",
            str(bounds.left),
            str(bounds.bottom),
            str(bounds.right),
            str(bounds.top),
        ]
    )

    args.extend(tiles)
    args.append(output_file)

    subprocess.check_call(args, env=__gdal_env())

    return output_file


"""
 3) Convert to GeoJP2 with gdal_translate
"""


def __convert(dir_temp, input_file, water_file, rc, task_corridor_file=None):
    output_file = os.path.join(dir_temp, "terrain.tif")

    if task_corridor_file is not None:
        print("Masking terrain outside task corridor...")

        args = [
            "gdal_rasterize",
            "-i",
            "-optim",
            "VECTOR",
            "-b",
            "1",
            "-burn",
            "-31744",
            task_corridor_file,
            output_file,
        ]

        subprocess.check_call(args, env=__gdal_env())

    print("Masking coastlines...")

    args = [
        "gdal_rasterize",
        "-optim",
        "VECTOR",
        "-b",
        "1",
        "-burn",
        "-31744",
        water_file,
        output_file,
    ]

    subprocess.check_call(args, env=__gdal_env())

    output = FileList()
    output.add(output_file, False)

    print("Converting terrain to JP2 format...")
    input_file = os.path.join(dir_temp, "terrain.tif")
    output_file = os.path.join(dir_temp, "terrain.jp2")

    args = [
        "gdal_translate",
        "-of",
        "JP2OpenJPEG",
        "-co",
        "BLOCKXSIZE=256",
        "-co",
        "BLOCKYSIZE=256",
        "-co",
        "QUALITY=95",
        "-co",
        "NUM_THREADS=ALL_CPUS",
        input_file,
        output_file,
    ]

    subprocess.check_call(args, env=__gdal_env())

    output = FileList()
    output.add(output_file, False)

    world_file_tiff = os.path.join(dir_temp, "terrain.tfw")
    world_file = os.path.join(dir_temp, "terrain.j2w")
    if __use_world_file and os.path.exists(world_file_tiff):
        os.rename(world_file_tiff, world_file)
        output.add(world_file, True)

    return output


def __cleanup(dir_temp):
    for file in os.listdir(dir_temp):
        if file.endswith(".tif") and (
            file.startswith("srtm_") or file.startswith("terrain")
        ):
            os.unlink(os.path.join(dir_temp, file))


def __get_dem_dataset(arcseconds_per_pixel):
    dataset = os.environ.get("MAPGEN_DEM_DATASET")
    if dataset:
        return dataset
    if float(arcseconds_per_pixel) <= 1.5:
        return "dem1"
    return "dem3"


def __get_dem_extensions(arcseconds_per_pixel):
    extensions = os.environ.get("MAPGEN_DEM_EXTENSIONS")
    if extensions:
        return [extension.strip() for extension in extensions.split(",") if extension.strip()]
    if float(arcseconds_per_pixel) <= 1.5:
        return ["hgt", "tif"]
    return ["hgt"]


def create(
    bounds,
    arcseconds_per_pixel,
    downloader,
    dir_temp,
    task_routes=None,
    task_terrain_margin=30.0,
):
    # calculate height and width (in pixels) of map from geo coordinates
    px = round((bounds.right - bounds.left) * 3600 / arcseconds_per_pixel)
    py = round((bounds.top - bounds.bottom) * 3600 / arcseconds_per_pixel)
    # round up so only full jpeg2000 tiles (256x256) are used
    # works around a bug in openjpeg 2.0.0 library
    px = (int(px / 256) + 1) * 256
    py = (int(py / 256) + 1) * 256
    # and back to geo coordinates for size
    bounds.right = bounds.left + (px * arcseconds_per_pixel / 3600)
    bounds.bottom = bounds.top - (py * arcseconds_per_pixel / 3600)

    # Make sure the tiles are available
    dataset = __get_dem_dataset(arcseconds_per_pixel)
    extensions = __get_dem_extensions(arcseconds_per_pixel)
    print("Using DEM dataset {} with extensions {}...".format(dataset, ",".join(extensions)))
    tiles = __retrieve_tiles(downloader, dir_temp, bounds, dataset, extensions)
    if len(tiles) < 1:
        return FileList()

    try:
        terrain_file = __create(dir_temp, tiles, arcseconds_per_pixel, bounds)
        water_file = __retrieve_waterpolygons(downloader, dir_temp)
        task_corridor_file = __create_task_corridor_file(
            dir_temp, task_routes, task_terrain_margin
        )
        return __convert(dir_temp, terrain_file, water_file, bounds, task_corridor_file)
    finally:
        __cleanup(dir_temp)
