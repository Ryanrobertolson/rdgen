FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

RUN pip install --no-cache-dir playwright==1.63.0
WORKDIR /app
ENTRYPOINT ["python3", "/app/sync.py"]
