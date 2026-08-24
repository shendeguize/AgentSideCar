/**
 * Agent Sidecar — dsh host-half plugin (hello-slot smoke skeleton).
 *
 * Named exports only: postmortem 0001 documents that a default-exported
 * plugin object silently drops `inject`, so the loader must see the named
 * `name`/`inject`/`Config`/`apply` faces directly on the module namespace.
 *
 * This stage deliberately registers nothing beyond a load-proof log line;
 * supervisor/bridge/routes land in M1 and will grow `inject` to the real
 * minimal service set (webServer, subprocess, agents, settings).
 *
 * @module @shendeguize/dsh-agent-sidecar
 */

import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'

export const name = 'agent-sidecar'

/**
 * Empty for the smoke stage: the hello plugin consumes no services, so it
 * must not gate its activation on any (ctx.logger is a cordis built-in).
 */
export const inject: string[] = []

/** Composition config (schemastery-validated; all defaults, zero-config mount). */
export interface Config {}

export const Config: z<Config> = z.object({})

/**
 * Hello-slot smoke: prove the host half loads inside the dsh process.
 * @param ctx - plugin context handed by the cordis loader.
 * @param _config - schema-validated composition config (unused at this stage).
 */
export function apply(ctx: Context, _config: Config): void {
  ctx.logger.info('agent-sidecar: host half loaded (hello-slot smoke)')
}
