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
const { spawn, execFile, execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');

const APP_VERSION = require('./package.json').version;

// Where the git-pulled Flask app lives (venv, supervisor, static, .env). In dev
// we run `electron .` from inside the repo, so the repo is one level up from
// electron/. When packaged, the signed exe is dropped in a subfolder of the
// install dir (e.g. <install>\electron-dist\Sales Buddy.exe), so the repo is one
// level up from the exe's folder. SALESBUDDY_REPO overrides both if set.
const REPO_ROOT = process.env.SALESBUDDY_REPO
  ? process.env.SALESBUDDY_REPO
  : (app.isPackaged
    ? path.resolve(path.dirname(process.execPath), '..')
    : path.resolve(__dirname, '..'));
const IS_WIN = process.platform === 'win32';
const PYTHON = path.join(
  REPO_ROOT, 'venv', 'Scripts', IS_WIN ? 'python.exe' : 'python'
);
const ICON = path.join(REPO_ROOT, 'static', 'icon.ico');
const LOG_DIR = path.join(REPO_ROOT, 'logs');
const MAIN_LOG = path.join(LOG_DIR, 'electron-main.log');
const STACK_LOG = path.join(LOG_DIR, 'electron-stack.log');
// Sentinel the web app drops (from the Electron window OR a real browser tab)
// to ask the shell to run a git-pull update. Electron owns the update because
// it owns the supervisor process; letting server.ps1 tear the tree down would
// race with our own supervisor-restart handler.
const UPDATE_REQUEST_FILE = path.join(REPO_ROOT, 'data', 'electron-update.request');

const PORT = readEnvPort();
const HEALTH_URL = `http://localhost:${PORT}/health`;
const BASE_URL = `http://localhost:${PORT}/`;

let supervisorProc = null;
let mainWindow = null;
let tray = null;
let isQuitting = false;
let isUpdating = false;
let isBooting = true;
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
  // SALESBUDDY_ELECTRON tells the backend it's running under the shell, so the
  // admin Update button delegates to Electron (sentinel file) instead of
  // spawning server.ps1 and fighting our supervisor.
  const env = { ...process.env, PORT: String(PORT), SALESBUDDY_ELECTRON: '1' };
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
    if (!isQuitting && !isUpdating) {
      // The Python supervisor should not exit on its own; if it does, bring the
      // whole stack back up after a short delay. Suppressed during an update -
      // runUpdate() stops the stack on purpose and relaunches when done.
      restartTimer = setTimeout(startStack, 2000);
    }
  });
}

