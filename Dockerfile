# Tri Coach — container image for cloud hosting (Fly.io).
FROM python:3.12-slim

WORKDIR /srv

# Deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY app ./app
COPY static ./static

# Server + persistent paths (mounted volume lives at /data on Fly).
ENV PYTHONUNBUFFERED=1 \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8080 \
    DB_PATH=/data/coach.db \
    GARMIN_TOKENSTORE=/data/garminconnect

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
