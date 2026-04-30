FROM python:3.12-slim

WORKDIR /app

COPY framing.py mux.py store.py main.py ./

ENTRYPOINT ["python", "main.py"]
