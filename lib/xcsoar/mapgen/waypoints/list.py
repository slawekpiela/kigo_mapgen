# -*- coding: utf-8 -*-
from xcsoar.mapgen.waypoints.waypoint import Waypoint
from xcsoar.mapgen.georect import GeoRect


class WaypointList:
    def __init__(self):
        self.__list = []
        self.__task_routes = []

    def __len__(self):
        return len(self.__list)

    def __getitem__(self, i):
        if i < 0 or i > len(self):
            return None
        return self.__list[i]

    def __iter__(self):
        return iter(self.__list)

    def append(self, wp):
        if not isinstance(wp, Waypoint):
            raise TypeError("Waypoint expected")

        self.__list.append(wp)

    def append_task_route(self, route):
        points = tuple(route)
        if len(points) < 1:
            return
        self.__task_routes.append(points)

    def extend(self, wp_list):
        if not isinstance(wp_list, WaypointList):
            raise TypeError("WaypointList expected")

        self.__list.extend(wp_list)
        self.__task_routes.extend(wp_list.__task_routes)

    def has_task_routes(self):
        return len(self.__task_routes) > 0

    def get_task_routes(self):
        return tuple(
            tuple((point.lon, point.lat) for point in route)
            for route in self.__task_routes
        )

    def __get_point_bounds(self, points, offset_distance):
        rc = GeoRect(180, -180, -90, 90)
        for point in points:
            rc.left = min(rc.left, point.lon)
            rc.right = max(rc.right, point.lon)
            rc.top = max(rc.top, point.lat)
            rc.bottom = min(rc.bottom, point.lat)

        rc.expand(offset_distance)
        return rc

    def get_bounds(self, offset_distance=15.0, task_offset_distance=30.0):
        if self.has_task_routes():
            points = []
            for route in self.__task_routes:
                points.extend(route)
            return self.__get_point_bounds(points, task_offset_distance)

        return self.__get_point_bounds(self.__list, offset_distance)
