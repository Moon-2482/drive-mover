FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py mega_dl.py run.sh ./
COPY frontend ./frontend
# drive-migration engine is bundled (no external dependency at runtime)
COPY drive-migration ./drive-migration
ENV PORT=7860 HOST=0.0.0.0 DRIVEMOVER_MODE=api
ENV MOVER_DB=/data/mover.db MOVER_STAGING=/data/staging MOVER_TMPDIR=/data/tmp
RUN mkdir -p /data
EXPOSE 7860
CMD ["python3", "server.py"]
