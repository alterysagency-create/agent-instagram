FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY start_production.py .
COPY services ./services
RUN useradd --create-home appuser
USER appuser
CMD ["python", "start_production.py"]
