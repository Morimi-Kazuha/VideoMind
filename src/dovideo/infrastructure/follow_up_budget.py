"""OFF by default; no invented production token threshold."""
import os
from dovideo.application.follow_up_budget import FollowUpBudgetMode, FollowUpBudgetSettings


def follow_up_budget_settings_from_environment(environ=None):
    values = os.environ if environ is None else environ
    try:
        mode = FollowUpBudgetMode(values.get("DOVIDEO_FOLLOW_UP_BUDGET_MODE", "OFF").strip().upper())
        raw = values.get("DOVIDEO_FOLLOW_UP_MAX_CHAT_TOKENS")
        limit = None if raw is None else int(raw)
        if raw is not None and (not isinstance(raw, str) or not raw.strip().isdigit()):
            raise ValueError("invalid token limit")
        return FollowUpBudgetSettings(mode, limit)
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid Follow-up budget settings; ENFORCED requires a positive explicit token limit") from None
