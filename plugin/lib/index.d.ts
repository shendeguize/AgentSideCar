import z from "@deepseek-ai/schemastery";
import { Context } from "@deepseek-ai/cordis";

//#region src/index.d.ts
declare const name = "agent-sidecar";
/**
 * Empty for the smoke stage: the hello plugin consumes no services, so it
 * must not gate its activation on any (ctx.logger is a cordis built-in).
 */
declare const inject: string[];
/** Composition config (schemastery-validated; all defaults, zero-config mount). */
interface Config {}
declare const Config: z<Config>;
/**
 * Hello-slot smoke: prove the host half loads inside the dsh process.
 * @param ctx - plugin context handed by the cordis loader.
 * @param _config - schema-validated composition config (unused at this stage).
 */
declare function apply(ctx: Context, _config: Config): void;
//#endregion
export { Config, apply, inject, name };