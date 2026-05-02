# planner_output_sanitizer.py
# Değişiklikler:
#   FIX — _sanitize_step içinde content=step.content eklendi.
#          Daha önce content alanı taşınmıyordu — WRITE_FILE / APPEND_FILE kırılıyordu.

import re
from models import AgentPlan, PlanStep


def _looks_like_path(text: str) -> bool:
    t = text.lower()
    return any(x in t for x in [":\\", ":/", "\\", "/", "%userprofile%", "[username]"])


def _sanitize_path_string(raw: str) -> str:
    text = raw.strip().strip('"\'')
    text = text.replace("\t", "\\t")
    text = text.replace("\n", "\\n")
    text = text.replace("\r", "\\r")

    # Tüm diğer kontrol karakterlerini kaldır (0x00–0x1F)
    text = re.sub(r'[\x00-\x1f]', '', text)

    if _looks_like_path(text):
        text = re.sub(r"[\\/]+", r"\\", text)

    return text


def _sanitize_step(step: PlanStep) -> PlanStep:
    return PlanStep(
        step_no=step.step_no,
        action=step.action,
        target=_sanitize_path_string(step.target),
        reason=step.reason.strip(),
        content=step.content,  # FIX: content taşınıyor
    )


def sanitize_plan(plan: AgentPlan) -> AgentPlan:
    sanitized_steps = [_sanitize_step(step) for step in plan.steps]

    return AgentPlan(
        goal=plan.goal.strip(),
        summary=plan.summary.strip(),
        steps=sanitized_steps,
        risk_level=plan.risk_level,
        risk_notes=[note.strip() for note in plan.risk_notes],
        permission_scope=plan.permission_scope.strip(),
        single_task_ok=plan.single_task_ok,
        forbidden_request_detected=plan.forbidden_request_detected,
        requires_real_execution=plan.requires_real_execution,
        clarification_needed=plan.clarification_needed,
        clarification_question=(
            plan.clarification_question.strip()
            if plan.clarification_question else None
        )
    )
