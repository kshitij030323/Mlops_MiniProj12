"""Published data contracts (D1/F1). JSON Schemas are exported to contracts/ by scripts/export_contracts.py."""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

CONTRACT_VERSION = "1.0.0"


class Channel(str, Enum):
    web = "web"
    email = "email"
    app = "app"
    phone = "phone"


class EntityTypeClaimed(str, Enum):
    broker = "broker"
    mutual_fund = "mutual_fund"
    registrar = "registrar"
    listed_company = "listed_company"
    depository = "depository"
    depository_participant = "depository_participant"
    bank = "bank"
    nbfc = "nbfc"
    insurance = "insurance"
    other = "other"
    unknown = "unknown"


class Regulator(str, Enum):
    SEBI = "SEBI"
    RBI = "RBI"
    IRDAI = "IRDAI"
    PFRDA = "PFRDA"
    unknown = "unknown"


class Complainant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", max_length=200)
    pan: Optional[str] = Field(default=None, pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$", description="Synthetic PAN only")
    state: str = Field(min_length=2, max_length=50)


class RunOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt_variant: Optional[Literal["v1", "v2"]] = None
    use_cache: bool = True
    demo_fault: Optional[Literal["unsupported_citation"]] = Field(
        default=None, description="Only honoured when DEMO_FAULTS_ENABLED=true; used to demonstrate self-verification")


class ComplaintRequest(BaseModel):
    """Inbound investor complaint. Synthetic data only."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_reference: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,40}$")
    complainant: Complainant
    channel: Channel
    complaint_text: str = Field(min_length=30, max_length=5000)
    entity_name: Optional[str] = Field(default=None, max_length=200)
    entity_type_claimed: EntityTypeClaimed = EntityTypeClaimed.unknown
    product: Optional[str] = Field(default=None, max_length=100)
    amount_inr: Optional[float] = Field(default=None, ge=0, le=1e10)
    incident_date: dt.date
    complained_to_entity_on: Optional[dt.date] = None
    entity_response_received: bool = False
    entity_response_date: Optional[dt.date] = None
    regulator_addressed: Regulator = Regulator.SEBI
    callback_url: Optional[HttpUrl] = None
    options: RunOptions = Field(default_factory=RunOptions)

    @field_validator("incident_date")
    @classmethod
    def _not_future(cls, v: dt.date) -> dt.date:
        if v > dt.date.today():
            raise ValueError("incident_date cannot be in the future")
        if v < dt.date(1990, 1, 1):
            raise ValueError("incident_date before 1990-01-01 is out of range")
        return v

    @model_validator(mode="after")
    def _cross_field(self):
        today = dt.date.today()
        if self.complained_to_entity_on:
            if self.complained_to_entity_on < self.incident_date:
                raise ValueError("complained_to_entity_on must be on or after incident_date")
            if self.complained_to_entity_on > today:
                raise ValueError("complained_to_entity_on cannot be in the future")
        if self.entity_response_received and not self.entity_response_date:
            raise ValueError("entity_response_date is required when entity_response_received is true")
        if self.entity_response_date:
            if not self.complained_to_entity_on:
                raise ValueError("entity_response_date requires complained_to_entity_on")
            if self.entity_response_date < self.complained_to_entity_on:
                raise ValueError("entity_response_date must be on or after complained_to_entity_on")
            if self.entity_response_date > today:
                raise ValueError("entity_response_date cannot be in the future")
        if not self.complaint_text.strip() or len(set(self.complaint_text.split())) < 5:
            raise ValueError("complaint_text must contain at least 5 distinct words")
        return self


class Citation(BaseModel):
    clause_id: str
    doc_id: str = ""
    regulator: str = ""
    reference: str = ""
    passage_id: str = ""
    quote: str


Route = Literal["assign_to_entity", "refer_to_other_regulator", "not_maintainable", "escalate_to_human"]


class Determination(BaseModel):
    route: Route
    domain: str
    regulator: Optional[str] = None
    responsible_entity: Optional[str] = None
    entity_type: Optional[str] = None
    timeline: Optional[str] = None
    reason: Optional[str] = None
    provisional_route: Optional[str] = Field(default=None, description="Route the agents would have given; NOT issued")
    basis: str
    citations: list[Citation] = Field(default_factory=list)


class Escalation(BaseModel):
    required: bool
    reason: Optional[str] = None
    path: Optional[str] = None


class RunAccepted(BaseModel):
    run_id: str
    status: str
    status_url: str
    correlation_id: str
    cached: bool = False


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    satisfactory: bool
    reason: str = Field(min_length=3, max_length=1000)
    reviewer: Optional[str] = Field(default=None, max_length=100)


class RunResult(BaseModel):
    """Outbound contract for GET /runs/{run_id}."""
    run_id: str
    status: Literal["queued", "running", "completed", "escalated", "degraded", "failed"]
    correlation_id: str
    determination: Optional[Determination] = None
    escalation: Optional[Escalation] = None
    verification: Optional[dict] = None
    prompt_versions: dict = Field(default_factory=dict)
    usage: dict = Field(default_factory=dict)
    timings: dict = Field(default_factory=dict)
    disclaimer: str = ("Routing recommendation only. This system does not dispose of complaints; "
                       "a grievance officer decides.")
    contract_version: str = CONTRACT_VERSION
