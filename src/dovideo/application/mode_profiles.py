"""Authoritative deterministic prompt and output contracts for each mode."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from dovideo.domain import AnalysisMode, ModeProfile


_MODE_PROFILES: Mapping[AnalysisMode, ModeProfile] = MappingProxyType(
    {
        AnalysisMode.GENERAL: ModeProfile(
            mode=AnalysisMode.GENERAL,
            display_name="通用分析",
        ),
        AnalysisMode.LEARNING: ModeProfile(
            mode=AnalysisMode.LEARNING,
            display_name="学习分析",
            plan_instruction=(
                "围绕用户的学习目标制定可核验步骤，优先提取核心知识点与解释、"
                "概念之间的前置和递进关系、难点或易混淆点，以及学习路径和复习要点；"
                "每项任务都必须能由 VideoContext 中的 ASR、OCR 或时间戳证据支持。"
            ),
            execute_instruction=(
                "将结果组织为学习型产物。sections 必须使用 knowledge_outline、"
                "difficult_points、review_questions 这三个 key，并为每个 key 提供"
                "至少一条具体、相关且来自 VideoContext 的内容：知识点大纲及概念关系；"
                "难点或易混淆点及有依据的辨析；可由材料回答的复习或自测题。"
                "缺乏证据时明确说明限制，不得臆造或以空泛占位满足要求。"
            ),
            critic_instruction=(
                "除通用目标覆盖与证据核验外，明确核对知识点解释、概念关系和难点是否"
                "符合源材料。逐项检查 knowledge_outline、difficult_points、"
                "review_questions 是否都含具体、相关、非空且有材料依据的内容；"
                "缺项、空白或无依据的占位内容必须令 passed=false 并说明缺口。"
            ),
            required_section_keys=(
                "knowledge_outline",
                "difficult_points",
                "review_questions",
            ),
        ),
        AnalysisMode.REVIEW: ModeProfile(
            mode=AnalysisMode.REVIEW,
            display_name="审查分析",
            plan_instruction=(
                "围绕用户指定的审查对象安排证据核验步骤，区分有依据的优点与不足，"
                "优先检查逻辑问题、夸大或无依据的表述、遗漏、取舍和风险，并提出"
                "适当且可执行的改进点。每项事实判断都必须能由 VideoContext 核验；"
                "不要给出脱离材料的主观评分。"
            ),
            execute_instruction=(
                "将结果组织为审查型产物。sections 必须使用 strengths、"
                "issues_and_risks、omissions、improvements 这四个 key，并为每个 key"
                "提供具体、相关且有材料依据的内容：优点；逻辑问题、夸大表述或风险；"
                "重要遗漏；适当的可执行改进建议。区分视频事实与推断，不得臆造或用"
                "空泛占位填充段落。"
            ),
            critic_instruction=(
                "除通用目标覆盖与证据核验外，检查审查对象、逻辑问题、夸大表述、"
                "遗漏和改进意见是否与源材料相符，不要要求主观评分。逐项检查 strengths、"
                "issues_and_risks、omissions、improvements 是否都有具体、相关、非空且"
                "有依据的内容；缺项、空白或无依据的占位内容必须令 passed=false 并说明缺口。"
            ),
            required_section_keys=(
                "strengths",
                "issues_and_risks",
                "omissions",
                "improvements",
            ),
        ),
        AnalysisMode.CREATION: ModeProfile(
            mode=AnalysisMode.CREATION,
            display_name="创作素材分析",
            plan_instruction=(
                "把源视频作为后续创作的素材，安排可核验步骤以发现可复用观点、叙事或"
                "内容结构、关键片段、吸引点或切入角度、改编机会，以及支持这些建议的"
                "原始时间点。区分视频中实际发生的内容与创作提案；所有事实依据必须来自"
                "VideoContext，不要执行发布或自动化创作流程。"
            ),
            execute_instruction=(
                "将结果组织为创作素材分析，而不是代为发布。sections 必须使用"
                "key_moments、hooks_and_titles、script_outline、adaptation_ideas 这四个"
                "key，并为每个 key 提供具体、相关的内容：带源时间点的可复用片段；有源材料"
                "支撑的吸引点或标题角度；可供后续改写的口播/叙事提纲；适合延展的改编方向。"
                "明确区分源事实与创作提案，不得伪造素材或证据。"
            ),
            critic_instruction=(
                "除通用目标覆盖与证据核验外，核对关键片段是否对应真实源时间点，标题、"
                "切入角度和口播提纲中的事实是否有材料支持，并确认改编建议被明确标为提案。"
                "逐项检查 key_moments、hooks_and_titles、script_outline、adaptation_ideas"
                "是否都有具体、相关、非空的内容；源事实须有依据，不能用空白或无依据占位。"
                "缺项或违反上述边界必须令 passed=false 并说明缺口。"
            ),
            required_section_keys=(
                "key_moments",
                "hooks_and_titles",
                "script_outline",
                "adaptation_ideas",
            ),
        ),
    }
)


def mode_profile_for(mode: AnalysisMode) -> ModeProfile:
    """Return the one immutable production profile assigned to ``mode``."""

    if not isinstance(mode, AnalysisMode):
        raise TypeError("mode must be an AnalysisMode")
    return _MODE_PROFILES[mode]


__all__ = ["mode_profile_for"]
