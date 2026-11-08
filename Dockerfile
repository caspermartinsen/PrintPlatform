FROM node:22-slim AS preview
WORKDIR /preview
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/preview-entry.js ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /app
COPY cloud /app/cloud
COPY frontend /app/frontend
COPY --from=preview /preview/built /app/frontend/built
COPY service/server.py service/telemetry.py service/print_plan.py /app/service/
RUN useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
USER app
ENV PORT=3000 DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 3000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/healthz',timeout=3)"
CMD ["python", "cloud/server.py"]
