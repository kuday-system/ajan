# lockbox.py v1.1
# Değişiklikler v1.0'a göre:
#   FIX — verify() içinde string == yerine hmac.compare_digest() kullanılıyor.
#          Sabit zamanlı karşılaştırma — timing side-channel kapatıldı.

import hashlib
import hmac
import json

from models import AgentPlan, LockedPlan


def _to_canonical_json(plan: AgentPlan) -> str:
    """Planı her zaman aynı çıktıyı veren canonical JSON'a çevirir."""
    return json.dumps(
        plan.model_dump(),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":")
    )


def _hash_json(canonical_json: str) -> str:
    """Canonical JSON'dan SHA256 hash üretir."""
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class PlanLockbox:
    def lock(self, plan: AgentPlan) -> LockedPlan:
        canonical = _to_canonical_json(plan)
        plan_hash = _hash_json(canonical)
        return LockedPlan(
            plan_hash=plan_hash,
            canonical_plan_json=canonical
        )

    def verify(self, locked_plan: LockedPlan) -> bool:
        """
        Hash doğrulaması sabit zamanlı karşılaştırma ile yapılır.
        hmac.compare_digest() timing side-channel'ı engeller.
        """
        current_hash = _hash_json(locked_plan.canonical_plan_json)
        return hmac.compare_digest(current_hash, locked_plan.plan_hash)
