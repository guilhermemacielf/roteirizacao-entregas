# Imagem do app Flask (roteirização). O OSRM roda em container separado
# (ver docker-compose.prod.yml) — este aqui é só o backend Python.
FROM python:3.12-slim

WORKDIR /app

# Dependências do sistema: nada além do básico (ortools traz binários
# próprios; requests/flask são puro Python).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

# gunicorn: 2 workers sync, timeout alto (roteirização de cluster grande
# + chamadas OSRM podem levar dezenas de segundos).
EXPOSE 5000
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "600", "--access-logfile", "-", "--access-logformat", "%(h)s \"%(r)s\" %(s)s %(b)sb %(L)ss"]
