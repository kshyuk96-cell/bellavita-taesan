FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY bootstrap.py ./bootstrap.py
RUN useradd --create-home production && chown -R production:production /app
USER production
ENV PYTHONUNBUFFERED=1
CMD ["python", "bootstrap.py"]
