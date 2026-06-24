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
import { fileURLToPath } from "url";
import http from "http";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const isDev = process.env.NODE_ENV === "development";

let mainWindow = null;
let pythonProcess = null;

const PYTHON_PORT = 7899;
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
    titleBarStyle: "hiddenInset",
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
