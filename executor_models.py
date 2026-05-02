# executor_models.py v2.0
# Görev: execution katmanına ait veri modelleri.
# Planner/review modelleriyle karışmaz.

from enum import Enum
from typing import List, Optional, Any
from pydantic import BaseModel, Field


class ExecutionStatus(str, Enum):
    SUCCESS   = "SUCCESS"
    FAILED    = "FAILED"
    SKIPPED   = "SKIPPED"   # preflight engelledi
    ROLLED_BACK = "ROLLED_BACK"


class RollbackCapability(str, Enum):
    FULL     = "FULL"      # tam geri alınabilir
    PARTIAL  = "PARTIAL"   # snapshot varsa
    NONE     = "NONE"      # geri alınamaz


class StepExecutionResult(BaseModel):
    step_no: int
    action: str                          # ActionType.value
    target: str
    status: ExecutionStatus
    message: str = Field(..., min_length=1)
    rollback_available: bool = False
    rollback_capability: RollbackCapability = RollbackCapability.NONE
    rollback_metadata: Optional[dict] = None   # handler bazlı
    error: Optional[str] = None


class ExecutionResult(BaseModel):
    run_id: str = Field(..., min_length=1)
    status: ExecutionStatus
    total_steps: int
    completed_steps: int
    step_results: List[StepExecutionResult]
    rolled_back: bool = False
    summary: str = Field(..., min_length=1)