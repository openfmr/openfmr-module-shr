"""
=============================================================================
 OpenFMR — SHR Ingestion Engine: Bundle Validation & Reference Extraction
=============================================================================
 Responsible for parsing incoming FHIR R4 Transaction Bundles and extracting
 the external references that must be validated before the bundle can be
 persisted into the Shared Health Record.

 A typical clinical Transaction Bundle from an EMR contains resources like
 Encounter, Observation, Condition, etc. Each of these references:
   - A Patient (who the clinical data is about)   → validated against the CR
   - A Location or Organization (where it happened) → validated against the HFR

 This module extracts those references so the Ingestion Engine can verify
 them before forwarding the bundle to the SHR FHIR server.
=============================================================================
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("shr.validation")

# ---------------------------------------------------------------------------
# FHIR resource types that are themselves registry entries, NOT clinical data.
# We skip these when scanning for Patient/Facility references because they
# ARE the referenced resources, not consumers of them.
# ---------------------------------------------------------------------------
REGISTRY_RESOURCE_TYPES = frozenset({
    "Patient",
    "Location",
    "Organization",
    "Practitioner",
    "PractitionerRole",
})


# ---------------------------------------------------------------------------
# Data class to hold the extracted references from a bundle.
# ---------------------------------------------------------------------------
@dataclass
class ExtractedReferences:
    """
    Container for all external references extracted from a FHIR Bundle.

    Attributes:
        patient_references:   Set of unique Patient references (e.g. "Patient/abc-123").
        facility_references:  Set of unique Location/Organization references
                              (e.g. "Location/facility-001", "Organization/org-xyz").
    """
    patient_references: set[str] = field(default_factory=set)
    facility_references: set[str] = field(default_factory=set)

    @property
    def has_references(self) -> bool:
        """Return True if at least one reference was extracted."""
        return bool(self.patient_references or self.facility_references)


# ---------------------------------------------------------------------------
# Core extraction logic
# ---------------------------------------------------------------------------
def extract_references(bundle: dict) -> ExtractedReferences:
    """
    Walk through every entry in a FHIR Transaction Bundle and extract
    Patient and Location/Organization references from the clinical resources.

    The function looks for references in the following standard FHIR fields:
      - resource.subject.reference       → typically a Patient reference
      - resource.patient.reference        → alternative Patient reference
      - resource.location[].location.reference → Location references
      - resource.managingOrganization.reference → Organization reference
      - resource.performer[].reference    → may include Organization
      - resource.serviceProvider.reference → Organization that provided service

    Args:
        bundle: A FHIR Bundle resource (Python dict) with type "transaction".

    Returns:
        An ExtractedReferences instance containing all unique Patient and
        facility references found in the bundle.

    Raises:
        ValueError: If the input is not a valid FHIR Bundle of type 'transaction'.
    """
    # -----------------------------------------------------------------------
    # Step 1: Validate the top-level bundle structure.
    # -----------------------------------------------------------------------
    resource_type = bundle.get("resourceType")
    bundle_type = bundle.get("type")

    if resource_type != "Bundle":
        raise ValueError(
            f"Expected resourceType 'Bundle', got '{resource_type}'."
        )
    if bundle_type not in ("transaction", "batch"):
        raise ValueError(
            f"Expected bundle type 'transaction' or 'batch', got '{bundle_type}'."
        )

    entries = bundle.get("entry", [])
    if not entries:
        logger.warning("Received an empty transaction bundle (no entries).")

    refs = ExtractedReferences()

    # -----------------------------------------------------------------------
    # Step 2: Iterate over each entry and extract references.
    # -----------------------------------------------------------------------
    for idx, entry in enumerate(entries):
        resource = entry.get("resource")
        if not resource:
            logger.debug("Entry %d has no 'resource' key — skipping.", idx)
            continue

        resource_type_name = resource.get("resourceType", "Unknown")

        # Skip registry-type resources; we only care about clinical resources
        # that REFERENCE patients and facilities.
        if resource_type_name in REGISTRY_RESOURCE_TYPES:
            logger.debug(
                "Entry %d is a registry resource (%s) — skipping reference extraction.",
                idx,
                resource_type_name,
            )
            continue

        logger.debug("Scanning entry %d (%s) for references.", idx, resource_type_name)

        # --- Patient references -------------------------------------------
        _extract_patient_refs(resource, refs)

        # --- Facility references (Location / Organization) ----------------
        _extract_facility_refs(resource, refs)

    logger.info(
        "Extracted %d patient ref(s) and %d facility ref(s) from bundle with %d entries.",
        len(refs.patient_references),
        len(refs.facility_references),
        len(entries),
    )

    return refs


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------
def _extract_patient_refs(resource: dict, refs: ExtractedReferences) -> None:
    """
    Extract Patient references from common FHIR fields.

    Looks at:
      - resource.subject.reference
      - resource.patient.reference
    """
    # subject.reference (most common — Encounter, Observation, Condition, etc.)
    subject_ref = _get_nested(resource, "subject", "reference")
    if subject_ref and subject_ref.startswith("Patient/"):
        refs.patient_references.add(subject_ref)

    # patient.reference (some resource types like Coverage, Claim, etc.)
    patient_ref = _get_nested(resource, "patient", "reference")
    if patient_ref and patient_ref.startswith("Patient/"):
        refs.patient_references.add(patient_ref)


def _extract_facility_refs(resource: dict, refs: ExtractedReferences) -> None:
    """
    Extract Location and Organization references from common FHIR fields.

    Looks at:
      - resource.location[].location.reference
      - resource.managingOrganization.reference
      - resource.serviceProvider.reference
      - resource.performer[].reference  (when it references an Organization)
    """
    # location[].location.reference (Encounter.location is an array)
    locations = resource.get("location", [])
    if isinstance(locations, list):
        for loc_entry in locations:
            loc_ref = _get_nested(loc_entry, "location", "reference")
            if loc_ref and _is_facility_reference(loc_ref):
                refs.facility_references.add(loc_ref)

    # managingOrganization.reference
    managing_org_ref = _get_nested(resource, "managingOrganization", "reference")
    if managing_org_ref and _is_facility_reference(managing_org_ref):
        refs.facility_references.add(managing_org_ref)

    # serviceProvider.reference (Encounter)
    service_provider_ref = _get_nested(resource, "serviceProvider", "reference")
    if service_provider_ref and _is_facility_reference(service_provider_ref):
        refs.facility_references.add(service_provider_ref)

    # performer[].reference (DiagnosticReport, Observation — may reference Orgs)
    performers = resource.get("performer", [])
    if isinstance(performers, list):
        for performer in performers:
            perf_ref = performer.get("reference") if isinstance(performer, dict) else None
            if perf_ref and _is_facility_reference(perf_ref):
                refs.facility_references.add(perf_ref)


def _is_facility_reference(ref: str) -> bool:
    """Check if a reference string points to a Location or Organization."""
    return ref.startswith("Location/") or ref.startswith("Organization/")


def _get_nested(obj: dict, key1: str, key2: str) -> str | None:
    """
    Safely retrieve a nested value: obj[key1][key2].
    Returns None if any key is missing or the intermediate value is not a dict.
    """
    intermediate = obj.get(key1)
    if isinstance(intermediate, dict):
        return intermediate.get(key2)
    return None
