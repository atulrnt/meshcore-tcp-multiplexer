FROM python:3.12-slim

LABEL net.unraid.docker.icon="https://github.com/atulrnt/meshcore-tcp-multiplexer/blob/main/meshcore.png?raw=true"

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY framing.py mux.py store.py telemetry.py main.py ./

ENTRYPOINT ["python", "main.py"]
