// The Worker on loopback, for tests and a trial on one machine: `node dev_server.mjs ORIGIN [OWNER_JWK_JSON [DIGEST]]`.
// Listens on 127.0.0.1 at a free port and prints `{"port": N}` once; every request is handed to the Worker as if it
// had reached ORIGIN, so a signature over the mailbox's https origin verifies. It logs no path, header or body.
import http from "node:http";
import worker from "./worker.mjs";
import { memoryEnv } from "./memory_env.mjs";

const [origin, ownerText, digest] = process.argv.slice(2);
if (!origin || !/^https:\/\/[a-z0-9.-]+(:[0-9]+)?$/.test(origin)) {
  process.stderr.write("usage: node dev_server.mjs https://ORIGIN [OWNER_JWK_JSON]\n");
  process.exit(2);
}
// With no owner key given, a loopback trial admits any creator; the deployed Worker never does.
const env = memoryEnv({ ownerJwk: ownerText ? JSON.parse(ownerText) : null, digest: digest || "loopback",
  openCreation: !ownerText });

const server = http.createServer((req, res) => {
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", async () => {
    try {
      const body = Buffer.concat(chunks);
      const headers = new Headers();
      for (const [k, v] of Object.entries(req.headers)) headers.set(k, Array.isArray(v) ? v.join(", ") : v);
      // A request as the Worker runtime hands it over, a GET's body included (Cloudflare delivers it; Node's own
      // Request refuses one, which once hid a bug from these tests).
      const request = { url: origin + req.url, method: req.method, headers,
        arrayBuffer: async () => body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength) };
      const answer = await worker.fetch(request, env);
      const out = Buffer.from(await answer.arrayBuffer());
      res.writeHead(answer.status, Object.fromEntries(answer.headers));
      res.end(out);
    } catch (error) {
      res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: { code: "internal", message: error.name } }));
    }
  });
});
server.listen(0, "127.0.0.1", () => {
  process.stdout.write(JSON.stringify({ port: server.address().port }) + "\n");
});
