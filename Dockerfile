FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV HOME=/tmp
ENV PORT=8000
ENV BROWSER_PROFILE_DIR=/tmp/nusa_operator_profile
ENV WORKSPACE_DIR=/app/workspace_files

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/workspace_files /tmp/nusa_operator_profile \
    && chmod -R 777 /app/workspace_files /tmp

EXPOSE 8000

CMD ["python3", "main.py"]
