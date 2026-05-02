# rule_engine.py v1.4.1
# Değişiklikler v1.4'e göre:
#   TEMİZLİK — SAFE_ACTIONS seti kaldırıldı (hiç kullanılmıyordu, kafa karışıklığı yaratıyordu)
#
# Değişiklikler v1.3.1'e göre:
#   BUG 3  — Zone resolve: normalize edilmiş path ile ALLOWED_USER_ZONES karşılaştırması düzeltildi
#             (resolved.resolve() ile case-insensitive birebir eşleşme)
#   BUG 4  — LIST_DIR ve READ_FILE "gerçek yürütme" olarak algılanmıyacak
#   BUG 5  — requires_real_execution yalnızca MUTATING action içeren planlarda dikkate alınıyor
#   BUG 12 — requires_real_execution flag: READ/LIST/CREATE_DIR izinli akışta yok sayılıyor
#   Q1     — Duplicate reason mesajları tek noktada temizleniyor (warnings da dahil)

import os
import re
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import List, Tuple

from config import (
    DOWNLOADS_DIR,
    FORBIDDEN_KEYWORDS,
    FORBIDDEN_PATH_HINTS,
    FORBIDDEN_EXTENSIONS,
    INTERNAL_BASE_PATH,
    ALLOWED_USER_ZONES,
    MAX_STEPS,
    PATH_NORMALIZE,
    DESKTOP_DIR,
    DOCUMENTS_DIR,
)
from models import AgentPlan, ActionGroup, ActionType, PlanReview

# --- Mutating Action Kümesi ---
# requires_real_execution flag'i yalnızca bu action'lar için dikkate alınır.

MUTATING_ACTIONS = {
    ActionType.WRITE_FILE,
    ActionType.APPEND_FILE,
    ActionType.DELETE_FILE,
    ActionType.MOVE_FILE,
    ActionType.COPY_FILE,
}


# --- Windows Reserved Names ---

_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5",
    "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5",
    "lpt6", "lpt7", "lpt8", "lpt9",
}

# --- Bare Zone Labels ---
_BARE_ZONE_LABELS = {"desktop", "documents", "downloads", "userhome"}


# --- Path Zone ---

class PathZone(Enum):
    SYSTEM_SPACE  = "system_space"
    PROGRAM_SPACE = "program_space"
    USER_SPACE    = "user_space"
    INTERNAL      = "internal"
    UNKNOWN       = "unknown"


# --- Yardımcı ---

def _normalize(text: str) -> str:
    if PATH_NORMALIZE:
        return text.lower().replace("\\", "/")
    return text


def _sanitize_path_text(raw: str) -> str:
    text = raw.strip().strip('"\'')

    # Geçersiz env variable mapping
    text = text.replace("%UserHome%", "%USERPROFILE%")
    text = text.replace("%userHome%", "%USERPROFILE%")
    text = text.replace("%userhome%", "%USERPROFILE%")
    text = text.replace("%Desktop%", "%USERPROFILE%\\Desktop")
    text = text.replace("%Documents%", "%USERPROFILE%\\Documents")
    text = text.replace("%Downloads%", "%USERPROFILE%\\Downloads")

    text = os.path.expandvars(text)
    username = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    text = text.replace("[Username]", username).replace("[username]", username)
    text = text.lower().replace("\\", "/")
    return text


def _classify_path_text(path_text: str) -> PathZone:
    p = path_text

    if p in {"c:", "c:/"}:
        return PathZone.SYSTEM_SPACE

    if any(hint in p for hint in ["c:/windows", "c:/system32", "programdata"]):
        return PathZone.SYSTEM_SPACE

    if any(hint in p for hint in ["c:/program files", "c:/program files (x86)"]):
        return PathZone.PROGRAM_SPACE

    if p == "desktop" or p.startswith(("desktop/", "desktop\\")):
        return PathZone.USER_SPACE
    if p == "documents" or p.startswith(("documents/", "documents\\")):
        return PathZone.USER_SPACE
    if p == "downloads" or p.startswith(("downloads/", "downloads\\")):
        return PathZone.USER_SPACE
    if p == "userhome" or p.startswith(("userhome/", "userhome\\")):
        return PathZone.USER_SPACE
    if "/users/" in p or p.startswith("c:/users"):
        return PathZone.USER_SPACE

    return PathZone.UNKNOWN


# --- Hardened Path Yardımcıları ---

def _is_unc(path: str) -> bool:
    return path.startswith("\\\\") or path.startswith("//")


def _is_device_path(path: str) -> bool:
    return path.startswith("\\\\.\\") or path.startswith("\\??\\")


def _is_drive_relative(path: str) -> bool:
    return bool(re.match(r'^[a-zA-Z]:[^/\\]', path))


