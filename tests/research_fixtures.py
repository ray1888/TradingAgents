"""Shared frozen-bundle loaders for research contract tests."""

from __future__ import annotations

import json
from pathlib import Path

from tradingagents.research.schemas import ResearchBundle
from tradingagents.research.synthesizer import CallableSynthesizer

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "research" / "v1"


def load_contract(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def hog_bundle() -> ResearchBundle:
    return ResearchBundle.model_validate(load_contract("bundle-industry-hog.json"))


def candidate_bundle() -> ResearchBundle:
    return ResearchBundle.model_validate(load_contract("bundle-candidate-review.json"))


def hog_synthesizer():
    def _fn(prompt: str, schema):
        return {
            "conclusion": "产能仍高，价格回升尚不足以确认反转。",
            "hypotheses": [
                {
                    "statement": "若能繁母猪持续去化，未来 3-12 个月供给压力减轻",
                    "kind": "inference",
                    "evidence_ids": ["ev-ind-1"],
                }
            ],
            "facts": [
                {
                    "statement": "农业农村部公布能繁母猪存栏",
                    "kind": "fact",
                    "evidence_ids": ["ev-ind-1"],
                }
            ],
            "market_expectations": [],
            "inferences": [
                {
                    "statement": "价格季节性回升可能只是短期波动",
                    "kind": "inference",
                    "evidence_ids": ["ev-media-1"],
                }
            ],
            "expectation_gap_status": "identified",
            "expectation_gap": "模型不应在无共识材料时声称预期差成立",
            "catalysts": [
                {
                    "statement": "能繁母猪去化加速",
                    "kind": "inference",
                    "evidence_ids": ["ev-ind-1"],
                }
            ],
            "tracking_indicators": ["能繁母猪存栏", "出栏均重", "饲料成本"],
            "falsifiers": [
                {
                    "statement": "能繁母猪存栏重新回升",
                    "kind": "inference",
                    "evidence_ids": ["ev-ind-1"],
                }
            ],
            "related_companies": [
                {
                    "ticker": "002714.SZ",
                    "name": "牧原股份",
                    "transmission": "成本与出栏量传导猪价",
                    "benefit_conditions": "成本低于行业且产能去化",
                    "risks": "产能扩张对冲猪价",
                    "evidence_ids": ["ev-ann-1"],
                }
            ],
            "supporting_evidence_ids": ["ev-ind-1", "ev-ann-1"],
            "opposing_evidence_ids": ["ev-media-1"],
            "missing_materials": [],
            "applicability": "观察周期 3-12 个月，不替代策略持有规则。",
            "observation_horizon": "3-12 months",
            "version_changes": [],
        }

    return CallableSynthesizer(_fn)


def candidate_synthesizer(*, fail_ticker: str | None = None):
    def _fn(prompt: str, schema):
        items = [
            {
                "ticker": "002714.SZ",
                "analysis_state": "completed",
                "research_support": "支持",
                "rationale": "公告与信号方向一致",
                "evidence_ids": ["ev-c-ann"],
                "event_impacts": [],
                "risks": [],
                "cycle_match": "mismatch",
                "cycle_match_rationale": "行业 3-12 个月逻辑长于 5-20 日策略",
                "data_completeness": "partial",
                "missing_materials": [],
                "applicability": "不改变原排名",
            },
            {
                "ticker": "600519.SS",
                "analysis_state": "completed",
                "research_support": "反对",
                "rationale": "批价走弱与超跌反转假设冲突",
                "evidence_ids": ["ev-c-news"],
                "event_impacts": [],
                "risks": [
                    {
                        "statement": "批价下行",
                        "kind": "fact",
                        "evidence_ids": ["ev-c-news"],
                    }
                ],
                "cycle_match": "unknown",
                "cycle_match_rationale": "无行业档案",
                "data_completeness": "missing_industry_context",
                "missing_materials": ["行业档案"],
                "applicability": "不改变原排名",
            },
            {
                "ticker": "000858.SZ",
                "analysis_state": "completed",
                "research_support": "证据不足",
                "rationale": "诉讼结果不明",
                "evidence_ids": ["ev-c-risk"],
                "event_impacts": [],
                "risks": [
                    {
                        "statement": "重大诉讼",
                        "kind": "fact",
                        "evidence_ids": ["ev-c-risk"],
                    }
                ],
                "cycle_match": "unknown",
                "cycle_match_rationale": "无行业档案",
                "data_completeness": "partial",
                "missing_materials": [],
                "applicability": "不改变原排名",
            },
        ]
        if fail_ticker:
            items = [
                item
                if item["ticker"] != fail_ticker
                else {**item, "analysis_state": "failed", "error": "llm timeout"}
                for item in items
            ]
        return {"items": items, "missing_materials": [], "applicability": "旁侧研究"}

    return CallableSynthesizer(_fn)
