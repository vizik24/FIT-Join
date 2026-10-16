FROM python:3.11.17-slim-bookworm

WORKDIR /app

COPY requirements.txt .

RUN pip install -r requirements.txt && pip cache purge

COPY . .

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

