FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY azure_reviewer/*.py ./azure_reviewer/
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 reviewer
USER reviewer
WORKDIR /work
ENTRYPOINT ["python", "-m", "azure_reviewer"]
