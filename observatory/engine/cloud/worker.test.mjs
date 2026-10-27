// The Worker's ports over the contract's vectors (fabric/node-vectors/peer/peer-share.json), and its handler's own
// answers: `node --test cloud/worker.test.mjs` from the engine folder. tests/test_cloud_worker.py runs it, and then
// drives the engine's two share stores against the Worker over HTTP.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import worker, { peerExchangeVerdict, peerFrame, peerJwk, peerKeys, peerRecordStands, peerSealedShape, peerThumbprint,
  instant, rfc3339, rfc8785, parseDictionary, ShareMailbox, b64urlEncode, contentDigest } from "./worker.mjs";
import { memoryEnv, MemoryStorage } from "./memory_env.mjs";

const vectors = JSON.parse(readFileSync(new URL("../fabric/node-vectors/peer/peer-share.json", import.meta.url), "utf8"));
const ORIGIN = "https://mailbox.example";

test("keys and thumbprints", async () => {
  for (const k of vectors.keys) {
    assert.equal(peerJwk(k.jwk), true, k.name);
    assert.equal(await peerThumbprint(k.jwk), k.thumbprint, k.name);
  }
  const jwk = vectors.keys[0].jwk;
  assert.equal(peerJwk({ ...jwk, y: jwk.x }), false);
  assert.equal(peerKeys({ sig: jwk, kem: jwk }), false);
});

test("one attempt per link: every exchange vector", async () => {
  for (const c of vectors.exchanges) {
    assert.deepEqual(await peerExchangeVerdict(c.shareId, c.claimDeadline, c.frames, c.at), c.expect, c.name);
  }
});

test("claims are frames", () => {
  for (const c of vectors.claims) assert.notEqual(peerFrame(c.frame), undefined, c.name);
});

test("sealed shapes: the envelope and report vectors whose own shape holds", () => {
  const shapeWrong = ["a member more", "a header that carries the value", "a time with an offset, not UTC"];
  for (const c of vectors.envelopes) assert.equal(peerSealedShape(c.envelope, "fabric-peer-share-envelope/1"), !shapeWrong.includes(c.name), c.name);
  for (const c of vectors.reports) assert.equal(peerSealedShape(c.report, "fabric-peer-share-report/1"), !["a member more", "a time with an offset, not UTC"].includes(c.name), c.name);
});

test("revocation and deletion records", async () => {
  for (const c of vectors.records) assert.deepEqual(await peerRecordStands(c.record, c.type, c.shareId, c.signerSig), c.expect, c.name);
});

test("times: a day or a second that does not exist is no instant", () => {
  assert.equal(instant("2026-10-27T10:00:00Z"), Date.UTC(2026, 9, 27, 10));
  for (const bad of ["2026-02-30T00:00:00Z", "2026-10-27T24:00:00Z", "2026-10-27T10:00:60Z", "2026-10-27T10:00:00+01:00"]) {
    assert.equal(instant(bad), undefined, bad);
  }
});

test("RFC 8785 sorts members by code units", () => {
  assert.equal(rfc8785({ b: 1, a: [true, null, "é"] }), '{"a":[true,null,"é"],"b":1}');
});

test("signature fields parse as the client writes them, and nothing else", () => {
  const sig = 'fabric-peer-share=("@method" "@target-uri" "content-digest");created=1;nonce="AAAAAAAAAAAAAAAAAAAAAA";keyid="k";alg="ecdsa-p256-sha256", observatory-owner=("@method");created=2';
  const parsed = parseDictionary(sig);
  assert.equal(parsed.length, 2);
  assert.deepEqual(parsed[0][1].v, ["@method", "@target-uri", "content-digest"]);
  assert.equal(parseDictionary("x=(\"a\""), null);
  assert.equal(parseDictionary("x=:not base64!:"), null);
});

test("the well-known answer, and a landing page that runs no script", async () => {
  const env = memoryEnv({ ownerJwk: vectors.keys[0].jwk, digest: "d" });
  const wk = await (await worker.fetch(new Request(ORIGIN + "/.well-known/observatory-cloud"), env)).json();
  assert.equal(wk.protocol, "observatory-cloud/1");
  assert.equal(wk.owner, vectors.keys[0].thumbprint);
  const page = await worker.fetch(new Request(ORIGIN + "/s/" + "a".repeat(32)), env);
  const html = await page.text();
  assert.equal(page.status, 200);
  assert.doesNotMatch(html, /<script/i);
  assert.match(page.headers.get("content-security-policy"), /default-src 'none'/);
  assert.equal(page.headers.get("referrer-policy"), "no-referrer");
});

