"""
=============================================================================
 OpenFMR — SHR Ingestion Engine: FastAPI Application
=============================================================================
 The Ingestion Engine is the gatekeeper for the Shared Health Record (SHR).
 It exposes a single POST endpoint that external EMR systems call to submit
 FHIR Transaction Bundles containing clinical data.

 Workflow:
   1. Receive a FHIR Transaction Bundle (JSON) via POST /ingest/bundle.
   2. Parse the bundle and extract Patient and facility references.
   3. Validate that every referenced Patient exists in the Client Registry.
   4. Validate that every referenced Location/Organisation exists in the HFR.
   5. If any reference is invalid → reject with HTTP 400 + OperationOutcome.
   6. If all references are valid → forward the bundle to the SHR FHIR server
      and return the FHIR server's response to the caller.

 This design ensures that no clinical data enters the SHR unless the patient
 and facility are already registered in their respective registries.
=============================================================================
"""

import os
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.validation import extract_references, ExtractedReferences
from app.fhir_client import post_bundle, verify_patient_exists, verify_facility_exists

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("shr.ingestion")

# ---------------------------------------------------------------------------
# Environment variables (injected by Docker Compose)
# ---------------------------------------------------------------------------
SHR_FHIR_URL = os.getenv("SHR_FHIR_URL", "http://shr-fhir-server:8080/fhir")
CR_FHIR_URL = os.getenv("CR_FHIR_URL", "http://cr-fhir-server:8080/fhir")
HFR_FHIR_URL = os.getenv("HFR_FHIR_URL", "http://hfr-fhir-server:8080/fhir")


# ---------------------------------------------------------------------------
# Application lifespan (startup / shutdown logging)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Log configuration on startup and perform cleanup on shutdown."""
    logger.info("=" * 70)
    logger.info("  SHR Ingestion Engine — Starting Up")
    logger.info("  SHR FHIR Server : %s", SHR_FHIR_URL)
    logger.info("  Client Registry : %s", CR_FHIR_URL)
    logger.info("  Facility Registry: %s", HFR_FHIR_URL)
    logger.info("=" * 70)
    yield
    logger.info("SHR Ingestion Engine — Shutting down.")


