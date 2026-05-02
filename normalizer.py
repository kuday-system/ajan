# normalizer.py v0.6
# Değişiklikler v0.5'e göre:
#   FIX — Grup B: CREATE_DIR adımı filtreleme.
#          Kullanıcı metni açıkça klasör oluşturma içermiyorsa
#          LLM'in ürettiği CREATE_DIR adımları plandan çıkarılır.
#          step_no'lar ardından yeniden sıralanır.

import logging
import re
from models import AgentPlan, ActionType, PlanStep

logger = logging.getLogger("normalizer")


# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

_ENV_VAR_TO_LABEL: dict[str, str | None] = {
    "%desktop%":   "Desktop",
    "%documents%": "Documents",
    "%downloads%": "Downloads",
    "%userhome%":  None,
}

_VALID_SHORT_LABELS = {"desktop", "documents", "downloads", "userhome"}

# Kullanıcı metni bu pattern'lerden birini içeriyorsa CREATE_DIR meşrudur.
_CREATE_DIR_TRIGGERS = re.compile(
    r"(klasör|klasor|dizin|folder|dir|directory)\s*(oluştur|olustur|yap|kur|aç|ac|create|make)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# _is_already_valid
# ---------------------------------------------------------------------------

def _is_already_valid(target: str) -> bool:
    stripped = target.strip()
    lower = stripped.lower()

    for label in _VALID_SHORT_LABELS:
        if lower == label:
            return True
        if lower.startswith(label + "\\") or lower.startswith(label + "/"):
            return True

    if len(stripped) >= 3 and stripped[1:3] in (":\\", ":/"):
        return True
    if stripped.startswith("/"):
        return True

    return False


# ---------------------------------------------------------------------------
# Grup A — env var mapping
# ---------------------------------------------------------------------------

def _apply_env_var_mapping(target: str) -> str | None:
    stripped = target.strip()
    lower = stripped.lower()

    for bad_prefix, label in _ENV_VAR_TO_LABEL.items():
        if lower.startswith(bad_prefix):
            if label is None:
                logger.debug(
                    f"normalize | group=A | bad_prefix='{bad_prefix}' "
                    f"→ label=None, clarification'a bırakıldı"
                )
                return None

            remainder = stripped[len(bad_prefix):]
            if remainder and not remainder.startswith(("\\", "/")):
                remainder = "\\" + remainder
            result = label + remainder
            return result

    return None


# ---------------------------------------------------------------------------
# _normalize_step — Grup A
# ---------------------------------------------------------------------------

def _normalize_step(step: PlanStep) -> PlanStep:
    original = step.target

    if _is_already_valid(original):
        return step

    after_env = _apply_env_var_mapping(original)

    if after_env is not None:
        logger.info(
            f"normalize | group=A | step={step.step_no} | action={step.action.value} "
            f"| before='{original}' | after='{after_env}'"
        )
        return PlanStep(
            step_no=step.step_no,
            action=step.action,
            target=after_env,
            reason=step.reason,
            content=step.content,
        )

    logger.debug(
        f"normalize | no_match | step={step.step_no} | action={step.action.value} "
        f"| target='{original}' → dokunulmadı"
    )
    return step


# ---------------------------------------------------------------------------
# Grup B — CREATE_DIR filtreleme
# ---------------------------------------------------------------------------

def _user_requested_dir_creation(user_text: str) -> bool:
    """Kullanıcı metni açıkça klasör oluşturma içeriyor mu?"""
    return bool(_CREATE_DIR_TRIGGERS.search(user_text))


def _filter_spurious_create_dir(
    steps: list[PlanStep],
    user_text: str,
) -> list[PlanStep]:
    """
    Kullanıcı klasör oluşturmak istemediyse LLM'in eklediği
    CREATE_DIR adımlarını atar. step_no'ları yeniden sıralar.
    """
    if _user_requested_dir_creation(user_text):
        return steps  # meşru, dokunma

    filtered = []
    removed = []
    for step in steps:
        if step.action == ActionType.CREATE_DIR:
            removed.append(step)
        else:
            filtered.append(step)

    if removed:
        for r in removed:
            logger.info(
                f"normalize | group=B | CREATE_DIR kaldırıldı "
                f"| step={r.step_no} | target='{r.target}' "
                f"| kullanıcı klasör belirtmedi"
            )
        # step_no'ları 1'den başlayarak yeniden sırala
        reordered = [
            PlanStep(
                step_no=i + 1,
                action=s.action,
                target=s.target,
                reason=s.reason,
                content=s.content,
            )
            for i, s in enumerate(filtered)
        ]
        return reordered

    return steps


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_plan(plan: AgentPlan, user_text: str) -> AgentPlan:
    """
    Planın her adımını normalize eder.
    Grup A: env var mapping
    Grup B: gereksiz CREATE_DIR filtreleme

    Yeni AgentPlan döndürür — orijinal değişmez.
    Hiçbir step değişmediyse aynı objeyi döndürür (identity check).
    """
    # Grup A
    normalized_steps = [
        _normalize_step(step)
        for step in plan.steps
    ]

    # Grup B
    normalized_steps = _filter_spurious_create_dir(normalized_steps, user_text)

    if (
        len(normalized_steps) == len(plan.steps)
        and all(n is o for n, o in zip(normalized_steps, plan.steps))
    ):
        return plan

    return AgentPlan(
        goal=plan.goal,
        summary=plan.summary,
        steps=normalized_steps,
        risk_level=plan.risk_level,
        risk_notes=list(plan.risk_notes),
        permission_scope=plan.permission_scope,
        single_task_ok=plan.single_task_ok,
        forbidden_request_detected=plan.forbidden_request_detected,
        requires_real_execution=plan.requires_real_execution,
        clarification_needed=plan.clarification_needed,
        clarification_question=plan.clarification_question,
    )