test("refusals before any share state: unknown routes, unknown shares, unsigned and oversized requests", async () => {
  const env = memoryEnv({ openCreation: true });
  const id = "b".repeat(32);
  assert.equal((await worker.fetch(new Request(ORIGIN + "/v1/nothing"), env)).status, 404);
  assert.equal((await (await worker.fetch(new Request(`${ORIGIN}/v1/shares/${id}`), env)).json()).error.code, "unknown-share");
  const create = await worker.fetch(new Request(`${ORIGIN}/v1/shares/${id}`, { method: "PUT", body: "{}" }), env);
  assert.equal(create.status, 400);
  const big = await worker.fetch(new Request(`${ORIGIN}/v1/shares/${id}/frames`, { method: "POST", body: "x".repeat(300_000),
    headers: { "content-length": "300000" } }), env);
  assert.equal(big.status, 413);
});

test("the alarm forgets an unsealed share a day after its claim deadline", async () => {
  const storage = new MemoryStorage();
  let now = Date.UTC(2026, 9, 27);
  const box = new ShareMailbox(storage, { now: () => now });
  await storage.put("share", { shareId: "c".repeat(32), claimDeadline: "2026-10-28T00:00:00Z", sealed: false, envelopes: {} });
  await box.alarm();
  assert.ok(await storage.get("share"));
  now = Date.UTC(2026, 9, 29, 1);
  await box.alarm();
  assert.equal(await storage.get("share"), undefined);
});

test("a sealed share is forgotten 30 days after its last copy ends, its revocation or its last renewal, never later for being asked", async () => {
  const storage = new MemoryStorage();
  let now = Date.UTC(2026, 10, 1);
  const box = new ShareMailbox(storage, { now: () => now });
  const sealed = { shareId: "d".repeat(32), claimDeadline: "2026-10-28T00:00:00Z", createdAt: "2026-10-27T00:00:00Z",
    sealed: true, envelopeEnds: { OPENROUTER_API_KEY: "2026-11-26T00:00:00Z" }, revocation: null, lastRenewal: "2026-11-01T00:00:00Z" };
  assert.equal(box.keepUntil(sealed), Date.UTC(2026, 11, 26));
  now = Date.UTC(2027, 5, 1);                                   // asked much later: the day does not move
  assert.equal(box.keepUntil(sealed), Date.UTC(2026, 11, 26));
  await storage.put("share", sealed);
  await storage.put("env:OPENROUTER_API_KEY", { header: {} });
  await box.alarm();
  assert.equal(await storage.get("share"), undefined);
  assert.equal(await storage.get("env:OPENROUTER_API_KEY"), undefined);
  // Taken back: its copies are gone at once, and it is forgotten 30 days after the revocation.
  const revoked = { ...sealed, envelopeEnds: {}, revocation: { at: "2026-11-02T00:00:00Z" }, revokedAt: "2026-11-02T00:00:00Z",
    lastRenewal: "2026-11-01T00:00:00Z" };
  assert.equal(box.keepUntil(revoked), Date.UTC(2026, 11, 2));
});

test("a report too large to keep is refused, and reports are kept apart from the share", async () => {
  const storage = new MemoryStorage();
  const box = new ShareMailbox(storage);
  const report = vectors.reports[0].report;
  const share = { shareId: report.header.shareId, reportSeqs: [] };
  const big = await box.report(share, "recipient", report, new Uint8Array(130 * 1024));
  assert.equal(big.status, 413);
  const ok = await box.report(share, "recipient", report, new Uint8Array(1000));
  assert.equal(ok.status, 200);
  assert.deepEqual(share.reportSeqs, [report.header.sequence]);
  assert.ok(await storage.get(`rep:${report.header.sequence}`));
  assert.equal(Object.hasOwn(share, "reports"), false);
});

test("after sealing: one late claim per key, the recipient's abort apart", async () => {
  const box = new ShareMailbox(new MemoryStorage());
  const claim = vectors.claims[0].frame;
  const share = { shareId: claim.shareId, sealed: true, frames: [], lateAbort: null, lateClaims: [] };
  await box.frame(share, "claimant", claim, Date.now());
  await box.frame(share, "claimant", claim, Date.now());
  await box.frame(share, "recipient", { schema: "peer-share-frame/1", step: "abort", role: "recipient", shareId: claim.shareId }, Date.now());
  assert.equal(share.lateClaims.length, 1);
  assert.equal(box.lateFrames(share).length, 2);
  assert.equal(box.lateFrames(share)[0].step, "abort");
});

test("a Worker with no readable owner key files no share", async () => {
  const env = memoryEnv();                                       // no OWNER_JWK, and no trial flag
  const id = "e".repeat(32);
  const body = JSON.stringify({ shareId: id, sender: { sig: vectors.keys[0].jwk }, claimDeadline: rfc3339(Date.now() + 86_400_000) });
  const answerOf = await worker.fetch(new Request(`${ORIGIN}/v1/shares/${id}`, { method: "PUT", body }), env);
  assert.equal(answerOf.status, 503);
  assert.equal((await answerOf.json()).error.code, "no-owner");
});

