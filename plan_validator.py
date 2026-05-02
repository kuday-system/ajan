# plan_validator.py v1.2
# Değişiklikler v1.1'e göre:
#   FIX — Kol 1b: WRITE_FILE / APPEND_FILE için content boşsa artık INVALID değil
#          ASK_CLARIFICATION dönüyor. Reason dinamik üretiliyor:
#          "WRITE_FILE için content zorunlu. Desktop\kuday.txt dosyasına ne yazılacağını belirtin."

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Union

from config import MAX_STEPS

if TYPE_CHECKING:
    from models import AgentPlan

_CONTENT_REQUIRED_ACTIONS = {"WRITE_FILE", "APPEND_FILE"}


# ---------------------------------------------------------------------------
# Sonuç tipleri
# ---------------------------------------------------------------------------

class ValidationStatus(str, Enum):
    VALID               = "VALID"
    INVALID             = "INVALID"
    ASK_CLARIFICATION   = "ASK_CLARIFICATION"


@dataclass
class ValidationResult:
    status:   ValidationStatus
    reasons:  list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def is_valid(self) -> bool:
        return self.status == ValidationStatus.VALID


# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

REQUIRED_STEP_FIELDS: tuple[str, ...] = ("step_no", "action", "target")

_RE_WIN_PATH    = re.compile(r"^[A-Za-z]:\\", re.IGNORECASE)
_RE_ENV_VAR     = re.compile(r"\$[A-Z_][A-Z0-9_]*|%[A-Z_][A-Z0-9_%]+%", re.IGNORECASE)
_RE_NATURAL_TGT = re.compile(r"\b(the|a|an|this|that|those|these)\b", re.IGNORECASE)
_RE_MULTI_TGT   = re.compile(r"[,;]|\band\b|\bve\b", re.IGNORECASE)

_VAGUE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bşey\b",           re.IGNORECASE),
    re.compile(r"\bbir\s+şey\b",     re.IGNORECASE),
    re.compile(r"\biş\s+yap\b",      re.IGNORECASE),
    re.compile(r"\bbir\s+şeyler\b",  re.IGNORECASE),
    re.compile(r"\bfalan\b",         re.IGNORECASE),
    re.compile(r"\bfilan\b",         re.IGNORECASE),
    re.compile(r"\bfalan\s+filan\b", re.IGNORECASE),
    re.compile(r"\bneyse\b",         re.IGNORECASE),
    re.compile(r"\bvs[\.,;!?\s]*$",  re.IGNORECASE | re.MULTILINE),
)


# ---------------------------------------------------------------------------
# Ana sınıf
# ---------------------------------------------------------------------------

