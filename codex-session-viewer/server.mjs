import http from "node:http";
import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  getDefaultSessionsRoot,
  groupSummariesByDay,
  loadSessionSummaries,
  parseSessionFile,
} from "./src/session-store.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const publicDir = path.join(__dirname, "public");
const sessionsRoot = process.env.SESSIONS_ROOT || getDefaultSessionsRoot();
const port = Number(process.env.PORT || 59111);
const host = process.env.HOST || "127.0.0.1";

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
  const fullPath = path.join(publicDir, normalizedPath);

  if (!fullPath.startsWith(publicDir)) {
    sendText(response, 403, "Forbidden");
    return;
  }

  try {
    const content = await fs.readFile(fullPath);
    const extension = path.extname(fullPath);
    const contentType =
      extension === ".html"
        ? "text/html"
        : extension === ".css"
          ? "text/css"
          : extension === ".js"
            ? "application/javascript"
            : "application/octet-stream";

    response.writeHead(200, {
      "Content-Type": `${contentType}; charset=utf-8`,
      "Cache-Control": "no-store",
    });
    response.end(content);
  } catch (error) {
    sendText(response, 404, "Not Found");
  }
}

function buildSessionPayload(parsedSession) {
  const session = {
    id: parsedSession.sessionMeta.id,
    filePath: parsedSession.filePath,
    lineCount: parsedSession.lineCount,
    recordCount: parsedSession.recordCount,
    parseErrors: parsedSession.parseErrors,
    sessionMeta: parsedSession.sessionMeta,
    turns: parsedSession.turns.map((turn) => ({
      id: turn.id,
      index: turn.index,
      startedAt: turn.startedAt,
      finishedAt: turn.finishedAt,
      promptFocus: turn.promptFocus,
      promptPreview: turn.promptPreview,
      userMessages: turn.userMessages,
      humanUserMessages: turn.humanUserMessages,
      injectedUserMessages: turn.injectedUserMessages,
      developerMessages: turn.developerMessages,
      runtimeDeveloperMessages: turn.runtimeDeveloperMessages,
      memoryMessages: turn.memoryMessages,
      assistantMessages: turn.assistantMessages,
      commentaryMessages: turn.commentaryMessages,
      userMessageEvents: turn.userMessageEvents,
      toolCalls: turn.toolCalls,
      toolOutputs: turn.toolOutputs,
      guardianAssessments: turn.guardianAssessments,
      mcpToolCallResults: turn.mcpToolCallResults,
      patchApplyEvents: turn.patchApplyEvents,
      commandResults: turn.commandResults,
      webSearchEvents: turn.webSearchEvents,
      errors: turn.errors,
      taskStarted: turn.taskStarted,
      taskCompleted: turn.taskCompleted,
      contextCompactedCount: turn.contextCompactedCount,
      reasoningCount: turn.reasoningCount,
      tokenSnapshots: turn.tokenSnapshots,
      lastTokenSnapshot: turn.lastTokenSnapshot,
      skillTrace: turn.skillTrace,
      promptEnvelope: turn.promptEnvelope,
      approvalTrace: turn.approvalTrace,
      compactionTrace: turn.compactionTrace,
      turnMechanics: turn.turnMechanics,
      turnContext: turn.turnContext,
      turnContextCompact: turn.turnContextCompact,
      approxPrompt: turn.approxPrompt,
      historyTranscript: turn.historyTranscript,
      rawRecords: turn.records,
    })),
  };

  return session;
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
      sessionsRoot,
      now: new Date().toISOString(),
    });
    return;
  }

  if (pathname === "/api/sessions") {
    try {
      const summaries = await loadSessionSummaries(sessionsRoot);
      const grouped = groupSummariesByDay(summaries);
      sendJson(response, 200, {
        sessionsRoot,
        summaryCount: summaries.length,
        groups: grouped,
      });
    } catch (error) {
      sendJson(response, 500, {
        error: error instanceof Error ? error.message : String(error),
      });
    }
    return;
  }

  if (pathname.startsWith("/api/sessions/")) {
    try {
      const sessionId = decodeURIComponent(pathname.slice("/api/sessions/".length));
      const summaries = await loadSessionSummaries(sessionsRoot);
      const summary = summaries.find((item) => item.id === sessionId);

      if (!summary?.filePath) {
        sendJson(response, 404, { error: "Session not found" });
        return;
      }

      const parsedSession = await parseSessionFile(summary.filePath);
      sendJson(response, 200, buildSessionPayload(parsedSession));
    } catch (error) {
      sendJson(response, 500, {
        error: error instanceof Error ? error.message : String(error),
      });
    }
    return;
  }

  await serveStatic(response, pathname);
});

server.listen(port, host, () => {
  console.log(
    `Codex session viewer running at http://${host}:${port} (sessions root: ${sessionsRoot})`,
  );
});
