# Debate Scenario - 观点论证推理 (viewpoint argumentation reasoning)
# 正方 (pro) 与 反方 (con) execution agents 对抗辩论，裁判综合裁决。
from typing import Dict, Any

from scenarios.base_scenario import BaseScenario
from scenarios.examples.debate_presets import resolve_preset
from agents.agent_manager import agent_manager
from core.event_bus import event_bus
from logger import logger


# 默认系统提示词 — 可被 config 覆盖
PRO_PROMPT = (
    "你是正方辩手。针对给定辩题，你必须站在【支持/肯定】的立场，"
    "提出清晰、有逻辑、有依据的论证。结构：1) 核心论点 2) 论据(至少两条) 3) 结论。"
    "语言简练有力，不超过400字。"
)
CON_PROMPT = (
    "你是反方辩手。针对给定辩题，你必须站在【反对/否定】的立场，"
    "提出清晰、有逻辑、有依据的论证。结构：1) 核心论点 2) 论据(至少两条) 3) 结论。"
    "语言简练有力，不超过400字。"
)
JUDGE_PROMPT = (
    "你是中立裁判。基于正反双方的论证，客观评估哪一方论证更严密、更有说服力，"
    "给出最终结论与理由。结构：1) 正方摘要 2) 反方摘要 3) 评判 4) 最终结论。"
)


class DebateScenario(BaseScenario):
    """
    观点论证推理：正方与反方执行 agent 对抗辩论，裁判综合裁决。

    Workflow:
    1. 从 config (或 config_id 预设) 读取辩题与轮次
    2. 正方 / 反方 execution agent 并行提交，各自论证
    3. 可选多轮：每轮看到对方上一轮观点进行反驳
    4. 裁判 execution agent 综合双方论证给出裁决
    """

    def get_scenario_type(self) -> str:
        return "debate"

    def initialize(self, config: Dict[str, Any]) -> None:
        # 若指定 config_id (如 341c3477-...)，解析对应预设作为默认
        config_id = config.get("config_id")
        if config_id:
            config = {**resolve_preset(config_id), **config}

        self.topic = config.get("topic") or config.get("proposition", "")
        if not self.topic:
            raise ValueError("Debate scenario requires 'topic' (the proposition to argue)")

        self.rounds = max(1, int(config.get("rounds", 1)))
        self.timeout = int(config.get("timeout", 180))
        self.pro_prompt = config.get("pro_system_prompt", PRO_PROMPT)
        self.con_prompt = config.get("con_system_prompt", CON_PROMPT)
        self.judge_prompt = config.get("judge_system_prompt", JUDGE_PROMPT)

        logger.info(f"DebateScenario initialized: topic={self.topic!r} rounds={self.rounds}")

    def run(self) -> Dict[str, Any]:
        sid = self.context.scenario_id
        event_bus.emit("scenario.debate_started", {
            "scenario_id": sid,
            "topic": self.topic,
            "rounds": self.rounds,
        })

        try:
            pro_arg = ""
            con_arg = ""

            for r in range(self.rounds):
                round_label = f"第{r + 1}轮"

                # 正反双方并行提交 (异步执行，再分别等待)
                pro_task = self._submit_side("正方", self.pro_prompt, con_arg, round_label)
                con_task = self._submit_side("反方", self.con_prompt, pro_arg, round_label)

                pro_res = self.wait_for_task(pro_task, timeout=self.timeout)
                con_res = self.wait_for_task(con_task, timeout=self.timeout)

                pro_arg = self._extract_output(pro_res, "正方")
                con_arg = self._extract_output(con_res, "反方")

                logger.info(f"Debate {sid} {round_label} done: pro={len(pro_arg)}c con={len(con_arg)}c")

            # 裁判综合裁决
            judge_task = agent_manager.submit_task(
                goal=self._judge_goal(pro_arg, con_arg),
                agent_type="execution",
                scenario_id=sid,
                timeout_seconds=self.timeout,
                context={"role": "裁判", "system_prompt": self.judge_prompt},
            )
            judge_res = self.wait_for_task(judge_task, timeout=self.timeout)
            verdict = self._extract_output(judge_res, "裁判")

            event_bus.emit("scenario.debate_completed", {
                "scenario_id": sid,
                "task_state": judge_res["state"],
            })

            return {
                "success": judge_res["state"] == "success",
                "topic": self.topic,
                "rounds": self.rounds,
                "pro_argument": pro_arg,
                "con_argument": con_arg,
                "verdict": verdict,
            }

        except Exception as e:
            err = str(e)
            logger.error(f"Debate scenario error: {err}")
            event_bus.emit("scenario.debate_failed", {"scenario_id": sid, "error": err})
            return {"success": False, "error": err}

    def _submit_side(self, side: str, system_prompt: str,
                     opponent_prior: str, round_label: str) -> str:
        """Submit one side's argument task. opponent_prior enables rebuttal."""
        goal = f"辩题：{self.topic}\n请作为{side}进行论证。"
        if opponent_prior:
            goal += f"\n\n对方{round_label}观点：\n{opponent_prior}\n请在此基础上反驳。"
        return agent_manager.submit_task(
            goal=goal,
            agent_type="execution",
            scenario_id=self.context.scenario_id,
            timeout_seconds=self.timeout,
            context={"role": side, "system_prompt": system_prompt},
        )

    def _judge_goal(self, pro: str, con: str) -> str:
        return (f"辩题：{self.topic}\n\n"
                f"【正方论证】\n{pro}\n\n"
                f"【反方论证】\n{con}\n\n"
                f"请作出裁决。")

    @staticmethod
    def _extract_output(task_result: Dict[str, Any], side: str) -> str:
        """Pull the LLM output from a wait_for_task result, or an error marker."""
        if task_result["state"] == "success":
            result = task_result.get("result") or {}
            return result.get("output", "")
        return f"[{side}失败: {task_result.get('error')}]"

    def cleanup(self) -> None:
        logger.info("DebateScenario cleaned up")