# ---------------------------------------------------------------------------
# FastAPI application instance
# ---------------------------------------------------------------------------
app = FastAPI(
    title="OpenFMR — SHR Ingestion Engine",
    description=(
        "Validates incoming FHIR Transaction Bundles by cross-referencing "
        "Patient and facility identifiers against the Client Registry (CR) "
        "and Health Facility Registry (HFR), then persists valid bundles "
        "into the Shared Health Record (SHR) FHIR server."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Health check endpoint
# ---------------------------------------------------------------------------
@app.get("/health", tags=["System"])
async def health_check():
    """Simple liveness probe for container orchestration."""
    return {"status": "healthy", "service": "shr-ingestion-engine"}


# ---------------------------------------------------------------------------
# Main ingestion endpoint
# ---------------------------------------------------------------------------
@app.post("/ingest/bundle", tags=["Ingestion"], status_code=status.HTTP_200_OK)
async def ingest_bundle(request: Request):
    """
    Receive a FHIR Transaction Bundle, validate all external references,
    and forward the bundle to the SHR FHIR server if valid.

    **Request Body:** A FHIR R4 Bundle resource of type "transaction".

    **Success Response (200):** The transaction-response Bundle from the SHR.

    **Error Responses:**
      - 400: Invalid bundle structure or unresolved external references.
      - 502: The SHR FHIR server returned an error.
      - 504: Timeout communicating with CR, HFR, or SHR.
    """

    # -----------------------------------------------------------------------
    # Step 1: Parse the incoming JSON body.
    # -----------------------------------------------------------------------
    try:
        bundle_json: dict[str, Any] = await request.json()
    except Exception as exc:
        logger.error("Failed to parse request body as JSON: %s", exc)
        return _operation_outcome(
            status_code=status.HTTP_400_BAD_REQUEST,
            severity="error",
            code="structure",
            diagnostics="Request body is not valid JSON.",
        )

    logger.info(
        "Received bundle: resourceType=%s, type=%s, entries=%d",
        bundle_json.get("resourceType", "?"),
        bundle_json.get("type", "?"),
        len(bundle_json.get("entry", [])),
    )

    # -----------------------------------------------------------------------
    # Step 2: Extract Patient and facility references from the bundle.
    # -----------------------------------------------------------------------
    try:
        refs: ExtractedReferences = extract_references(bundle_json)
    except ValueError as exc:
        logger.warning("Bundle validation failed: %s", exc)
        return _operation_outcome(
            status_code=status.HTTP_400_BAD_REQUEST,
            severity="error",
            code="structure",
            diagnostics=str(exc),
        )

    logger.info(
        "References extracted — Patients: %s | Facilities: %s",
        refs.patient_references or "(none)",
        refs.facility_references or "(none)",
    )

    # -----------------------------------------------------------------------
    # Step 3 & 4: Validate all references concurrently against CR and HFR.
    # -----------------------------------------------------------------------
    validation_errors: list[str] = []

    try:
        validation_errors = await _validate_references(refs)
    except httpx.TimeoutException:
        logger.error("Timeout during reference validation against CR/HFR.")
        return _operation_outcome(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            severity="error",
            code="timeout",
            diagnostics=(
                "Timed out while validating references against the Client Registry "
                "or Health Facility Registry. Please try again later."
            ),
        )
    except httpx.ConnectError as exc:
        logger.error("Connection error during reference validation: %s", exc)
        return _operation_outcome(
            status_code=status.HTTP_502_BAD_GATEWAY,
            severity="error",
            code="transient",
            diagnostics=(
                "Unable to connect to the Client Registry or Health Facility "
                "Registry. Ensure all OpenFMR modules are running."
            ),
        )

    # -----------------------------------------------------------------------
    # Step 5: If any references are invalid, reject with OperationOutcome.
    # -----------------------------------------------------------------------
    if validation_errors:
        logger.warning(
            "Bundle rejected — %d invalid reference(s): %s",
            len(validation_errors),
            validation_errors,
        )
        return _operation_outcome_multi(
            status_code=status.HTTP_400_BAD_REQUEST,
            severity="error",
            code="not-found",
            diagnostics_list=validation_errors,
        )

    # -----------------------------------------------------------------------
    # Step 6: All references valid — forward the bundle to the SHR.
    # -----------------------------------------------------------------------
    logger.info("All references validated. Forwarding bundle to SHR FHIR server.")

    try:
        shr_response = await post_bundle(SHR_FHIR_URL, bundle_json)
        logger.info("Bundle successfully persisted in the SHR.")
        return JSONResponse(content=shr_response, status_code=status.HTTP_200_OK)

    except httpx.HTTPStatusError as exc:
        # The SHR FHIR server rejected the bundle (4xx/5xx).
        logger.error(
            "SHR FHIR server returned HTTP %d: %s",
            exc.response.status_code,
            exc.response.text[:500],
        )
        return _operation_outcome(
            status_code=status.HTTP_502_BAD_GATEWAY,
            severity="error",
            code="exception",
            diagnostics=(
                f"The SHR FHIR server rejected the bundle with HTTP "
                f"{exc.response.status_code}. Details: {exc.response.text[:300]}"
            ),
        )
    except httpx.TimeoutException:
        logger.error("Timeout forwarding bundle to SHR FHIR server.")
        return _operation_outcome(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            severity="error",
            code="timeout",
            diagnostics=(
                "Timed out while forwarding the bundle to the SHR FHIR server. "
                "The clinical data has NOT been saved. Please retry."
            ),
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------
async def _validate_references(refs: ExtractedReferences) -> list[str]:
    """
    Concurrently validate all Patient and facility references.

    Uses asyncio.gather to fire all verification requests in parallel,
    minimising the total latency when multiple references need checking.

    Returns:
        A list of human-readable error strings for every reference that
        could not be found. An empty list means everything is valid.
    """
    errors: list[str] = []
    tasks = []

    # Build verification tasks for patients.
    for patient_ref in refs.patient_references:
        tasks.append(("patient", patient_ref, verify_patient_exists(CR_FHIR_URL, patient_ref)))

    # Build verification tasks for facilities.
    for facility_ref in refs.facility_references:
        tasks.append(("facility", facility_ref, verify_facility_exists(HFR_FHIR_URL, facility_ref)))

    if not tasks:
        # No external references to validate — the bundle is fine as-is.
        return errors

    # Fire all verification requests concurrently.
    results = await asyncio.gather(
        *(task[2] for task in tasks),
        return_exceptions=True,
    )

    # Check each result.
    for (ref_type, ref_value, _), result in zip(tasks, results):
        # Re-raise connection/timeout exceptions so the caller can handle them.
        if isinstance(result, (httpx.TimeoutException, httpx.ConnectError)):
            raise result

        if isinstance(result, Exception):
            logger.error("Unexpected error verifying %s '%s': %s", ref_type, ref_value, result)
            errors.append(
                f"Unexpected error while verifying {ref_type} '{ref_value}': {result}"
            )
        elif result is False:
            if ref_type == "patient":
                errors.append(
                    f"Patient reference '{ref_value}' does not exist in the "
                    f"Client Registry. The EMR must register the patient before "
                    f"submitting clinical data."
                )
            else:
                errors.append(
                    f"Facility reference '{ref_value}' does not exist in the "
                    f"Health Facility Registry. Please verify the facility ID."
                )

    return errors


# ---------------------------------------------------------------------------
# FHIR OperationOutcome builders
# ---------------------------------------------------------------------------
def _operation_outcome(
    status_code: int,
    severity: str,
    code: str,
    diagnostics: str,
) -> JSONResponse:
    """
    Build a FHIR R4 OperationOutcome response with a single issue.

    This is the standard FHIR way to communicate errors back to the caller.
    """
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": severity,
                "code": code,
                "diagnostics": diagnostics,
            }
        ],
    }
    return JSONResponse(content=outcome, status_code=status_code)


def _operation_outcome_multi(
    status_code: int,
    severity: str,
    code: str,
    diagnostics_list: list[str],
) -> JSONResponse:
    """
    Build a FHIR R4 OperationOutcome with multiple issues — one per
    invalid reference. This gives the EMR a complete picture of all
    missing references in a single response.
    """
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": severity,
                "code": code,
                "diagnostics": diag,
            }
            for diag in diagnostics_list
        ],
    }
    return JSONResponse(content=outcome, status_code=status_code)
