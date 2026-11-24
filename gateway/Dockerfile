FROM python:3.12-slim

WORKDIR /app
# логи сразу в консоль docker compose, без буферизации
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["python", "app.py"]
