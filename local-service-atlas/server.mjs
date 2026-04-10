import http from "node:http";
import { execFile } from "node:child_process";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

const execFileAsync = promisify(execFile);

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const publicDir = path.join(__dirname, "public");
const configDir = path.join(__dirname, "config");
const servicesConfigPath = path.join(configDir, "services.json");
const serverPort = Number(process.env.PORT || 9114);
const currentUser = os.userInfo().username;
const probeConcurrency = 8;
const probeTimeoutMs = 1200;
const selfPid = process.pid;

const mimeTypes = new Map([
  [".html", "text/html"],
  [".css", "text/css"],
  [".js", "application/javascript"],
  [".json", "application/json"],
  [".svg", "image/svg+xml"],
]);

function sendJson(response, statusCode, data) {
  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  response.end(JSON.stringify(data, null, 2));
}

function sendText(response, statusCode, text, contentType = "text/plain") {
  response.writeHead(statusCode, {
    "Content-Type": `${contentType}; charset=utf-8`,
    "Cache-Control": "no-store",
  });
  response.end(text);
}

async function serveStatic(response, pathname) {
  const normalizedPath = pathname === "/" ? "/index.html" : pathname;
  const filePath = path.join(publicDir, normalizedPath);

  if (!filePath.startsWith(publicDir)) {
    sendText(response, 403, "Forbidden");
    return;
  }

  try {
    const content = await fs.readFile(filePath);
    const contentType = mimeTypes.get(path.extname(filePath)) || "application/octet-stream";
    response.writeHead(200, {
      "Content-Type": `${contentType}; charset=utf-8`,
      "Cache-Control": "no-store",
    });
    response.end(content);
  } catch {
    sendText(response, 404, "Not Found");
  }
}

async function readRequestJson(request) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(chunk);
  }

  if (chunks.length === 0) {
    return {};
  }

  const raw = Buffer.concat(chunks).toString("utf8");
  return JSON.parse(raw);
}

function normalizeAliasInput(payload) {
  const name = typeof payload?.name === "string" ? payload.name.trim() : "";
  const group = typeof payload?.group === "string" ? payload.group.trim() : "";
  const description =
    typeof payload?.description === "string" ? payload.description.trim() : "";
  const tagsInput = Array.isArray(payload?.tags)
    ? payload.tags
    : typeof payload?.tags === "string"
      ? payload.tags.split(",")
      : [];
  const tags = [...new Set(tagsInput.map((value) => String(value).trim()).filter(Boolean))];

  if (!name && !group && !description && tags.length === 0) {
    return null;
  }

  return {
    name,
    group,
    description,
    tags,
    updatedAt: new Date().toISOString(),
  };
}

async function readServicesConfig() {
  try {
    const raw = await fs.readFile(servicesConfigPath, "utf8");
    const parsed = JSON.parse(raw);
    return {
      ports: parsed && typeof parsed === "object" && parsed.ports ? parsed.ports : {},
    };
  } catch (error) {
    if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") {
      return { ports: {} };
    }
    throw error;
  }
}

async function writeServicesConfig(config) {
  await fs.mkdir(configDir, { recursive: true });
  await fs.writeFile(servicesConfigPath, `${JSON.stringify(config, null, 2)}\n`, "utf8");
}

function parseAddress(rawAddress) {
  if (!rawAddress) {
    return null;
  }

  const lastColon = rawAddress.lastIndexOf(":");
  if (lastColon === -1) {
    return null;
  }

  const host = rawAddress.slice(0, lastColon);
  const port = Number(rawAddress.slice(lastColon + 1));
  if (!Number.isFinite(port)) {
    return null;
  }

  return {
    raw: rawAddress,
    host,
    port,
    scope: classifyHostScope(host),
  };
}

function classifyHostScope(host) {
  if (host === "127.0.0.1" || host === "[::1]") {
    return "loopback";
  }
  if (host === "*" || host === "0.0.0.0" || host === "[::]") {
    return "all-interfaces";
  }
  if (host.startsWith("192.168.") || host.startsWith("10.") || host.startsWith("172.")) {
    return "private-network";
  }
  return "specific-interface";
}

function scoreAddress(address) {
  if (address.scope === "loopback" && address.host === "127.0.0.1") {
    return 40;
  }
  if (address.scope === "loopback") {
    return 35;
  }
  if (address.scope === "all-interfaces") {
    return 30;
  }
  if (address.scope === "private-network") {
    return 20;
  }
  return 10;
}

function pickProbeHost(addresses) {
  const sorted = [...addresses].sort((left, right) => scoreAddress(right) - scoreAddress(left));
  const best = sorted[0];
  if (!best) {
    return "127.0.0.1";
  }
  if (best.host === "*" || best.host === "0.0.0.0" || best.host === "[::]") {
    return "127.0.0.1";
  }
  return best.host;
}

