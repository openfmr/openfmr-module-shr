# =============================================================================
# Dockerfile — SHR Ingestion Engine
# =============================================================================
# A lightweight FastAPI microservice that validates incoming FHIR Transaction
# Bundles by cross-referencing Patient and Facility identifiers against the
# Client Registry (CR) and Health Facility Registry (HFR) before persisting
# the bundle into the local Shared Health Record (SHR) FHIR server.
# =============================================================================

FROM python:3.11-slim

# Metadata labels.
LABEL maintainer="OpenFMR Team"
LABEL description="SHR Ingestion Engine — FHIR Bundle validation and forwarding"

# Prevent Python from buffering stdout/stderr (important for Docker logs).
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first (layer caching optimisation).
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code.
COPY app/ ./app/

# Expose the FastAPI default port.
EXPOSE 8000

# Run the application with Uvicorn.
# --host 0.0.0.0 ensures the server binds to all network interfaces.
# --workers 4 provides concurrency for handling multiple EMR submissions.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
