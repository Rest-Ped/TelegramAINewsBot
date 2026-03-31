FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY . /app

RUN chmod +x ./setup.sh && sh ./setup.sh build

EXPOSE 8080

CMD ["sh", "./setup.sh", "start"]
