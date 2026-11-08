FROM ubuntu:24.04
ARG BAMBU_URL=https://github.com/bambulab/BambuStudio/releases/download/v02.08.04.57/BambuStudio_ubu24-v02.08.04.57-20260922164607.AppImage
ARG BAMBU_SHA256=4870e355fa5e98e3a47bcd196b7ae16c2e9c15896a81ceaabe09ddbefa80d2f0
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates curl xvfb xauth libgtk-3-0 libwebkit2gtk-4.1-0 libglu1-mesa libgl1 libegl1 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 libsecret-1-0 libnss3 libxss1 libasound2t64 libfreetype6 libfontconfig1 fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/bambu
RUN apt-get update && apt-get install -y --no-install-recommends squashfs-tools && rm -rf /var/lib/apt/lists/*
COPY slicer/extract_appimage.py /tmp/extract_appimage.py
RUN curl -fL "$BAMBU_URL" -o /tmp/bambu.AppImage && echo "$BAMBU_SHA256  /tmp/bambu.AppImage" | sha256sum -c - && python3 /tmp/extract_appimage.py /tmp/bambu.AppImage squashfs-root >/dev/null && rm /tmp/bambu.AppImage
WORKDIR /app
COPY slicer /app/slicer
COPY cloud/model_files.py /app/cloud/model_files.py
RUN profiles=$(find /opt/bambu/squashfs-root -type d -path '*/profiles/BBL' | head -1) && test -n "$profiles" && python3 slicer/profiles.py "$profiles" /profiles && mkdir -p "$profiles/machine_full" && cp "$profiles/machine/Bambu Lab P2S.json" "$profiles/machine_full/Bambu Lab P2S.json" && cp /profiles/machine.json "$profiles/machine_full/Bambu Lab P2S 0.4 nozzle.json"
COPY slicer/bambu-headless /usr/local/bin/bambu-headless
RUN chmod 755 /usr/local/bin/bambu-headless && useradd --uid 10001 --create-home slicer
USER slicer
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=30s --timeout=5s --start-period=6m CMD python3 -c "from pathlib import Path; import time; assert time.time()-Path('/tmp/slicer-heartbeat').stat().st_mtime<360"
CMD ["python3", "slicer/worker.py"]