def _has_traversal(path: str) -> bool:
    parts = re.split(r'[\\/]', path)
    return ".." in parts


def _has_reserved_name(path: str) -> bool:
    parts = re.split(r'[\\/]', path)
    return any(p.lower().split(".")[0] in _RESERVED_NAMES for p in parts if p)


def _resolve_to_path(raw: str) -> Path | None:
    try:
        return Path(str(PureWindowsPath(raw)))
    except Exception:
        return None


def _in_zone(path: Path, zones: list[Path]) -> bool:
    """
    BUG 3: resolve() ile normalize edilmiş path karşılaştırması.
    OneDrive ve sembolik link durumlarında doğru eşleşme sağlar.
    """
    try:
        resolved_path = path.resolve()
    except Exception:
        resolved_path = path

    for zone in zones:
        if zone is None:
            continue
        try:
            resolved_zone = zone.resolve()
        except Exception:
            resolved_zone = zone
        try:
            resolved_path.relative_to(resolved_zone)
            return True
        except ValueError:
            continue
    return False


# --- Hardened Path Kontrol ---

def check_path_hardened(
    raw_target: str,
    step_no: int,
    action: ActionType,
) -> Tuple[List[str], List[str]]:
    """
    Hardened path kontrolü.
    Akış: temizle → UNC → device → drive-relative → reserved →
          traversal → resolve → action/zone çapraz kontrol

    BUG 3: _in_zone artık resolve() ile karşılaştırıyor.

    Dönüş: (hard_violations, warnings)
    """
    raw_target = os.path.expandvars(raw_target)

    hard = []
    warnings = []

    stripped = raw_target.strip()
    if not stripped or stripped in {".", "/", "\\"}:
        hard.append(f"[step {step_no}] DENY_REASON=EMPTY_PATH: '{raw_target}'")
        return hard, warnings

    if _is_unc(stripped):
        hard.append(f"[step {step_no}] DENY_REASON=UNC_PATH: '{raw_target}'")
        return hard, warnings

    if _is_device_path(stripped):
        hard.append(f"[step {step_no}] DENY_REASON=DEVICE_PATH: '{raw_target}'")
        return hard, warnings

    if _is_drive_relative(stripped):
        hard.append(f"[step {step_no}] DENY_REASON=DRIVE_RELATIVE_PATH: '{raw_target}'")
        return hard, warnings

    if _has_reserved_name(stripped):
        hard.append(f"[step {step_no}] DENY_REASON=RESERVED_NAME: '{raw_target}'")
        return hard, warnings

    if _has_traversal(stripped):
        hard.append(f"[step {step_no}] DENY_REASON=PATH_TRAVERSAL: '{raw_target}'")
        return hard, warnings

    resolved = _resolve_to_path(stripped)
    if resolved is None:
        hard.append(f"[step {step_no}] DENY_REASON=INVALID_PATH: '{raw_target}'")
        return hard, warnings

    if action in ActionGroup.INTERNAL:
        if _in_zone(resolved, [INTERNAL_BASE_PATH]):
            return hard, warnings
        hard.append(
            f"[step {step_no}] DENY_REASON=INTERNAL_ACTION_OUTSIDE_BASE: '{raw_target}'"
        )
        return hard, warnings

    if action in ActionGroup.USER_FILE:
        sanitized = _sanitize_path_text(raw_target)
        zone = _classify_path_text(sanitized)

        if zone in (PathZone.SYSTEM_SPACE, PathZone.PROGRAM_SPACE):
            hard.append(
                f"[step {step_no}] DENY_REASON={zone.value.upper()}: '{raw_target}'"
            )
            return hard, warnings

        _SHORT_LABELS = {"desktop", "documents", "downloads", "userhome"}
        base = sanitized.split("/")[0].split("\\")[0]
        if base in _SHORT_LABELS:
            return hard, warnings

        if not resolved.is_absolute():
            if action == ActionType.CREATE_DIR:
                hard.append(
                    f"[step {step_no}] INVALID_TARGET_FORMAT: "
                    f"'{raw_target}' zone içermiyor, hedef belirtilmeli."
                )
            else:
                hard.append(
                    f"[step {step_no}] DENY_REASON=RELATIVE_PATH_NOT_ALLOWED: '{raw_target}'"
                )
            return hard, warnings

        # BUG 3: _in_zone artık resolve() ile doğru karşılaştırıyor
        if _in_zone(resolved, ALLOWED_USER_ZONES):
            return hard, warnings

        hard.append(
            f"[step {step_no}] DENY_REASON=OUTSIDE_ALLOWED_ZONES: '{raw_target}'"
        )
        return hard, warnings

    warnings.append(
        f"[step {step_no}] WARN=UNKNOWN_ACTION_GROUP: action={action}"
    )
    return hard, warnings


