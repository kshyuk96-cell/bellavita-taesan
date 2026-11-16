FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 && rm -rf /var/lib/apt/lists/*
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN python -m playwright install --with-deps chromium && chmod -R a+rX /opt/playwright
COPY bootstrap.py ./bootstrap.py
RUN useradd --create-home production && chown -R production:production /app
USER production
ENV PYTHONUNBUFFERED=1
CMD ["python", "bootstrap.py"]
