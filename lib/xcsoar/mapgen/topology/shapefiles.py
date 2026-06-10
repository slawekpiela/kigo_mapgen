# -*- coding: utf-8 -*-
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from xcsoar.mapgen.georect import GeoRect
from xcsoar.mapgen.filelist import FileList

DEFAULT_TOPOLOGY_JOBS = 6

__cmd_ogr2ogr = "ogr2ogr"
__cmd_shptree = "shptree"


def __filter_datasets(bounds, datasets):
    return [
        dataset
        for dataset in datasets
        if bounds.intersects(GeoRect(*dataset["bounds"]))
    ]


def __topology_jobs(layer_count):
    if layer_count <= 1:
        return 1

    raw = os.environ.get("MAPGEN_TOPOLOGY_JOBS", "")
    if raw:
        try:
            requested = int(raw)
        except ValueError:
            requested = DEFAULT_TOPOLOGY_JOBS
    else:
        requested = DEFAULT_TOPOLOGY_JOBS

    if requested <= 1:
        return 1

    return min(layer_count, requested, os.cpu_count() or 1)


def __create_layer_from_dataset(bounds, layer, dataset, data_dir, append, dir_temp):
    if not isinstance(bounds, GeoRect):
        raise TypeError

    print(("Reading dataset {} ...".format(dataset["name"])))
    arg = [__cmd_ogr2ogr, "-skipfailures", "-lco", "ENCODING=UTF-8"]

    if append:
        arg.append("-update")
        arg.append("-append")
    else:
        arg.extend(["-select", layer["label"] if "label" in layer else ""])

    if "where" in layer:
        arg.extend(["-where", layer["where"]])

    arg.extend(
        [
            "-spat",
            str(bounds.left),
            str(bounds.bottom),
            str(bounds.right),
            str(bounds.top),
        ]
    )

    arg.append(dir_temp)
    arg.append(data_dir)

    arg.append(layer["layer"])
    arg.extend(["-nln", layer["name"]])

    subprocess.check_call(arg)


def __create_layer_index(layer, dir_temp):
    print(("Generating index file for layer {} ...".format(layer["name"])))
    subprocess.check_call(
        [__cmd_shptree, os.path.join(dir_temp, layer["name"] + ".shp")]
    )


def __create_layer(bounds, layer, dataset_paths, dir_temp, compressed=False):
    print(("Creating topology layer {} ...".format(layer["name"])))

    layer_temp = os.path.join(dir_temp, "_topology_layers", layer["name"])
    if os.path.exists(layer_temp):
        shutil.rmtree(layer_temp)
    os.makedirs(layer_temp)

    for i, (dataset, data_dir) in enumerate(dataset_paths):
        __create_layer_from_dataset(
            bounds, layer, dataset, data_dir, i != 0, layer_temp
        )

    if os.path.exists(os.path.join(layer_temp, layer["name"] + ".shp")):
        __create_layer_index(layer, layer_temp)

        outputs = []
        for extension in (".shp", ".shx", ".dbf", ".prj", ".qix"):
            path = os.path.join(layer_temp, layer["name"] + extension)
            if os.path.exists(path):
                outputs.append((path, layer["name"] + extension, compressed))
        return layer, outputs

    return layer, []


def __create_index_file(dir_temp, index):
    file = open(os.path.join(dir_temp, "topology.tpl"), "w")
    try:
        file.write(
            "* filename, range, icon, label_index, r, g, b, pen_width, label_range, label_important_range, alpha\n"
        )
        for layer in index:
            file.write(
                layer["name"]
                + ","
                + str(layer["range"])
                + ",,"
                + ("1" if "label" in layer else "")
                + ","
                + layer["color"]
                + ","
                + str(layer.get("pen_width", 1))
                + ","
                + str(layer.get("label_range", layer["range"]))
                + ","
                + str(layer.get("label_important_range", 0))
                + ","
                + str(layer.get("alpha", 255))
                + "\n"
            )
    finally:
        file.close()
    return os.path.join(dir_temp, "topology.tpl")


def __layer_is_excluded(layer, excluded_layers):
    return layer["name"] in excluded_layers or layer["layer"] in excluded_layers


def __active_layers(layers, level_of_detail, excluded_layers):
    return [
        layer
        for layer in layers
        if (
            layer["level_of_detail"] <= level_of_detail
            and not __layer_is_excluded(layer, excluded_layers)
        )
    ]


def __retrieve_required_dataset_paths(bounds, layers, datasets, downloader):
    paths = {}
    for layer in layers:
        for dataset in __filter_datasets(bounds, datasets[layer["dataset"]]):
            name = dataset["name"]
            if name not in paths:
                paths[name] = downloader.retrieve_extracted(name + ".7z")
    return paths


def __layer_dataset_paths(bounds, layer, datasets, dataset_paths):
    return [
        (dataset, dataset_paths[dataset["name"]])
        for dataset in __filter_datasets(bounds, datasets[layer["dataset"]])
    ]


def __move_layer_outputs(dir_temp, layer_outputs):
    moved = []
    for source, name, compressed in layer_outputs:
        destination = os.path.join(dir_temp, name)
        if os.path.exists(destination):
            os.unlink(destination)
        shutil.move(source, destination)
        moved.append((destination, compressed))
    return moved


def create(
    bounds,
    downloader,
    dir_temp,
    compressed=False,
    level_of_detail=3,
    excluded_layers=None,
):
    topology = downloader.manifest()["topology"]
    layers = topology["layers"]
    datasets = topology["datasets"]
    excluded_layers = set(excluded_layers or ())
    active_layers = __active_layers(layers, level_of_detail, excluded_layers)
    dataset_paths = __retrieve_required_dataset_paths(
        bounds, active_layers, datasets, downloader
    )
    topology_jobs = __topology_jobs(len(active_layers))
    print(
        (
            "Creating {} topology layers with jobs={} ...".format(
                len(active_layers), topology_jobs
            )
        )
    )

    files = FileList()
    index = []
    results = {}
    if topology_jobs == 1:
        for layer in active_layers:
            results[layer["name"]] = __create_layer(
                bounds,
                layer,
                __layer_dataset_paths(bounds, layer, datasets, dataset_paths),
                dir_temp,
                compressed,
            )
    else:
        with ThreadPoolExecutor(max_workers=topology_jobs) as executor:
            futures = {
                executor.submit(
                    __create_layer,
                    bounds,
                    layer,
                    __layer_dataset_paths(bounds, layer, datasets, dataset_paths),
                    dir_temp,
                    compressed,
                ): layer
                for layer in active_layers
            }
            for future in as_completed(futures):
                layer = futures[future]
                results[layer["name"]] = future.result()
                print(("Topology layer {} complete.".format(layer["name"])))

    for layer in active_layers:
        _, layer_outputs = results[layer["name"]]
        if layer_outputs:
            index.append(layer)
            for path, compress in __move_layer_outputs(dir_temp, layer_outputs):
                files.add(path, compress)

    shutil.rmtree(os.path.join(dir_temp, "_topology_layers"), ignore_errors=True)

    files.add(__create_index_file(dir_temp, index), True)
    return files
