// The share mailbox, `observatory-cloud/1`, as a Cloudflare Worker with one Durable Object per share
// (contract DEC-0142 PS-10; OBS-263; the routes and the name are Project Observatory's, CT-185).
//
// The specification is the engine's in-process mailbox, `node/mailbox.py` (`FakeMailbox`): the same routes, the same
// answers, the same refusals. `tests/test_cloud_worker.py` runs the engine's two share stores against this file over
// HTTP, and `cloud/worker.test.mjs` runs the contract's vectors through its ports.
//
// What it does, and nothing more (PS-10): it carries frames, envelopes, reports and records unchanged and holds
// revocations; it signs nothing, opens nothing and decides nothing about a value or about whom the sender trusts. It
// runs `peerExchangeVerdict` over a share's frames until the first envelope is stored, carries a burning frame with its
// reason, then stores no envelope and grants no lease for a burnt share; after sealing it carries a later claim or
// abort to the sender only; it grants an honest recipient a lease of at most 24 hours and counts each delivery; it
// refuses every renewal and delivery after a revocation. It sees ciphertext and names. It logs nothing it is sent.
//
// Requests are signed with DEC-0056's construction (RFC 9421: `@method`, `@target-uri`, `content-digest`; `created`,
// `nonce`, `keyid`, `alg`) under the label `fabric-peer-share`, keyid the RFC 7638 thumbprint of the signing share
// #sig, `ecdsa-p256-sha256`; a share's creation also under `observatory-owner` by the owner key `full cloud deploy`
// binds as OWNER_JWK. Each accepted request other than a read keeps its nonce under its own key
// (`nonce:<created>:...`) until its signature is too old to be accepted again, when it is purged; a refused request or a
// read stores none, so neither the share's record nor its keys grow from them.

export const PROTOCOL = "observatory-cloud/1";
export const PEER_SHARE_PROTOCOL = "fabric-peer-share/1";
export const VERSION = "1.3.0";
const LABEL = "fabric-peer-share";
const OWNER_LABEL = "observatory-owner";
const ALG = "ecdsa-p256-sha256";
const WINDOW_SECONDS = 300;
const MAX_BODY_BYTES = 262_144;
const DEFAULT_LEASE_SECONDS = 3600;
const MAX_LEASE_SECONDS = 86_400;
const MAX_CLAIM_DAYS = 7;
const MAX_ENVELOPE_DAYS = 90;
const DAY_MS = 86_400_000;
const MAX_FRAMES = 16;
const MAX_REPORTS = 1000;
const REQUESTS_PER_MINUTE = 120;
const FRAME_SCHEMA = "peer-share-frame/1";
const COMMIT_TYPE = "fabric-peer-commit/1";
const ENVELOPE_TYPE = "fabric-peer-share-envelope/1";
const REPORT_TYPE = "fabric-peer-share-report/1";
const REVOCATION_TYPE = "peer-share-revocation/1";
const DELETION_TYPE = "peer-share-deletion/1";

const SHARE_ID = /^[0-9a-f]{32}$/;
const BYTES32 = /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/;
const B64URL = /^[A-Za-z0-9_-]+$/;
const SIG = /^[A-Za-z0-9_-]{86}$/;
const SLOT = /^[A-Z][A-Z0-9_]{0,63}$/;
const PROVIDER = /^[a-z][a-z0-9-]{1,62}$/;
const COMMIT = /^sha256:[0-9a-f]{64}$/;
const NONCE = /^[A-Za-z0-9_-]{22,64}$/;
const INSTANT = /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,9})?Z$/;

// ---- Encodings ------------------------------------------------------------------------------------------------------

const enc = new TextEncoder();
const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const isString = (v) => typeof v === "string";
const exactly = (v, members) => Object.keys(v).length === members.length && members.every((m) => Object.hasOwn(v, m));
const counter = (v) => Number.isSafeInteger(v) && v >= 1;

