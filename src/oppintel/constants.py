"""Canonical constants for the platform.

NOT_VERIFIED is the single string used whenever a fact cannot be substantiated by a
public source. It is applied at render time, never stored in the database: in the
database an unverified field is NULL, which keeps query behaviour clean and prevents a
sentinel value from being mistaken for real data.
"""

NOT_VERIFIED = "Not verified."

# The 20 fields required for every project record. Used for validation and reporting so
# that a missing field is a deliberate, visible state rather than an oversight.
REQUIRED_PROJECT_FIELDS = (
    "project_name",
    "address",
    "city",
    "state",
    "project_type",
    "estimated_project_value",
    "square_footage",
    "permit_number",
    "permit_date",
    "project_status",
    "owner",
    "developer",
    "general_contractor",
    "architect",
    "mechanical_hvac_evidence",
    "source_name",
    "source_url",
    "source_date",
    "last_verified",
)

# Fields whose absence is normal for free public permit data. Reported separately in the
# dashboard so that a sparse-but-honest record is distinguishable from a broken one.
SPARSE_BY_NATURE = ("developer", "architect")

# Fields that should always be substantiated, because a permit feed cannot exist without
# them. A NULL here indicates an ingestion defect rather than a sparse source.
ALWAYS_EXPECTED = ("address", "city", "state", "permit_number", "source_name", "source_url")

# Classification labels.
HIGH = "HIGH"
MEDIUM = "MEDIUM"
NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
CLASSIFICATIONS = (HIGH, MEDIUM, NEEDS_VERIFICATION)

# Evidence strength tiers for mechanical/HVAC scope.
EVIDENCE_TIER_MECHANICAL_PERMIT = 1
EVIDENCE_TIER_MECHANICAL_SCOPE = 2
EVIDENCE_TIER_TRADE_IMPLICATION = 3

TIER_LABELS = {
    EVIDENCE_TIER_MECHANICAL_PERMIT: "Confirmed mechanical permit",
    EVIDENCE_TIER_MECHANICAL_SCOPE: "Mechanical scope stated in permit text",
    EVIDENCE_TIER_TRADE_IMPLICATION: "Trade implication from project class",
}

# Party roles.
ROLE_OWNER = "owner"
ROLE_DEVELOPER = "developer"
ROLE_GENERAL_CONTRACTOR = "general_contractor"
ROLE_ARCHITECT = "architect"
ROLE_MECHANICAL_CONTRACTOR = "mechanical_contractor"