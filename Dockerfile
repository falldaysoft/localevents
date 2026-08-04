FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN SECRET_KEY=build python manage.py collectstatic --noinput

EXPOSE 8080

# Threads, not just processes: one slow request must never be the whole pod.
# The default is a single sync worker, so any request blocked on a stalled
# database took /healthz down with it and the liveness probe restarted a
# container that was fine. Threads because this workload waits on the database
# and on outbound HTTP far more than it computes, and because the node this
# runs on has more spare I/O concurrency than spare memory.
CMD ["gunicorn", "localevents.wsgi:application", \
     "--bind", "0.0.0.0:8080", \
     "--worker-class", "gthread", \
     "--workers", "2", \
     "--threads", "4"]
