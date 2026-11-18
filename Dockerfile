# The provisioner image, which the Kubernetes runner runs as a Job per launch
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY trovi_launcher ./trovi_launcher
RUN pip install --no-cache-dir '.[chi]'

RUN useradd --create-home provisioner
USER provisioner
CMD ["python", "-m", "trovi_launcher.provisioner.chameleon"]
