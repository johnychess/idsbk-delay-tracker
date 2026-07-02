FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway: attach a Volume and mount it at /data — the container filesystem
# is wiped on every redeploy/restart; without the Volume the DB is lost.
ENV DB_PATH=/data/tracker.sqlite

CMD ["python", "-m", "collector.main"]
