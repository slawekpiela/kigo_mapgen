# Kigo Airspace Service

Anton serves prepared OpenAir packages to Kigo Nav through the TaskMap HTTP
container.

Endpoint:

```text
POST /api/airspace
```

Request body:

```json
{
  "format_version": 1,
  "countries": [
    {
      "code": "PL",
      "controlled_airspace_base": "FL95"
    }
  ],
  "task": {
    "points": [
      {
        "lat": 49.0,
        "lon": 19.0
      }
    ]
  }
}
```

The service looks for prepared country packages in:

```text
/maps/airspaces
/opt/mapgen/airspaces
/home/slawek/kigo-airspaces
```

Override with `KIGO_AIRSPACE_PACKAGE_DIRS`, separated by commas or semicolons.
The cache directory defaults to `/tmp/kigo_airspace_cache`; override it with
`KIGO_AIRSPACE_CACHE_DIR`.

Supported package filenames include:

```text
PL.txt
kigo_PL.txt
airspace_PL.txt
usa-faa-sua-today.txt
```

`.zip` packages are accepted when they contain `.txt`, `.air`, or `.openair`
files.

Behavior:

- Returns one merged UTF-8 OpenAir `.txt` file.
- Applies `controlled_airspace_base` by removing areas that start at or above
  the selected FL and clipping higher tops to that FL.
- If task points are supplied, keeps areas within `KIGO_AIRSPACE_TASK_RADIUS_KM`
  of the task or containing a task point. The default radius is `1000`.
- Caches responses by request and source package modification state.
- Returns `404` when a country package is not prepared on Anton.

Smoke test:

```bash
python3 tools/test_kigo_airspace.py
```

The TaskMap Docker image must copy `bin/kigo_airspace.py`; `bin/kigo-task-map-server`
routes `POST /api/airspace` to that module.
