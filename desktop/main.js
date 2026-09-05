const { app, BrowserWindow, ipcMain, net } = require("electron");
const path = require("path");

function createWindow() {
  const win = new BrowserWindow({
    width: 1120,
    height: 780,
    minWidth: 820,
    minHeight: 560,
    autoHideMenuBar: true,
    title: "婚讼管家",
    backgroundColor: "#f6f7f9",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  win.loadFile(path.join(__dirname, "index.html"));
}

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

// 渲染进程通过 IPC 请求 DeepSeek API（主进程 net 模块无 CORS 限制）
ipcMain.handle("dlc-chat-start", (event, payload) => {
  const request = net.request({
    method: "POST",
    url: "https://api.deepseek.com/chat/completions"
  });
  request.setHeader("Content-Type", "application/json");
  request.setHeader("Authorization", "Bearer " + payload.apiKey);

  request.on("response", (response) => {
    const status = response.statusCode;
    if (status !== 200) {
      let body = "";
      response.on("data", (c) => { body += c.toString(); });
      response.on("end", () => {
        event.sender.send("dlc-chat-error", "API 错误 " + status + "：" + body.slice(0, 300));
      });
      return;
    }
    response.on("data", (chunk) => {
      event.sender.send("dlc-chat-chunk", chunk.toString());
    });
    response.on("end", () => {
      event.sender.send("dlc-chat-done");
    });
  });

  request.on("error", (err) => {
    event.sender.send("dlc-chat-error", err.message);
  });

  const body = {
    model: payload.model,
    messages: payload.messages,
    stream: !!payload.stream,
    temperature: 0.3
  };
  if (payload.maxTokens) body.max_tokens = payload.maxTokens;

  request.write(JSON.stringify(body));
  request.end();
  return true;
});

// 通用 OpenAI 兼容接口代理（视觉模型等第三方服务，非流式，主进程无 CORS 限制）
ipcMain.handle("dlc-api-request", (event, payload) => {
  return new Promise((resolve) => {
    const request = net.request({ method: "POST", url: payload.url });
    request.setHeader("Content-Type", "application/json");
    request.setHeader("Authorization", "Bearer " + payload.apiKey);
    let body = "";
    request.on("response", (response) => {
      response.on("data", (c) => { body += c.toString(); });
      response.on("end", () => {
        if (response.statusCode !== 200) {
          resolve({ ok: false, status: response.statusCode, error: "API 错误 " + response.statusCode + "：" + body.slice(0, 300) });
          return;
        }
        try {
          resolve({ ok: true, data: JSON.parse(body) });
        } catch (e) {
          resolve({ ok: false, error: "返回内容解析失败" });
        }
      });
    });
    request.on("error", (err) => resolve({ ok: false, error: err.message }));
    request.write(JSON.stringify(payload.body));
    request.end();
  });
});

// 文档解析：PDF（pdf-parse）/ Word docx（mammoth），渲染进程传入文件 ArrayBuffer
ipcMain.handle("dlc-parse-file", async (event, payload) => {
  try {
    const buf = Buffer.from(payload.buffer);
    const parts = payload.name.split(".");
    const ext = parts.length > 1 ? parts.pop().toLowerCase() : "";
    if (ext === "pdf") {
      const { PDFParse } = require("pdf-parse");
      const parser = new PDFParse({ data: buf });
      const result = await parser.getText();
      await parser.destroy();
      return { ok: true, text: result.text || "" };
    }
    if (ext === "docx") {
      const mammoth = require("mammoth");
      const result = await mammoth.extractRawText({ buffer: buf });
      return { ok: true, text: result.value || "" };
    }
    if (ext === "doc") {
      return { ok: false, error: "暂不支持旧版 .doc 格式，请先在 Word/WPS 中另存为 .docx 再上传" };
    }
    return { ok: false, error: "不支持的文件类型：" + ext };
  } catch (e) {
    return { ok: false, error: e.message || "解析异常" };
  }
});
