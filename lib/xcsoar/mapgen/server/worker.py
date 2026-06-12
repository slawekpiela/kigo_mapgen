# -*- coding: utf-8 -*-
import os
import sys
import time
import traceback
import shutil
from contextlib import contextmanager
from xcsoar.mapgen.server.job import Job
from xcsoar.mapgen.generator import Generator
from xcsoar.mapgen.util import check_commands


DEFAULT_ESTIMATED_POWER_WATTS = 25.0


class Worker:
    def __init__(
        self,
        dir_jobs,
        dir_data,
        mail_server=None,
        mail_sender=None,
        **kwargs
    ):
        check_commands()
        self.__dir_jobs = os.path.abspath(dir_jobs)
        self.__dir_data = os.path.abspath(dir_data)
        self.__estimated_power_watts = self.__read_estimated_power_watts()
        self.__run = False

    @staticmethod
    def __read_estimated_power_watts():
        raw_value = os.environ.get("MAPGEN_ESTIMATED_POWER_WATTS")
        if not raw_value:
            return DEFAULT_ESTIMATED_POWER_WATTS

        try:
            value = float(raw_value)
        except ValueError:
            print(
                (
                    "Invalid MAPGEN_ESTIMATED_POWER_WATTS={!r}; using {:.1f} W."
                    .format(raw_value, DEFAULT_ESTIMATED_POWER_WATTS)
                )
            )
            return DEFAULT_ESTIMATED_POWER_WATTS

        if value <= 0:
            print(
                (
                    "Invalid MAPGEN_ESTIMATED_POWER_WATTS={!r}; using {:.1f} W."
                    .format(raw_value, DEFAULT_ESTIMATED_POWER_WATTS)
                )
            )
            return DEFAULT_ESTIMATED_POWER_WATTS

        return value

    @staticmethod
    def __format_bounds(bounds):
        if not bounds:
            return "none"

        return (
            "left={:.6f},right={:.6f},top={:.6f},bottom={:.6f}"
            .format(bounds.left, bounds.right, bounds.top, bounds.bottom)
        )

    def __log_job_metrics(self, job, started_at, status):
        try:
            elapsed_seconds = max(0.0, time.monotonic() - started_at)
            energy_wh = self.__estimated_power_watts * elapsed_seconds / 3600.0
            description = job.description
            print(
                (
                    "mapgen_job_metrics uuid={} status={} name={!r} "
                    "bounds=\"{}\" elapsed_seconds={:.1f} "
                    "estimated_power_watts={:.1f} estimated_energy_wh={:.4f} "
                    "estimated_energy_kwh={:.8f}"
                ).format(
                    job.uuid,
                    status,
                    description.name,
                    self.__format_bounds(description.bounds),
                    elapsed_seconds,
                    self.__estimated_power_watts,
                    energy_wh,
                    energy_wh / 1000.0,
                )
            )
        except Exception as e:
            print(("Failed to log job metrics: {}".format(e)))

    @contextmanager
    def __high_quality_data_environment(self):
        data_url = os.environ.get("MAPGEN_HIGH_QUALITY_DATA_URL")
        if not data_url:
            raise RuntimeError(
                "High quality data generation requires MAPGEN_HIGH_QUALITY_DATA_URL."
            )

        overrides = {
            "MAPGEN_DATA_URL": data_url,
            "MAPGEN_DEM_DATASET": os.environ.get(
                "MAPGEN_HIGH_QUALITY_DEM_DATASET", "dem1"
            ),
            "MAPGEN_DEM_EXTENSIONS": os.environ.get(
                "MAPGEN_HIGH_QUALITY_DEM_EXTENSIONS", "tif"
            ),
        }
        previous = {key: os.environ.get(key) for key in overrides}
        try:
            os.environ.update(overrides)
            yield os.path.join(self.__dir_data, "high-quality")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    @contextmanager
    def __data_environment(self, description):
        if getattr(description, "high_quality", False):
            raise RuntimeError("High quality topology is disabled.")
        if getattr(description, "terrain_plus", False):
            raise RuntimeError("1 arc-second terrain is disabled.")
        if getattr(description, "level_of_detail", 3) > 3:
            raise RuntimeError("Topology level 4 is disabled.")
        if float(getattr(description, "resolution", 9.0)) < 3.0:
            raise RuntimeError("1 arc-second terrain is disabled.")

        if not getattr(description, "high_quality", False):
            yield self.__dir_data
            return

        with self.__high_quality_data_environment() as dir_data:
            yield dir_data

    def __do_job(self, job):
        started_at = time.monotonic()
        job_status = "error"
        try:
            print(
                (
                    "Generating map file for job uuid={}, name={}".format(
                        job.uuid, job.description.name
                    )
                )
            )
            description = job.description

            if not description.waypoint_file and not description.bounds:
                print("No waypoint file or bounds set. Aborting.")
                job.delete()
                job_status = "deleted"
                return

            with self.__data_environment(description) as dir_data:
                generator = Generator(dir_data, job.file_path("tmp"))

                generator.set_bounds(description.bounds)
                generator.add_information_file(job.description.name)
                generator.add_attribution_file()

                if description.use_topology:
                    job.update_status("Creating topology files...")
                    excluded_topology_layers = []
                    if getattr(description, "omit_path_lines", False) or getattr(
                        description, "omit_path_track_lines", False
                    ):
                        excluded_topology_layers.append("path_line")
                    if getattr(description, "omit_track_lines", False) or getattr(
                        description, "omit_path_track_lines", False
                    ):
                        excluded_topology_layers.append("track_line")
                    generator.add_topology(
                        compressed=description.compressed,
                        level_of_detail=description.level_of_detail,
                        excluded_topology_layers=tuple(excluded_topology_layers),
                    )

                if description.use_terrain:
                    job.update_status("Creating terrain files...")
                    task_routes = getattr(description, "task_routes", None)
                    task_terrain_margin = getattr(
                        description, "task_terrain_margin", 30.0
                    )
                    if getattr(description, "terrain_plus", False) and not getattr(
                        description, "high_quality", False
                    ):
                        with self.__high_quality_data_environment() as terrain_data:
                            generator.add_terrain(
                                description.resolution,
                                dir_data=terrain_data,
                                task_routes=task_routes,
                                task_terrain_margin=task_terrain_margin,
                            )
                    else:
                        generator.add_terrain(
                            description.resolution,
                            task_routes=task_routes,
                            task_terrain_margin=task_terrain_margin,
                        )

                if description.welt2000:
                    job.update_status("Adding welt2000 waypoints...")
                    generator.add_welt2000()
                elif description.waypoint_file:
                    job.update_status("Adding waypoint file...")
                    generator.add_waypoint_file(job.file_path(description.waypoint_file))

                if description.waypoint_details_file:
                    job.update_status("Adding waypoint details file...")
                    generator.add_waypoint_details_file(
                        job.file_path(description.waypoint_details_file)
                    )

                if description.airspace_file:
                    job.update_status("Adding airspace file...")
                    generator.add_airspace_file(job.file_path(description.airspace_file))

                job.update_status("Creating map file...")

                try:
                    generator.create(job.map_file())
                finally:
                    generator.cleanup()

                shutil.rmtree(job.file_path("tmp"))
                job.done()
                job_status = "done"
        except Exception as e:
            print(("Error: {}".format(e)))
            traceback.print_exc(file=sys.stdout)
            job.error()
            return
        finally:
            self.__log_job_metrics(job, started_at, job_status)

        print(("Map {} is ready for use.".format(job.map_file())))

    def run(self):
        self.__run = True
        print(("Monitoring {} for new jobs...".format(self.__dir_jobs)))
        while self.__run:
            try:
                job = Job.get_next(self.__dir_jobs)
                if not job:
                    time.sleep(0.5)
                    continue
                self.__do_job(job)
            except Exception as e:
                print(("Error: {}".format(e)))
                traceback.print_exc(file=sys.stdout)
