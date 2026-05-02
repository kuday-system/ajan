# models.py v1.2.4
# Değişiklikler v1.2.3'e göre:
#   - ActionGroup.MUTATING düzeltildi:
#     DELETE_FILE ve COPY_FILE eklendi, CREATE_DIR korundu.
#     rule_engine.py MUTATING_ACTIONS ile tutarsızlık giderildi.
#     Not: rule_engine.py'e dokunulmadı.

from enum import Enum
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


# --- Risk ve Karar Tipleri ---

RiskLevel = Literal["low", "medium", "high", "critical"]
PermissionDecision = Literal["deny", "ask_user", "ask_clarification"]


# --- Action Enum ---

class ActionType(str, Enum):
    READ_FILE            = "READ_FILE"
    WRITE_FILE           = "WRITE_FILE"
    APPEND_FILE          = "APPEND_FILE"
    MOVE_FILE            = "MOVE_FILE"
    COPY_FILE            = "COPY_FILE"
    DELETE_FILE          = "DELETE_FILE"
    CREATE_DIR           = "CREATE_DIR"
    LIST_DIR             = "LIST_DIR"
    INTERNAL_LOG_WRITE   = "INTERNAL_LOG_WRITE"
    INTERNAL_STATE_WRITE = "INTERNAL_STATE_WRITE"
    INTERNAL_STATE_READ  = "INTERNAL_STATE_READ"


# --- Action Grupları ---

class ActionGroup:
    USER_FILE = {
        ActionType.READ_FILE,
        ActionType.WRITE_FILE,
        ActionType.APPEND_FILE,
        ActionType.MOVE_FILE,
        ActionType.COPY_FILE,
        ActionType.DELETE_FILE,
        ActionType.CREATE_DIR,
        ActionType.LIST_DIR,
    }

    INTERNAL = {
        ActionType.INTERNAL_LOG_WRITE,
        ActionType.INTERNAL_STATE_WRITE,
        ActionType.INTERNAL_STATE_READ,
    }

    MUTATING = {
        ActionType.WRITE_FILE,
        ActionType.APPEND_FILE,
        ActionType.DELETE_FILE,
        ActionType.MOVE_FILE,
        ActionType.COPY_FILE,
        ActionType.CREATE_DIR,
    }

    DESTRUCTIVE = {
        ActionType.DELETE_FILE,
        ActionType.MOVE_FILE,
    }


# --- Yardımcı ---

def _strip_list(values: List[str]) -> List[str]:
    return [v.strip() for v in values if v.strip()]


def _require_non_empty(value: str, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} boş olamaz")
    return stripped


# --- Modeller ---

class UserCommand(BaseModel):
    raw_text: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def normalize(self):
        self.raw_text = _require_non_empty(self.raw_text, "raw_text")
        return self


class PlanStep(BaseModel):
    step_no: int = Field(..., ge=1)
    action: ActionType
    target: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    content: Optional[str] = None          # WRITE_FILE / APPEND_FILE için; diğer action'larda None

    @model_validator(mode="after")
    def normalize(self):
        self.target = _require_non_empty(self.target, "target")
        self.reason = _require_non_empty(self.reason, "reason")
        return self


class AgentPlan(BaseModel):
    goal: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    steps: List[PlanStep] = Field(..., min_length=1)
    risk_level: RiskLevel
    risk_notes: List[str] = Field(default_factory=list)
    permission_scope: str = Field(..., min_length=1)
    single_task_ok: bool
    forbidden_request_detected: bool
    requires_real_execution: bool = False
    clarification_needed: bool = False
    clarification_question: Optional[str] = None

    @field_validator("risk_notes", mode="before")
    @classmethod
    def clean_risk_notes(cls, v):
        return _strip_list(v) if v else []

    @model_validator(mode="after")
    def validate_plan(self):
        self.goal = _require_non_empty(self.goal, "goal")
        self.summary = _require_non_empty(self.summary, "summary")
        self.permission_scope = _require_non_empty(self.permission_scope, "permission_scope")

        if self.clarification_needed:
            if not self.clarification_question:
                raise ValueError("clarification_needed=True ise soru zorunlu")
            self.clarification_question = _require_non_empty(
                self.clarification_question, "clarification_question"
            )

        step_nos = [s.step_no for s in self.steps]
        if len(step_nos) != len(set(step_nos)):
            raise ValueError("step_no değerleri benzersiz olmalı")
        if step_nos != sorted(step_nos):
            raise ValueError("step_no değerleri artan sırada olmalı")

        return self


class PlanReview(BaseModel):
    decision: PermissionDecision
    reasons: List[str] = Field(..., min_length=1)

    @field_validator("reasons", mode="before")
    @classmethod
    def clean_reasons(cls, v):
        cleaned = _strip_list(v)
        if not cleaned:
            raise ValueError("reasons boş olamaz")
        return cleaned


class LockedPlan(BaseModel):
    plan_hash: str = Field(..., min_length=1)
    canonical_plan_json: str = Field(..., min_length=1)


class SimulationResult(BaseModel):
    status: Literal["simulated"]
    summary: str = Field(..., min_length=1)
    simulated_outputs: List[str] = Field(..., min_length=1)

    @field_validator("simulated_outputs", mode="before")
    @classmethod
    def clean_outputs(cls, v):
        cleaned = _strip_list(v)
        if not cleaned:
            raise ValueError("simulated_outputs boş olamaz")
        return cleaned

    @model_validator(mode="after")
    def normalize(self):
        self.summary = _require_non_empty(self.summary, "summary")
        return self