# --- Plan Structure Kontrolü ---

def _resolve_zone_key(target: str) -> str | None:
    sanitized = _sanitize_path_text(target)

    _SHORT_MAP = {
        "desktop":   "desktop",
        "documents": "documents",
        "downloads": "downloads",
        "userhome":  "userhome",
    }
    base = sanitized.split("/")[0].split("\\")[0]
    if base in _SHORT_MAP:
        return _SHORT_MAP[base]

    expanded = os.path.expandvars(target.strip())
    try:
        resolved = Path(str(PureWindowsPath(expanded))).resolve()
    except Exception:
        return None

    zone_map = {}
    if DESKTOP_DIR:
        zone_map["desktop"] = DESKTOP_DIR
    if DOCUMENTS_DIR:
        zone_map["documents"] = DOCUMENTS_DIR
    if DOWNLOADS_DIR:
        zone_map["downloads"] = DOWNLOADS_DIR

    for key, zone_path in zone_map.items():
        try:
            resolved.relative_to(zone_path.resolve())
            return key
        except ValueError:
            continue

    return None


_DEPENDENT_CHAINS = {
    (ActionType.CREATE_DIR, ActionType.WRITE_FILE),
    (ActionType.CREATE_DIR, ActionType.APPEND_FILE),
    (ActionType.CREATE_DIR, ActionType.LIST_DIR),
}


def _is_dependent_chain(steps) -> bool:
    if len(steps) < 2:
        return True
    for i in range(len(steps) - 1):
        current = steps[i]
        nxt = steps[i + 1]

        current_zone = _resolve_zone_key(current.target)
        next_zone = _resolve_zone_key(nxt.target)

        same_zone = current_zone is not None and current_zone == next_zone
        is_known_chain = (current.action, nxt.action) in _DEPENDENT_CHAINS

        if not (same_zone and is_known_chain):
            return False
    return True


def _check_plan_structure(plan: AgentPlan) -> Tuple[List[str], List[str]]:
    hard = []
    warnings = []

    # CREATE_DIR invalid target format kontrolü
    for step in plan.steps:
        if step.action == ActionType.CREATE_DIR:
            normalized = step.target.strip().lower().replace("\\", "/")
            if (
                "/" not in normalized
                and "\\" not in normalized
                and normalized not in _BARE_ZONE_LABELS
                and not any(normalized.startswith(z + "/") for z in _BARE_ZONE_LABELS)
            ):
                msg_w = (
                    f"[step {step.step_no}] INVALID_TARGET_FORMAT: "
                    f"'{step.target}' sadece klasör adı, zone belirtilmemiş."
                )
                msg_h = (
                    f"[plan-structure] INVALID_TARGET_FORMAT: "
                    f"Adım {step.step_no} hedef zone içermiyor, açıklama gerekiyor."
                )
                if msg_w not in warnings:
                    warnings.append(msg_w)
                if msg_h not in hard:
                    hard.append(msg_h)

    # CREATE_DIR bare zone kontrolü
    for step in plan.steps:
        if step.action == ActionType.CREATE_DIR:
            normalized = step.target.strip().lower().replace("\\", "/")
            if normalized in _BARE_ZONE_LABELS:
                msg_w = (
                    f"[step {step.step_no}] CREATE_DIR_BARE_ZONE: "
                    f"'{step.target}' sadece zone etiketi, klasör adı eksik."
                )
                msg_h = (
                    f"[plan-structure] CREATE_DIR_BARE_ZONE: "
                    f"Adım {step.step_no} hedef klasör adı belirtilmemiş, açıklama gerekiyor."
                )
                if msg_w not in warnings:
                    warnings.append(msg_w)
                if msg_h not in hard:
                    hard.append(msg_h)

    if len(plan.steps) <= 1:
        return hard, warnings

    user_zones = set()
    for step in plan.steps:
        key = _resolve_zone_key(step.target)
        if key is not None:
            user_zones.add(key)

    if len(user_zones) > 1 and not _is_dependent_chain(plan.steps):
        msg_w = (
            f"[plan-structure] Birden fazla bağımsız görev tespit edildi "
            f"(zone'lar: {sorted(user_zones)}). Açıklama bekleniyor."
        )
        msg_h = "[plan-structure] MULTI_TASK_DETECTED: Bağımsız çok görev tek komutta birleştirilmiş."
        if msg_w not in warnings:
            warnings.append(msg_w)
        if msg_h not in hard:
            hard.append(msg_h)

    return hard, warnings


# --- Kontrol fonksiyonları ---