function formatReachability(addresses) {
  const scopes = [...new Set(addresses.map((address) => address.scope))];
  if (scopes.includes("all-interfaces")) {
    return "对外监听";
  }
  if (scopes.includes("private-network")) {
    return "局域网接口";
  }
  if (scopes.includes("specific-interface")) {
    return "指定接口";
  }
  return "仅本机";
}

async function listListeningPorts() {
  const { stdout } = await execFileAsync("lsof", [
    "-nP",
    "-iTCP",
    "-sTCP:LISTEN",
    "+c",
    "0",
    "-F",
    "pcuLn",
  ]);

  const processes = [];
  let current = null;

  for (const rawLine of stdout.split(/\r?\n/)) {
    if (!rawLine) {
      continue;
    }

    const field = rawLine[0];
    const value = rawLine.slice(1);

    if (field === "p") {
      current = {
        pid: Number(value),
        command: "",
        uid: "",
        user: "",
        addresses: [],
      };
      processes.push(current);
      continue;
    }

    if (!current) {
      continue;
    }

    if (field === "c") {
      current.command = value;
      continue;
    }

    if (field === "u") {
      current.uid = value;
      continue;
    }

    if (field === "L") {
      current.user = value;
      continue;
    }

    if (field === "n") {
      const parsedAddress = parseAddress(value);
      if (parsedAddress) {
        current.addresses.push(parsedAddress);
      }
    }
  }

  const services = [];

  for (const processInfo of processes) {
    const byPort = new Map();
    for (const address of processInfo.addresses) {
      const existing = byPort.get(address.port);
      if (!existing) {
        byPort.set(address.port, []);
      }
      byPort.get(address.port).push(address);
    }

    for (const [port, addresses] of byPort.entries()) {
      if (port === serverPort && processInfo.pid === selfPid) {
        continue;
      }

      const probeHost = pickProbeHost(addresses);
      services.push({
        pid: processInfo.pid,
        command: processInfo.command || "unknown",
        user: processInfo.user || currentUser,
        port,
        addresses: addresses.sort((left, right) => scoreAddress(right) - scoreAddress(left)),
        bindLabel: addresses.map((address) => address.raw).join(", "),
        reachability: formatReachability(addresses),
        probeHost,
      });
    }
  }

  return services;
}

function extractHtmlTitle(bodyText) {
  const titleMatch = bodyText.match(/<title[^>]*>([^<]+)<\/title>/i);
  if (titleMatch) {
    return titleMatch[1].replace(/\s+/g, " ").trim();
  }

  const headingMatch = bodyText.match(/<h1[^>]*>([^<]+)<\/h1>/i);
  if (headingMatch) {
    return headingMatch[1].replace(/\s+/g, " ").trim();
  }

  return "";
}

function isTextLike(contentType) {
  if (!contentType) {
    return true;
  }

  return /(text\/|json|javascript|xml)/i.test(contentType);
}

async function probeHttpService(service) {
  const attempts = [
    { protocol: "http", host: service.probeHost },
  ];

  if (service.probeHost === "[::1]") {
    attempts.push({ protocol: "http", host: "127.0.0.1" });
  }

  if (service.port === 443 || String(service.port).endsWith("443")) {
    attempts.unshift({ protocol: "https", host: service.probeHost });
  }

  let lastError = "unreachable";

  for (const attempt of attempts) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), probeTimeoutMs);
    const url = `${attempt.protocol}://${attempt.host}:${service.port}/`;

    try {
      const response = await fetch(url, {
        redirect: "manual",
        signal: controller.signal,
      });
      clearTimeout(timeout);

      const contentType = response.headers.get("content-type") || "";
      const serverHeader = response.headers.get("server") || "";
      const location = response.headers.get("location") || "";
      let title = "";
      let preview = "";

      if (isTextLike(contentType)) {
        const body = await response.text();
        const clipped = body.slice(0, 6000);
        title = extractHtmlTitle(clipped);
        preview = clipped
          .replace(/<script[\s\S]*?<\/script>/gi, " ")
          .replace(/<style[\s\S]*?<\/style>/gi, " ")
          .replace(/<[^>]+>/g, " ")
          .replace(/\s+/g, " ")
          .trim()
          .slice(0, 180);
      }

      const isPageLike = /text\/html|application\/xhtml\+xml/i.test(contentType) || Boolean(title);

      return {
        ok: response.status < 400,
        isHttp: true,
        isPageLike,
        url,
        protocol: attempt.protocol,
        status: response.status,
        statusText: response.statusText,
        contentType,
        title,
        preview,
        location,
        serverHeader,
      };
    } catch (error) {
      clearTimeout(timeout);
      lastError =
        error instanceof Error
          ? error.name === "AbortError"
            ? "timeout"
            : error.message
          : String(error);
      continue;
    }
  }

  return {
    ok: false,
    isHttp: false,
    isPageLike: false,
    error: lastError,
  };
}

