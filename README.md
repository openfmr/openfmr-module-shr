# OpenFMR Module: Shared Health Record (SHR)

The **Shared Health Record (SHR)** module is the central clinical data repository for the [OpenFMR](https://github.com/openfmr) Health Information Exchange. It stores FHIR R4 clinical resources — Encounters, Observations, Conditions, DiagnosticReports, MedicationRequests, and more — submitted by external Electronic Medical Record (EMR) systems.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      openfmr_global_net                         │
│                                                                 │
│  ┌───────────┐    ┌───────────────────┐    ┌────────────────┐   │
│  │  EMR /    │───▶│  shr-ingestion-   │───▶│  shr-fhir-     │   │
│  │  Client   │    │  engine (:8000)   │    │  server (:8080)│   │
│  └───────────┘    └───────┬───┬───────┘    └───────┬────────┘   │
│                       │       │                    │             │
│              ┌────────▼┐  ┌───▼────────┐   ┌──────▼─────────┐   │
│              │  CR     │  │  HFR       │   │  shr-postgres  │   │
│              │  (CReg) │  │  (Facility)│   │  (PostgreSQL)  │   │
│              └─────────┘  └────────────┘   └────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Components

| Service | Image / Build | Purpose |
|---|---|---|
| `shr-postgres` | `postgres:15` | Persistent storage for the HAPI FHIR server |
| `shr-fhir-server` | `hapiproject/hapi:latest` | FHIR R4 clinical data repository |
| `shr-ingestion-engine` | Built from `./ingestion-engine` | Gatekeeper that validates Patient & facility references before persisting bundles |

## Ingestion Workflow

1. An EMR submits a FHIR **Transaction Bundle** to `POST /ingest/bundle`.
2. The Ingestion Engine **extracts** all `Patient` and `Location`/`Organization` references from the clinical resources in the bundle.
3. It **validates** each Patient reference against the **Client Registry (CR)**.
4. It **validates** each facility reference against the **Health Facility Registry (HFR)**.
5. If any reference is missing → the bundle is **rejected** with an HTTP 400 and a FHIR `OperationOutcome` detailing every invalid reference.
6. If all references are valid → the bundle is **forwarded** to the local SHR FHIR server and the transaction response is returned to the EMR.

## Quick Start

### Prerequisites

- Docker & Docker Compose v2+
- The `openfmr_global_net` Docker network (created by `openfmr-core`)
- The **Client Registry** (`openfmr-module-cr`) and **Health Facility Registry** (`openfmr-module-hfr`) modules running on the same network

### 1. Configure Environment

```bash
cp .env.example .env
# Edit .env to set secure passwords and port mappings.
```

### 2. Start the Module

```bash
docker compose up -d
```

### 3. Verify

```bash
# HAPI FHIR server metadata
curl http://localhost:8084/fhir/metadata | jq .

# Ingestion Engine health check
curl http://localhost:8085/health
```

### 4. Submit a Bundle

```bash
curl -X POST http://localhost:8085/ingest/bundle \
  -H "Content-Type: application/fhir+json" \
  -d @sample-bundle.json
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SHR_POSTGRES_DB` | `shr_hapi` | PostgreSQL database name |
| `SHR_POSTGRES_USER` | `shr_admin` | PostgreSQL username |
| `SHR_POSTGRES_PASSWORD` | `shr_secret_password` | PostgreSQL password |
| `SHR_FHIR_PORT` | `8084` | Host port for the FHIR server |
| `SHR_INGESTION_PORT` | `8085` | Host port for the Ingestion Engine |
| `FHIR_REQUEST_TIMEOUT` | `30` | Timeout (seconds) for cross-module FHIR calls |

### HAPI FHIR Tuning

The HAPI FHIR server configuration is in [`config/hapi-application.yaml`](config/hapi-application.yaml). Key tuning parameters:

- **HikariCP pool**: 50 max connections for high-concurrency writes
- **Hibernate batching**: batch size of 50 with ordered inserts/updates
- **Bundle limits**: up to 500 resources per page, 100 MB binary size
- **Validation**: disabled at the FHIR server level (handled upstream by the Ingestion Engine)

## API Reference

### `GET /health`

Liveness probe.

**Response:** `{"status": "healthy", "service": "shr-ingestion-engine"}`

### `POST /ingest/bundle`

Submit a FHIR Transaction Bundle for ingestion.

**Request Body:** FHIR R4 `Bundle` resource with `type: "transaction"`.

**Responses:**

| Code | Description |
|---|---|
| 200 | Bundle accepted — returns the `transaction-response` from the SHR |
| 400 | Invalid bundle or unresolved references (OperationOutcome) |
| 502 | SHR FHIR server error |
| 504 | Timeout communicating with CR, HFR, or SHR |

## License

This module is part of the OpenFMR project.
