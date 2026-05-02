# planner.py v1.4
# Değişiklikler v1.3'e göre:
#   FIX — _parse_action içinde action string normalize ediliyor: strip().upper()
#          LLM bazen "write_file" / "Write_File" üretebilir — artık yakalanıyor.

import logging
from pydantic import ValidationError
from models import AgentPlan, ActionType
from ollama_client import OllamaClient
from prompts import build_prompt
from config import DEFAULT_MODEL

logger = logging.getLogger("planner")


class PlannerError(Exception):
    """Planner katmanından gelen denetlenebilir hata."""
    pass


class Planner:
    def __init__(self, model: str = DEFAULT_MODEL):
        self.client = OllamaClient(model=model)

    def build_plan(self, user_text: str) -> AgentPlan:
        # 1) LLM çağrısı
        try:
            raw_dict = self.client.call_model(build_prompt(user_text))
        except Exception as e:
            logger.error(f"LLM iletişim hatası | {e}")
            raise PlannerError(f"LLM yanıt vermedi: {e}") from e

        # 2) Temel yapı kontrolü
        if not isinstance(raw_dict, dict):
            logger.error(f"LLM dict dışı yanıt | tip={type(raw_dict)}")
            raise PlannerError("LLM geçersiz yapı döndürdü: dict bekleniyor.")

        # 3) Strict action doğrulaması — normalize edilmiş değerle
        for i, step in enumerate(raw_dict.get("steps", [])):
            step["action"] = self._parse_action(step.get("action", ""), i)

        # 4) Pydantic validation
        try:
            plan = AgentPlan(**raw_dict)
        except ValidationError as e:
            logger.error(f"AgentPlan validation hatası | {e}")
            raise PlannerError(f"Plan şema doğrulaması başarısız: {e}") from e

        logger.info(
            f"Plan oluşturuldu | risk={plan.risk_level} | "
            f"steps={len(plan.steps)} | forbidden={plan.forbidden_request_detected}"
        )
        return plan

    def _parse_action(self, action_raw: str, step_index: int) -> ActionType:
        if not isinstance(action_raw, str):
            raise PlannerError(
                f"Adım {step_index + 1}: action string olmalı, "
                f"gelen tip={type(action_raw).__name__}"
            )

        # FIX: strip().upper() — LLM küçük/karışık harf üretebilir
        candidate = action_raw.strip().upper()

        try:
            return ActionType(candidate)
        except ValueError as e:
            valid_values = [m.value for m in ActionType]
            logger.warning(f"Geçersiz action | step={step_index} | raw='{action_raw}'")
            raise PlannerError(
                f"Adım {step_index + 1}: Geçersiz action '{action_raw}'. "
                f"Geçerli: {valid_values}"
            ) from e
