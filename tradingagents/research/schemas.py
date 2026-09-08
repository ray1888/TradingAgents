"""Pydantic models for research bundles, task requests, and structured reports."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from tradingagents.research.protocol import (
    ANALYSIS_STATES,
    CLAIM_KINDS,
    COMPLETENESS,
    CYCLE_MATCH,
    GAP_STATUSES,
    MAX_CANDIDATES,
    NEWS_KINDS,
    RESEARCH_TYPES,
    SCHEMA_VERSION,
    SUPPORT_LEVELS,
    VERSION_CHANGES,
)


class RunSubmitRequest(BaseModel):
    schema_version: str
    research_type: Literal["industry_thesis", "candidate_review"]
    bundle_id: str
    idempotency_key: str

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {value!r}; expected {SCHEMA_VERSION}")
        return value

    @field_validator("bundle_id", "idempotency_key")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class EvidenceItem(BaseModel):
    evidence_id: str
    kind: str
    source_identity: str
    title: str = ""
    body: str = ""
    published_at: str | None = None
    first_seen_at: str | None = None
    available_at: str | None = None
    content_hash: str | None = None
    revision: int = 1
    url: str | None = None
    vendor_record_id: str | None = None
    tickers: list[str] = Field(default_factory=list)
    is_repost: bool = False
    canonical_evidence_id: str | None = None
    coverage_gap: str | None = None

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in NEWS_KINDS:
            raise ValueError(f"unknown evidence kind {value!r}")
        return value

    @field_validator("evidence_id", "source_identity")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class IndustryMember(BaseModel):
    ticker: str
    name: str | None = None
    role: str | None = None


class IndustryIndicator(BaseModel):
    name: str
    value: str | None = None
    unit: str | None = None
    period: str | None = None
    published_at: str | None = None
    available_at: str | None = None
    source_identity: str
    evidence_id: str | None = None


class CompanyProfile(BaseModel):
    ticker: str
    name: str | None = None
    summary: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class QuantSignal(BaseModel):
    ticker: str
    rank: int
    strategy_name: str
    signal_reasons: list[str] = Field(default_factory=list)
    holding_horizon: str | None = None
    selection_run_id: str | None = None
    quant_metrics: dict[str, Any] = Field(default_factory=dict)


class BundleVersions(BaseModel):
    snapshot_id: str
    financial_manifest_id: str
    industry_members_version: str | None = None
    evidence_version: str | None = None
    prior_report_id: str | None = None


class BundleScope(BaseModel):
    theme: str | None = None
    tickers: list[str] = Field(default_factory=list)
    horizon: str | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    industry_archive_present: bool = False


class ResearchBundle(BaseModel):
    schema_version: str
    bundle_id: str
    research_type: Literal["industry_thesis", "candidate_review"]
    as_of_time: str
    as_of_date: str
    versions: BundleVersions
    scope: BundleScope = Field(default_factory=BundleScope)
    industry_members: list[IndustryMember] = Field(default_factory=list)
    industry_indicators: list[IndustryIndicator] = Field(default_factory=list)
    company_profiles: list[CompanyProfile] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    signals: list[QuantSignal] = Field(default_factory=list)
    prior_report: dict[str, Any] | None = None
    market_expectation_materials: list[EvidenceItem] = Field(default_factory=list)
    coverage_notes: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported bundle schema_version {value!r}")
        return value

    @model_validator(mode="after")
    def _unique_evidence_ids(self) -> ResearchBundle:
        seen: set[str] = set()
        for item in self.evidence + self.market_expectation_materials:
            if item.evidence_id in seen:
                raise ValueError(f"duplicate evidence_id {item.evidence_id!r}")
            seen.add(item.evidence_id)
        return self

    def evidence_ids(self) -> set[str]:
        ids = {item.evidence_id for item in self.evidence}
        ids.update(item.evidence_id for item in self.market_expectation_materials)
        for indicator in self.industry_indicators:
            if indicator.evidence_id:
                ids.add(indicator.evidence_id)
        return ids


class CitedClaim(BaseModel):
    statement: str
    kind: Literal["fact", "market_expectation", "inference"] = "inference"
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in CLAIM_KINDS:
            raise ValueError(f"unknown claim kind {value!r}")
        return value


class RelatedCompany(BaseModel):
    ticker: str
    name: str | None = None
    transmission: str
    benefit_conditions: str
    risks: str
    evidence_ids: list[str] = Field(default_factory=list)


class VersionChange(BaseModel):
    judgment: str
    change: Literal["strengthened", "weakened", "overturned", "initial"]
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("change")
    @classmethod
    def _change(cls, value: str) -> str:
        if value not in VERSION_CHANGES:
            raise ValueError(f"unknown version change {value!r}")
        return value


class NewsEvent(BaseModel):
    evidence_id: str
    event: str
    entities: list[str] = Field(default_factory=list)
    impact_direction: Literal["positive", "negative", "mixed", "unclear"] = "unclear"
    time_span: str | None = None
    source_identity: str
    source_class: Literal["official_disclosure", "media", "commentary", "rumor", "other"]
    is_repost: bool = False
    canonical_evidence_id: str | None = None
    linked_ticker: str | None = None
    linked_hypothesis: str | None = None


class NewsAnalysis(BaseModel):
    events: list[NewsEvent] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)
    ignored_instruction_evidence_ids: list[str] = Field(default_factory=list)


class IndustryThesisBody(BaseModel):
    conclusion: str
    hypotheses: list[CitedClaim] = Field(default_factory=list)
    facts: list[CitedClaim] = Field(default_factory=list)
    market_expectations: list[CitedClaim] = Field(default_factory=list)
    inferences: list[CitedClaim] = Field(default_factory=list)
    expectation_gap_status: Literal["identified", "pending_verification", "none"]
    expectation_gap: str | None = None
    catalysts: list[CitedClaim] = Field(default_factory=list)
    tracking_indicators: list[str] = Field(default_factory=list)
    falsifiers: list[CitedClaim] = Field(default_factory=list)
    related_companies: list[RelatedCompany] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    opposing_evidence_ids: list[str] = Field(default_factory=list)
    missing_materials: list[str] = Field(default_factory=list)
    applicability: str
    observation_horizon: str | None = None
    version_changes: list[VersionChange] = Field(default_factory=list)

    @field_validator("expectation_gap_status")
    @classmethod
    def _gap(cls, value: str) -> str:
        if value not in GAP_STATUSES:
            raise ValueError(f"unknown expectation_gap_status {value!r}")
        return value


class CandidateReviewItem(BaseModel):
    ticker: str
    analysis_state: Literal["completed", "failed", "not_analyzed"] = "completed"
    error: str | None = None
    quant_signal: QuantSignal | None = None
    research_support: Literal["支持", "混合", "反对", "证据不足"] | None = None
    rationale: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    event_impacts: list[NewsEvent] = Field(default_factory=list)
    risks: list[CitedClaim] = Field(default_factory=list)
    cycle_match: Literal["match", "mismatch", "unknown"] | None = None
    cycle_match_rationale: str | None = None
    data_completeness: Literal["complete", "partial", "missing_industry_context"] | None = None
    missing_materials: list[str] = Field(default_factory=list)
    applicability: str | None = None
    industry_background_missing: bool = False

    @field_validator("research_support")
    @classmethod
    def _support(cls, value: str | None) -> str | None:
        if value is not None and value not in SUPPORT_LEVELS:
            raise ValueError(f"unknown research_support {value!r}")
        return value

    @field_validator("cycle_match")
    @classmethod
    def _cycle(cls, value: str | None) -> str | None:
        if value is not None and value not in CYCLE_MATCH:
            raise ValueError(f"unknown cycle_match {value!r}")
        return value

    @field_validator("data_completeness")
    @classmethod
    def _complete(cls, value: str | None) -> str | None:
        if value is not None and value not in COMPLETENESS:
            raise ValueError(f"unknown data_completeness {value!r}")
        return value

    @field_validator("analysis_state")
    @classmethod
    def _state(cls, value: str) -> str:
        if value not in ANALYSIS_STATES:
            raise ValueError(f"unknown analysis_state {value!r}")
        return value


class CandidateReviewBody(BaseModel):
    items: list[CandidateReviewItem] = Field(default_factory=list)
    missing_materials: list[str] = Field(default_factory=list)
    applicability: str = ""

    @model_validator(mode="after")
    def _cap(self) -> CandidateReviewBody:
        analyzed = [item for item in self.items if item.analysis_state != "not_analyzed"]
        if len(analyzed) > MAX_CANDIDATES:
            raise ValueError(f"candidate_review analyzes at most {MAX_CANDIDATES} names")
        return self


class ReportProvenance(BaseModel):
    model: str
    prompt_version: str
    code_version: str
    generated_at: str
    llm_provider: str | None = None


class StructuredReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    report_id: str
    run_id: str
    research_type: Literal["industry_thesis", "candidate_review"]
    bundle_id: str
    as_of_time: str
    as_of_date: str
    versions: BundleVersions
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    news: NewsAnalysis = Field(default_factory=NewsAnalysis)
    industry_thesis: IndustryThesisBody | None = None
    candidate_review: CandidateReviewBody | None = None
    conclusion: str
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    opposing_evidence_ids: list[str] = Field(default_factory=list)
    missing_materials: list[str] = Field(default_factory=list)
    applicability: str
    status: Literal["completed", "partial", "failed", "incomplete"] = "completed"
    validation_errors: list[str] = Field(default_factory=list)
    provenance: ReportProvenance
    readable_markdown: str = ""

    @field_validator("research_type")
    @classmethod
    def _type(cls, value: str) -> str:
        if value not in RESEARCH_TYPES:
            raise ValueError(f"unknown research_type {value!r}")
        return value
