import z from "@deepseek-ai/schemastery";
//#region src/index.ts
const name = "agent-sidecar";
/**
* Empty for the smoke stage: the hello plugin consumes no services, so it
* must not gate its activation on any (ctx.logger is a cordis built-in).
*/
const inject = [];
const Config = z.object({});
/**
* Hello-slot smoke: prove the host half loads inside the dsh process.
* @param ctx - plugin context handed by the cordis loader.
* @param _config - schema-validated composition config (unused at this stage).
*/
function apply(ctx, _config) {
	ctx.logger.info("agent-sidecar: host half loaded (hello-slot smoke)");
}
//#endregion
export { Config, apply, inject, name };
