# consistency_checker.py v1.4
# Yerel Güvenli Ajan v1.4
#
# Değişiklikler v1.3'e göre:
#   FIX 7 — _check_summary_steps action-aware hale getirildi.
#            Önceki davranış: summary keyword içermiyorsa her zaman ask_clarification.
#            Yeni davranış:
#              - Summary boş / placeholder ise → ask_clarification (değişmedi).
#              - Summary doluysa VE plan içinde OPEN_URL veya WEB_SEARCH varsa
#                → summary action keyword içermese bile kabul et.
#                Çünkü internet action zaten işlemi belirtiyor.
#              - Dosya action'ları için keyword kontrolü eskisi gibi devam eder.
#            Geçerli örnek:
#              summary="YouTube", action=OPEN_URL  → geçer
#              summary="YouTube", action=WEB_SEARCH → geçer
#            Geçersiz (clarify) örnek:
#              summary="string" → clarify
#              summary=""       → clarify
#              summary="özet"   → clarify
#
# Önceki düzeltmeler (v1.1–v1.3'ten taşındı):
#   FIX 1 — Scope kontrolü: absolute path, UNC, C:\ → scope dışı → deny
#   FIX 2 — Risk kontrolü: DELETE_FILE + risk=low → deny (clarify değil)
#   FIX 3 — Multi intent çelişki: read + delete gibi zıt intent → deny
#   FIX 4 — Summary check: substring yerine action keyword overlap → false positive azaldı
#   FIX 5 — Summary check: OPEN_URL / WEB_SEARCH kelimeleri eklendi
#   FIX 6 — check_consistency son dönüşü "allow" → "ask_user" olarak düzeltildi.
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

# FIX 7 — Internet action'ları: bu action'lar varsa summary keyword kontrolü atlanır.
_INTERNET_ACTIONS = {ActionType.OPEN_URL, ActionType.WEB_SEARCH}

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
# Kontrol 2 — Summary ↔ Steps uyumu  (FIX 4 + FIX 5 + FIX 7)
# ---------------------------------------------------------------------------

def _check_summary_steps(plan: AgentPlan) -> list[str]:
    """
    FIX 4: Substring yerine action keyword overlap kullan.
    FIX 5: OPEN_URL ve WEB_SEARCH için tipik summary kelimeleri eklendi.
    FIX 7: Action-aware kontrol.
           Plan içinde OPEN_URL veya WEB_SEARCH varsa ve summary boş/placeholder
           değilse → keyword kontrolü atlanır, doğrudan geçer.
           Çünkü internet action zaten işlemi belirtiyor; summary sadece hedefi
           (ör. "YouTube") belirtmek için yeterlidir.
           Dosya action'larında keyword kontrolü eskisi gibi devam eder.
    """
    clarify: list[str] = []

    summary = (plan.summary or "").strip().lower()

    # Placeholder veya boş → her zaman clarify (FIX 7 bile kurtaramaz)
    _PLACEHOLDERS = {"string", "text", "özet", "summary", "açıklama", "plan"}
    if not summary or summary in _PLACEHOLDERS:
        clarify.append(
            "[consistency] Summary boş veya placeholder — plan amacı belirsiz."
        )
        return clarify

    # FIX 7 — Plan internet action içeriyorsa keyword kontrolünü atla.
    actions = _actions_in_plan(plan)
    if actions & _INTERNET_ACTIONS:
        # Summary doluysa ve placeholder değilse internet action için yeterli.
        return clarify

    # Dosya action'ları için keyword kontrolü (FIX 4 + FIX 5 korundu)
    _SUMMARY_ACTION_KEYWORDS = re.compile(
        r"\b("
        # Dosya / klasör action'ları (FIX 4)
        r"oku|yaz|sil|taşı|kopyala|listele|oluştur|ekle|kaydet|"
        r"read|write|delete|move|copy|list|create|append|save|show|"
        # FIX 5 — OPEN_URL action kelimeleri
        r"aç|açıl|açılır|açılacak|open|ziyaret|browse|"
        # FIX 5 — WEB_SEARCH action kelimeleri
        r"ara|arama|aranır|aranacak|search|tara|bul|find"
        r")\b",
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
    Internet scope (OPEN_URL / WEB_SEARCH) → prefix kontrolü yapılmaz.
    """
    deny: list[str] = []

    scope = (plan.permission_scope or "").strip().lower()
    expected_prefix = _SCOPE_TO_PREFIX.get(scope)

    if expected_prefix is None:
        # "Internet", "User", "Internal" vb. → prefix kontrolü yapılmaz
        return deny

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
        PlanReview(decision="ask_user")          → tüm kontroller geçti, kullanıcı onayı bekleniyor
        PlanReview(decision="ask_clarification") → belirsiz/şüpheli
        PlanReview(decision="deny")              → ciddi çelişki
    """
    all_deny: list[str]    = []
    all_clarify: list[str] = []

    # 1) Goal ↔ Action  (+FIX 3 çelişen intent)
    d, c = _check_goal_action(plan, user_text)
    all_deny.extend(d)
    all_clarify.extend(c)

    # 2) Summary ↔ Steps  (FIX 4 + FIX 5 + FIX 7)
    all_clarify.extend(_check_summary_steps(plan))

    # 3) Risk ↔ Action  (FIX 2)
    d, c = _check_risk_action(plan)
    all_deny.extend(d)
    all_clarify.extend(c)

    # 4) Scope ↔ Target  (FIX 1)
    all_deny.extend(_check_scope_target(plan))

    # Karar: deny > ask_clarification > ask_user
    if all_deny:
        reasons = all_deny + all_clarify
        logger.warning(f"consistency | DENY | {reasons}")
        return PlanReview(decision="deny", reasons=reasons)

    if all_clarify:
        logger.info(f"consistency | ASK_CLARIFICATION | {all_clarify}")
        return PlanReview(decision="ask_clarification", reasons=all_clarify)

    # FIX 6 — "allow" PlanReview'da geçersiz değer; reasons boş olamaz.
    logger.debug("consistency | ASK_USER")
    return PlanReview(
        decision="ask_user",
        reasons=["Tutarlılık kontrolü geçti. Kullanıcı onayı bekleniyor."],
    )
