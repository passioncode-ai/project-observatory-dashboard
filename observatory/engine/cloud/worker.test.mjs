// The Worker's ports over the contract's vectors (fabric/node-vectors/peer/peer-share.json), and its handler's own
// answers: `node --test cloud/worker.test.mjs` from the engine folder. tests/test_cloud_worker.py runs it, and then
// drives the engine's two share stores against the Worker over HTTP.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import worker, { peerExchangeVerdict, peerFrame, peerJwk, peerKeys, peerRecordStands, peerSealedShape, peerThumbprint,
  instant, rfc8785, parseDictionary, ShareMailbox } from "./worker.mjs";
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
  const env = memoryEnv();
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
