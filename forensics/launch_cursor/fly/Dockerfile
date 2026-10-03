FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY fly/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && mkdir -p /app/forensics/launch_cursor/fly /app/.local \
    && chmod 0700 /app/.local

COPY pons_*.py /app/forensics/launch_cursor/
COPY fly/entrypoint.py fly/runtime-standby.json fly/runtime-live.json fly/runtime-trader-paid.json /app/forensics/launch_cursor/fly/
RUN chmod 0600 /app/forensics/launch_cursor/fly/runtime-standby.json \
               /app/forensics/launch_cursor/fly/runtime-live.json \
               /app/forensics/launch_cursor/fly/runtime-trader-paid.json

CMD ["python", "/app/forensics/launch_cursor/fly/entrypoint.py"]
