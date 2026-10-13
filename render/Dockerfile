# YourContent post render service: headless Chromium (Playwright) + the 35 post styles + their fonts.
# Built and run by compose.yaml (service "render"): internal network only, read-only, no capabilities.
# Playwright's image ships Chromium and its system libraries; the base is pinned by digest, the Python package
# by version to match it.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0
RUN pip install --no-cache-dir --break-system-packages playwright==1.63.0
WORKDIR /app
# Fonts come from their official source (the Google Fonts repository) at one pinned commit, each checked against
# the sha256 in fonts.json; a mismatch fails the build. They are never committed. Licences: FONTS.md.
COPY fonts.json fetch_fonts.py ./
RUN python3 fetch_fonts.py --out /opt/fonts
COPY server.py page.html engine.js base.css FONTS.md ./
COPY styles/ styles/
ENV RENDER_HOST=0.0.0.0 \
    RENDER_PORT=8191 \
    RENDER_FONT_DIR=/opt/fonts \
    HOME=/tmp \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER pwuser
EXPOSE 8191
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8191/healthz', timeout=4)"]
CMD ["python3", "server.py"]
