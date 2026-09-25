"""Lifecycle orchestration foundations.

Cross-cutting types and utilities used by every lifecycle subsystem
(intake, comps, judgment, underwriting, memo, CRM) and the orchestrator.

See docs/superpowers/specs/2026-05-05-deal-lifecycle-design.md.
"""

from plat_agent.lifecycle.atomic import atomic_write_json, atomic_write_text
from plat_agent.lifecycle.cache import compute_input_hash, is_satisfied, write_provenance
from plat_agent.lifecycle.complete_marker import (
    CompleteMarker,
    read_complete_marker,
    write_complete_marker,
)
from plat_agent.lifecycle.crm import (
    CRM_FILENAME,
    CRMRow,
    CRMStep,
    atomic_append,
    book_deal,
    crm_path,
    get_latest,
    list_deals,
)
from plat_agent.lifecycle.defaults import (
    CONTRACT_VERSION,
    DEFAULT_LEVERAGE_V1,
    DEFAULT_TTL_DAYS,
    JUDGMENT_ENGINE_V1,
    LEVERAGE_SOURCE_V1_HARDCODED,
    VALIDATION_THRESHOLDS,
    derive_recommendation,
)
from plat_agent.lifecycle.judgment import (
    DeltaFlag,
    DeltaSeverity,
    DeterministicJudgmentEngine,
    JudgmentEngine,
    JudgmentResult,
    JudgmentStep,
    PositioningClass,
    PositioningValue,
    ValidatedField,
    build_engine_inputs,
    render_thesis,
)
from plat_agent.lifecycle.memo import (
    BrokerClaim,
    MemoContent,
    MemoStep,
    PDFRenderResult,
    assemble_memo_content,
    render_broker_claims,
    render_memo_markdown,
    render_returns,
    render_risks,
    write_memo_markdown,
    write_memo_pdf,
)
from plat_agent.lifecycle.protocol import LifecycleStep, StepResult, StepStatus
from plat_agent.lifecycle.punchlist import (
    PunchlistParseError,
    read_punchlist_json,
    reconcile_and_regenerate_punchlist,
    reconcile_punchlist,
    render_punchlist_markdown,
    write_punchlist_json,
    write_punchlist_markdown,
)
from plat_agent.lifecycle.state import (
    BlockerItem,
    LifecycleState,
    LifecycleStatus,
    RecommendationEnum,
    StepName,
)

__all__ = [
    # atomic.py
    "atomic_write_json",
    "atomic_write_text",
    # cache.py
    "compute_input_hash",
    "is_satisfied",
    "write_provenance",
    # complete_marker.py
    "CompleteMarker",
    "read_complete_marker",
    "write_complete_marker",
    # crm.py
    "CRM_FILENAME",
    "CRMRow",
    "CRMStep",
    "atomic_append",
    "book_deal",
    "crm_path",
    "get_latest",
    "list_deals",
    # defaults.py
    "CONTRACT_VERSION",
    "DEFAULT_LEVERAGE_V1",
    "DEFAULT_TTL_DAYS",
    "JUDGMENT_ENGINE_V1",
    "LEVERAGE_SOURCE_V1_HARDCODED",
    "VALIDATION_THRESHOLDS",
    "derive_recommendation",
    # judgment.py
    "DeltaFlag",
    "DeltaSeverity",
    "DeterministicJudgmentEngine",
    "JudgmentEngine",
    "JudgmentResult",
    "JudgmentStep",
    "PositioningClass",
    "PositioningValue",
    "ValidatedField",
    "build_engine_inputs",
    "render_thesis",
    # memo.py
    "BrokerClaim",
    "MemoContent",
    "MemoStep",
    "PDFRenderResult",
    "assemble_memo_content",
    "render_broker_claims",
    "render_memo_markdown",
    "render_returns",
    "render_risks",
    "write_memo_markdown",
    "write_memo_pdf",
    # protocol.py
    "LifecycleStep",
    "StepResult",
    "StepStatus",
    # punchlist.py
    "PunchlistParseError",
    "read_punchlist_json",
    "reconcile_and_regenerate_punchlist",
    "reconcile_punchlist",
    "render_punchlist_markdown",
    "write_punchlist_json",
    "write_punchlist_markdown",
    # state.py
    "BlockerItem",
    "LifecycleState",
    "LifecycleStatus",
    "RecommendationEnum",
    "StepName",
]
