// 婚讼管家 · 桌面壳（v2.1）
// 职责：找到并启动 Python 后端（python_app/main.py），等端口就绪后加载页面。
// 所有业务逻辑（法律库检索、自主学习、LLM 代理、文件解析）都在 Python 端。
const { app, BrowserWindow, dialog } = require("electron");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

let backend = null;
let win = null;
let lastPort = 0;
let backendLog = "";

// 依次探测可能的 python_app 位置
function findPythonAppDir() {
  const candidates = [
    path.join(__dirname, "..", "python_app"),                        // 源码模式：desktop/../python_app
    path.join(process.resourcesPath, "app", "python_app"),
    path.join(path.dirname(process.execPath), "python_app"),         // 与 exe 同级
    path.join(path.dirname(process.execPath), "resources", "python_app")
  ];
  for (const c of candidates) {
    if (fs.existsSync(path.join(c, "main.py"))) return c;
  }
  return null;
}

// 探测可用的 Python 解释器
function findPython() {
  const tries = [
    { cmd: "python", args: ["--version"] },
    { cmd: "python3", args: ["--version"] },
    { cmd: "py", args: ["-3", "--version"] }
  ];
  for (const t of tries) {
    try {
      const r = spawnSync(t.cmd, t.args, { encoding: "utf8", windowsHide: true });
      if (r.status === 0) return t;
    } catch (e) { /* 继续尝试下一个 */ }
  }
  return null;
}

function startBackend(pythonAppDir) {
  const py = findPython();
  if (!py) throw new Error("未找到 Python。请安装 Python 3.9+（安装时勾选 Add to PATH）。");
  const portFile = path.join(os.tmpdir(), "hunsonguanjia-port-" + process.pid + ".txt");
  try { fs.unlinkSync(portFile); } catch (e) { /* 不存在则忽略 */ }
  const args = (py.cmd === "py" ? ["-3"] : [])
    .concat([path.join(pythonAppDir, "main.py"), "--no-window", "--port-file", portFile]);
  backend = spawn(py.cmd, args, {
    cwd: pythonAppDir,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"]
  });
  backend.stdout.on("data", (d) => { backendLog += d.toString(); });
  backend.stderr.on("data", (d) => { backendLog += d.toString(); });
  backend.on("exit", (code) => {
    backend = null;
    // 后端意外退出时，前端状态芯片会自动显示「服务未连接」
  });
  return portFile;
}

function waitForPort(portFile, timeoutMs, cb) {
  const start = Date.now();
  const iv = setInterval(() => {
    try {
      const p = fs.readFileSync(portFile, "utf8").trim();
      if (/^\d+$/.test(p)) {
        clearInterval(iv);
        cb(null, parseInt(p, 10));
        return;
      }
    } catch (e) { /* 尚未写入 */ }
    if (backend === null && Date.now() - start > 3000) {
      clearInterval(iv);
      cb(new Error("后端进程已退出"), 0);
      return;
    }
    if (Date.now() - start > timeoutMs) {
      clearInterval(iv);
      cb(new Error("等待后端启动超时"), 0);
    }
  }, 250);
}

function createWindow(port) {
  lastPort = port;
  win = new BrowserWindow({
    width: 1180,
    height: 800,
    minWidth: 860,
    minHeight: 600,
    autoHideMenuBar: true,
    title: "婚讼管家",
    backgroundColor: "#f6f7f9",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  win.loadURL("http://127.0.0.1:" + port + "/");
  win.on("closed", () => { win = null; });
}

app.whenReady().then(() => {
  const pythonAppDir = findPythonAppDir();
  if (!pythonAppDir) {
    dialog.showErrorBox(
      "婚讼管家",
      "未找到 python_app 目录。\n\n请把 python_app 文件夹放到与婚讼管家.exe 同级目录，\n或从源码 desktop/ 目录运行 npm start。"
    );
    app.quit();
    return;
  }
  let portFile;
  try {
    portFile = startBackend(pythonAppDir);
  } catch (e) {
    dialog.showErrorBox("婚讼管家", "启动 Python 后端失败：\n" + e.message);
    app.quit();
    return;
  }
  waitForPort(portFile, 30000, (err, port) => {
    if (err) {
      dialog.showErrorBox(
        "婚讼管家",
        "Python 后端启动失败：" + err.message + "\n\n后端日志：\n" + backendLog.slice(-800)
      );
      app.quit();
      return;
    }
    createWindow(port);
  });
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0 && lastPort) createWindow(lastPort);
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("will-quit", () => {
  if (backend) {
    try { backend.kill(); } catch (e) { /* 忽略 */ }
  }
});
