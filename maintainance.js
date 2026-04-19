#!/usr/bin/env node
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync, chmodSync, renameSync } from 'fs';
import { basename, dirname, isAbsolute, join, normalize, resolve } from 'path';
import { fileURLToPath } from 'url';
import { execFileSync, spawnSync } from 'child_process';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const PKG = JSON.parse(readFileSync(join(__dirname, 'package.json'), 'utf-8'));
const PRODUCT_NAME = PKG.name || 'remote-process-server';
const CURRENT_VERSION = PKG.version;
const IS_WINDOWS = process.platform === 'win32';

function getWindowsProgramFilesDir() {
    if (!IS_WINDOWS) return null;

    const env = process.env;
    const candidates = [env.ProgramW6432, env.ProgramFiles, env['ProgramFiles(x86)']].filter(Boolean);
    if (candidates.length > 0) return candidates[0];

    for (const cmd of ['powershell', 'pwsh']) {
        try {
            const out = execFileSync(cmd, ['-NoProfile', '-Command', '[Environment]::GetFolderPath("ProgramFiles")'], {
                encoding: 'utf8',
                stdio: ['ignore', 'pipe', 'ignore'],
            }).trim();
            if (out) return out;
        } catch {
            // keep trying
        }
    }

    return env.SystemDrive ? (env.SystemDrive + '\\Program Files') : 'C:\\Program Files';
}

function getDefaultInstallRoot() {
    if (IS_WINDOWS) {
        return join(getWindowsProgramFilesDir(), PRODUCT_NAME);
    }
    return join('/opt', PRODUCT_NAME);
}

function normalizeInstallRoot(inputPath) {
    const raw = inputPath && String(inputPath).trim();
    const target = raw ? raw : getDefaultInstallRoot();
    return normalize(isAbsolute(target) ? target : resolve(process.cwd(), target));
}

function getPackageRoot(installRoot) {
    return join(installRoot, 'package');
}

function getVersionDir(installRoot, version) {
    return join(getPackageRoot(installRoot), version);
}

function getInstallDataPath(installRoot) {
    return join(installRoot, 'installation.data');
}

function readJsonIfExists(pathname) {
    if (!existsSync(pathname)) return null;
    return JSON.parse(readFileSync(pathname, 'utf-8'));
}

function writeJsonAtomic(pathname, value) {
    const tmp = `${pathname}.tmp-${process.pid}-${Date.now()}`;
    writeFileSync(tmp, `${JSON.stringify(value, null, 2)}\n`, 'utf-8');
    try {
        renameSync(tmp, pathname);
    } catch (err) {
        try {
            rmSync(pathname, { force: true });
        } catch {
            // ignore
        }
        renameSync(tmp, pathname);
    }
}

function copyTree(sourceDir, targetDir) {
    mkdirSync(targetDir, { recursive: true });
    cpSync(sourceDir, targetDir, {
        recursive: true,
        force: true,
        dereference: false,
        preserveTimestamps: true,
        errorOnExist: false,
    });
}