// ---- Signed requests, as a node makes them, for the handler's own paths ------------------------------------------

const N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551n;
const toBig = (bytes) => bytes.reduce((acc, b) => (acc << 8n) | BigInt(b), 0n);
const toBytes = (n, len) => Uint8Array.from({ length: len }, (_, i) => Number((n >> BigInt(8 * (len - 1 - i))) & 0xffn));

async function shareKey() {
  const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
  const jwk = await crypto.subtle.exportKey("jwk", pair.publicKey);
  return { pub: { kty: "EC", crv: "P-256", x: jwk.x, y: jwk.y }, priv: pair.privateKey };
}

async function es256(priv, text) {
  const raw = new Uint8Array(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, priv, new TextEncoder().encode(text)));
  const s = toBig(raw.slice(32));
  if (s > N / 2n) raw.set(toBytes(N - s, 32), 32);              // low-S, as every node signs
  return raw;
}

const random32 = () => b64urlEncode(crypto.getRandomValues(new Uint8Array(32)));

async function signed(method, url, doc, signers, created) {
  const bytes = doc === null ? new Uint8Array() : new TextEncoder().encode(JSON.stringify(doc));
  const digest = await contentDigest(bytes);
  const inputs = [], sigs = [];
  for (const [label, key] of signers) {
    const nonce = b64urlEncode(crypto.getRandomValues(new Uint8Array(16)));
    const params = `("@method" "@target-uri" "content-digest");created=${created};nonce="${nonce}";keyid="${await peerThumbprint(key.pub)}";alg="ecdsa-p256-sha256"`;
    const base = `"@method": ${method}\n"@target-uri": ${url}\n"content-digest": ${digest}\n"@signature-params": ${params}`;
    inputs.push(`${label}=${params}`);
    sigs.push(`${label}=:${btoa(String.fromCharCode(...(await es256(key.priv, base))))}:`);
  }
  const headers = new Headers({ "content-digest": digest, "signature-input": inputs.join(", "), signature: sigs.join(", ") });
  return { url, method, headers, arrayBuffer: async () => bytes.buffer };
}

test("a flood of self-signed claims never grows the share's record, and the revocation still lands", async () => {
  const storage = new MemoryStorage({ maxValueBytes: 16_384 });   // far below a Durable Object's limit
  const now = Date.UTC(2026, 9, 28, 12);
  const box = new ShareMailbox(storage, { openCreation: true, now: () => now });
  const id = "f".repeat(32), sender = await shareKey(), created = Math.floor(now / 1000);
  const base = `${ORIGIN}/v1/shares/${id}`;
  const made = await box.handle(await signed("PUT", base, { shareId: id, sender: { sig: sender.pub }, claimDeadline: rfc3339(now + 86_400_000),
    leaseSeconds: 3600 }, [["fabric-peer-share", sender]], created), id, null);
  assert.equal(made.status, 201);
  const stranger = await shareKey(), kem = await shareKey();
  const claim = { schema: "peer-share-frame/1", step: "claim", shareId: id, keys: { sig: stranger.pub, kem: kem.pub }, mac: random32(),
    commit: "sha256:" + "0".repeat(64) };
  const statuses = new Map();
  for (let i = 0; i < 400; i++) {
    // `created` 299 s ahead: each nonce would live about 600 s if it were kept.
    const answered = await box.handle(await signed("POST", `${base}/frames`, claim, [["fabric-peer-share", stranger]], created + 299), id, "frames");
    statuses.set(answered.status, (statuses.get(answered.status) || 0) + 1);
  }
  // The link's one claim, and the second that burns it (carried, so both sides learn why); the rest refused.
  assert.equal(statuses.get(202), 2);
  assert.equal((statuses.get(409) || 0) + (statuses.get(429) || 0), 398);
  assert.ok(JSON.stringify(await storage.get("share")).length < 8_000);
  assert.ok((await storage.list({ prefix: "nonce:" })).size <= 3);     // the creation's and the two carried claims
  const unsigned = { type: "peer-share-revocation/1", shareId: id, at: rfc3339(now), signer: await peerThumbprint(sender.pub) };
  const record = { ...unsigned, sig: b64urlEncode(await es256(sender.priv, rfc8785(unsigned))) };
  const revoked = await box.handle(await signed("PUT", `${base}/revocation`, record, [["fabric-peer-share", sender]], created), id, "revocation");
  assert.equal(revoked.status, 200);
  assert.equal((await storage.get("share")).revokedAt, rfc3339(now));
});

