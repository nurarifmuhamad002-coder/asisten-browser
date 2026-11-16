FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV HOME=/tmp
ENV PORT=8000

# --- OCR: tesseract + bahasa Indonesia ---
# Tanpa ini, PDF hasil scan tidak bisa dibaca (pytesseract butuh binary tesseract)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-ind \
        tesseract-ocr-eng \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python3 -m playwright install chromium \
    && chmod -R 755 /ms-playwright

COPY . .
RUN chmod 1777 /tmp

EXPOSE 8000

CMD ["python3", "main.py"]
