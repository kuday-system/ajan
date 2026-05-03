# normalizer.py v0.7
# Değişiklikler v0.6'ya göre:
#   FIX (Grup C) — OPEN_URL için malformed URL normalizer eklendi.
#
#   Kök sorun: _is_already_valid() fonksiyonu "https:\www.youtube.com" stringini
#   geçerli sayıyordu. Çünkü stripped[1:3] == ":\\" koşulu drive-letter kontrolü
#   için yazılmıştı, ama backslash'li URL'leri de yakalıyordu.
#   Sonuç: Grup A hiç çalışmıyor, bozuk URL executor'a ulaşıyordu.
#
#   Grup C ne yapar:
#     1. Sadece OPEN_URL action'larına uygulanır.
#     2. Zaten geçerli URL (https://...) ise dokunulmaz.
#     3. Bozuk separator düzeltilir:
#          https:\www.youtube.com   → https://www.youtube.com
#          https:/www.youtube.com   → https://www.youtube.com
#          http:\example.com        → http://example.com
#     4. Scheme'siz domain normalize edilir:
#          www.youtube.com          → https://www.youtube.com
#          youtube.com              → https://www.youtube.com
#     5. Düzeltme sonrası netloc hâlâ boşsa dokunulmaz (executor MALFORMED_URL verir).
#     6. Davranış değişikliği yok: Grup A ve Grup B olduğu gibi korundu.

import logging
import re
from urllib.parse import urlparse

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

# Grup C — bozuk scheme separator pattern'ları
# "https:\..." veya "https:/..." → "https://..."
_RE_MALFORMED_SCHEME = re.compile(
    r"^(https?):(?:/(?!/)|\\+)(.*)",  # tek slash veya bir+ backslash
    re.IGNORECASE,
)

# Grup C — scheme'siz domain: "www.youtube.com" veya "youtube.com"
_RE_SCHEMELESS_DOMAIN = re.compile(
    r"^(www\.[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}|[a-zA-Z0-9\-]+\.[a-zA-Z]{2,})",
)

# Bilinen site adı → canonical domain (sadece scheme'siz kısa isim için)
_SITE_NAME_MAP: dict[str, str] = {
    "youtube": "www.youtube.com",
    "google":  "www.google.com",
    "github":  "github.com",
    "twitter": "twitter.com",
    "x":       "x.com",
    "reddit":  "www.reddit.com",
    "wikipedia": "www.wikipedia.org",
}


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

    # Dosya path kontrolü: C:\ veya C:/ — sadece gerçek drive letter için
    # NOT: "https:\..." bu koşula GİRMEMELİ — scheme 5+ karakter, drive letter 1 karakter
    if len(stripped) >= 3 and len(stripped[0]) == 1 and stripped[1:3] in (":\\", ":/"):
        if stripped[0].isalpha() and stripped[0].upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            # Tek harf + :\ veya :/ → gerçek Windows drive path
            # "https" 5 karakter, bu koşula girmez
            return True

    if stripped.startswith("/"):
        return True

    return False


# ---------------------------------------------------------------------------
# Grup C — OPEN_URL malformed URL düzeltici
# ---------------------------------------------------------------------------

def _fix_open_url(target: str, step_no: int) -> str | None:
    """
    OPEN_URL target'ını normalize eder.
    Değişiklik varsa düzeltilmiş string döner.
    Değişiklik yoksa None döner (dokunulmadı).
    Düzeltilemiyorsa None döner (executor MALFORMED_URL versin).
    """
    raw = target.strip()

    # 1. Zaten geçerli https:// veya http:// URL mi?
    try:
        parsed = urlparse(raw)
        if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
            # Geçerli — dokunma
            return None
    except Exception:
        pass

    # 2. Bozuk separator: https:\... veya https:/...
    m = _RE_MALFORMED_SCHEME.match(raw)
    if m:
        scheme = m.group(1).lower()
        rest   = m.group(2)
        # rest başındaki ekstra backslash/slash'ları temizle
        rest = rest.lstrip("\\/")
        fixed = f"{scheme}://{rest}"
        # Doğrulama
        try:
            p = urlparse(fixed)
            if p.netloc:
                return fixed
        except Exception:
            pass

    # 3. Scheme'siz domain: www.youtube.com veya youtube.com
    if _RE_SCHEMELESS_DOMAIN.match(raw):
        fixed = f"https://{raw}"
        try:
            p = urlparse(fixed)
            if p.netloc:
                return fixed
        except Exception:
            pass

    # 4. Düzeltilemedi
    return None


# ---------------------------------------------------------------------------
# _normalize_step — Grup A + Grup C
# ---------------------------------------------------------------------------

def _normalize_step(step: PlanStep) -> PlanStep:
    original = step.target

    # Grup C — OPEN_URL için önce malformed URL kontrolü
    if step.action == ActionType.OPEN_URL:
        fixed = _fix_open_url(original, step.step_no)
        if fixed is not None:
            logger.info(
                f"normalize | group=C | step={step.step_no} | action=OPEN_URL "
                f"| before={repr(original)} | after={repr(fixed)}"
            )
            return PlanStep(
                step_no=step.step_no,
                action=step.action,
                target=fixed,
                reason=step.reason,
                content=step.content,
            )
        else:
            # Zaten geçerli veya düzeltilemez — Grup A'ya gerek yok, dön
            logger.debug(
                f"normalize | group=C | no_change | step={step.step_no} "
                f"| action=OPEN_URL | target={repr(original)}"
            )
            return step

    # Grup A — dosya path env var mapping (OPEN_URL dışı action'lar)
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
    Grup A: env var mapping (dosya action'ları)
    Grup B: gereksiz CREATE_DIR filtreleme
    Grup C: OPEN_URL malformed URL düzeltici

    Yeni AgentPlan döndürür — orijinal değişmez.
    Hiçbir step değişmediyse aynı objeyi döndürür (identity check).
    """
    # Grup A + Grup C
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