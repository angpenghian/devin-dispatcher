FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir click>=8.1 httpx>=0.27 jinja2>=3.1

COPY dispatcher.py prompt.j2 output_schema.json ./

ENTRYPOINT ["python", "/app/dispatcher.py"]
CMD ["--help"]