class PlanValidator:
    """
    Üç kontrol kolunu sırasıyla çalıştırır:
        1. Yapısal kontrol   → INVALID üretebilir
        2. Güvenlik pattern  → INVALID veya ASK_CLARIFICATION üretebilir
        3. Minimal semantik  → INVALID veya ASK_CLARIFICATION üretebilir

    Öncelik: Yapısal > Güvenlik pattern > Semantik
    İlk INVALID bulunduğunda semantiğe bakılmaz.

    Not: Kol 1b (content kontrolü) ASK_CLARIFICATION üretir ve ayrı toplanır.
    Yapısal INVALID yoksa clarification'larla birleştirilir.
    """

    def validate(
        self,
        plan: "Union[AgentPlan, dict[str, Any]]",
        user_text: str | None = None,
    ) -> ValidationResult:
        if isinstance(plan, dict):
            pass
        elif hasattr(plan, "model_dump"):
            plan = plan.model_dump()
        else:
            return ValidationResult(
                status=ValidationStatus.INVALID,
                reasons=["Plan ne dict ne de model_dump destekli bir obje."],
            )

        warnings: list[str] = []

        # --- Kol 1: Yapısal ---
        structural_issues, content_clarifications = self._check_structural(plan, warnings)
        if structural_issues:
            return ValidationResult(
                status=ValidationStatus.INVALID,
                reasons=structural_issues,
                warnings=warnings,
            )

        # --- Kol 2: Güvenlik pattern ---
        sec_invalid, sec_clarify = self._check_security_patterns(plan, warnings)
        if sec_invalid:
            return ValidationResult(
                status=ValidationStatus.INVALID,
                reasons=sec_invalid,
                warnings=warnings,
            )

        # --- Kol 3: Minimal semantik ---
        sem_invalid, sem_clarify = self._check_minimal_semantic(plan, warnings, user_text)
        if sem_invalid:
            return ValidationResult(
                status=ValidationStatus.INVALID,
                reasons=sem_invalid,
                warnings=warnings,
            )

        clarifications = content_clarifications + sec_clarify + sem_clarify
        if clarifications:
            return ValidationResult(
                status=ValidationStatus.ASK_CLARIFICATION,
                reasons=clarifications,
                warnings=warnings,
            )

        return ValidationResult(
            status=ValidationStatus.VALID,
            warnings=warnings,
        )

    # -----------------------------------------------------------------------
    # Kol 1 — Yapısal kontrol
    # -----------------------------------------------------------------------

    def _check_structural(
        self,
        plan: dict[str, Any],
        warnings: list[str],
    ) -> tuple[list[str], list[str]]:
        """
        İki liste döndürür:
          - issues:               INVALID üretecek hatalar
          - content_clarifications: ASK_CLARIFICATION üretecek content eksiklikleri
        """
        issues: list[str] = []
        content_clarifications: list[str] = []

        if not isinstance(plan, dict):
            return ["Plan bir dict değil."], []

        steps = plan.get("steps")
        if not steps:
            issues.append("'steps' alanı eksik veya boş.")
            return issues, content_clarifications

        if not isinstance(steps, list):
            issues.append("'steps' bir liste olmalı.")
            return issues, content_clarifications

        if len(steps) > MAX_STEPS:
            issues.append(
                f"Adım sayısı limiti aşıldı: {len(steps)} > {MAX_STEPS} (config.MAX_STEPS)."
            )

        step_nos: list[int] = []

        for i, step in enumerate(steps):
            prefix = f"steps[{i}]"

            if not isinstance(step, dict):
                issues.append(f"{prefix}: dict olmalı.")
                continue

            for field_name in REQUIRED_STEP_FIELDS:
                if field_name not in step:
                    issues.append(f"{prefix}: '{field_name}' alanı eksik.")

            step_no = step.get("step_no")
            if step_no is not None:
                if not isinstance(step_no, int):
                    issues.append(f"{prefix}: 'step_no' int olmalı, geldi: {type(step_no).__name__}.")
                else:
                    step_nos.append(step_no)

            target = step.get("target")
            if target is not None and not isinstance(target, str):
                issues.append(f"{prefix}: 'target' str olmalı, geldi: {type(target).__name__}.")

            action = step.get("action")
            if action is not None and not isinstance(action, str):
                issues.append(f"{prefix}: 'action' str olmalı, geldi: {type(action).__name__}.")

            # --- Kol 1b: content kontrolü ---
            action_upper = action.split(".")[-1].upper() if isinstance(action, str) else ""
            content = step.get("content")
            content_present = bool(content and str(content).strip())

            if action_upper in _CONTENT_REQUIRED_ACTIONS:
                if not content_present:
                    target_label = target if isinstance(target, str) and target.strip() else "hedef dosya"
                    content_clarifications.append(
                        f"{action_upper} için content zorunlu. "
                        f"'{target_label}' dosyasına ne yazılacağını belirtin."
                    )
            else:
                if content_present:
                    issues.append(
                        f"{prefix}: '{action}' için 'content' olmamalı ama dolu geldi."
                    )

        if len(step_nos) != len(set(step_nos)):
            issues.append("'step_no' değerleri unique olmalı, tekrar var.")

        if step_nos and step_nos != sorted(step_nos):
            warnings.append("'step_no' sıralı değil; beklenmedik çalışma sırası oluşabilir.")

        return issues, content_clarifications

    # -----------------------------------------------------------------------
    # Kol 2 — Güvenlik pattern kontrolü
    # -----------------------------------------------------------------------

    def _check_security_patterns(
        self,
        plan: dict[str, Any],
        warnings: list[str],
    ) -> tuple[list[str], list[str]]:
        invalid:  list[str] = []
        clarify:  list[str] = []

        for i, step in enumerate(plan.get("steps", [])):
            if not isinstance(step, dict):
                continue

            prefix = f"steps[{i}]"
            target = step.get("target", "")
            action = step.get("action", "")

            if not isinstance(target, str):
                continue

            if _RE_WIN_PATH.match(target):
                invalid.append(
                    f"{prefix}: Windows path formatı geçersiz target — '{target}'."
                )

            if _RE_ENV_VAR.search(target):
                invalid.append(
                    f"{prefix}: Environment variable içeren target geçersiz — '{target}'."
                )

            if _RE_NATURAL_TGT.search(target):
                clarify.append(
                    f"{prefix}: Target doğal dil içeriyor, somut bir değer bekleniyor — '{target}'."
                )

            action_target_issue = self._check_action_target_mismatch(action, target, prefix)
            if action_target_issue:
                invalid.append(action_target_issue)

            if _RE_MULTI_TGT.search(target):
                clarify.append(
                    f"{prefix}: Target birden fazla hedef içeriyor gibi görünüyor — '{target}'."
                )

            if not target.strip():
                invalid.append(f"{prefix}: Target boş veya yalnızca boşluk.")

        return invalid, clarify

    def _check_action_target_mismatch(
        self,
        action: str,
        target: str,
        prefix: str,
    ) -> str | None:
        return None

    # -----------------------------------------------------------------------
    # Kol 3 — Minimal semantik kontrol
    # -----------------------------------------------------------------------

    def _check_minimal_semantic(
        self,
        plan: dict[str, Any],
        warnings: list[str],
        user_text: str | None = None,
    ) -> tuple[list[str], list[str]]:
        invalid: list[str] = []
        clarify: list[str] = []

        steps = plan.get("steps", [])

        vague_reason = self._check_vague_goal(user_text or "")
        if vague_reason:
            clarify.append(vague_reason)

        if self._is_multi_task(steps):
            clarify.append(
                "Plan birden fazla bağımsız görevi kapsıyor gibi görünüyor. "
                "Her plan tek bir göreve odaklanmalı."
            )

        conflict = self._find_obvious_conflict(steps)
        if conflict:
            invalid.append(f"Çelişen adımlar tespit edildi: {conflict}")

        return invalid, clarify

    def _check_vague_goal(self, user_text: str) -> str | None:
        if not user_text or not isinstance(user_text, str):
            return None
        for pattern in _VAGUE_PATTERNS:
            if pattern.search(user_text):
                return (
                    f"Komut belirsiz — '{user_text}'. "
                    "Lütfen ne yapmak istediğinizi daha açık belirtin."
                )
        return None

    def _is_multi_task(self, steps: list[Any]) -> bool:
        return False

    def _find_obvious_conflict(self, steps: list[Any]) -> str | None:
        return None
