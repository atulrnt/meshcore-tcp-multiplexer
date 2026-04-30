FROM python:3.12-slim

LABEL net.unraid.docker.icon="https://github.com/atulrnt/meshcore-tcp-multiplexer/blob/main/meshcore.png?raw=true"

WORKDIR /app

COPY framing.py mux.py store.py main.py ./

ENTRYPOINT ["python", "main.py"]