def _check_user_text(user_text: str) -> List[str]:
    reasons = []
    normalized = _normalize(user_text)

    for keyword in FORBIDDEN_KEYWORDS:
        if keyword in normalized:
            reasons.append(f"[kullanıcı metni] Yasaklı anahtar kelime: '{keyword}'")

    for path_hint in FORBIDDEN_PATH_HINTS:
        if _normalize(path_hint) in normalized:
            reasons.append(f"[kullanıcı metni] Yasaklı path: '{path_hint}'")

    for ext in FORBIDDEN_EXTENSIONS:
        if ext in normalized:
            reasons.append(f"[kullanıcı metni] Yasaklı uzantı: '{ext}'")

    return reasons


def _plan_has_mutating_action(plan: AgentPlan) -> bool:
    """
    BUG 4, 5, 12: Planda en az bir MUTATING action var mı?
    READ_FILE, LIST_DIR, CREATE_DIR gibi safe action'lar mutating sayılmaz.
    """
    return any(step.action in MUTATING_ACTIONS for step in plan.steps)


def _check_plan(plan: AgentPlan) -> Tuple[List[str], List[str]]:
    hard = []
    warnings = []

    if plan.forbidden_request_detected:
        hard.append("[plan] Model yasaklı alan tespit etti.")

    if len(plan.steps) == 0:
        hard.append("[plan] Plan boş, hiç adım yok.")

    if len(plan.steps) > MAX_STEPS:
        hard.append(f"[plan] Adım sayısı limiti aşıldı: {len(plan.steps)} > {MAX_STEPS}")

    if plan.risk_level in ("high", "critical"):
        hard.append(f"[plan] Risk seviyesi çok yüksek: {plan.risk_level}")

    # BUG 4, 5, 12: requires_real_execution yalnızca MUTATING action varsa dikkate alınır.
    # LIST_DIR / READ_FILE gibi safe action'lar bu flag'i tetiklemez.
    if plan.requires_real_execution and _plan_has_mutating_action(plan):
        hard.append("[plan] Gerçek yürütme isteniyor, v1'de izin verilmez.")

    for step in plan.steps:
        step_hard, step_warn = check_path_hardened(
            step.target, step.step_no, step.action
        )
        hard.extend(step_hard)
        warnings.extend(step_warn)

        normalized_target = _normalize(step.target)

        for keyword in FORBIDDEN_KEYWORDS:
            if keyword in normalized_target:
                hard.append(
                    f"[step {step.step_no}] Yasaklı kelime target'ta: '{keyword}'"
                )

        for ext in FORBIDDEN_EXTENSIONS:
            if ext in normalized_target:
                hard.append(
                    f"[step {step.step_no}] Yasaklı uzantı target'ta: '{ext}'"
                )

        for hint in FORBIDDEN_PATH_HINTS:
            if _normalize(hint) in normalized_target:
                hard.append(
                    f"[step {step.step_no}] Yasaklı path hint target'ta: '{hint}'"
                )

    structure_hard, structure_warn = _check_plan_structure(plan)
    hard.extend(structure_hard)
    warnings.extend(structure_warn)

    # Q1: Duplicate temizliği — hard ve warnings ayrı ayrı
    hard = list(dict.fromkeys(hard))
    warnings = list(dict.fromkeys(warnings))

    return hard, warnings


# --- RuleEngine ---

class RuleEngine:
    def review(self, user_text: str, plan: AgentPlan) -> PlanReview:
        user_hard = _check_user_text(user_text)
        plan_hard, plan_warnings = _check_plan(plan)

        # Q1: Birleşik duplicate temizliği
        all_hard = list(dict.fromkeys(user_hard + plan_hard))

        if all_hard:
            # MULTI_TASK veya CREATE_DIR_BARE_ZONE → ask_clarification
            if all(
                "MULTI_TASK_DETECTED" in r or
                "CREATE_DIR_BARE_ZONE" in r or
                "INVALID_TARGET_FORMAT" in r
                for r in all_hard
            ):
                return PlanReview(
                    decision="ask_clarification",
                    reasons=all_hard
                )
            return PlanReview(decision="deny", reasons=all_hard)

        if plan.clarification_needed:
            clarification_reason = plan.clarification_question or "İstek net değil."
            return PlanReview(
                decision="ask_clarification",
                reasons=[f"[plan] Model açıklama istiyor: {clarification_reason}"]
            )

        reasons = ["Plan kurallara uygun. Kullanıcı onayı bekleniyor."]
        # Q1: plan_warnings zaten deduplicate edilmiş geldi
        reasons.extend(plan_warnings)

        return PlanReview(decision="ask_user", reasons=reasons)