async function mapLimit(values, limit, mapper) {
  const results = new Array(values.length);
  let cursor = 0;

  async function worker() {
    while (cursor < values.length) {
      const index = cursor;
      cursor += 1;
      results[index] = await mapper(values[index], index);
    }
  }

  const workers = Array.from({ length: Math.min(limit, values.length) }, () => worker());
  await Promise.all(workers);
  return results;
}

function mergeAlias(service, alias) {
  const tags = [...new Set([...(alias?.tags || []), service.command])];
  const detectedName = service.probe?.title || "";
  const hasUsefulTitle = detectedName && !/^404\b/i.test(detectedName);

  return {
    ...service,
    alias: alias || null,
    displayName: alias?.name || detectedName || service.command,
    displayGroup:
      alias?.group ||
      (service.probe?.ok && service.probe?.isPageLike
        ? "Detected Web UI"
        : service.probe?.ok
          ? "HTTP API"
        : service.probe?.isHttp
          ? "HTTP Service"
          : "Listening Port"),
    description:
      alias?.description ||
      service.probe?.preview ||
      (service.probe?.isHttp
        ? "检测到 HTTP 响应，但根路径不是可直接使用的页面。"
        : "检测到本机监听端口，但未识别为 HTTP 页面。"),
    tags,
      hasUsefulTitle,
  };
}

function computeRank(service) {
  let rank = 0;
  if (service.alias?.name) {
    rank += 500;
  }
  if (service.probe?.ok) {
    rank += 300;
  }
  if (service.probe?.title) {
    rank += 120;
  }
  if (service.user === currentUser) {
    rank += 40;
  }
  if (service.reachability === "仅本机") {
    rank += 10;
  }
  return rank - service.port / 100000;
}

async function discoverServices() {
  const config = await readServicesConfig();
  const discovered = await listListeningPorts();
  const probes = await mapLimit(discovered, probeConcurrency, probeHttpService);

  const services = discovered
    .map((service, index) => ({
      ...service,
      probe: probes[index],
      isNamed: Boolean(config.ports[String(service.port)]?.name),
    }))
    .map((service) => mergeAlias(service, config.ports[String(service.port)]))
    .sort((left, right) => computeRank(right) - computeRank(left));

  const summary = {
    total: services.length,
    web: services.filter((service) => service.probe?.ok && service.probe?.isPageLike).length,
    named: services.filter((service) => service.alias?.name).length,
    localOnly: services.filter((service) => service.reachability === "仅本机").length,
  };

  return {
    generatedAt: new Date().toISOString(),
    hostUser: currentUser,
    configPath: servicesConfigPath,
    summary,
    services,
  };
}

function buildAliasResponse(port, alias) {
  return {
    ok: true,
    port,
    alias,
    configPath: servicesConfigPath,
  };
}

const server = http.createServer(async (request, response) => {
  if (!request.url) {
    sendText(response, 400, "Bad Request");
    return;
  }

  const requestUrl = new URL(request.url, `http://${request.headers.host}`);
  const pathname = requestUrl.pathname;

  if (pathname === "/api/health") {
    sendJson(response, 200, {
      ok: true,
      port: serverPort,
      pid: selfPid,
      now: new Date().toISOString(),
      configPath: servicesConfigPath,
    });
    return;
  }

  if (pathname === "/api/services") {
    try {
      const payload = await discoverServices();
      sendJson(response, 200, payload);
    } catch (error) {
      sendJson(response, 500, {
        error: error instanceof Error ? error.message : String(error),
      });
    }
    return;
  }

  if (pathname.startsWith("/api/aliases/")) {
    const portText = decodeURIComponent(pathname.slice("/api/aliases/".length));
    const portNumber = Number(portText);

    if (!Number.isFinite(portNumber)) {
      sendJson(response, 400, { error: "Invalid port" });
      return;
    }

    if (request.method === "POST") {
      try {
        const payload = await readRequestJson(request);
        const config = await readServicesConfig();
        const normalized = normalizeAliasInput(payload);
        if (normalized) {
          config.ports[String(portNumber)] = normalized;
        } else {
          delete config.ports[String(portNumber)];
        }
        await writeServicesConfig(config);
        sendJson(response, 200, buildAliasResponse(portNumber, normalized));
      } catch (error) {
        sendJson(response, 500, {
          error: error instanceof Error ? error.message : String(error),
        });
      }
      return;
    }

    sendJson(response, 405, { error: "Method not allowed" });
    return;
  }

  await serveStatic(response, pathname);
});

server.listen(serverPort, () => {
  console.log(`Local Service Atlas running at http://127.0.0.1:${serverPort}`);
});
