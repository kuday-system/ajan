# consistency_checker.py v1.1
# Yerel Güvenli Ajan v1.4
#
# Değişiklikler v1.0'a göre:
#   FIX 1 — Scope kontrolü: absolute path, UNC, C:\ → scope dışı → deny
#   FIX 2 — Risk kontrolü: DELETE_FILE + risk=low → deny (clarify değil)
#   FIX 3 — Multi intent çelişki: read + delete gibi zıt intent → deny
#   FIX 4 — Summary check: substring yerine action keyword overlap → false positive azaldı
#
# Konum: validator → consistency_checker → rule_engine
# Kapsam dışı: path güvenliği → rule_engine | format/yapı → validator

from __future__ import annotations

import re
import logging

from models import AgentPlan, ActionType, PlanReview

logger = logging.getLogger("consistency_checker")

# ---------------------------------------------------------------------------
# Intent pattern'ları
# ---------------------------------------------------------------------------

_READ_INTENT   = re.compile(r"\b(oku|göster|listele|bak|read|show|list|view)\b", re.I)
_WRITE_INTENT  = re.compile(r"\b(yaz|oluştur|ekle|kaydet|create|write|append|add|save)\b", re.I)
_DELETE_INTENT = re.compile(r"\b(sil|kaldır|delete|remove)\b", re.I)
_MOVE_INTENT   = re.compile(r"\b(taşı|move)\b", re.I)
_COPY_INTENT   = re.compile(r"\b(kopyala|copy)\b", re.I)

_INTENT_MAP: list[tuple[re.Pattern, set[ActionType]]] = [
    (_READ_INTENT,   {ActionType.READ_FILE, ActionType.LIST_DIR}),
    (_WRITE_INTENT,  {ActionType.WRITE_FILE, ActionType.APPEND_FILE, ActionType.CREATE_DIR}),
    (_DELETE_INTENT, {ActionType.DELETE_FILE}),
    (_MOVE_INTENT,   {ActionType.MOVE_FILE}),
    (_COPY_INTENT,   {ActionType.COPY_FILE}),
]

# FIX 3 — Çelişen intent çiftleri: ikisi aynı anda varsa → deny
_CONFLICTING_INTENTS: list[tuple[re.Pattern, re.Pattern, str]] = [
    (_READ_INTENT,  _DELETE_INTENT, "oku/listele + sil/delete"),
    (_WRITE_INTENT, _DELETE_INTENT, "yaz/oluştur + sil/delete"),
    (_COPY_INTENT,  _DELETE_INTENT, "kopyala/copy + sil/delete"),
]

# scope etiketi → beklenen target prefix
_SCOPE_TO_PREFIX: dict[str, str] = {
    "desktop":   "desktop",
    "documents": "documents",
    "downloads": "downloads",
}

# FIX 1 — Absolute path / UNC / drive pattern'ları
_RE_ABS_WIN  = re.compile(r"^[a-zA-Z]:[/\\]")   # C:\ veya C:/
_RE_ABS_UNIX = re.compile(r"^/")                 # /home/... veya /
_RE_UNC      = re.compile(r"^\\\\|^//")          # \\server veya //server


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _actions_in_plan(plan: AgentPlan) -> set[ActionType]:
    return {step.action for step in plan.steps}


def _target_prefix(target: str) -> str:
    return target.strip().lower().replace("\\", "/").split("/")[0]


def _is_absolute_or_unc(target: str) -> bool:
    """FIX 1: absolute path veya UNC → her zaman scope dışı."""
    t = target.strip()
    return bool(
        _RE_ABS_WIN.match(t) or
        _RE_ABS_UNIX.match(t) or
        _RE_UNC.match(t)
    )


# ---------------------------------------------------------------------------
# Kontrol 1 — Goal ↔ Action uyumu  (+FIX 3 çelişen intent)
# ---------------------------------------------------------------------------

def _check_goal_action(plan: AgentPlan, user_text: str) -> tuple[list[str], list[str]]:
    """
    Döner: (deny_reasons, clarify_reasons)

    FIX 3: Çelişen intent çifti (read+delete gibi) varsa → deny, kontrol durmaz.
    Tek uyumsuz intent → deny.
    Çok uyumsuz intent → clarify.
    """
    deny: list[str] = []
    clarify: list[str] = []

    actions = _actions_in_plan(plan)
    text = user_text + " " + (plan.goal or "")

    # FIX 3 — Çelişen intent çifti kontrolü (action'dan bağımsız, metin bazlı)
    for pat_a, pat_b, label in _CONFLICTING_INTENTS:
        if pat_a.search(text) and pat_b.search(text):
            deny.append(
                f"[consistency] Çelişen intent tespit edildi: '{label}' — "
                "tek komutta zıt işlemler belirtilmiş."
            )

    # Her intent için action uyumu
    matched_intents: list[tuple[str, set[ActionType]]] = []
    for pattern, expected_actions in _INTENT_MAP:
        if pattern.search(text):
            matched_intents.append((pattern.pattern, expected_actions))

    if not matched_intents:
        return deny, clarify

    for pattern_str, expected_actions in matched_intents:
        overlap = actions & expected_actions
        if not overlap:
            conflict_actions = [a.value for a in actions]
            expected_values  = [a.value for a in expected_actions]
            msg = (
                f"[consistency] Intent '{pattern_str}' için "
                f"beklenen: {expected_values}, planda: {conflict_actions}."
            )
            if len(matched_intents) == 1:
                deny.append(msg)
            else:
                clarify.append(msg)

    return deny, clarify


