import { apiRequest } from './api.js'

export function createWorkspaceMetadata(sidebar, captureWorkspace, nextGeneration, generation) {
  const refreshAgentMeta = async (
    id,
    goal,
    includeEvaluation,
    analysisMode = sidebar.value.analysisMode || 'GENERAL',
  ) => {
    const current = captureWorkspace()
    const request = nextGeneration()
    const params = new URLSearchParams({
      id: String(id),
      goal,
      mode: analysisMode,
    })
    const requests = [
      apiRequest(`/analysis/agent-plan?${params}`),
      apiRequest(`/analysis/agent-trace?${params}`),
    ]
    if (includeEvaluation)
      requests.push(apiRequest(`/analysis/agent-evaluation?${params}`))

    const settled = await Promise.allSettled(requests)
    if (!current() || request !== generation()) return
    const [plan, trace, evaluation] = await Promise.all(
      settled.map(readSettledJson),
    )
    if (!current() || request !== generation()) return
    if (plan && !sidebar.value.editingPlan) sidebar.value.plan = plan
    if (trace) sidebar.value.trace = trace
    if (includeEvaluation && evaluation) sidebar.value.evaluation = evaluation
  }

  return { refreshAgentMeta }
}

async function readSettledJson(result) {
  if (result.status !== 'fulfilled' || !result.value.ok) return null
  try {
    return await result.value.json()
  } catch (error) {
    console.warn('Agent metadata response is invalid', error)
    return null
  }
}

