FROM python:3.12-slim
# ffmpeg: probe media, grab last frames, stitch extensions, render previews and exports.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app.py ./
COPY medialab/ medialab/
COPY static/ static/
# The UI's fonts (Inter, Space Grotesk, JetBrains Mono; OFL-1.1) come from the same pinned, hash-checked source as the
# render service's (render/fonts.json); they are never committed. Their licences land in static/fonts/licences/.
COPY render/fonts.json render/fetch_fonts.py /tmp/fonts/
RUN python3 /tmp/fonts/fetch_fonts.py --manifest /tmp/fonts/fonts.json --out static/fonts \
      --family Inter --family "Space Grotesk" --family "JetBrains Mono" && rm -rf /tmp/fonts
# The craft runner's Python (it runs in its own container, craft/Dockerfile); here only so tests/test_craft.py runs.
COPY craft/__init__.py craft/craftd.py craft/recipes.py craft/mcp_client.py craft/craft-mcp craft/
# tests/ ships in the image so the suite runs in the container (see THIRD-PARTY-NOTICES: test fixtures)
COPY tests/ tests/
# Container paths; compose mounts the real folders here. 0.0.0.0 inside the container only: compose
# publishes the port on 127.0.0.1 unless an override says otherwise.
ENV YOURCONTENT_HOST=0.0.0.0 \
    YOURCONTENT_PORT=8190 \
    YOURCONTENT_DATA=/data \
    YOURCONTENT_COMFY_OUTPUT=/output \
    YOURCONTENT_UPLOADS=/uploads \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /data /output /uploads && chown 1000:1000 /data /output /uploads
USER 1000:1000
EXPOSE 8190
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python3", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/config' % os.environ.get('YOURCONTENT_PORT', '8190'), timeout=4)"]
CMD ["python3", "app.py"]
