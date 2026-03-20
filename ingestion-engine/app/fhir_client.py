"""
=============================================================================
 OpenFMR — SHR Ingestion Engine: FHIR Client
=============================================================================
 Provides asynchronous helper functions for communicating with FHIR R4
 servers across the OpenFMR network. Each function uses the `httpx` async
 HTTP client with configurable timeouts to gracefully handle network
 issues between Docker containers.

 Functions:
   - post_bundle()            : Forwards a FHIR Transaction Bundle to a server.
   - verify_patient_exists()  : Confirms a Patient resource exists in the CR.
   - verify_facility_exists() : Confirms a Location/Organization exists in the HFR.
=============================================================================
"""

import os
import logging
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Network timeout (in seconds) for all outgoing FHIR requests.
# Adjustable via the FHIR_REQUEST_TIMEOUT environment variable.
FHIR_TIMEOUT = float(os.getenv("FHIR_REQUEST_TIMEOUT", "30"))

# Standard FHIR JSON content type header.
FHIR_HEADERS = {
    "Content-Type": "application/fhir+json",
    "Accept": "application/fhir+json",
}

logger = logging.getLogger("shr.fhir_client")


# ---------------------------------------------------------------------------
# Helper: Build an httpx async client with sensible defaults.
# ---------------------------------------------------------------------------
def _build_client() -> httpx.AsyncClient:
    """
    Construct a reusable async HTTP client with:
      - A generous timeout that covers connect + read phases.
      - Standard FHIR headers applied to every request.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=10.0,        # Max time to establish a TCP connection.
            read=FHIR_TIMEOUT,   # Max time to wait for the response body.
            write=10.0,          # Max time to send the request body.
            pool=5.0,            # Max time to acquire a connection from the pool.
        ),
        headers=FHIR_HEADERS,
    )


# ---------------------------------------------------------------------------
# 1. Post a FHIR Transaction Bundle to a target FHIR server.
# ---------------------------------------------------------------------------
async def post_bundle(fhir_server_url: str, bundle_json: dict[str, Any]) -> dict[str, Any]:
    """
    Forward a complete FHIR Transaction Bundle to the specified FHIR server.

    Args:
        fhir_server_url: The base URL of the FHIR server (e.g. http://shr-fhir-server:8080/fhir).
        bundle_json:     The full FHIR Bundle resource as a Python dict.

    Returns:
        The parsed JSON response from the FHIR server (typically a Bundle of
        type 'transaction-response').

    Raises:
        httpx.TimeoutException:  If the request exceeds the configured timeout.
        httpx.HTTPStatusError:   If the server returns a 4xx/5xx response.
    """
    logger.info("Forwarding transaction bundle to %s", fhir_server_url)

    async with _build_client() as client:
        response = await client.post(
            fhir_server_url,
            json=bundle_json,
        )
        # Raise on 4xx/5xx so the caller can handle it.
        response.raise_for_status()

    logger.info("Bundle accepted by FHIR server (HTTP %d)", response.status_code)
    return response.json()


# ---------------------------------------------------------------------------
# 2. Verify that a Patient exists in the Client Registry (CR).
# ---------------------------------------------------------------------------
async def verify_patient_exists(cr_url: str, patient_reference: str) -> bool:
    """
    Query the Client Registry's FHIR server to confirm that the referenced
    Patient resource exists.

    The `patient_reference` is expected in the standard FHIR reference format:
        "Patient/<id>"

    This function issues a lightweight HTTP GET (read-by-id) against the CR.
    A 200 response means the patient exists; any other status means it does not.

    Args:
        cr_url:             Base FHIR URL of the Client Registry
                            (e.g. http://cr-fhir-server:8080/fhir).
        patient_reference:  FHIR reference string, e.g. "Patient/abc-123".

    Returns:
        True if the Patient exists, False otherwise.
    """
    # Build the full resource URL: <base>/Patient/<id>
    resource_url = f"{cr_url.rstrip('/')}/{patient_reference}"
    logger.info("Verifying patient existence at %s", resource_url)

    try:
        async with _build_client() as client:
            response = await client.get(resource_url)

        if response.status_code == 200:
            logger.info("Patient '%s' verified in Client Registry.", patient_reference)
            return True

        # 404 is the expected "not found" response.
        logger.warning(
            "Patient '%s' not found in Client Registry (HTTP %d).",
            patient_reference,
            response.status_code,
        )
        return False

    except httpx.TimeoutException:
        logger.error(
            "Timeout while verifying patient '%s' against Client Registry at %s.",
            patient_reference,
            cr_url,
        )
        raise
    except httpx.ConnectError:
        logger.error(
            "Could not connect to Client Registry at %s. Is the CR module running?",
            cr_url,
        )
        raise


# ---------------------------------------------------------------------------
# 3. Verify that a Location/Organization exists in the Health Facility
#    Registry (HFR).
# ---------------------------------------------------------------------------
async def verify_facility_exists(hfr_url: str, facility_reference: str) -> bool:
    """
    Query the Health Facility Registry's FHIR server to confirm that the
    referenced Location or Organization resource exists.

    The `facility_reference` can be in either format:
        "Location/<id>"     — physical site
        "Organization/<id>" — managing organisation

    Args:
        hfr_url:              Base FHIR URL of the HFR
                              (e.g. http://hfr-fhir-server:8080/fhir).
        facility_reference:   FHIR reference string, e.g. "Location/facility-001".

    Returns:
        True if the facility exists, False otherwise.
    """
    resource_url = f"{hfr_url.rstrip('/')}/{facility_reference}"
    logger.info("Verifying facility existence at %s", resource_url)

    try:
        async with _build_client() as client:
            response = await client.get(resource_url)

        if response.status_code == 200:
            logger.info("Facility '%s' verified in HFR.", facility_reference)
            return True

        logger.warning(
            "Facility '%s' not found in HFR (HTTP %d).",
            facility_reference,
            response.status_code,
        )
        return False

    except httpx.TimeoutException:
        logger.error(
            "Timeout while verifying facility '%s' against HFR at %s.",
            facility_reference,
            hfr_url,
        )
        raise
    except httpx.ConnectError:
        logger.error(
            "Could not connect to Health Facility Registry at %s. Is the HFR module running?",
            hfr_url,
        )
        raise
