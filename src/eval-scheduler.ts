import { DurableObject } from "cloudflare:workers";
import { EvalRunner } from "./eval/runner";

interface EvalEnv {
  EVAL_RUNNER: DurableObjectNamespace<EvaluationSchedule>;
  KV: KVNamespace;
}

export class EvaluationSchedule extends DurableObject<EvalEnv> {
  private readonly runner = new EvalRunner(this.ctx.storage, this.env.KV, (url, init) => fetch(url, init));

  wake() {
    return this.runner.wake();
  }

  alarm() {
    return this.runner.tick();
  }
}

export default {
  async scheduled(_controller, env) {
    await env.EVAL_RUNNER.getByName("production", { locationHint: "enam" }).wake();
  },
} satisfies ExportedHandler<EvalEnv>;
