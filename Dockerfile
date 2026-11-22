# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY fleet_alerts ./fleet_alerts
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".[postgres]"

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --system --uid 10001 --home /app app
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
COPY samples ./samples
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)" || exit 1
ENTRYPOINT ["fleet-alerts"]
CMD ["api", "--host", "0.0.0.0", "--port", "8080"]