# ---------------------------------------------------------------------------
# Kontrol 2 — Summary ↔ Steps uyumu  (FIX 4)
# ---------------------------------------------------------------------------

def _check_summary_steps(plan: AgentPlan) -> list[str]:
    """
    FIX 4: Substring yerine action keyword overlap kullan.
    Summary'de action tipini çağrıştıran kelime varsa → uyumlu say.
    Sadece placeholder/boş summary → clarify.
    False positive azaldı: hedef adı eşleşmesi artık zorunlu değil.
    """
    clarify: list[str] = []

    summary = (plan.summary or "").strip().lower()

    # Placeholder veya boş
    _PLACEHOLDERS = {"string", "text", "özet", "summary", "açıklama", "plan"}
    if not summary or summary in _PLACEHOLDERS:
        clarify.append(
            "[consistency] Summary boş veya placeholder — plan amacı belirsiz."
        )
        return clarify

    # Summary'de en az bir action türüne işaret eden kelime var mı?
    _SUMMARY_ACTION_KEYWORDS = re.compile(
        r"\b(oku|yaz|sil|taşı|kopyala|listele|oluştur|ekle|kaydet|"
        r"read|write|delete|move|copy|list|create|append|save|show)\b",
        re.I,
    )
    if not _SUMMARY_ACTION_KEYWORDS.search(summary):
        clarify.append(
            "[consistency] Summary herhangi bir işlem içermiyor — plan amacı belirsiz."
        )

    return clarify


# ---------------------------------------------------------------------------
# Kontrol 3 — Risk ↔ Action uyumu  (FIX 2)
# ---------------------------------------------------------------------------

def _check_risk_action(plan: AgentPlan) -> tuple[list[str], list[str]]:
    """
    FIX 2: DELETE_FILE + risk_level="low" → deny (eski: clarify).
    forbidden_request_detected + low/medium risk → deny (değişmedi).
    """
    deny: list[str] = []
    clarify: list[str] = []

    actions = _actions_in_plan(plan)

    # FIX 2: deny'a yükseltildi
    if ActionType.DELETE_FILE in actions and plan.risk_level == "low":
        deny.append(
            "[consistency] DELETE_FILE eylemi var ama risk_level='low' — "
            "silme işlemi düşük risk olarak işaretlenemez."
        )

    if plan.forbidden_request_detected and plan.risk_level in ("low", "medium"):
        deny.append(
            "[consistency] forbidden_request_detected=True ama "
            f"risk_level='{plan.risk_level}' — çelişkili risk değerlendirmesi."
        )

    return deny, clarify


# ---------------------------------------------------------------------------
# Kontrol 4 — permission_scope ↔ target uyumu  (FIX 1)
# ---------------------------------------------------------------------------

def _check_scope_target(plan: AgentPlan) -> list[str]:
    """
    FIX 1: Absolute path (C:\\, /, UNC) → her zaman scope dışı → deny.
    Farklı zone prefix → deny (eski davranış korundu).
    """
    deny: list[str] = []

    scope = (plan.permission_scope or "").strip().lower()
    expected_prefix = _SCOPE_TO_PREFIX.get(scope)

    if expected_prefix is None:
        return deny  # "User", "Internal" vb. → kontrol edilmez

    for step in plan.steps:
        target = step.target

        # FIX 1 — Absolute / UNC → kesinlikle scope dışı
        if _is_absolute_or_unc(target):
            deny.append(
                f"[consistency] permission_scope='{plan.permission_scope}' ama "
                f"adım {step.step_no} target='{target}' absolute/UNC path — scope dışı."
            )
            continue

        # Bilinen zone prefix'i farklı mı?
        prefix = _target_prefix(target)
        if prefix and prefix in _SCOPE_TO_PREFIX and prefix != expected_prefix:
            deny.append(
                f"[consistency] permission_scope='{plan.permission_scope}' ama "
                f"adım {step.step_no} target='{target}' farklı zone."
            )

    return deny


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_consistency(plan: AgentPlan, user_text: str) -> PlanReview:
    """
    Döner:
        PlanReview(decision="allow")             → sorun yok
        PlanReview(decision="ask_clarification") → belirsiz/şüpheli
        PlanReview(decision="deny")              → ciddi çelişki
    """
    all_deny: list[str]    = []
    all_clarify: list[str] = []

    # 1) Goal ↔ Action  (+FIX 3 çelişen intent)
    d, c = _check_goal_action(plan, user_text)
    all_deny.extend(d)
    all_clarify.extend(c)

    # 2) Summary ↔ Steps  (FIX 4)
    all_clarify.extend(_check_summary_steps(plan))

    # 3) Risk ↔ Action  (FIX 2)
    d, c = _check_risk_action(plan)
    all_deny.extend(d)
    all_clarify.extend(c)

    # 4) Scope ↔ Target  (FIX 1)
    all_deny.extend(_check_scope_target(plan))

    # Karar: deny > ask_clarification > allow
    if all_deny:
        reasons = all_deny + all_clarify
        logger.warning(f"consistency | DENY | {reasons}")
        return PlanReview(decision="deny", reasons=reasons)

    if all_clarify:
        logger.info(f"consistency | ASK_CLARIFICATION | {all_clarify}")
        return PlanReview(decision="ask_clarification", reasons=all_clarify)

    logger.debug("consistency | ALLOW")
    return PlanReview(decision="allow", reasons=[])
