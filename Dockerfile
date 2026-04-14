FROM python:3.12-slim

LABEL maintainer="NuHeat Thermostat Control"
LABEL description="NuHeat floor heating thermostat control server"

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY nuheat/ nuheat/
COPY setup.py .

# Install the package
RUN pip install --no-cache-dir .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')" || exit 1

# Default: start the REST API server
CMD ["python", "-m", "nuheat.cli", "serve", "--host", "0.0.0.0", "--port", "8080"]
