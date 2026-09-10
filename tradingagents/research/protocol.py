"""Frozen protocol constants for the QuantLab research-task API."""

from __future__ import annotations

SCHEMA_VERSION = "1"
PROTOCOL_VERSION = "1"
PROMPT_VERSION = "research-v1"
MAX_CANDIDATES = 10
ANALYSIS_CONCURRENCY = 1

RESEARCH_TYPES = ("industry_thesis", "candidate_review")

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

SUPPORT_LEVELS = ("支持", "混合", "反对", "证据不足")
GAP_STATUSES = ("identified", "pending_verification", "none")
NEWS_KINDS = (
    "official_disclosure",
    "media",
    "commentary",
    "rumor",
    "industry_indicator",
    "company_profile",
    "signal",
    "market_expectation",
    "valuation_reference",
    "prior_report",
)
NEWS_IMPACT = ("positive", "negative", "mixed", "unclear")
CYCLE_MATCH = ("match", "mismatch", "unknown")
COMPLETENESS = ("complete", "partial", "missing_industry_context")
ANALYSIS_STATES = ("completed", "failed", "not_analyzed")
VERSION_CHANGES = ("strengthened", "weakened", "overturned", "initial")
CLAIM_KINDS = ("fact", "market_expectation", "inference")
