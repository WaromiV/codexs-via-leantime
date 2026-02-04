FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./

ENV DB_HOST=mariadb \
    DB_PORT=3306 \
    DB_USER=leantime \
    DB_PASSWORD=leantime_pw \
    DB_NAME=leantime

EXPOSE 8000

CMD ["python", "server.py"]
