FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN SECRET_KEY=build python manage.py collectstatic --noinput

EXPOSE 8080

CMD ["gunicorn", "localevents.wsgi:application", "--bind", "0.0.0.0:8080"]
