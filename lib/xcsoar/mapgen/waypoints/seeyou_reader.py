# -*- coding: utf-8 -*-
from xcsoar.mapgen.waypoints.waypoint import Waypoint
from xcsoar.mapgen.waypoints.list import WaypointList

__TASK_MARKER = "-----Related Tasks-----"


def __decode_line(line):
    if isinstance(line, bytes):
        return line.decode("utf-8", "replace")

    return line


class __CSVLine:
    def __init__(self, line):
        self.__line = line
        self.__index = 0

    def has_next(self):
        return self.__index < len(self.__line)

    def __next__(self):
        if self.__index >= len(self.__line):
            return None

        in_quotes = False

        for i in range(self.__index, len(self.__line)):
            if self.__line[i] == '"':
                in_quotes = not in_quotes

            if self.__line[i] == "," and not in_quotes:
                break

        next = (
            self.__line[self.__index : i + 1].rstrip(",").strip('"').replace('"', '"')
        )
        self.__index = i + 1

        return next

    next = __next__


def __parse_csv_fields(line):
    fields = []
    line = __CSVLine(line)
    while line.has_next():
        fields.append(next(line))
    return fields


def __parse_altitude(str):
    str = str.lower()
    if str.endswith("ft") or str.endswith("f"):
        str = str.rstrip("ft")
        return int(float(str) * 0.3048)
    else:
        str = str.rstrip("m")
        if len(str) > 0:
            return int(float(str))
        else:
            return None


def __parse_coordinate(str):
    str = str.lower()
    negative = str.endswith("s") or str.endswith("w")
    is_lon = str.endswith("e") or str.endswith("w")
    str = str.rstrip("sw") if negative else str.rstrip("ne")

    # degrees + minutes / 60
    if is_lon:
        a = int(str[:3]) + float(str[3:]) / 60
    else:
        a = int(str[:2]) + float(str[2:]) / 60

    if negative:
        a *= -1
    return a


def __parse_length(str):
    str = str.lower()
    if str.endswith("m"):
        str = str.rstrip("m")
        return int(float(str))
    else:
        return None


def __parse_waypoint_fields(fields, bounds=None):
    if len(fields) < 6:
        return None

    try:
        lat = __parse_coordinate(fields[3])
        if bounds and (lat > bounds.top or lat < bounds.bottom):
            return None

        lon = __parse_coordinate(fields[4])
        if bounds and (lon > bounds.right or lon < bounds.left):
            return None

        wp = Waypoint()
        wp.lat = lat
        wp.lon = lon
        wp.altitude = __parse_altitude(fields[5])
        wp.name = fields[0].strip()
        wp.short_name = fields[1].strip()
        wp.country_code = fields[2].strip()
    except Exception:
        return None

    try:
        if len(fields) > 6 and len(fields[6]) > 0:
            wp.cup_type = int(fields[6])
    except Exception:
        pass

    try:
        if len(fields) > 7 and len(fields[7]) > 0:
            wp.runway_dir = int(fields[7])
    except Exception:
        pass

    try:
        if len(fields) > 8 and len(fields[8]) > 0:
            wp.runway_len = __parse_length(fields[8])
    except Exception:
        pass

    try:
        if len(fields) > 9 and len(fields[9]) > 0:
            wp.freq = float(fields[9])
    except Exception:
        pass

    if len(fields) > 10 and len(fields[10]) > 0:
        wp.comment = fields[10].strip()

    return wp


def __normalise_name(name):
    return name.strip().casefold()


def __build_waypoint_lookup(waypoint_list):
    lookup = {}
    for wp in waypoint_list:
        if wp.name:
            lookup[__normalise_name(wp.name)] = wp
        if wp.short_name:
            lookup.setdefault(__normalise_name(wp.short_name), wp)
    return lookup


def __is_task_metadata_line(fields):
    if len(fields) == 0:
        return True

    keyword = fields[0].strip().casefold()
    return (
        keyword.startswith("options")
        or keyword.startswith("obszone")
        or keyword.startswith("starts=")
    )


def __parse_task_point_definition(fields, bounds=None):
    if len(fields) < 2:
        return None, None

    first = fields[0].strip()
    if not first.casefold().startswith("point="):
        return None, None

    try:
        index = int(first.split("=", 1)[1])
    except ValueError:
        return None, None

    return index, __parse_waypoint_fields(fields[1:], bounds)


def __flush_task_route(waypoint_list, points):
    if points is None:
        return

    route = []
    for point in points:
        if point is None:
            if len(route) >= 2:
                waypoint_list.append_task_route(route)
            route = []
        else:
            route.append(point)

    if len(route) >= 2:
        waypoint_list.append_task_route(route)


def __parse_task_fields(fields, waypoint_lookup):
    if len(fields) < 2:
        return None

    start = 1
    if __normalise_name(fields[0]) in waypoint_lookup:
        start = 0

    points = []
    for name in fields[start:]:
        name = name.strip()
        if not name:
            continue
        points.append(waypoint_lookup.get(__normalise_name(name)))

    return points if len(points) >= 2 else None


def parse_seeyou_waypoints(lines, bounds=None):
    waypoint_list = WaypointList()
    task_lines = []

    first = True
    in_tasks = False
    for line in lines:
        line = __decode_line(line)
        if first:
            first = False
            continue

        line = line.strip()
        if line == "" or line.startswith("*"):
            continue

        if line == __TASK_MARKER:
            in_tasks = True
            continue

        if in_tasks:
            task_lines.append(line)
            continue

        if line == "name,code,country,lat,lon,elev,style,rwdir,rwlen,freq,desc":
            continue

        wp = __parse_waypoint_fields(__parse_csv_fields(line), bounds)
        if wp is not None:
            waypoint_list.append(wp)

    waypoint_lookup = __build_waypoint_lookup(waypoint_list)
    task_points = None
    for line in task_lines:
        fields = __parse_csv_fields(line)

        point_index, point = __parse_task_point_definition(fields, bounds)
        if point_index is not None:
            if task_points is not None and point is not None:
                while len(task_points) <= point_index:
                    task_points.append(None)
                task_points[point_index] = point
            continue

        if __is_task_metadata_line(fields):
            continue

        new_task_points = __parse_task_fields(fields, waypoint_lookup)
        if new_task_points is not None:
            __flush_task_route(waypoint_list, task_points)
            task_points = new_task_points

    __flush_task_route(waypoint_list, task_points)

    return waypoint_list