export function b64urlEncode(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function b64urlDecode(text) {
  const s = text.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(s + "=".repeat((4 - (s.length % 4)) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

const b64 = (bytes) => { let s = ""; for (const b of bytes) s += String.fromCharCode(b); return btoa(s); };

/** RFC 8785 for the JSON this mailbox reads: members sorted by UTF-16 code units, numbers as ECMAScript writes them. */
export function rfc8785(value) {
  if (value === null || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError("no canonical form");
    return JSON.stringify(value);
  }
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(rfc8785).join(",") + "]";
  if (isObject(value)) {
    return "{" + Object.keys(value).sort().map((k) => JSON.stringify(k) + ":" + rfc8785(value[k])).join(",") + "}";
  }
  throw new TypeError("no canonical form");
}

async function sha256(bytes) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", typeof bytes === "string" ? enc.encode(bytes) : bytes));
}

const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");

// ---- Times ----------------------------------------------------------------------------------------------------------

function daysFromCivil(y, m, d) {
  y -= m <= 2 ? 1 : 0;
  const era = Math.floor(y / 400);
  const yoe = y - era * 400;
  const doy = Math.floor((153 * (m + (m > 2 ? -3 : 9)) + 2) / 5) + d - 1;
  const doe = yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy;
  return era * 146097 + doe - 719468;
}

/** The milliseconds of an RFC 3339 UTC instant on a day its month has, or undefined (the relay family's `instant`). */
export function instant(value) {
  const m = isString(value) ? INSTANT.exec(value) : null;
  if (!m) return undefined;
  const [y, mo, d, h, mi, s] = m.slice(1, 7).map(Number);
  const leap = y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1];
  if (!(mo >= 1 && mo <= 12) || d < 1 || d > days || h > 23 || mi > 59 || s > 59) return undefined;
  const ms = m[7] ? Number((m[7].slice(1) + "00").slice(0, 3)) : 0;
  return (daysFromCivil(y, mo, d) * 86400 + h * 3600 + mi * 60 + s) * 1000 + ms;
}

export const rfc3339 = (ms) => new Date(Math.floor(ms / 1000) * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");

// ---- P-256 keys and ES256 -------------------------------------------------------------------------------------------

const P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffffn;
const A = P - 3n;
const B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604bn;
const HALF_N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551n / 2n;
const big = (bytes) => bytes.reduce((acc, b) => (acc << 8n) | BigInt(b), 0n);

function onCurve(jwk) {
  const x = big(b64urlDecode(jwk.x)), y = big(b64urlDecode(jwk.y));
  if (x >= P || y >= P) return false;
  return (y * y) % P === (((x * x) % P) * x + A * x + B) % P;
}

/** A P-256 public JWK with exactly kty, crv, x and y, its point on the curve. */
export function peerJwk(v) {
  return isObject(v) && exactly(v, ["kty", "crv", "x", "y"]) && v.kty === "EC" && v.crv === "P-256"
    && isString(v.x) && BYTES32.test(v.x) && isString(v.y) && BYTES32.test(v.y) && onCurve(v);
}

/** The RFC 7638 SHA-256 thumbprint of a share key, or undefined. */
export async function peerThumbprint(jwk) {
  if (!peerJwk(jwk)) return undefined;
  return b64urlEncode(await sha256(rfc8785({ crv: jwk.crv, kty: jwk.kty, x: jwk.x, y: jwk.y })));
}

export function peerKeys(v) {
  return isObject(v) && exactly(v, ["sig", "kem"]) && peerJwk(v.sig) && peerJwk(v.kem)
    && !(v.sig.x === v.kem.x && v.sig.y === v.kem.y);
}

/** ES256 (raw r ‖ s, base64url, low-S only) by a share key over `data`. */
export async function verifyEs256(jwk, data, sig) {
  if (!peerJwk(jwk) || !isString(sig) || !SIG.test(sig)) return false;
  const raw = b64urlDecode(sig);
  if (raw.length !== 64 || b64urlEncode(raw) !== sig || big(raw.slice(32)) > HALF_N) return false;
  try {
    const key = await crypto.subtle.importKey("jwk", { kty: "EC", crv: "P-256", x: jwk.x, y: jwk.y, ext: true },
      { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
    return await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, raw,
      typeof data === "string" ? enc.encode(data) : data);
  } catch {
    return false;
  }
}

// ---- Frames and the one attempt per link (PS-6) -------------------------------------------------------------------

const FRAME_MEMBERS = {
  claim: ["schema", "step", "shareId", "keys", "mac", "commit"],
  commit: ["schema", "step", "shareId", "keys", "commit"],
  reveal: ["schema", "step", "role", "shareId", "nonce"],
  abort: ["schema", "step", "role", "shareId"],
};

export function peerFrame(v) {
  if (!isObject(v) || v.schema !== FRAME_SCHEMA || !isString(v.step)) return undefined;
  const members = Object.hasOwn(FRAME_MEMBERS, v.step) ? FRAME_MEMBERS[v.step] : undefined;
  if (members === undefined || !exactly(v, members) || !isString(v.shareId) || !SHARE_ID.test(v.shareId)) return undefined;
  if ((v.step === "claim" || v.step === "commit") && (!peerKeys(v.keys) || !isString(v.commit) || !COMMIT.test(v.commit))) return undefined;
  if (v.step === "claim" && (!isString(v.mac) || !BYTES32.test(v.mac))) return undefined;
  if ((v.step === "reveal" || v.step === "abort") && v.role !== "sender" && v.role !== "recipient") return undefined;
  if (v.step === "reveal" && (!isString(v.nonce) || !BYTES32.test(v.nonce))) return undefined;
  return v;
}

async function opens(commit, role, shareId, keys, nonce) {
  const text = rfc8785({ type: COMMIT_TYPE, shareId, role, keys, nonce });
  return `sha256:${hex(await sha256(text))}` === commit;
}

/** The contract's `peerExchangeVerdict`, as the mailbox runs it over a share's frames in the order received. */
export async function peerExchangeVerdict(shareId, claimDeadline, frames, at) {
  const deadline = instant(claimDeadline), now = instant(at);
  if (!isString(shareId) || !SHARE_ID.test(shareId) || deadline === undefined || now === undefined || !Array.isArray(frames)) {
    return { state: "burnt", reason: "malformed", index: -1 };
  }
  let claim, commit, complete = false, aborted = false;
  const revealed = new Set();
  for (const [index, value] of frames.entries()) {
    const burn = (reason) => ({ state: "burnt", reason, index });
    const frame = peerFrame(value);
    if (frame === undefined || frame.shareId !== shareId) return burn("malformed");
    if (aborted) return burn("closed");
    if (complete) {
      if (frame.step === "abort") { aborted = true; continue; }
      return burn("closed");
    }
    if (frame.step === "claim") {
      if (claim !== undefined) return burn("repeated");
      claim = frame;
    } else if (frame.step === "commit") {
      if (commit !== undefined) return burn("repeated");
      if (claim === undefined) return burn("out-of-order");
      commit = frame;
    } else if (frame.step === "reveal") {
      if (claim === undefined || commit === undefined) return burn("out-of-order");
      if (revealed.has(frame.role)) return burn("repeated");
      const source = frame.role === "sender" ? commit : claim;
      if (!(await opens(source.commit, frame.role, shareId, source.keys, frame.nonce))) return burn("commit-mismatch");
      revealed.add(frame.role);
      if (revealed.size === 2) complete = true;
    } else {
      return burn("aborted");
    }
  }
  if (aborted) return { state: "burnt", reason: "aborted", index: frames.length - 1 };
  if (complete) return { state: "complete" };
  return now > deadline ? { state: "burnt", reason: "expired", index: frames.length } : { state: "open" };
}

// ---- Sealed messages and records: their shape, and a record's signature -------------------------------------------

const HEADERS = {
  [ENVELOPE_TYPE]: ["type", "alg", "suite", "recipient", "signer", "shareId", "slot", "provider", "sequence", "iat", "exp", "enc"],
  [REPORT_TYPE]: ["type", "alg", "suite", "recipient", "signer", "shareId", "sequence", "iat", "enc"],
};

/** Whether a value has the shape of a sealed message of `type`: the mailbox reads the header's names, never the ciphertext. */
export function peerSealedShape(v, type) {
  if (!isObject(v) || !exactly(v, ["header", "ct", "sig"]) || !isObject(v.header)) return false;
  const h = v.header;
  if (h.type !== type || !exactly(h, HEADERS[type])) return false;
  const envelope = type === ENVELOPE_TYPE;
  return isString(h.alg) && [...h.alg].length <= 16 && JSON.stringify(h.suite) === "[16,1,1]"
    && isString(h.recipient) && BYTES32.test(h.recipient) && isString(h.signer) && BYTES32.test(h.signer)
    && isString(h.shareId) && SHARE_ID.test(h.shareId) && counter(h.sequence) && instant(h.iat) !== undefined
    && (!envelope || (isString(h.slot) && SLOT.test(h.slot) && isString(h.provider) && PROVIDER.test(h.provider) && instant(h.exp) !== undefined))
    && isString(h.enc) && B64URL.test(h.enc) && isString(v.ct) && B64URL.test(v.ct) && isString(v.sig) && SIG.test(v.sig);
}

/** The contract's `peerRecordStands`: a revocation or a deletion receipt, signed by the share #sig named. */
export async function peerRecordStands(record, type, shareId, signerSig) {
  const members = type === REVOCATION_TYPE ? ["type", "shareId", "at", "signer", "sig"] : ["type", "shareId", "at", "revocation", "signer", "sig"];
  if (!isObject(record) || record.type !== type || !exactly(record, members) || !isString(record.shareId) || !SHARE_ID.test(record.shareId)
    || instant(record.at) === undefined || !isString(record.signer) || !BYTES32.test(record.signer) || !isString(record.sig) || !SIG.test(record.sig)
    || (type === DELETION_TYPE && (!isString(record.revocation) || !COMMIT.test(record.revocation))) || !isString(shareId) || !SHARE_ID.test(shareId)
    || !peerJwk(signerSig)) return { ok: false, reason: "malformed" };
  if (record.shareId !== shareId) return { ok: false, reason: "wrong-share" };
  if (record.signer !== (await peerThumbprint(signerSig))) return { ok: false, reason: "bad-key" };
  const { sig, ...unsigned } = record;
  return (await verifyEs256(signerSig, rfc8785(unsigned), sig)) ? { ok: true } : { ok: false, reason: "bad-signature" };
}

// ---- Request signatures (DEC-0056 under the share keys, PS-10) ----------------------------------------------------

/** RFC 8941 dictionary members as `{key: {value, params}}` for the two shapes these fields carry: an inner list of
 * strings with parameters, or a byte sequence. Answers null on anything else. */
export function parseDictionary(text) {
  const out = [];
  let i = 0;
  const skip = () => { while (text[i] === " " || text[i] === "\t") i++; };
  const key = () => {
    const m = /^[a-z*][a-z0-9_\-.*]*/.exec(text.slice(i));
    if (!m) throw new Error("key");
    i += m[0].length;
    return m[0];
  };
  const string = () => {
    if (text[i] !== '"') throw new Error("string");
    let s = "";
    for (i++; i < text.length; i++) {
      const c = text[i];
      if (c === "\\") { s += text[++i]; continue; }
      if (c === '"') { i++; return s; }
      if (c < " " || c > "~") throw new Error("string");
      s += c;
    }
    throw new Error("string");
  };
  const bare = () => {
    if (text[i] === '"') return { t: "string", v: string() };
    const m = /^-?[0-9]{1,15}/.exec(text.slice(i));
    if (m) { i += m[0].length; return { t: "integer", v: Number(m[0]) }; }
    throw new Error("item");
  };
  const params = () => {
    const p = [];
    while (text[i] === ";") {
      i++; skip();
      const k = key();
      if (text[i] !== "=") throw new Error("param");
      i++;
      p.push([k, bare()]);
    }
    return p;
  };
  try {
    skip();
    while (i < text.length) {
      const k = key();
      if (text[i] !== "=") throw new Error("member");
      i++;
      let value;
      if (text[i] === "(") {
        i++;
        const items = [];
        for (;;) {
          while (text[i] === " ") i++;
          if (text[i] === ")") { i++; break; }
          const item = bare();
          if (item.t !== "string" || text[i] === ";") throw new Error("inner");
          items.push(item.v);
        }
        value = { t: "inner", v: items };
      } else if (text[i] === ":") {
        const end = text.indexOf(":", i + 1);
        if (end < 0) throw new Error("bytes");
        const body = text.slice(i + 1, end);
        if (!/^[A-Za-z0-9+/]*={0,2}$/.test(body)) throw new Error("bytes");
        value = { t: "bytes", v: Uint8Array.from(atob(body), (c) => c.charCodeAt(0)) };
        i = end + 1;
      } else {
        throw new Error("value");
      }
      out.push([k, value, params()]);
      skip();
      if (i >= text.length) break;
      if (text[i] !== ",") throw new Error("comma");
      i++; skip();
      if (i >= text.length) throw new Error("trailing");
    }
  } catch {
    return null;
  }
  return out;
}

export async function contentDigest(body) {
  return `sha-256=:${b64(await sha256(body))}:`;
}

/** The signature under `label`, checked for its shape, digest and freshness (not yet for its key). */
export async function readSignature(headers, label, { method, uri, body, atMs }) {
  const inputs = parseDictionary(headers.get("signature-input") || "");
  const sigs = parseDictionary(headers.get("signature") || "");
  if (inputs === null || sigs === null) return { ok: false, code: "signature-missing" };
  const mine = inputs.filter((m) => m[0] === label), signed = sigs.filter((m) => m[0] === label);
  if (mine.length !== 1 || signed.length !== 1) return { ok: false, code: "signature-missing" };
  const [, value, params] = mine[0];
  if (value.t !== "inner" || JSON.stringify(value.v) !== JSON.stringify(["@method", "@target-uri", "content-digest"])
    || JSON.stringify(params.map((p) => p[0])) !== JSON.stringify(["created", "nonce", "keyid", "alg"])) return { ok: false, code: "signature-missing" };
  const got = Object.fromEntries(params);
  if (got.created.t !== "integer" || got.nonce.t !== "string" || got.keyid.t !== "string" || got.alg.t !== "string"
    || got.alg.v !== ALG || !NONCE.test(got.nonce.v) || !BYTES32.test(got.keyid.v)) return { ok: false, code: "signature-missing" };
  const [, sigValue, sigParams] = signed[0];
  if (sigValue.t !== "bytes" || sigParams.length || sigValue.v.length !== 64) return { ok: false, code: "signature-missing" };
  const digest = await contentDigest(body);
  if ((headers.get("content-digest") || "").trim() !== digest) return { ok: false, code: "digest-mismatch" };
  if (Math.abs(atMs / 1000 - got.created.v) > WINDOW_SECONDS) return { ok: false, code: "signature-stale" };
  const paramsText = `("@method" "@target-uri" "content-digest");created=${got.created.v};nonce="${got.nonce.v}";keyid="${got.keyid.v}";alg="${ALG}"`;
  const base = `"@method": ${method}\n"@target-uri": ${uri}\n"content-digest": ${digest}\n"@signature-params": ${paramsText}`;
  return { ok: true, keyid: got.keyid.v, nonce: got.nonce.v, created: got.created.v, base, signature: b64urlEncode(sigValue.v) };
}

async function signatureHolds(read, jwk) {
  return read.ok === true && (await peerThumbprint(jwk)) === read.keyid && (await verifyEs256(jwk, read.base, read.signature));
}

// ---- Answers ---------------------------------------------------------------------------------------------------------

const SECURITY = {
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
};

function answer(status, body) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...SECURITY } });
}

