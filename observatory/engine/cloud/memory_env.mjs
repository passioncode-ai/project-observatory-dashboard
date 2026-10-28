// An in-memory stand-in for the Worker's Durable Object namespace, for the tests and the loopback server only: one
// ShareBox per share id, its storage a Map that copies what it holds (as Durable Object storage does), its requests
// one at a time (as `blockConcurrencyWhile` keeps them). Never deployed.
import { ShareBox } from "./worker.mjs";

export class MemoryStorage {
  /** `maxValueBytes`: refuse a value larger than this, as a Durable Object refuses one past its limit. */
  constructor({ maxValueBytes = Infinity } = {}) { this.map = new Map(); this.alarmAt = null; this.maxValueBytes = maxValueBytes; }
  async get(key) { const v = this.map.get(key); return v === undefined ? undefined : structuredClone(v); }
  async put(key, value) {
    if (JSON.stringify(value).length > this.maxValueBytes) throw new RangeError("value too large");
    this.map.set(key, structuredClone(value));
  }
  async delete(key) { return this.map.delete(key); }
  async deleteAll() { this.map.clear(); this.alarmAt = null; }
  /** As a Durable Object lists: keys in sorted order, `end` exclusive, at most `limit`. */
  async list({ prefix = "", start = "", end = null, limit = Infinity } = {}) {
    return new Map([...this.map].filter(([k]) => k.startsWith(prefix) && k >= start && (end === null || k < end))
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).slice(0, limit).map(([k, v]) => [k, structuredClone(v)]));
  }
  async setAlarm(ms) { this.alarmAt = ms; }
  async getAlarm() { return this.alarmAt; }
}

class MemoryContext {
  constructor() { this.storage = new MemoryStorage(); this.queue = Promise.resolve(); }
  blockConcurrencyWhile(fn) {
    const run = this.queue.then(fn, fn);
    this.queue = run.catch(() => {});
    return run;
  }
}

export class MemoryNamespace {
  constructor(env) { this.env = env; this.boxes = new Map(); }
  idFromName(name) { return name; }
  get(id) {
    if (!this.boxes.has(id)) this.boxes.set(id, new ShareBox(new MemoryContext(), this.env));
    return this.boxes.get(id);
  }
}

/** An env as the deployed Worker sees it: the namespace, the owner's key, the digest. */
export function memoryEnv({ ownerJwk = null, digest = null, openCreation = false } = {}) {
  const env = { OWNER_JWK: ownerJwk ? JSON.stringify(ownerJwk) : undefined, WORKER_DIGEST: digest,
    OPEN_CREATION: openCreation ? "loopback-trial" : undefined };
  env.SHARES = new MemoryNamespace(env);
  return env;
}
