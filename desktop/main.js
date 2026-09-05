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
