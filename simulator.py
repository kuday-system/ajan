import json
from models import AgentPlan, LockedPlan, SimulationResult


class Simulator:
    def run(self, locked_plan: LockedPlan) -> SimulationResult:
        # Güvenilir kaynak: canonical JSON, canlı objeye güvenilmez
        plan_dict = json.loads(locked_plan.canonical_plan_json)
        plan = AgentPlan(**plan_dict)

        outputs = []
        for step in plan.steps:
            outputs.append(
                f"[SİMÜLASYON] Adım {step.step_no}: '{step.action.value}' "
                f"→ Hedef: '{step.target}' | Sebep: '{step.reason}'"
            )

        return SimulationResult(
            status="simulated",
            summary=f"Plan simüle edildi. {len(plan.steps)} adım işlendi. Gerçek işlem yapılmadı.",
            simulated_outputs=outputs
        )