const refuse = (status, code, message, detail = {}) => answer(status, { error: { code, message, ...detail } });

/** The share's landing page (PS-10): no script, so nothing of the link's fragment is read or sent. */
const LANDING = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>A key shared with you</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:36rem;margin:3rem auto;padding:0 1rem;color:#1d1d1f}
code{background:#f2f2f2;padding:.1rem .3rem;border-radius:4px}</style></head>
<body><h1>A key shared with you</h1>
<p>Someone shared a key with you through Project Observatory. This page cannot open it, and it reads nothing from your
link.</p>
<p>Copy the whole link from the address bar, including the part after <code>#</code>. Then, in a terminal on the
machine where your agents run:</p>
<p><code>project-observatory full share accept</code></p>
<p>Paste the link when it asks, and press Enter. Observatory shows a six-digit code. Read it to the person who sent you
the link.</p>
<p>No Project Observatory yet? Install it from <a href="https://passioncode.ai/">passioncode.ai</a>, then come back to
this link.</p>
</body></html>
`;

// ---- One share's mailbox, over a storage the Durable Object or a test provides -------------------------------------

/** A nonce's key: its signature's creation time first (zero-padded, so keys sort by it), then the key and the nonce. */
const nonceTime = (seconds) => String(Math.max(0, seconds)).padStart(12, "0");
const nonceKey = (read) => `nonce:${nonceTime(read.created)}:${read.keyid}:${read.nonce}`;

/** The most a sealed envelope or report may weigh as stored: under a Durable Object's value limit, so one padded
 * message can never make the share's own record (and with it a revocation) fail to store. */
const MAX_ITEM_BYTES = 120 * 1024;
/** Late claims carried after sealing: one per claiming key, at most this many keys; the recipient's abort apart. */
const MAX_LATE_CLAIMS = 16;
/** A failed signature costs a verify and nothing stored; past this many in a minute such requests answer 429. */
const FAILURES_PER_MINUTE = 600;

/**
 * The rules for one share. `storage` holds `get`, `put`, `delete`, `deleteAll`, `list({prefix})` and `setAlarm`;
 * `ownerJwk` is the owner's key; `openCreation` admits any creator (a loopback trial's and a test's choice, never the
 * deployed Worker's, which fails closed without an owner key); `now()` answers milliseconds. The share's own record is
 * kept under `share`, each envelope under `env:<SLOT>` and each report under `rep:<sequence>`, so no one message can
 * make the record too large to store.
 */
export class ShareMailbox {
  constructor(storage, { ownerJwk = null, openCreation = false, now = () => Date.now() } = {}) {
    this.storage = storage;
    this.ownerJwk = ownerJwk;
    this.openCreation = openCreation;
    this.now = now;
    this.failures = { minute: 0, count: 0 };
  }

  async handle(request, shareId, sub) {
    const url = new URL(request.url);
    const method = request.method;
    const body = new Uint8Array(await request.arrayBuffer());
    if (body.length > MAX_BODY_BYTES) return refuse(413, "too-large", "the body is larger than the mailbox carries");
    const now = this.now();
    let doc = null;
    if (body.length) {
      try { doc = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body)); } catch { return refuse(400, "malformed", "the body is not JSON"); }
    }
    const uri = url.origin + url.pathname + url.search;
    const sig = { method, uri, body, atMs: now };
    if (sub === null && method === "PUT") return this.create(shareId, request.headers, sig, doc, now);
    const share = await this.storage.get("share");
    if (!share) return refuse(404, "unknown-share", "the mailbox holds no such share");
    // A claim is signed by its own keys, and only a claim being carried (a POST) may name its signer so: a body
    // sent with any other request is never a key this share admits.
    const claim = sub === "frames" && method === "POST" && isObject(doc) && doc.step === "claim" ? doc : null;
    delete share.nonces;                                         // kept under their own keys since 1.2.0
    const signed = await this.signer(share, request.headers, sig, claim, now);
    if (signed instanceof Response) return signed;               // nothing stored for a request no party signed
    const signer = signed.who;
    // Each party has its own minute bucket, counted only once its signature holds, and the sender taking a share back
    // is never refused for it: a stranger or a hostile recipient cannot keep the sender's revocation out.
    const revoking = sub === "revocation" && method === "PUT" && signer === "sender";
    const minute = Math.floor(now / 60000);
    share.rates = share.rates && share.rates.minute === minute ? share.rates : { minute, counts: {} };
    share.rates.counts[signer] = (share.rates.counts[signer] || 0) + 1;
    if (!revoking && share.rates.counts[signer] > REQUESTS_PER_MINUTE) {
      await this.storage.put("share", share);                    // the counts only: a constant size, no nonce
      return refuse(429, "too-many", "too many requests from this key for this share; try again in a minute");
    }
    const until = await this.storage.get(signed.nonceKey);
    if (until !== undefined && until >= now) return refuse(401, "signature-replayed", "this nonce was used");
    let result;
    if (sub === "frames" && method === "POST") result = await this.frame(share, signer, doc, now);
    else if (sub === "frames" && method === "GET") {
      if (signer !== "sender" && signer !== "recipient") result = refuse(403, "forbidden", "only the two sides read the frames");
      else {
        await this.expire(share, now);
        result = answer(200, { frames: share.frames, verdict: share.verdict, late: this.lateFrames(share), sealed: share.sealed });
      }
    } else if (sub && sub.startsWith("envelopes/") && method === "PUT") result = await this.envelope(share, signer, sub.slice(10), doc, body, now);
    else if (sub === "lease" && method === "POST") result = await this.lease(share, signer, now);
    else if (sub === "reports" && method === "POST") result = await this.report(share, signer, doc, body);
    else if (sub === "reports" && method === "GET") {
      if (signer !== "sender") result = refuse(403, "forbidden", "only the sender reads the reports");
      else {
        const after = Number.parseInt(url.searchParams.get("after") || "0", 10) || 0;
        const reports = [];
        for (const seq of share.reportSeqs.filter((s) => s > after).slice(0, 200)) {
          const report = await this.storage.get(`rep:${seq}`);
          if (report) reports.push(report);
        }
        result = answer(200, { reports });
      }
    } else if (sub === "revocation" && method === "PUT") result = await this.revocation(share, signer, doc);
    else if (sub === "deletion" && method === "PUT") result = await this.deletion(share, signer, doc);
    else if (sub === null && method === "GET") {
      if (signer !== "sender" && signer !== "recipient") result = refuse(403, "forbidden", "only the two sides read a share");
      else {
        await this.expire(share, now);
        result = answer(200, this.status(share));
      }
    } else result = refuse(405, "method-not-allowed", "no such method on this route");
    await this.storage.put("share", share);
    if (result.status < 400 && method !== "GET") {
      // Only an accepted request that changes something keeps its nonce (a read played again changes nothing), under
      // its own key: a flood of refused requests or of reads stores nothing.
      await this.storage.put(signed.nonceKey, signed.until);
    }
    await this.purgeNonces(now);
    await this.storage.setAlarm(this.keepUntil(share));
    return result;
  }

  /** Forgets every nonce whose signature is too old to be accepted again (readSignature refuses one created more than
   * WINDOW_SECONDS ago). Keys begin with the zero-padded creation time, so a listing up to that time returns exactly the
   * stale ones, oldest first, however many are live after them. */
  async purgeNonces(now) {
    const end = `nonce:${nonceTime(Math.floor(now / 1000) - WINDOW_SECONDS)}`;
    for (let round = 0; round < 8; round++) {
      const stale = await this.storage.list({ prefix: "nonce:", end, limit: 256 });
      for (const key of stale.keys()) await this.storage.delete(key);
      if (stale.size < 256) break;
    }
  }

  async signer(share, headers, sig, claim, now) {
    const minute = Math.floor(now / 60000);
    const failed = (response) => {
      this.failures = this.failures.minute === minute ? this.failures : { minute, count: 0 };
      return ++this.failures.count > FAILURES_PER_MINUTE ? refuse(429, "too-many", "too many unsigned requests") : response;
    };
    const read = await readSignature(headers, LABEL, sig);
    if (!read.ok) return failed(refuse(401, read.code, "the request is not signed by a share key"));
    let sides = { sender: share.senderSig };
    if (share.recipientSig !== null) sides.recipient = share.recipientSig;
    if (claim !== null && isObject(claim.keys)) sides = { claimant: claim.keys.sig };
    let who = null;
    for (const [name, jwk] of Object.entries(sides)) if (await signatureHolds(read, jwk)) { who = name; break; }
    if (who === null) {
      if (share.recipientSig === null && sig.method === "GET" && new URL(sig.uri).pathname.endsWith("/frames")) {
        return failed(refuse(409, "no-claim", "no claim has reached the mailbox"));
      }
      return failed(refuse(401, "signature-invalid", "the signature is not by a key of this share"));
    }
    // Kept as long as a signature made at `created` is fresh (created + 300 s): a clock ahead of ours included.
    return { who, nonceKey: nonceKey(read), until: Math.max(now, read.created * 1000) + WINDOW_SECONDS * 1000 };
  }

  async create(shareId, headers, sig, doc, now) {
    // Fails closed: a mailbox whose owner key is missing or unreadable files no share for anyone.
    if (this.ownerJwk === null && !this.openCreation) {
      return refuse(503, "no-owner", "this mailbox has no owner key bound; it files no share until it is deployed again");
    }
    if (!isObject(doc) || doc.shareId !== shareId || !isObject(doc.sender) || !peerJwk(doc.sender.sig)) {
      return refuse(400, "malformed", "a share names its id and the sender share #sig");
    }
    const deadline = instant(doc.claimDeadline);
    const lease = Object.hasOwn(doc, "leaseSeconds") ? doc.leaseSeconds : DEFAULT_LEASE_SECONDS;
    if (deadline === undefined || deadline <= now || deadline - now > MAX_CLAIM_DAYS * DAY_MS + 60_000) {
      return refuse(400, "claim-deadline", "a claim deadline is in the future and at most 7 days away");
    }
    if (!Number.isSafeInteger(lease) || lease < 60 || lease > MAX_LEASE_SECONDS) return refuse(400, "lease", "a lease is 60 s to 24 h");
    const read = await readSignature(headers, LABEL, sig);
    if (!(await signatureHolds(read, doc.sender.sig))) return refuse(401, read.code || "signature-invalid", "a share is created by its own #sig");
    if (this.ownerJwk !== null) {
      const owner = await readSignature(headers, OWNER_LABEL, sig);
      if (!(await signatureHolds(owner, this.ownerJwk))) return refuse(403, "not-owner", "only the mailbox's owner files shares here");
    }
    const existing = await this.storage.get("share");
    if (existing) {
      const same = existing.senderSig.x === doc.sender.sig.x && existing.senderSig.y === doc.sender.sig.y;
      return same ? answer(200, { created: false }) : refuse(409, "exists", "the share id is taken");
    }
    const share = {
      shareId, senderSig: doc.sender.sig, claimDeadline: doc.claimDeadline, leaseSeconds: lease, createdAt: rfc3339(now),
      frames: [], lateAbort: null, lateClaims: [], verdict: { state: "open" }, recipientSig: null, sealed: false,
      envelopeEnds: {}, revocation: null, deletion: null, reportSeqs: [], deliveries: 0, lastRenewal: null, revokedAt: null,
    };
    await this.storage.put("share", share);
    await this.storage.put(nonceKey(read), Math.max(now, read.created * 1000) + WINDOW_SECONDS * 1000);
    await this.storage.setAlarm(this.keepUntil(share));
    return answer(201, { created: true });
  }

  lateFrames(share) {
    return [...(share.lateAbort ? [share.lateAbort] : []), ...share.lateClaims.map((c) => c.frame)];
  }

  async expire(share, now) {
    if (share.verdict.state === "open" && !share.sealed) {
      share.verdict = await peerExchangeVerdict(share.shareId, share.claimDeadline, share.frames, rfc3339(now));
    }
  }

  async frame(share, signer, frame, now) {
    if (peerFrame(frame) === undefined || frame.shareId !== share.shareId) return refuse(400, "malformed", "not a frame of this share");
    const step = frame.step, role = Object.hasOwn(frame, "role") ? frame.role : null;
    if (step === "claim" && signer !== "claimant") return refuse(403, "forbidden", "a claim is signed by its own keys");
    if ((step === "commit" && signer !== "sender") || (role !== null && role !== signer)) return refuse(403, "forbidden", "a side posts only its own frames");
    if (share.sealed) {
      // After the first envelope: no verdict; a later claim or abort is carried to the sender as a warning. One abort
      // (the recipient's own) and one claim per claiming key, so strangers cannot crowd the recipient's abort out.
      // A repeat is answered without being accepted, so it stores no nonce.
      if (step === "abort") {
        if (share.lateAbort !== null) return refuse(409, "repeated", "the recipient's abort was already carried");
        share.lateAbort = frame;
        return answer(202, { carried: true, late: true });
      }
      if (step === "claim") {
        const key = `${frame.keys.sig.x}.${frame.keys.sig.y}`;
        if (share.lateClaims.some((c) => c.key === key)) return refuse(409, "repeated", "this key's claim was already carried");
        if (share.lateClaims.length >= MAX_LATE_CLAIMS) return refuse(429, "too-many", "this share carries no more late claims");
        share.lateClaims.push({ key, frame });
        return answer(202, { carried: true, late: true });
      }
      return refuse(409, "sealed", "the share is sealed; only a claim or an abort is carried now");
    }
    await this.expire(share, now);
    if (share.verdict.state === "burnt") return refuse(409, "burnt", "this exchange is burnt", { reason: share.verdict.reason });
    if (share.frames.length >= MAX_FRAMES) return refuse(429, "too-many", "this exchange carries no more frames");
    share.frames.push(frame);
    if (step === "claim" && share.recipientSig === null) share.recipientSig = frame.keys.sig;
    share.verdict = await peerExchangeVerdict(share.shareId, share.claimDeadline, share.frames, rfc3339(now));
    return answer(202, { carried: true, index: share.frames.length - 1, verdict: share.verdict });
  }

  async envelope(share, signer, slot, env, body, now) {
    if (signer !== "sender") return refuse(403, "forbidden", "only the sender stores envelopes");
    if (share.revocation !== null) return refuse(410, "revoked", "the share was taken back");
    await this.expire(share, now);
    if (!share.sealed) {
      if (share.verdict.state === "burnt") return refuse(409, "burnt", "this exchange is burnt", { reason: share.verdict.reason });
      if (share.verdict.state !== "complete") return refuse(409, "not-complete", "the exchange has not completed");
    }
    if (!SLOT.test(slot) || !peerSealedShape(env, ENVELOPE_TYPE) || env.header.shareId !== share.shareId || env.header.slot !== slot) {
      return refuse(400, "malformed", "not an envelope of this share and slot");
    }
    if (body.length > MAX_ITEM_BYTES) return refuse(413, "too-large", "an envelope is larger than the mailbox keeps");
    if (!Object.hasOwn(share.envelopeEnds, slot) && Object.keys(share.envelopeEnds).length >= 64) return refuse(429, "too-many", "a share carries at most 64 slots");
    await this.storage.put(`env:${slot}`, env);
    // Kept by the mailbox's own clock: an `exp` past the contract's 90 days is read as 90 days from now.
    share.envelopeEnds[slot] = rfc3339(Math.min(instant(env.header.exp), now + MAX_ENVELOPE_DAYS * DAY_MS));
    share.sealed = true;
    return answer(200, { stored: true });
  }

  async lease(share, signer, now) {
    if (signer !== "recipient") return refuse(403, "forbidden", "only the recipient renews");
    if (share.revocation !== null) return refuse(410, "revoked", "the share was taken back", { revocation: share.revocation });
    await this.expire(share, now);
    if (!share.sealed) {
      if (share.verdict.state === "burnt") return refuse(409, "burnt", "this exchange is burnt", { reason: share.verdict.reason });
      return refuse(409, "not-sealed", "the sender has not sealed the share yet");
    }
    const ends = Object.values(share.envelopeEnds).map((exp) => instant(exp) ?? 0);
    if (ends.length && now >= Math.max(...ends)) {
      return refuse(409, "ended", "every sealed copy of this share has ended; the sender's machine seals it again");
    }
    share.deliveries += 1;
    share.lastRenewal = rfc3339(now);
    const lease = { grantedAt: rfc3339(now), expiresAt: rfc3339(now + Math.min(share.leaseSeconds, MAX_LEASE_SECONDS) * 1000) };
    const envelopes = [];
    for (const slot of Object.keys(share.envelopeEnds)) {
      const env = await this.storage.get(`env:${slot}`);
      if (env) envelopes.push(env);
    }
    return answer(200, { lease, envelopes });
  }

  async report(share, signer, report, body) {
    if (signer !== "recipient") return refuse(403, "forbidden", "only the recipient reports");
    if (!peerSealedShape(report, REPORT_TYPE) || report.header.shareId !== share.shareId) return refuse(400, "malformed", "not a report of this share");
    if (body.length > MAX_ITEM_BYTES) return refuse(413, "too-large", "a report is larger than the mailbox keeps");
    const seq = report.header.sequence;
    if (share.reportSeqs.includes(seq)) return answer(200, { stored: false });
    await this.storage.put(`rep:${seq}`, report);
    share.reportSeqs.push(seq);
    share.reportSeqs.sort((a, b) => a - b);
    while (share.reportSeqs.length > MAX_REPORTS) await this.storage.delete(`rep:${share.reportSeqs.shift()}`);
    return answer(200, { stored: true });
  }

  async revocation(share, signer, record) {
    if (signer !== "sender") return refuse(403, "forbidden", "only the sender takes a share back");
    if (!(await peerRecordStands(record, REVOCATION_TYPE, share.shareId, share.senderSig)).ok) {
      return refuse(400, "malformed", "not a revocation the sender's share #sig signed");
    }
    if (share.revocation === null) {
      share.revocation = record;
      // When it was taken back, by the mailbox's own clock: a revocation dated in the future cannot keep a share.
      share.revokedAt = rfc3339(Math.min(this.now(), Math.max(instant(record.at) ?? 0, instant(share.createdAt) ?? 0)));
      // Taken back: no sealed copy is ever served again, so none is kept.
      for (const slot of Object.keys(share.envelopeEnds)) await this.storage.delete(`env:${slot}`);
      share.envelopeEnds = {};
    }
    return answer(200, { held: true, revocation: share.revocation });
  }

  async deletion(share, signer, record) {
    if (signer !== "recipient") return refuse(403, "forbidden", "only the recipient returns a deletion receipt");
    if (!(await peerRecordStands(record, DELETION_TYPE, share.shareId, share.recipientSig)).ok) {
      return refuse(400, "malformed", "not a deletion receipt the recipient's share #sig signed");
    }
    share.deletion = record;
    return answer(200, { held: true });
  }

  status(share) {
    return {
      protocol: PROTOCOL, shareId: share.shareId, claimDeadline: share.claimDeadline, leaseSeconds: share.leaseSeconds,
      verdict: share.verdict, sealed: share.sealed, deliveries: share.deliveries, lastRenewal: share.lastRenewal,
      revocation: share.revocation, deletion: share.deletion, reports: share.reportSeqs.length,
      late: this.lateFrames(share).length,
    };
  }

  /** When the share is forgotten whole. Unsealed: a day after its claim deadline. Sealed: 30 days after the latest
   * of its last sealed copy's end, its revocation and its last renewal, so a recipient that comes back late still
   * learns it was taken back. Never measured from now: a request does not push it out. */
  keepUntil(share) {
    if (!share.sealed) return (instant(share.claimDeadline) ?? 0) + DAY_MS;
    const times = [...Object.values(share.envelopeEnds || {}).map((exp) => instant(exp) ?? 0),
      // A share revoked under 1.1.0 has no revokedAt: its revocation's own date stands in, clamped to now.
      instant(share.revokedAt) ?? Math.min(instant(share.revocation?.at) ?? 0, this.now()),
      instant(share.lastRenewal) ?? 0, instant(share.createdAt) ?? 0];
    return Math.max(...times) + 30 * DAY_MS;
  }

  /** The alarm: a share past its keep-until is forgotten whole, its sealed copies and reports with it. */
  async alarm() {
    const share = await this.storage.get("share");
    if (!share) return;
    const until = this.keepUntil(share);
    if (this.now() >= until) await this.storage.deleteAll();
    else await this.storage.setAlarm(until);
  }
}

// ---- The Worker and its Durable Object -------------------------------------------------------------------------------

const ROUTE = /^\/v1\/shares\/([0-9a-f]{32})(?:\/(frames|envelopes\/[A-Z][A-Z0-9_]{0,63}|lease|reports|revocation|deletion))?$/;

function ownerOf(env) {
  if (!env.OWNER_JWK) return null;
  try { const jwk = JSON.parse(env.OWNER_JWK); return peerJwk(jwk) ? jwk : null; } catch { return null; }
}

/** One share's Durable Object: every request for a share runs here, one at a time. */
export class ShareBox {
  constructor(ctx, env) {
    this.ctx = ctx;
    // Without a readable owner key the mailbox files no share (fails closed); only the loopback trial and the tests
    // set OPEN_CREATION, and `full cloud deploy` never binds it.
    this.mailbox = new ShareMailbox(ctx.storage, { ownerJwk: ownerOf(env), openCreation: env.OPEN_CREATION === "loopback-trial" });
  }

  async fetch(request) {
    const match = ROUTE.exec(new URL(request.url).pathname);
    if (!match) return refuse(404, "not-found", "no such route");
    return this.ctx.blockConcurrencyWhile(() => this.mailbox.handle(request, match[1], match[2] ?? null));
  }

  async alarm() {
    await this.mailbox.alarm();
  }
}

/** The Worker: the well-known answer and the landing page here, every share's route in its own Durable Object. */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/.well-known/observatory-cloud") {
      const owner = ownerOf(env);
      return answer(200, { protocol: PROTOCOL, peerShare: PEER_SHARE_PROTOCOL, version: VERSION,
        digest: env.WORKER_DIGEST || null, owner: owner ? await peerThumbprint(owner) : null });
    }
    if (request.method === "GET" && /^\/s\/[0-9a-f]{32}$/.test(url.pathname)) {
      return new Response(LANDING, { status: 200, headers: { "content-type": "text/html; charset=utf-8", ...SECURITY,
        "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'" } });
    }
    const match = ROUTE.exec(url.pathname);
    if (!match) return refuse(404, "not-found", "no such route");
    const length = Number(request.headers.get("content-length") || "0");
    if (length > MAX_BODY_BYTES) return refuse(413, "too-large", "the body is larger than the mailbox carries");
    if (!env.SHARES) return refuse(503, "unavailable", "the mailbox has no share store bound");
    return env.SHARES.get(env.SHARES.idFromName(match[1])).fetch(request);
  },
};
