FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

WORKDIR /app
COPY server.py analysis.py geo.py maps.py http_app.py start.py /app/

CMD ["python3", "/app/start.py"]
