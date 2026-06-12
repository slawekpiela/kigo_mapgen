# Global task-map base tiles

`tools/build_global_base_tiles.py` builds reusable 100 km XCM base tiles through
the mapgen HTTP frontend.

Current Anton run:

```sh
python3 /home/slawek/mapgen/tools/build_global_base_tiles.py \
  --output /home/slawek/mapgen-output/global-base-tiles \
  --geojson /home/slawek/mapgen-output/global-base-tiles/countries.geojson \
  --timeout-s 1200 \
  --poll-s 10
```

The output directory contains:

- `*.xcm` tile files,
- `manifest-global-100km.json`,
- `state.jsonl`,
- `build.log`,
- `countries.geojson`.

The script is resumable. Existing complete XCM files are skipped. A complete
tile must contain `terrain.jp2`, `terrain.j2w`, and `topology.tpl`.
Known failed or incomplete tiles are skipped on resume unless `--retry-failed`
is passed.

TaskMap fallback jobs must submit mapgen with plain `highres=on`. This keeps the
standard mapgen topology and 3 arc-second terrain, then TaskMap injects runway
areas, center lines, and threshold labels as a post-process. Do not enable
`high_quality` for TaskMap fallback unless the visual topology is intentionally
being changed.

TaskMap post-processing also forces POL_HighRes-style label ranges in
`topology.tpl`: city labels at 15, town labels at 10, and all split suburb /
village labels at 3. Base XCM crop is disabled by default because prebuilt
high-quality tiles can change the visual topology; enable it only with
`KIGO_TASK_MAP_USE_BASE_CROP=1`.

On Anton, start the detached build with:

```sh
python3 /home/slawek/mapgen/tools/start_global_base_tiles.py
```

Default target set:

- Europe without Russia and Belarus,
- USA,
- Canada,
- Australia,
- Japan,
- Namibia,
- South Africa.
