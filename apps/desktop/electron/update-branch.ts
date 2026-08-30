/**
 * Resolve which git branch Desktop self-update should target.
 *
 * Desktop used to default to `main` whenever `updates.json` was missing, even
 * when `config.yaml` already named a different `updates.branch`. That made the
 * in-app Update button pass `--branch main` into `hermes update`, which
 * overrides YAML and can switch a fork checkout off its integration branch.
 *
 * Precedence: an explicit Desktop `updates.json` branch wins; otherwise the
 * install's `config.yaml` `updates.branch`; otherwise `main`.
 */

export const DEFAULT_UPDATE_BRANCH = 'main'

export function parseUpdatesBranchFromConfigYaml(text: string): string | null {
  const lines = text.split(/\r?\n/)
  let inUpdates = false
  let updatesIndent = -1

  for (const line of lines) {
    const indent = (line.match(/^[ \t]*/)?.[0] ?? '').length
    const body = line.slice(indent)
    const trimmed = body.trim()

    if (!trimmed || trimmed.startsWith('#')) {
      continue
    }

    if (!inUpdates) {
      if (indent === 0 && /^updates:\s*(?:#.*)?$/.test(body)) {
        inUpdates = true
        updatesIndent = indent
      }

      continue
    }

    if (indent <= updatesIndent) {
      inUpdates = false
      if (indent === 0 && /^updates:\s*(?:#.*)?$/.test(body)) {
        inUpdates = true
        updatesIndent = indent
      }

      continue
    }

    const match = trimmed.match(/^branch:\s*(?:["']([^"']+)["']|([^#\s]+))\s*(?:#.*)?$/)

    if (match) {
      const value = (match[1] || match[2] || '').trim()

      return value || null
    }
  }

  return null
}

export function resolveDesktopUpdateBranch(opts: {
  configYamlBranch?: string | null
  defaultBranch?: string
  desktopConfigBranch?: string | null
}): string {
  const desktop = (opts.desktopConfigBranch ?? '').trim()

  if (desktop) {
    return desktop
  }

  const yaml = (opts.configYamlBranch ?? '').trim()

  if (yaml) {
    return yaml
  }

  return (opts.defaultBranch ?? DEFAULT_UPDATE_BRANCH).trim() || DEFAULT_UPDATE_BRANCH
}