function stopStack() {
  if (restartTimer) { clearTimeout(restartTimer); restartTimer = null; }
  const pid = supervisorProc ? supervisorProc.pid : null;
  supervisorProc = null;
  if (!IS_WIN) {
    if (pid) { try { process.kill(-pid, 'SIGTERM'); } catch (_) {} }
    return;
  }
  // Windows teardown MUST be synchronous and belt-and-suspenders. Two reasons:
  //   1. before-quit does not await async work, so a fire-and-forget taskkill
  //      gets abandoned when Electron exits - leaving the backend alive. Use the
  //      *Sync* variants so quit actually waits for the kills to finish.
  //   2. The venv launcher shims (python.exe / waitress-serve.exe) spawn the REAL
  //      interpreter as a grandchild and then exit, breaking the parent-PID chain.
  //      So `taskkill /T` from the supervisor pid MISSES those orphans. That is
  //      exactly why a tray Quit left waitress + workers running on port 5151,
  //      whose stale WAL then corrupted a later DB restore. So we ALSO sweep any
  //      backend process whose command line lives under THIS install dir.
  if (pid) {
    log(`Stopping supervisor tree (pid ${pid})`);
    try {
      execFileSync('taskkill', ['/pid', String(pid), '/T', '/F'],
        { windowsHide: true, stdio: 'ignore' });
    } catch (_) { /* already gone */ }
  }
  // Scoped sweep: kill any python/waitress backend whose command line references
  // this install dir, catching shim-orphaned grandchildren taskkill /T can't see.
  try {
    const root = REPO_ROOT.replace(/'/g, "''");
    const ps =
      "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR " +
      "Name='pythonw.exe' OR Name='waitress-serve.exe'\" | Where-Object { " +
      "$_.CommandLine -like '*" + root + "*' } | ForEach-Object { " +
      "taskkill /PID $_.ProcessId /T /F 2>$null }";
    execFileSync('powershell',
      ['-NoProfile', '-NonInteractive', '-Command', ps],
      { windowsHide: true, stdio: 'ignore', timeout: 15000 });
  } catch (_) { /* best effort */ }
  log('Stack stopped');
}

function waitForServer(onReady) {
  const deadline = Date.now() + 60000; // up to 60s for first boot
  let fired = false;
  const finish = () => { if (fired) return; fired = true; onReady(); };
  const attempt = () => {
    const req = http.get(HEALTH_URL, (res) => {
      res.resume();
      if (res.statusCode === 200) return finish();
      retry();
    });
    req.on('error', retry);
    req.setTimeout(3000, () => req.destroy());
  };
  const retry = () => {
    if (fired) return;
    if (Date.now() > deadline) {
      log('Timed out waiting for the web server to come up');
      return finish(); // load anyway; the window will show its own error
    }
    setTimeout(attempt, 500);
  };
  attempt();
}

// ---------------------------------------------------------------------------
// Updates (Electron-managed git pull)
// ---------------------------------------------------------------------------

// Run one external command to completion, logging its output. Rejects on a
// non-zero exit so the update chain can stop and roll back.
function runStep(file, args, cwd) {
  return new Promise((resolve, reject) => {
    log(`> ${file} ${args.join(' ')}`);
    execFile(
      file, args,
      { cwd, windowsHide: true, maxBuffer: 16 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (stdout && stdout.trim()) log(stdout.trim());
        if (stderr && stderr.trim()) log(stderr.trim());
        if (err) return reject(err);
        resolve((stdout || '').trim());
      }
    );
  });
}

// Replace the window contents with a lightweight "updating" splash so the user
// isn't staring at a dead backend while we pull and reinstall.
function showUpdatingScreen() {
  const html =
    '<!doctype html><html><head><meta charset="utf-8"><style>' +
    'body{font-family:Segoe UI,system-ui,sans-serif;background:#0d1117;color:#e6edf3;' +
    'display:flex;flex-direction:column;align-items:center;justify-content:center;' +
    'height:100vh;margin:0}.s{width:42px;height:42px;border:4px solid #30363d;' +
    'border-top-color:#2f81f7;border-radius:50%;animation:spin 1s linear infinite;' +
    'margin-bottom:22px}@keyframes spin{to{transform:rotate(360deg)}}h1{font-weight:600;' +
    'font-size:20px;margin:0 0 8px}p{color:#8b949e;margin:0}</style></head><body>' +
    '<div class="s"></div><h1>Updating Sales Buddy</h1>' +
    '<p>Pulling the latest version. The app will restart automatically.</p>' +
    '</body></html>';
  if (!mainWindow) createWindow();
  showWindow();
  mainWindow.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
}

// Stop the stack, git pull, reinstall deps, then relaunch Electron. Relaunch
// re-runs this shell (picks up main.js changes) and re-spawns the stack (picks
// up backend changes); DB migrations run automatically on the next boot.
async function runUpdate(trigger) {
  if (isUpdating) return;
  isUpdating = true;
  log(`Update starting (trigger=${trigger})`);
  showUpdatingScreen();
  stopStack();
  try {
    await runStep('git', ['pull', '--ff-only'], REPO_ROOT);
    await runStep(
      PYTHON, ['-m', 'pip', 'install', '-q', '-r', 'requirements.txt'], REPO_ROOT
    );
    log('Update complete; relaunching');
    isQuitting = true;
    app.relaunch();
    app.exit(0);
  } catch (e) {
    const msg = (e && e.message) || String(e);
    log(`Update failed: ${msg}`);
    isUpdating = false;
    dialog.showErrorBox(
      'Sales Buddy Update',
      `The update could not be completed:\n\n${msg}\n\n` +
      'The current version will keep running.'
    );
    startStack();
    if (mainWindow) mainWindow.loadURL(BASE_URL);
  }
}

// Poll for the web-app sentinel. Works whether the user clicked Update in the
// Electron window or in a real browser tab pointed at the same local server.
function startUpdateRequestWatcher() {
  setInterval(() => {
    if (isUpdating) return;
    try {
      if (fs.existsSync(UPDATE_REQUEST_FILE)) {
        fs.unlinkSync(UPDATE_REQUEST_FILE);
        runUpdate('web');
      }
    } catch (_) { /* ignore */ }
  }, 1500);
}

// Compare the local HEAD against its upstream. Prompts to update when behind;
// when interactive, also reports "up to date" and surfaces errors.
async function checkForUpdates(interactive) {
  if (isUpdating) return;
  try {
    await runStep('git', ['fetch', '--quiet'], REPO_ROOT);
    const out = await runStep('git', ['rev-list', '--count', 'HEAD..@{u}'], REPO_ROOT);
    const behind = parseInt(out, 10) || 0;
    if (behind > 0) {
      const choice = dialog.showMessageBoxSync({
        type: 'question',
        buttons: ['Update Now', 'Later'],
        defaultId: 0,
        cancelId: 1,
        title: 'Sales Buddy',
        message: `An update is available (${behind} new change${behind === 1 ? '' : 's'}).`,
        detail: 'Sales Buddy will restart to apply the update.',
      });
      if (choice === 0) runUpdate('menu');
    } else if (interactive) {
      dialog.showMessageBox({
        type: 'info', title: 'Sales Buddy', buttons: ['OK'],
        message: "You're up to date.",
      });
    }
  } catch (e) {
    const msg = (e && e.message) || String(e);
    log(`Update check failed: ${msg}`);
    if (interactive) {
      dialog.showErrorBox('Sales Buddy', `Could not check for updates:\n\n${msg}`);
    }
  }
}

// ---------------------------------------------------------------------------
// Window + tray
// ---------------------------------------------------------------------------

// The whole app is same-origin HTTP navigation served from the local backend,
// so navigations must stay INSIDE the window; only genuinely off-site links
// (MSX, aka.ms, etc. - all target=_blank) go to the real browser. The old check
// was a string-prefix allowlist for exactly "localhost:PORT" / "127.0.0.1:PORT".
// That breaks on machines where the window's origin isn't one of those exact
// spellings - e.g. it loaded via [::1] (IPv6 localhost), the machine hostname,
// or the LAN IP because waitress binds 0.0.0.0. There, EVERY relative link
// resolves to that origin, fails the allowlist, and gets shoved to the browser
// (the "every GET opens a new browser window" bug). Fix: treat a URL as internal
// if it shares the host of the page we're currently on (covers all relative
// links regardless of the window's actual origin) or is any loopback host.
function safeCurrentUrl() {
  try { return mainWindow.webContents.getURL() || ''; } catch (_) { return ''; }
}

function isInternalUrl(target) {
  let u;
  try { u = new URL(target); } catch (_) { return true; } // relative/unparsable
  const scheme = u.protocol;
  if (scheme === 'data:' || scheme === 'blob:' || scheme === 'about:') return true;
  if (scheme !== 'http:' && scheme !== 'https:') return false; // mailto:, tel:, ...
  // Same host as the page we're currently on -> in-app navigation.
  try {
    const cur = new URL(safeCurrentUrl());
    if (cur.host && u.host === cur.host) return true;
  } catch (_) { /* no current page yet */ }
  // Any loopback host (any spelling, any port) is this machine's own backend.
  const host = u.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  return host === 'localhost' || host === '127.0.0.1' || host === '::1';
}

function createWindow() {
  // Exactly ONE window, ever. A second launch (single-instance -> second-instance
  // -> showWindow) or a double server-ready callback must focus the existing
  // window, never spawn an orphaned duplicate (observed 2026-07-23: a "Not
  // Running" window plus the real one during a slow dirty-install boot).
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
    return;
  }
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 900,
    minHeight: 600,
    show: false,
    title: 'Sales Buddy',
    icon: ICON,
    backgroundColor: '#0d1117',
    autoHideMenuBar: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });

  mainWindow.loadURL(BASE_URL);
  mainWindow.once('ready-to-show', () => mainWindow.show());

  // Mirror the page's <title>, which already ends in "- Sales Buddy". Appending
  // the app name here would double it ("Home - Sales Buddy - Sales Buddy"); only
  // fall back to the bare app name when a page provides no title.
  mainWindow.on('page-title-updated', (e) => {
    e.preventDefault();
    const t = mainWindow.webContents.getTitle();
    mainWindow.setTitle(t || 'Sales Buddy');
  });

  // Keep in-app navigation on the local server; send anything else (external
  // links, target=_blank, http(s) to other hosts) to the user's real browser.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isInternalUrl(url)) return { action: 'allow' };
    shell.openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (e, url) => {
    if (!isInternalUrl(url)) {
      e.preventDefault();
      log(`nav -> external browser: ${url} (from ${safeCurrentUrl()})`);
      shell.openExternal(url);
    }
  });

  // Keep the top-level Back/Forward buttons' enabled state in sync with the
  // active page's history.
  mainWindow.webContents.on('did-navigate', buildAppMenu);
  mainWindow.webContents.on('did-navigate-in-page', buildAppMenu);

  // F5 reloads the current page. Ctrl+R / Ctrl+Shift+R are handled by the
  // View menu roles; browsers bind F5 too, so wire it up for muscle memory.
  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.type === 'keyDown' && input.key === 'F5') {
      mainWindow.webContents.reload();
    }
  });

  // Right-click context menu with the navigation + clipboard actions people
  // expect from a browser (Electron ships none by default).
  mainWindow.webContents.on('context-menu', (_e, params) => {
    const h = navHistory();
    Menu.buildFromTemplate([
      { label: 'Back', enabled: !!(h && h.canGoBack()), click: goBack },
      { label: 'Forward', enabled: !!(h && h.canGoForward()), click: goForward },
      { role: 'reload' },
      { type: 'separator' },
      { role: 'cut', enabled: params.editFlags.canCut },
      { role: 'copy', enabled: params.editFlags.canCopy },
      { role: 'paste', enabled: params.editFlags.canPaste },
      { role: 'selectAll' },
    ]).popup({ window: mainWindow });
  });

  // Close button hides to tray instead of quitting, so the background worker
  // keeps running.
  mainWindow.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });
}

