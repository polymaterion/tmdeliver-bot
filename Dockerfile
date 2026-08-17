FROM python:3.12-slim

# Не пишем .pyc и не буферизуем stdout — логи сразу видны в `docker compose logs`
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Слой зависимостей отдельно от кода — при правках main.py/locales.py
# Docker переиспользует закешированный слой и не переустанавливает пакеты
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Бот работает через long polling (не webhook) — открытых портов наружу не требуется
CMD ["python", "main.py"]
