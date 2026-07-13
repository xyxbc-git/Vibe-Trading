/**
 * 贾维斯桌面终端 — Electron 主进程。
 *
 * 职责：
 *  1. 启动时自动 spawn Python FastAPI 后端
 *  2. 创建 BrowserWindow 加载 React 前端
 *  3. 应用退出时优雅关闭 Python 进程
 */

import { app, BrowserWindow, shell } from "electron";
import { spawn } from "child_process";
import path from "path";
import os from "os";
import fs from "fs";
import { fileURLToPath } from "url";
import http from "http";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const isDev = process.env.NODE_ENV === "development";

let mainWindow = null;
let pythonProcess = null;

/**
 * [Sprint0] 后端端口从统一配置中心 ~/.vibe-trading/config.yaml 的
 * system.dashboard_port 读取（轻量行解析，不引入 yaml 依赖）；
 * 缺失/解析失败回退 7899，与后端 jarvis_config 默认一致。
 */
function readDashboardPort() {
  try {
    const p = path.join(os.homedir(), ".vibe-trading", "config.yaml");
    const text = fs.readFileSync(p, "utf-8");
    const m = text.match(/^\s*dashboard_port:\s*(\d{4,5})\s*(?:#.*)?$/m);
    if (m) {
      const port = Number(m[1]);
      if (port >= 1024 && port <= 65535) return port;
    }
  } catch {
    // 配置不存在或不可读——用默认端口
  }
  return 7899;
}

const PYTHON_PORT = readDashboardPort();
const VITE_DEV_URL = "http://localhost:5173";

function getVibeTradingDir() {
  return path.resolve(__dirname, "..", "..");
}

function findPython() {
  const venvPython = path.join(getVibeTradingDir(), ".venv", "bin", "python");
  return venvPython;
}

function startPythonBackend() {
  const pythonBin = findPython();
  const dashboardScript = path.join(
    getVibeTradingDir(),
    "jarvis_dashboard.py",
  );

  console.log(`[JARVIS] Starting Python backend: ${pythonBin} ${dashboardScript}`);

  pythonProcess = spawn(pythonBin, [dashboardScript, "--port", String(PYTHON_PORT)], {
    cwd: getVibeTradingDir(),
    env: { ...process.env },
    stdio: ["ignore", "pipe", "pipe"],
  });

  pythonProcess.stdout?.on("data", (data) => {
    console.log(`[Python] ${data.toString().trim()}`);
  });

  pythonProcess.stderr?.on("data", (data) => {
    console.error(`[Python] ${data.toString().trim()}`);
  });

  pythonProcess.on("error", (err) => {
    console.error(`[JARVIS] Failed to start Python backend: ${err.message}`);
  });

  pythonProcess.on("close", (code) => {
    console.log(`[JARVIS] Python backend exited with code ${code}`);
    pythonProcess = null;
  });
}

function waitForBackend(timeoutMs = 30000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    function check() {
      if (Date.now() - start > timeoutMs) {
        reject(new Error("Python backend startup timeout"));
        return;
      }
      const req = http.get(`http://localhost:${PYTHON_PORT}/api/wallet`, (res) => {
        if (res.statusCode === 200) {
          resolve(true);
        } else {
          setTimeout(check, 500);
        }
      });
      req.on("error", () => setTimeout(check, 500));
      req.end();
    }
    check();
  });
}

function stopPythonBackend() {
  if (!pythonProcess) return;
  console.log("[JARVIS] Stopping Python backend...");
  pythonProcess.kill("SIGTERM");
  setTimeout(() => {
    if (pythonProcess) {
      pythonProcess.kill("SIGKILL");
    }
  }, 5000);
}

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    title: "JARVIS Terminal",
    titleBarStyle: "hidden",
    // 红绿灯定位在侧栏顶部 48px 预留区（Sidebar 顶部拖拽条）内垂直居中：
    // 按钮组约 52x12px，x:8 保证收起态 64px 侧栏内不越界，y:18 = (48-12)/2
    trafficLightPosition: { x: 8, y: 18 },
    backgroundColor: "#0d1117",
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  if (isDev) {
    mainWindow.loadURL(VITE_DEV_URL);
  } else {
    const indexPath = path.join(__dirname, "..", "dist", "index.html");
    mainWindow.loadFile(indexPath);
  }

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

app.whenReady().then(async () => {
  startPythonBackend();

  try {
    console.log("[JARVIS] Waiting for Python backend...");
    await waitForBackend();
    console.log("[JARVIS] Python backend is ready!");
  } catch {
    console.warn("[JARVIS] Backend not ready yet, opening window anyway...");
  }

  await createWindow();
});

app.on("window-all-closed", () => {
  stopPythonBackend();
  app.quit();
});

app.on("before-quit", () => {
  stopPythonBackend();
});

app.on("activate", () => {
  if (mainWindow === null) {
    createWindow();
  }
});
