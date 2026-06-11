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

Default target set:

- Europe without Russia and Belarus,
- USA,
- Canada,
- Australia,
- Japan,
- Namibia,
- South Africa.

