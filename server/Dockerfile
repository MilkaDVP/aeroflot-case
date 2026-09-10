# Образ сервера.
# Контекст сборки — корень репозитория: серверу нужны и код, и графы
# аэропортов из data/airports.
FROM python:3.12-slim

WORKDIR /app

# Зависимости ставятся отдельным слоем: правка кода не приводит
# к повторной установке пакетов при пересборке.
COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./
# Графы аэропортов, подготовленные офлайн. Сервер в интернет не ходит.
COPY data/airports/ /data/airports/

ENV AIRPORTS_DIR=/data/airports \
    DATABASE_URL=sqlite:////app/data/aeroflot.db \
    PYTHONUNBUFFERED=1

# Каталог базы данных выносится в том, чтобы данные переживали пересоздание
# контейнера, а сама база не попадала в образ.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