export function removeTreeBestEffort(pathname) {
    if (!existsSync(pathname)) return;

    if (!IS_WINDOWS) {
        try {
            rmSync(pathname, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
        } catch (err) {
            console.warn(`Warning: failed to remove ${pathname}`, err);
        }
        return;
    }

    // Windows: try only once
    try {
        rmSync(pathname, { recursive: true, force: true, maxRetries: 0 });
    } catch (err) {
        console.warn(`Warning: initial remove failed for ${pathname}`, err);
    }

    if (!existsSync(pathname)) return;

    const addon = require('native/delayed_delete.windows_amd64.node');
    if (!addon || typeof addon.scheduleDeleteOnReboot !== "function") {
        console.warn(`Warning: Windows native addon not available, cannot schedule reboot delete for ${pathname}`);
        return;
    }

    try {
        const result = addon.scheduleDeleteOnReboot(pathname);

        if (result?.failed?.length) {
            for (const item of result.failed) {
                console.warn(
                    `Warning: failed to schedule reboot delete for ${item.path} (code=${item.errorCode}): ${item.errorMessage}`
                );
            }
        }
    } catch (err) {
        console.warn(`Warning: failed to schedule reboot delete for ${pathname}`, err);
    }
}

function makeRuntimeLauncherJs() {
    return `#!/usr/bin/env node
import { readFileSync } from 'fs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';
import { spawnSync } from 'child_process';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const dataPath = join(__dirname, 'installation.data');

let data;
try {
    data = JSON.parse(readFileSync(dataPath, 'utf-8'));
} catch (err) {
    console.error('remote-process-server: unable to read installation.data:', err);
    process.exit(1);
}

const entryPath = join(__dirname, 'package', data.currentVersion, 'entry.js');
const result = spawnSync(process.execPath, [entryPath, ...process.argv.slice(2)], { stdio: 'inherit' });

if (result.error) {
    console.error('remote-process-server: failed to launch installed entry point:', result.error);
    process.exit(1);
}

process.exit(typeof result.status === 'number' ? result.status : 1);
`;
}

function makeCompatibilityEntryJs() {
    return `import './remote-process-server.js';
`;
}

function makeWindowsCmdWrapper() {
    return `@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
node "%SCRIPT_DIR%remote-process-server.js" %*
exit /b %ERRORLEVEL%
`;
}

function makePosixShellWrapper() {
    return `#!/usr/bin/env sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec node "$SCRIPT_DIR/remote-process-server.js" "$@"
`;
}

function writeLauncherFiles(installRoot) {
    mkdirSync(installRoot, { recursive: true });
    writeFileSync(join(installRoot, 'remote-process-server.js'), makeRuntimeLauncherJs(), 'utf-8');
    writeFileSync(join(installRoot, 'entry.js'), makeCompatibilityEntryJs(), 'utf-8');

    const wrapperPath = IS_WINDOWS
        ? join(installRoot, 'remote-process-server.cmd')
        : join(installRoot, 'remote-process-server');
    writeFileSync(wrapperPath, IS_WINDOWS ? makeWindowsCmdWrapper() : makePosixShellWrapper(), 'utf-8');

    if (!IS_WINDOWS) {
        try {
            chmodSync(join(installRoot, 'remote-process-server.js'), 0o755);
            chmodSync(join(installRoot, 'entry.js'), 0o755);
            chmodSync(wrapperPath, 0o755);
        } catch {
            // best effort only
        }
    }
}

function inferInstallRootFromModuleDir(moduleDir = __dirname) {
    const parent = dirname(moduleDir);
    if (basename(parent) === 'package') {
        return dirname(parent);
    }
    return null;
}

function loadInstallationData(installRoot) {
    return readJsonIfExists(getInstallDataPath(installRoot));
}

function writeInstallationData(installRoot, data) {
    writeJsonAtomic(getInstallDataPath(installRoot), data);
}

function install(targetArg = null) {
    const installRoot = normalizeInstallRoot(targetArg);
    const sourceDir = __dirname;
    const sourceVersion = CURRENT_VERSION;
    const previousData = loadInstallationData(installRoot);
    const previousVersion = previousData?.currentVersion || null;
    const versionDir = getVersionDir(installRoot, sourceVersion);

    mkdirSync(getPackageRoot(installRoot), { recursive: true });
    copyTree(sourceDir, versionDir);
    writeLauncherFiles(installRoot);

    const installedVersions = new Set(Array.isArray(previousData?.installedVersions) ? previousData.installedVersions : []);
    installedVersions.add(sourceVersion);

    const data = {
        productName: PRODUCT_NAME,
        currentVersion: sourceVersion,
        previousVersion,
        installedVersions: Array.from(installedVersions),
        installRoot,
        packageRoot: 'package',
        activeEntry: 'package/<version>/entry.js',
        createdAt: previousData?.createdAt || new Date().toISOString(),
        updatedAt: new Date().toISOString(),
    };
    writeInstallationData(installRoot, data);

    if (previousVersion && previousVersion !== sourceVersion) {
        removeTreeBestEffort(getVersionDir(installRoot, previousVersion));
        data.installedVersions = Array.from(new Set([...data.installedVersions, sourceVersion]));
        data.updatedAt = new Date().toISOString();
        writeInstallationData(installRoot, data);
    }

    return { installRoot, version: sourceVersion, dataPath: getInstallDataPath(installRoot) };
}

function uninstall(targetArg = null) {
    const inferredRoot = targetArg ? null : inferInstallRootFromModuleDir();
    const installRoot = normalizeInstallRoot(targetArg || inferredRoot || getDefaultInstallRoot());
    if (!existsSync(installRoot)) {
        return { installRoot, removed: false };
    }

    removeTreeBestEffort(installRoot);
    return { installRoot, removed: true };
}

function printStatus(result, verb) {
    if (verb === 'install') {
        console.log(`Installed ${PRODUCT_NAME} ${result.version} to ${result.installRoot}`);
        console.log(`Launcher: ${join(result.installRoot, IS_WINDOWS ? 'remote-process-server.cmd' : 'remote-process-server')}`);
        return;
    }
    if (verb === 'uninstall') {
        if (result.removed) {
            console.log(`Uninstalled ${PRODUCT_NAME} from ${result.installRoot}`);
        } else {
            console.log(`No installation found at ${result.installRoot}`);
        }
    }
}

function main(argv = process.argv.slice(2)) {
    const [command = '', ...rest] = argv;

    switch (command) {
        case 'install':
        case 'update': {
            const result = install(rest[0]);
            printStatus(result, 'install');
            break;
        }
        case 'uninstall': {
            const result = uninstall(rest[0]);
            printStatus(result, 'uninstall');
            break;
        }
        case 'where': {
            console.log(normalizeInstallRoot(rest[0]));
            break;
        }
        default:
            throw new Error(`Unknown maintenance command: ${command || '(empty)'}`);
    }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
    try {
        main();
    } catch (err) {
        console.error(err);
        process.exit(1);
    }
}

export {
    CURRENT_VERSION,
    PRODUCT_NAME,
    getDefaultInstallRoot,
    inferInstallRootFromModuleDir,
    install,
    main,
    normalizeInstallRoot,
    uninstall,
    writeLauncherFiles,
};
