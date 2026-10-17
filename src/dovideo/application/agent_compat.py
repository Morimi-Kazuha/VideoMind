"""Legacy constructor spelling and positional compatibility, with stable precedence."""

def normalize_agent_arguments(
    *,
    agent_execution_budget,
    budget,
    budget_config,
    budget_usage,
    budget_usage_source,
    checkpoint,
    checkpoint_service,
    context_service,
    critic,
    critic_port,
    durable_tool_checkpoint,
    event_publisher,
    events,
    execution_budget,
    execution_record,
    execution_record_service,
    execution_recorder,
    executor,
    executor_port,
    executor_turn,
    executor_turn_port,
    long_context,
    long_context_service,
    long_video_context_service,
    max_tool_requests_per_round,
    max_tool_requests_total,
    monotonic,
    monotonic_clock,
    planner,
    planner_port,
    task_event_publisher,
    tool_call_checkpoint,
    tool_call_checkpoint_port,
    tool_checkpoint,
    tool_checkpoint_port,
    tool_executor,
    tool_executor_port,
    tool_request_limit_per_round,
    tool_request_limit_total,
    usage,
    usage_port,
    usage_source,
    usage_tracker,
):
    context_service = (
        context_service
        if context_service is not None
        else long_context_service
        if long_context_service is not None
        else long_video_context_service
        if long_video_context_service is not None
        else long_context
    )
    planner = planner if planner is not None else planner_port
    executor = executor if executor is not None else executor_port
    checkpoint = checkpoint if checkpoint is not None else checkpoint_service
    event_publisher = (
        event_publisher
        if event_publisher is not None
        else task_event_publisher
        if task_event_publisher is not None
        else events
    )
    critic = critic if critic is not None else critic_port
    if budget_config is None:
        budget_config = budget
    if execution_budget is None:
        execution_budget = agent_execution_budget
    if usage_source is None:
        usage_source = (
            budget_usage
            if budget_usage is not None
            else usage
            if usage is not None
            else budget_usage_source
            if budget_usage_source is not None
            else usage_port
            if usage_port is not None
            else usage_tracker
        )
    if monotonic is None:
        monotonic = monotonic_clock

    execution_record_service = (
        execution_record_service
        if execution_record_service is not None
        else execution_recorder
        if execution_recorder is not None
        else execution_record
    )

    executor_turn = (
        executor_turn if executor_turn is not None else executor_turn_port
    )
    tool_executor = (
        tool_executor if tool_executor is not None else tool_executor_port
    )
    tool_checkpoint = (
        tool_checkpoint
        if tool_checkpoint is not None
        else tool_checkpoint_port
        if tool_checkpoint_port is not None
        else tool_call_checkpoint
        if tool_call_checkpoint is not None
        else tool_call_checkpoint_port
        if tool_call_checkpoint_port is not None
        else durable_tool_checkpoint
    )
    if max_tool_requests_per_round is not None:
        tool_request_limit_per_round = max_tool_requests_per_round
    if max_tool_requests_total is not None:
        tool_request_limit_total = max_tool_requests_total

    # Accommodate the equally natural role-first positional composition:
    # AgentLoopService(planner, executor, context_service, ...).
    if (
        context_service is not None
        and planner is not None
        and executor is not None
        and hasattr(context_service, "plan")
        and hasattr(planner, "execute")
        and hasattr(executor, "select_relevant")
    ):
        context_service, planner, executor = executor, context_service, planner

    return (context_service, planner, executor, checkpoint, event_publisher, critic, execution_record_service, executor_turn, tool_executor, tool_checkpoint, budget_config, execution_budget, usage_source, monotonic, tool_request_limit_per_round, tool_request_limit_total)
