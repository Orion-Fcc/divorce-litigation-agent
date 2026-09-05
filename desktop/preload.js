const { contextBridge, ipcRenderer } = require("electron");

// 暴露安全的桥接接口给页面；每次调用前清理旧监听器，避免流式回调重复叠加
contextBridge.exposeInMainWorld("dlcBridge", {
  chatStart(payload, handlers) {
    ipcRenderer.removeAllListeners("dlc-chat-chunk");
    ipcRenderer.removeAllListeners("dlc-chat-done");
    ipcRenderer.removeAllListeners("dlc-chat-error");
    ipcRenderer.on("dlc-chat-chunk", (_e, data) => handlers.onChunk && handlers.onChunk(data));
    ipcRenderer.on("dlc-chat-done", () => handlers.onDone && handlers.onDone());
    ipcRenderer.on("dlc-chat-error", (_e, msg) => handlers.onError && handlers.onError(msg));
    return ipcRenderer.invoke("dlc-chat-start", payload);
  },
  // 通用 API 代理（视觉模型等）
  apiRequest(payload) {
    return ipcRenderer.invoke("dlc-api-request", payload);
  },
  // 文档解析（PDF / docx），buffer 为 ArrayBuffer
  parseFile(name, buffer) {
    return ipcRenderer.invoke("dlc-parse-file", { name: name, buffer: buffer });
  }
});
