import http from "node:http";

const HOST = "127.0.0.1";
const PORT = Number(process.env.DAEMON_MODE_RECEIVER_PORT ?? 4317);
const MAX_BODY_BYTES = 10 * 1024 * 1024;

function readRequestBody(request) {
  return new Promise((resolve, reject) => {
    let body = "";

    request.setEncoding("utf8");

    request.on("data", (chunk) => {
      body += chunk;

      if (Buffer.byteLength(body, "utf8") > MAX_BODY_BYTES) {
        request.destroy();
        reject(new Error("Request body too large"));
      }
    });

    request.on("end", () => resolve(body));
    request.on("error", reject);
  });
}

function sendJson(response, statusCode, payload) {
  response.writeHead(statusCode, {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Type": "application/json"
  });
  response.end(JSON.stringify(payload));
}

function summarizeCapture(payload) {
  return {
    source: payload.source,
    navigationType: payload.navigationType,
    url: payload.url,
    title: payload.title,
    domain: payload.domain,
    capturedAt: payload.capturedAt,
    textLength: payload.textLength,
    textPreview: String(payload.text ?? "").replace(/\s+/g, " ").trim().slice(0, 240)
  };
}

const server = http.createServer(async (request, response) => {
  if (request.method === "OPTIONS") {
    sendJson(response, 204, {});
    return;
  }

  if (request.method !== "POST" || request.url !== "/captures") {
    sendJson(response, 404, {
      ok: false,
      error: "Use POST /captures"
    });
    return;
  }

  try {
    const body = await readRequestBody(request);
    const payload = JSON.parse(body);
    const summary = summarizeCapture(payload);

    console.log("\n[Daemon Mode receiver] Capture received");
    console.log(JSON.stringify(summary, null, 2));

    if (process.env.DAEMON_MODE_PRINT_FULL_PAYLOAD === "1") {
      console.log("[Daemon Mode receiver] Full payload");
      console.log(JSON.stringify(payload, null, 2));
    }

    sendJson(response, 202, {
      ok: true,
      receivedAt: new Date().toISOString(),
      textLength: payload.textLength
    });
  } catch (error) {
    console.error("[Daemon Mode receiver] Failed to handle capture", error);
    sendJson(response, 400, {
      ok: false,
      error: error.message
    });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[Daemon Mode receiver] Listening on http://${HOST}:${PORT}/captures`);
  console.log("[Daemon Mode receiver] Set DAEMON_MODE_PRINT_FULL_PAYLOAD=1 to print full captured text.");
});
