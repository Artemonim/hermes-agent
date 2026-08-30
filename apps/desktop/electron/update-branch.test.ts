import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  DEFAULT_UPDATE_BRANCH,
  parseUpdatesBranchFromConfigYaml,
  resolveDesktopUpdateBranch
} from './update-branch'

test('parseUpdatesBranchFromConfigYaml reads the updates mapping branch', () => {
  const yaml = ['model:', '  default: x', 'updates:', '  branch: dev', '  backup_keep: 5', ''].join('\n')

  assert.equal(parseUpdatesBranchFromConfigYaml(yaml), 'dev')
})

test('parseUpdatesBranchFromConfigYaml accepts a quoted branch', () => {
  assert.equal(parseUpdatesBranchFromConfigYaml('updates:\n  branch: "release/1.2"\n'), 'release/1.2')
})

test('parseUpdatesBranchFromConfigYaml ignores commented updates blocks', () => {
  const yaml = ['# updates:', '#   branch: main', 'gateway:', '  port: 1', 'updates:', '  branch: dev', ''].join('\n')

  assert.equal(parseUpdatesBranchFromConfigYaml(yaml), 'dev')
})

test('parseUpdatesBranchFromConfigYaml does not pick a branch from another section', () => {
  assert.equal(parseUpdatesBranchFromConfigYaml('profiles:\n  branch: other\n'), null)
})

test('parseUpdatesBranchFromConfigYaml returns null when updates has no branch', () => {
  assert.equal(parseUpdatesBranchFromConfigYaml('updates:\n  backup_keep: 5\n'), null)
})

test('resolveDesktopUpdateBranch prefers an explicit Desktop updates.json branch', () => {
  assert.equal(
    resolveDesktopUpdateBranch({
      desktopConfigBranch: 'main',
      configYamlBranch: 'dev'
    }),
    'main'
  )
})

test('resolveDesktopUpdateBranch falls back to config.yaml when Desktop has no branch', () => {
  assert.equal(
    resolveDesktopUpdateBranch({
      desktopConfigBranch: '  ',
      configYamlBranch: 'dev'
    }),
    'dev'
  )
})

test('resolveDesktopUpdateBranch uses main when neither source names a branch', () => {
  assert.equal(resolveDesktopUpdateBranch({}), DEFAULT_UPDATE_BRANCH)
})
