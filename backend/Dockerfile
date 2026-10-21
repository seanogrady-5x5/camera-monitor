FROM python:3.12-slim

WORKDIR /app

# ffmpeg for the timelapse runner (CPU encoding for now)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

EXPOSE 8011

# The fetcher runs from the same image with: python fetcher.py
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8011"]