// Direct-action navigation bound to the active page's history. Used by the
// top-level "< Back" / "Forward >" menu buttons and their accelerators.
function navHistory() {
  return (mainWindow && !mainWindow.isDestroyed())
    ? mainWindow.webContents.navigationHistory : null;
}
function goBack() {
  const h = navHistory();
  if (h && h.canGoBack()) h.goBack();
}
function goForward() {
  const h = navHistory();
  if (h && h.canGoForward()) h.goForward();
}

// Native application menu. Gives the shell the standard File/Edit/View/Window/
// Help structure users expect from a desktop app (plus our own actions). The
// leading "< Back" / "Forward >" entries are top-level buttons: clicking them
// navigates immediately, no submenu.
function buildAppMenu() {
  const h = navHistory();
  const template = [
    {
      label: 'File',
      submenu: [
        { label: 'Open in Browser', click: () => shell.openExternal(BASE_URL) },
        { type: 'separator' },
        { label: 'Check for Updates...', click: () => checkForUpdates(true) },
        {
          label: 'Restart Backend',
          click: () => {
            stopStack();
            startStack();
            if (mainWindow) mainWindow.loadURL(BASE_URL);
          },
        },
        { type: 'separator' },
        {
          label: 'Close',
          accelerator: 'CmdOrCtrl+W',
          click: () => { if (mainWindow) mainWindow.hide(); },
        },
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { role: 'selectAll' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    {
      label: 'Window',
      submenu: [
        { role: 'minimize' },
      ],
    },
    {
      role: 'help',
      submenu: [
        { label: 'Open Logs Folder', click: () => shell.openPath(LOG_DIR) },
        { label: 'Open App Folder', click: () => shell.openPath(REPO_ROOT) },
        { type: 'separator' },
        { label: 'About Sales Buddy', click: showAbout },
      ],
    },
    {
      label: '< Back',
      enabled: !!(h && h.canGoBack()),
      accelerator: 'Alt+Left',
      click: goBack,
    },
    {
      label: 'Forward >',
      enabled: !!(h && h.canGoForward()),
      accelerator: 'Alt+Right',
      click: goForward,
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function showAbout() {
  dialog.showMessageBox(mainWindow, {
    type: 'info',
    title: 'About Sales Buddy',
    message: 'Sales Buddy',
    detail:
      `Version ${APP_VERSION}\n` +
      `Electron ${process.versions.electron}  •  ` +
      `Chromium ${process.versions.chrome}  •  Node ${process.versions.node}\n\n` +
      'A note-taking desktop app for Azure technical sellers.',
    buttons: ['OK'],
    icon: fs.existsSync(ICON) ? ICON : undefined,
    noLink: true,
  });
}

function showWindow() {
  // During boot the window is intentionally deferred until /health answers, so a
  // tray click or a second-instance in that window must NOT create an early
  // "Not Running" window - the boot path creates the single window once the
  // server is up.
  if (isBooting) return;
  if (!mainWindow || mainWindow.isDestroyed()) return createWindow();
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
    { label: 'Check for Updates', click: () => checkForUpdates(true) },
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
    log(`Electron ready (v${APP_VERSION}, packaged=${app.isPackaged}, repo=${REPO_ROOT})`);
    // Group under one taskbar icon and identify our notifications on Windows.
    if (IS_WIN) app.setAppUserModelId('com.salesbuddy.desktop');
    buildAppMenu();
    startStack();
    waitForServer(() => {
      isBooting = false;
      createWindow();
      createTray();
      startUpdateRequestWatcher();
      // Quiet check shortly after boot: only prompts if an update is available.
      setTimeout(() => checkForUpdates(false), 5000);
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
