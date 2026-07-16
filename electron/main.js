// Sales Buddy - Electron desktop shell.
//
// This is the "shell/supervisor" model (option 1): Electron does NOT replace the
// Flask app - it wraps the SAME git-pulled repo. On launch it starts the Python
// supervisor (which spawns the web + worker under SALESBUDDY_SUPERVISED), waits
// for the web server to come up, then loads it in a BrowserWindow. It lives in
// the tray so the background worker keeps running (morning jobs), and it still
// works in a real browser tab because it's just a local web server.
//
// Updates come via git-pull of the repo (same mechanism as the standalone web
// app), so a single `git pull` updates the app logic in both worlds. Electron
// itself only needs updating when this shell code changes.

const { app, BrowserWindow, Tray, Menu, shell, dialog } = require('electron');
const { spawn, execFile } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');

const REPO_ROOT = path.resolve(__dirname, '..');
const IS_WIN = process.platform === 'win32';
const PYTHON = path.join(
  REPO_ROOT, 'venv', 'Scripts', IS_WIN ? 'python.exe' : 'python'
);
const ICON = path.join(REPO_ROOT, 'static', 'icon.ico');
const LOG_DIR = path.join(REPO_ROOT, 'logs');
const MAIN_LOG = path.join(LOG_DIR, 'electron-main.log');
const STACK_LOG = path.join(LOG_DIR, 'electron-stack.log');

const PORT = readEnvPort();
const HEALTH_URL = `http://localhost:${PORT}/health`;
const BASE_URL = `http://localhost:${PORT}/`;

let supervisorProc = null;
let mainWindow = null;
let tray = null;
let isQuitting = false;
let restartTimer = null;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function log(msg) {
  const line = `${new Date().toISOString()} ${msg}\n`;
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    fs.appendFileSync(MAIN_LOG, line);
  } catch (_) { /* ignore */ }
  process.stdout.write(line);
}

function readEnvPort() {
  try {
    const txt = fs.readFileSync(path.join(REPO_ROOT, '.env'), 'utf8');
    const m = txt.match(/^\s*PORT\s*=\s*(\d+)/m);
    if (m) return parseInt(m[1], 10);
  } catch (_) { /* ignore */ }
  return 5151;
}

function readFlaskEnv() {
  try {
    const txt = fs.readFileSync(path.join(REPO_ROOT, '.env'), 'utf8');
    const m = txt.match(/^\s*FLASK_ENV\s*=\s*(.+?)\s*$/m);
    if (m) return m[1].trim().replace(/^['"]|['"]$/g, '').toLowerCase();
  } catch (_) { /* ignore */ }
  return 'production';
}

// Build the environment for the spawned stack. Mirrors scripts/supervisor.ps1:
// point the Azure CLI at the app's isolated per-environment config dir so MSX
// token acquisition uses the same file-based cache as the web/server, and pass
// the port through. Respects an already-set AZURE_CONFIG_DIR.
function buildStackEnv() {
  const env = { ...process.env, PORT: String(PORT) };
  if (!env.AZURE_CONFIG_DIR) {
    const profileFolder =
      readFlaskEnv() === 'development' ? 'SalesBuddyDev' : 'SalesBuddy';
    const home = path.join(os.homedir(), profileFolder);
    env.SALESBUDDY_HOME = home;
    env.AZURE_CONFIG_DIR = path.join(home, '.azure');
  }
  return env;
}

// ---------------------------------------------------------------------------
// Backend stack (Python supervisor -> web + worker)
// ---------------------------------------------------------------------------

function startStack() {
  if (!fs.existsSync(PYTHON)) {
    log(`ERROR: python not found at ${PYTHON}`);
    dialog.showErrorBox(
      'Sales Buddy',
      `Python virtual environment not found at:\n${PYTHON}\n\n` +
      'Run the installer or start.bat once to set it up.'
    );
    return;
  }

  fs.mkdirSync(LOG_DIR, { recursive: true });
  const out = fs.openSync(STACK_LOG, 'a');

  log(`Starting supervisor: ${PYTHON} -m app.supervisor (PORT=${PORT})`);
  supervisorProc = spawn(PYTHON, ['-m', 'app.supervisor'], {
    cwd: REPO_ROOT,
    env: buildStackEnv(),
    // Write stdout/stderr to a file (not a pipe) so nothing can deadlock on a
    // full, unread pipe buffer.
    stdio: ['ignore', out, out],
    windowsHide: true,
  });

  supervisorProc.on('exit', (code, signal) => {
    log(`Supervisor exited (code=${code} signal=${signal})`);
    supervisorProc = null;
    if (!isQuitting) {
      // The Python supervisor should not exit on its own; if it does, bring the
      // whole stack back up after a short delay.
      restartTimer = setTimeout(startStack, 2000);
    }
  });
}

function stopStack() {
  if (restartTimer) { clearTimeout(restartTimer); restartTimer = null; }
  if (!supervisorProc) return;
  const pid = supervisorProc.pid;
  log(`Stopping supervisor tree (pid ${pid})`);
  if (IS_WIN) {
    // Kill the supervisor AND its descendants (web + worker); Windows does not
    // cascade kills to children.
    try { execFile('taskkill', ['/pid', String(pid), '/T', '/F']); } catch (_) {}
  } else {
    try { process.kill(-pid, 'SIGTERM'); } catch (_) {}
  }
  supervisorProc = null;
}

function waitForServer(onReady) {
  const deadline = Date.now() + 60000; // up to 60s for first boot
  const attempt = () => {
    const req = http.get(HEALTH_URL, (res) => {
      res.resume();
      if (res.statusCode === 200) return onReady();
      retry();
    });
    req.on('error', retry);
    req.setTimeout(3000, () => req.destroy());
  };
  const retry = () => {
    if (Date.now() > deadline) {
      log('Timed out waiting for the web server to come up');
      return onReady(); // load anyway; the window will show its own error
    }
    setTimeout(attempt, 500);
  };
  attempt();
}

// ---------------------------------------------------------------------------
// Window + tray
// ---------------------------------------------------------------------------

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    show: false,
    title: 'Sales Buddy',
    icon: ICON,
    autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });

  mainWindow.loadURL(BASE_URL);
  mainWindow.once('ready-to-show', () => mainWindow.show());

  // Close button hides to tray instead of quitting, so the background worker
  // keeps running.
  mainWindow.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });
}

function showWindow() {
  if (!mainWindow) return createWindow();
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function createTray() {
  tray = new Tray(ICON);
  tray.setToolTip('Sales Buddy');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open Sales Buddy', click: showWindow },
    { label: 'Open in Browser', click: () => shell.openExternal(BASE_URL) },
    { type: 'separator' },
    { label: 'Restart Backend', click: () => { stopStack(); startStack(); } },
    { type: 'separator' },
    { label: 'Quit', click: () => { isQuitting = true; app.quit(); } },
  ]));
  tray.on('click', showWindow);
  tray.on('double-click', showWindow);
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

// Single instance: focus the existing window instead of launching a second copy.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', showWindow);

  app.whenReady().then(() => {
    log('Electron ready');
    startStack();
    waitForServer(() => {
      createWindow();
      createTray();
    });
  });

  // Stay alive in the tray when the window is closed.
  app.on('window-all-closed', (e) => {
    // Intentionally do nothing (don't quit) - the tray keeps us running.
  });

  app.on('before-quit', () => {
    isQuitting = true;
    stopStack();
  });
}
