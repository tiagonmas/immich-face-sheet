FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 faceapp \
    && install -d -o faceapp -g faceapp /data

COPY --chown=faceapp:faceapp immich_face_sheet.py web_app.py ./
COPY --chown=faceapp:faceapp templates ./templates
COPY --chown=faceapp:faceapp static ./static

USER faceapp
EXPOSE 8080

CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:8080", "--access-logfile", "-", "web_app:app"]
