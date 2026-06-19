FROM debian:bullseye-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    libopenjp2-tools \
    gdal-bin \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY bin/kigo-task-map-server /opt/taskmap/kigo-task-map-server
COPY bin/kigo_airspace.py /opt/taskmap/kigo_airspace.py
COPY bin/crop_xcm_corridor.py /opt/taskmap/crop_xcm_corridor.py

EXPOSE 8080

CMD ["python3", "/opt/taskmap/kigo-task-map-server", \
     "--host", "0.0.0.0", "--port", "8080"]