test("a lease is refused once every sealed copy has ended, and nothing is counted", async () => {
  const box = new ShareMailbox(new MemoryStorage(), { now: () => Date.UTC(2026, 11, 1) });
  const share = { shareId: "a".repeat(32), sealed: true, revocation: null, verdict: { state: "complete" }, deliveries: 3, lastRenewal: null,
    leaseSeconds: 3600, envelopeEnds: { OPENROUTER_API_KEY: "2026-11-30T00:00:00Z" } };
  const answered = await box.lease(share, "recipient", Date.UTC(2026, 11, 1));
  assert.equal(answered.status, 409);
  assert.equal((await answered.json()).error.code, "ended");
  assert.equal(share.deliveries, 3);
  assert.equal(share.lastRenewal, null);
});

test("on a sealed share, repeated late claims and a recipient's reads store no nonce, and the revocation lands", async () => {
  const storage = new MemoryStorage({ maxValueBytes: 16_384 });
  const now = Date.UTC(2026, 9, 28, 12);
  const box = new ShareMailbox(storage, { now: () => now });
  const id = "c".repeat(32), sender = await shareKey(), recipient = await shareKey(), created = Math.floor(now / 1000);
  const base = `${ORIGIN}/v1/shares/${id}`;
  await storage.put("share", { shareId: id, senderSig: sender.pub, claimDeadline: rfc3339(now + 86_400_000), leaseSeconds: 3600,
    createdAt: rfc3339(now - 3_600_000), frames: [], lateAbort: null, lateClaims: [], verdict: { state: "complete" },
    recipientSig: recipient.pub, sealed: true, envelopeEnds: { OPENROUTER_API_KEY: rfc3339(now + 86_400_000) }, revocation: null,
    deletion: null, reportSeqs: [], deliveries: 0, lastRenewal: null, revokedAt: null });
  const stranger = await shareKey(), kem = await shareKey();
  const claim = { schema: "peer-share-frame/1", step: "claim", shareId: id, keys: { sig: stranger.pub, kem: kem.pub }, mac: random32(),
    commit: "sha256:" + "0".repeat(64) };
  const statuses = new Map();
  for (let i = 0; i < 300; i++) {
    const answered = await box.handle(await signed("POST", `${base}/frames`, claim, [["fabric-peer-share", stranger]], created + 299), id, "frames");
    statuses.set(answered.status, (statuses.get(answered.status) || 0) + 1);
  }
  assert.equal(statuses.get(202), 1);                            // carried once, as a warning to the sender
  assert.equal((statuses.get(409) || 0) + (statuses.get(429) || 0), 299);
  for (let i = 0; i < 200; i++) await box.handle(await signed("GET", base, null, [["fabric-peer-share", recipient]], created), id, null);
  assert.equal((await storage.list({ prefix: "nonce:" })).size, 1);
  assert.ok(JSON.stringify(await storage.get("share")).length < 8_000);
  const unsigned = { type: "peer-share-revocation/1", shareId: id, at: rfc3339(now), signer: await peerThumbprint(sender.pub) };
  const record = { ...unsigned, sig: b64urlEncode(await es256(sender.priv, rfc8785(unsigned))) };
  const revoked = await box.handle(await signed("PUT", `${base}/revocation`, record, [["fabric-peer-share", sender]], created), id, "revocation");
  assert.equal(revoked.status, 200);
});

test("every nonce too old to be accepted again is purged, however many younger ones sort after it", async () => {
  const storage = new MemoryStorage();
  const now = Date.UTC(2026, 9, 28, 12), seconds = Math.floor(now / 1000);
  const box = new ShareMailbox(storage, { now: () => now });
  const pad = (n) => String(n).padStart(12, "0");
  for (let i = 0; i < 600; i++) await storage.put(`nonce:${pad(seconds - 400)}:k:${String(i).padStart(4, "0")}`, 0);
  for (let i = 0; i < 10; i++) await storage.put(`nonce:${pad(seconds + i)}:k:fresh${i}`, now + 300_000);
  await box.purgeNonces(now);
  const left = [...(await storage.list({ prefix: "nonce:" })).keys()];
  assert.equal(left.length, 10);
  assert.ok(left.every((k) => k.includes("fresh")));
});

test("a share revoked before revokedAt existed is forgotten 30 days after its revocation", () => {
  const now = Date.UTC(2026, 11, 20);
  const box = new ShareMailbox(new MemoryStorage(), { now: () => now });
  const old = { sealed: true, envelopeEnds: {}, revocation: { at: "2026-10-28T00:00:00Z" }, lastRenewal: null, createdAt: "2026-10-27T00:00:00Z" };
  assert.equal(box.keepUntil(old), Date.UTC(2026, 10, 27));
});
