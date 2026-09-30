import test from 'node:test'
import assert from 'node:assert/strict'
import { uiPhaseForStage } from './uiStage.js'

test('domain stage adapter preserves last stable UI phase for unknown and terminal stages', () => {
  assert.equal(uiPhaseForStage('PLAN_COMPLETED'), 'PLANNER')
  assert.equal(uiPhaseForStage('EXECUTOR_STARTED'), 'EXECUTOR')
  assert.equal(uiPhaseForStage('CRITIC_RETRY_REQUIRED'), 'CRITIC')
  assert.equal(uiPhaseForStage('NEW_INTERNAL_STAGE', 'CRITIC'), 'CRITIC')
  assert.equal(uiPhaseForStage('COMPLETED', 'CRITIC'), 'CRITIC')
  assert.equal(uiPhaseForStage('constructor', 'CRITIC'), 'CRITIC')
  assert.equal(uiPhaseForStage('__proto__', 'CRITIC'), 'CRITIC')
})
