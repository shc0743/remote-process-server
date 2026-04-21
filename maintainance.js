#!/usr/bin/env node
import { createRequire } from 'node:module';
import {
    cpSync,
    existsSync,
    mkdirSync,
    readFileSync,
    readdirSync,
    rmSync,
    writeFileSync,
    chmodSync,
    renameSync,
    unlinkSync,
    rmdirSync,
    lstatSync,
} from 'fs';
import { basename, dirname, isAbsolute, join, normalize, resolve } from 'path';
import { fileURLToPath } from 'url';
import { execFileSync } from 'child_process';
import { createInterface } from 'node:readline/promises';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const PKG = JSON.parse(readFileSync(join(__dirname, 'package.json'), 'utf-8'));
const PRODUCT_NAME = PKG.name || 'remote-process-server';
const CURRENT_VERSION = PKG.version;

const IS_WINDOWS = process.platform === 'win32';
const require = createRequire(import.meta.url);

const ROOT_ARTIFACTS = [
    'installation.data',
    'remote-process-server.js',
    'remote-process-server.cmd',
    'remote-process-server',
    'package.json',
];

function unique(values) {
    return [...new Set(values.filter(Boolean))];
}

function isNonEmptyString(value) {
    return typeof value === 'string' && value.trim().length > 0;
}

function parseMaintenanceArgs(argv, { allowCreateLink = false, allowRestart = false } = {}) {
    let destination = null;
    let yes = false;
    let createLink = false;
    let restart = null;
    let restartSpecified = false;

    for (const token of argv) {
        if (token === '--yes' || token === '-y') {
            yes = true;
            continue;
        }

        if (allowCreateLink && token === '--create-link') {
            createLink = true;
            continue;
        }

        if (allowRestart && token === '--restart') {
            restartSpecified = true;
            restart = true;
            continue;
        }

        if (allowRestart && token.startsWith('--restart=')) {
            restartSpecified = true;
            const value = token.slice('--restart='.length).trim().toLowerCase();
            if (value === 'yes' || value === 'true' || value === '1') {
                restart = true;
                continue;
            }
            if (value === 'no' || value === 'false' || value === '0') {
                restart = false;
                continue;
            }
            throw new Error(`Invalid value for --restart: ${token.slice('--restart='.length)}`);
        }

        if (token === '-h' || token === '--help') {
            continue;
        }

        if (token.startsWith('-')) {
            throw new Error(`Unknown option: ${token}`);
        }

        if (destination === null) {
            destination = token;
        } else {
            throw new Error(`Unexpected positional argument: ${token}`);
        }
    }

    return {
        destination,
        yes,
        createLink,
        restart,
        restartSpecified,
    };
}

async function promptYesNo(message) {
    if (!process.stdin.isTTY) {
        return false;
    }

    const rl = createInterface({
        input: process.stdin,
        output: process.stdout,
    });

    try {
        const answer = await rl.question(message);
        return /^[yY](?:es)?$/.test(answer.trim());
    } finally {
        rl.close();
    }
}

function getWindowsSystemRootDir() {
    if (!IS_WINDOWS) {
        return null;
    }

    const env = process.env;
    const candidates = [
        env.SystemRoot,
        env.WINDIR,
    ].filter(isNonEmptyString);

    if (candidates.length > 0) {
        return normalize(candidates[0]);
    }

    return env.SystemDrive ? normalize(`${env.SystemDrive}\\Windows`) : 'C:\\Windows';
}

function getBinaryLinkTargetPath() {
    if (IS_WINDOWS) {
        return join(getWindowsSystemRootDir(), 'remote-process-server.cmd');
    }

    const candidates = [];
    if (isNonEmptyString(process.env.PREFIX)) {
        candidates.push(join(process.env.PREFIX, 'bin'));
    }
    candidates.push('/usr/bin', '/bin');

    for (const candidate of candidates) {
        if (existsSync(candidate)) {
            return join(candidate, 'remote-process-server');
        }
    }

    return null;
}

function makeWindowsSystemLinkWrapper(installRoot) {
    const target = join(installRoot, 'remote-process-server.cmd');
    return `@echo off
setlocal
set "TARGET=${target}"
if not exist "%TARGET%" (
    echo ${PRODUCT_NAME}: installation not found at "%TARGET%"
    exit /b 1
)
call "%TARGET%" %*
exit /b %ERRORLEVEL%
`;
}

function makePosixSystemLinkWrapper(installRoot) {
    const target = join(installRoot, 'remote-process-server');
    return `#!/usr/bin/env sh
set -eu
TARGET='${target.replace(/'/g, `'"'"'`)}'
if [ ! -x "$TARGET" ]; then
    printf '%s: installation not found at %s\\n' '${PRODUCT_NAME}' "$TARGET" >&2
    exit 1
fi
exec "$TARGET" "$@"
`;
}

function createBinaryLink(installRoot) {
    const targetPath = getBinaryLinkTargetPath();
    if (!targetPath) {
        return {
            created: false,
            targetPath: null,
            warning: `Warning: binary link creation failed (no suitable target directory was found for the system PATH). You may need to manually add the installation to PATH.`,
        };
    }

    const wrapper = IS_WINDOWS
        ? makeWindowsSystemLinkWrapper(installRoot)
        : makePosixSystemLinkWrapper(installRoot);

    try {
        mkdirSync(dirname(targetPath), { recursive: true });
        writeFileSync(targetPath, wrapper, 'utf-8');
        if (!IS_WINDOWS) {
            try {
                chmodSync(targetPath, 0o755);
            } catch {
                // best effort only
            }
        }
        return {
            created: true,
            targetPath,
            warning: null,
        };
    } catch (err) {
        return {
            created: false,
            targetPath,
            warning: `Warning: binary link creation failed (${err?.message || err}). You may need to manually add the installation to PATH.`,
        };
    }
}

async function maybePromptForContinuation(kind, productName, installRoot) {
    if (!process.stdin.isTTY) {
        return true;
    }

    const message = kind === 'install'
        ? `Will install ${productName} to ${installRoot}, continue? (y/N) `
        : `Will uninstall ${productName} from ${installRoot}, continue? (y/N) `;
    return await promptYesNo(message);
}

async function maybeRestartWindowsAfterUninstall(result, restartOption) {
    if (!IS_WINDOWS || !result.pendingPaths?.length) {
        return false;
    }

    if (restartOption === false) {
        return false;
    }

    let shouldRestart = restartOption === true;
    if (restartOption === null) {
        shouldRestart = await promptYesNo('Do you want to restart now to remove these files? (y/N) ');
    }

    if (!shouldRestart) {
        return false;
    }

    try {
        execFileSync('shutdown', ['/r', '/t', '0', '/f'], {
            stdio: 'inherit',
        });
        return true;
    } catch (err) {
        console.warn(`Warning: failed to restart Windows automatically (${err?.message || err}).`);
        return false;
    }
}

function getWindowsProgramFilesDir() {
    if (!IS_WINDOWS) return null;

    const env = process.env;
    const candidates = [
        env.ProgramW6432,
        env.ProgramFiles,
        env['ProgramFiles(x86)'],
    ].filter(Boolean);

    if (candidates.length > 0) {
        return candidates[0];
    }

    for (const cmd of ['powershell', 'pwsh']) {
        try {
            const out = execFileSync(
                cmd,
                ['-NoProfile', '-Command', '[Environment]::GetFolderPath("ProgramFiles")'],
                {
                    encoding: 'utf8',
                    stdio: ['ignore', 'pipe', 'ignore'],
                }
            ).trim();

            if (out) return out;
        } catch {
            // keep trying
        }
    }

    return env.SystemDrive ? `${env.SystemDrive}\\Program Files` : 'C:\\Program Files';
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
    try {
        return JSON.parse(readFileSync(pathname, 'utf-8'));
    } catch (err) {
        throw new Error(`Failed to parse JSON file: ${pathname}\n${err?.stack || err}`);
    }
}

function writeJsonAtomic(pathname, value) {
    const tmp = `${pathname}.tmp-${process.pid}-${Date.now()}`;
    writeFileSync(tmp, `${JSON.stringify(value)}\n`, 'utf-8');
    try {
        renameSync(tmp, pathname);
    } catch {
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

function isOurInstallationData(data) {
    return !!data
        && typeof data === 'object'
        && data.productName === PRODUCT_NAME
        && typeof data.currentVersion === 'string'
        && data.currentVersion.length > 0;
}

function readInstallationData(installRoot) {
    return readJsonIfExists(getInstallDataPath(installRoot));
}

function writeInstallationData(installRoot, data) {
    writeJsonAtomic(getInstallDataPath(installRoot), data);
}

function makeRootPackageJson() {
    return {
        name: PRODUCT_NAME,
        private: true,
        type: 'module',
        main: 'remote-process-server.js',
    };
}

function makeRuntimeLauncherJs() {
    return `#!/usr/bin/env node
import { existsSync, readFileSync } from 'fs';
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
    console.error('${PRODUCT_NAME}: unable to read installation.data:', err);
    process.exit(1);
}

if (!data || typeof data.currentVersion !== 'string' || !data.currentVersion) {
    console.error('${PRODUCT_NAME}: installation.data is invalid');
    process.exit(1);
}

const entryPath = join(__dirname, 'package', data.currentVersion, 'entry.js');

if (!existsSync(entryPath)) {
    console.error(\`${PRODUCT_NAME}: installed entry point not found: \${entryPath}\`);
    process.exit(1);
}

const result = spawnSync(process.execPath, [entryPath, ...process.argv.slice(2)], {
    stdio: 'inherit',
    env: process.env,
});

if (result.error) {
    console.error('${PRODUCT_NAME}: failed to launch installed entry point:', result.error);
    process.exit(1);
}

if (typeof result.status === 'number') {
    process.exit(result.status);
}

process.exit(1);
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

    writeFileSync(
        join(installRoot, 'remote-process-server.js'),
        makeRuntimeLauncherJs(),
        'utf-8'
    );

    const wrapperPath = IS_WINDOWS
        ? join(installRoot, 'remote-process-server.cmd')
        : join(installRoot, 'remote-process-server');

    writeFileSync(wrapperPath, IS_WINDOWS ? makeWindowsCmdWrapper() : makePosixShellWrapper(), 'utf-8');

    writeFileSync(
        join(installRoot, 'package.json'),
        `${JSON.stringify(makeRootPackageJson())}\n`,
        'utf-8'
    );

    if (!IS_WINDOWS) {
        try {
            chmodSync(join(installRoot, 'remote-process-server.js'), 0o755);
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

function isDeletionBlockedError(err) {
    const code = String(err?.code || '');
    return (
        code === 'EPERM'
        || code === 'EACCES'
        || code === 'EBUSY'
        || code === 'ENOTEMPTY'
        || code === 'ERROR_ACCESS_DENIED'
    );
}

function tryLoadWindowsRebootDeleteAddon() {
    if (!IS_WINDOWS) return null;

    try {
        return require('./native/node/delayed_delete.windows_amd64.node');
    } catch {
        return null;
    }
}

function tryScheduleDeleteOnReboot(paths) {
    const addon = tryLoadWindowsRebootDeleteAddon();
    if (!addon || typeof addon.scheduleDeleteOnReboot !== 'function') {
        return {
            supported: false,
            scheduledPaths: [],
            failedPaths: unique(paths),
        };
    }

    const scheduledPaths = [];
    const failedPaths = [];

    for (const pathname of unique(paths)) {
        try {
            addon.scheduleDeleteOnReboot(pathname);
            scheduledPaths.push(pathname);
        } catch {
            failedPaths.push(pathname);
        }
    }

    return {
        supported: true,
        scheduledPaths,
        failedPaths,
    };
}

function removeTreeBestEffort(pathname, options = {}) {
    const scheduleReboot = options.scheduleReboot !== false;

    const report = {
        target: pathname,
        removedPaths: [],
        pendingPaths: [],
        scheduledPaths: [],
        failedSchedulingPaths: [],
        warnings: [],
        existsAfter: false,
        removed: false,
        rebootNeeded: false,
    };

    if (!existsSync(pathname)) {
        report.removed = true;
        return report;
    }

    if (!IS_WINDOWS) {
        try {
            rmSync(pathname, {
                recursive: true,
                force: true,
                maxRetries: 3,
                retryDelay: 100,
            });
        } catch (err) {
            report.warnings.push(`Failed to remove ${pathname}: ${err?.message || err}`);
        }

        report.existsAfter = existsSync(pathname);
        report.removed = !report.existsAfter;
        return report;
    }

    const visit = (currentPath) => {
        if (!existsSync(currentPath)) {
            return;
        }

        let stat;
        try {
            stat = lstatSync(currentPath);
        } catch (err) {
            report.warnings.push(`Failed to stat ${currentPath}: ${err?.message || err}`);
            report.pendingPaths.push(currentPath);
            return;
        }

        if (stat.isDirectory() && !stat.isSymbolicLink()) {
            let children = [];
            try {
                children = readdirSync(currentPath);
            } catch (err) {
                report.warnings.push(`Failed to read directory ${currentPath}: ${err?.message || err}`);
                report.pendingPaths.push(currentPath);
                return;
            }

            for (const child of children) {
                visit(join(currentPath, child));
            }

            try {
                rmdirSync(currentPath);
                report.removedPaths.push(currentPath);
            } catch (err) {
                if (existsSync(currentPath)) {
                    if (isDeletionBlockedError(err)) {
                        report.pendingPaths.push(currentPath);
                    } else {
                        report.warnings.push(`Failed to remove directory ${currentPath}: ${err?.message || err}`);
                        report.pendingPaths.push(currentPath);
                    }
                }
            }
            return;
        }

        try {
            unlinkSync(currentPath);
            report.removedPaths.push(currentPath);
        } catch (err) {
            if (existsSync(currentPath)) {
                if (isDeletionBlockedError(err)) {
                    report.pendingPaths.push(currentPath);
                } else {
                    report.warnings.push(`Failed to remove file ${currentPath}: ${err?.message || err}`);
                    report.pendingPaths.push(currentPath);
                }
            }
        }
    };

    visit(pathname);

    report.pendingPaths = unique(report.pendingPaths.filter(existsSync));

    if (report.pendingPaths.length > 0 && scheduleReboot) {
        const scheduleResult = tryScheduleDeleteOnReboot(report.pendingPaths);
        report.scheduledPaths = scheduleResult.scheduledPaths;
        report.failedSchedulingPaths = scheduleResult.failedPaths;
        report.rebootNeeded = report.pendingPaths.length > 0;

        if (!scheduleResult.supported) {
            report.warnings.push(
                'Windows reboot-delete support is not available; remaining paths will need manual removal after restart.'
            );
        } else if (scheduleResult.failedPaths.length > 0) {
            report.warnings.push(
                `Some leftover paths could not be scheduled for deletion on reboot: ${scheduleResult.failedPaths.join(', ')}`
            );
        }
    }

    report.existsAfter = existsSync(pathname);
    report.removed = !report.existsAfter;

    return report;
}

function formatPathList(paths) {
    const list = unique(paths);
    if (list.length === 0) return '';
    if (list.length <= 3) return list.join(', ');
    return `${list.slice(0, 3).join(', ')} … (+${list.length - 3} more)`;
}

function prepareVersionDir(installRoot, version) {
    const versionDir = getVersionDir(installRoot, version);

    if (existsSync(versionDir)) {
        const cleanup = removeTreeBestEffort(versionDir, { scheduleReboot: false });
        if (existsSync(versionDir)) {
            const details = cleanup.pendingPaths.length > 0
                ? ` Remaining paths: ${formatPathList(cleanup.pendingPaths)}`
                : '';
            throw new Error(
                `Cannot prepare version directory for installation: ${versionDir}.${details}`
            );
        }
    }

    return versionDir;
}

async function install(targetArg = null, options = {}) {
    const { yes = false, createLink = false } = options;
    const installRoot = normalizeInstallRoot(targetArg);
    const sourceDir = __dirname;
    const sourceDirAbs = normalize(resolve(sourceDir));

    let previousData = null;
    let mode = 'install';

    if (existsSync(installRoot)) {
        const stat = lstatSync(installRoot);
        if (!stat.isDirectory()) {
            throw new Error(`Target path exists but is not a directory: ${installRoot}`);
        }

        previousData = readInstallationData(installRoot);

        if (isOurInstallationData(previousData)) {
            mode = 'update';
        } else {
            const entries = readdirSync(installRoot);
            if (entries.length > 0) {
                throw new Error(
                    `Target directory is not empty and is not a remote-process-server installation: ${installRoot}`
                );
            }
        }
    }

    if (!yes) {
        const confirmed = await maybePromptForContinuation('install', PRODUCT_NAME, installRoot);
        if (!confirmed) {
            throw new Error('Installation cancelled by user');
        }
    }

    mkdirSync(getPackageRoot(installRoot), { recursive: true });

    const versionDir = prepareVersionDir(installRoot, CURRENT_VERSION);
    const versionDirAbs = normalize(resolve(versionDir));

    let linkReport = {
        created: false,
        targetPath: null,
        warning: null,
    };

    try {
        copyTree(sourceDir, versionDir);
        writeLauncherFiles(installRoot);

        linkReport = createLink ? createBinaryLink(installRoot) : {
            created: false,
            targetPath: null,
            warning: null,
        };

        const now = new Date().toISOString();
        const installedVersions = new Set(
            Array.isArray(previousData?.installedVersions) ? previousData.installedVersions : []
        );

        if (previousData?.currentVersion) {
            installedVersions.add(previousData.currentVersion);
        }
        installedVersions.add(CURRENT_VERSION);

        const data = {
            productName: PRODUCT_NAME,
            installRoot,
            packageRoot: 'package',
            currentVersion: CURRENT_VERSION,
            previousVersion: mode === 'update' ? (previousData?.currentVersion || null) : null,
            installedVersions: Array.from(installedVersions),
            launcher: IS_WINDOWS ? 'remote-process-server.cmd' : 'remote-process-server',
            activeEntry: `package/${CURRENT_VERSION}/entry.js`,
            binaryLinkPath: linkReport.created ? linkReport.targetPath : (previousData?.binaryLinkPath || null),
            createdAt: previousData?.createdAt || now,
            updatedAt: now,
        };

        writeInstallationData(installRoot, data);

        let oldCleanup = null;
        const previousVersion = previousData?.currentVersion || null;

        if (mode === 'update' && previousVersion && previousVersion !== CURRENT_VERSION) {
            const oldVersionDir = getVersionDir(installRoot, previousVersion);
            const oldVersionDirAbs = normalize(resolve(oldVersionDir));

            // Avoid deleting the source directory if someone is updating from the
            // currently running installed tree.
            if (oldVersionDirAbs !== sourceDirAbs && oldVersionDirAbs !== versionDirAbs) {
                oldCleanup = removeTreeBestEffort(oldVersionDir, { scheduleReboot: true });
            } else {
                oldCleanup = {
                    target: oldVersionDir,
                    removedPaths: [],
                    pendingPaths: [],
                    scheduledPaths: [],
                    failedSchedulingPaths: [],
                    warnings: [
                        'Skipped old-version cleanup because it would have touched the currently running source tree.',
                    ],
                    existsAfter: existsSync(oldVersionDir),
                    removed: false,
                    rebootNeeded: false,
                    skipped: true,
                };
            }
        }

        return {
            installRoot,
            version: CURRENT_VERSION,
            previousVersion: previousData?.currentVersion || null,
            mode,
            dataPath: getInstallDataPath(installRoot),
            versionDir,
            oldCleanup,
            binaryLinkCreated: linkReport.created,
            binaryLinkPath: data.binaryLinkPath,
            binaryLinkWarning: linkReport.warning,
        };
    } catch (err) {
        // Best effort rollback for a failed install/update.
        try {
            removeTreeBestEffort(versionDir, { scheduleReboot: false });
        } catch {
            // ignore
        }
        if (linkReport?.created && linkReport.targetPath) {
            try {
                removeTreeBestEffort(linkReport.targetPath, { scheduleReboot: false });
            } catch {
                // ignore
            }
        }
        throw err;
    }
}

function removeKnownRootArtifacts(installRoot) {
    const reports = [];
    for (const artifact of ROOT_ARTIFACTS) {
        const fullPath = join(installRoot, artifact);
        if (!existsSync(fullPath)) continue;
        reports.push(removeTreeBestEffort(fullPath, { scheduleReboot: true }));
    }
    return reports;
}

function removeRootDirectoryIfEmpty(installRoot) {
    if (!existsSync(installRoot)) {
        return {
            removed: true,
            pendingPaths: [],
            remainingEntries: [],
            warnings: [],
        };
    }

    const entries = readdirSync(installRoot);
    if (entries.length > 0) {
        return {
            removed: false,
            pendingPaths: [],
            remainingEntries: entries,
            warnings: [],
        };
    }

    try {
        rmdirSync(installRoot);
        return {
            removed: true,
            pendingPaths: [],
            remainingEntries: [],
            warnings: [],
        };
    } catch (err) {
        const pendingPaths = [installRoot];
        const scheduleResult = tryScheduleDeleteOnReboot(pendingPaths);
        return {
            removed: false,
            pendingPaths,
            remainingEntries: [],
            warnings: scheduleResult.supported
                ? [`Failed to remove empty install root immediately: ${err?.message || err}`]
                : [
                    `Failed to remove empty install root immediately: ${err?.message || err}`,
                    'Windows reboot-delete support is unavailable, so the empty install root may need manual cleanup after restart.',
                ],
            rebootNeeded: true,
        };
    }
}

async function uninstall(targetArg = null, options = {}) {
    const { yes = false, restart = null, restartSpecified = false } = options;
    const inferredRoot = targetArg ? null : inferInstallRootFromModuleDir();
    const installRoot = normalizeInstallRoot(targetArg || inferredRoot || getDefaultInstallRoot());

    if (!existsSync(installRoot)) {
        return {
            installRoot,
            removed: false,
            notFound: true,
        };
    }

    const data = readInstallationData(installRoot);
    if (!isOurInstallationData(data)) {
        throw new Error(
            `The target directory is not a valid ${PRODUCT_NAME} installation (missing or invalid installation.data): ${installRoot}`
        );
    }

    if (!yes) {
        const confirmed = await maybePromptForContinuation('uninstall', PRODUCT_NAME, installRoot);
        if (!confirmed) {
            throw new Error('Uninstallation cancelled by user');
        }
    }

    const packageDir = getPackageRoot(installRoot);

    const binaryLinkReport = data.binaryLinkPath && existsSync(data.binaryLinkPath)
        ? removeTreeBestEffort(data.binaryLinkPath, { scheduleReboot: true })
        : {
            target: data.binaryLinkPath || null,
            removedPaths: [],
            pendingPaths: [],
            scheduledPaths: [],
            failedSchedulingPaths: [],
            warnings: [],
            existsAfter: false,
            removed: true,
            rebootNeeded: false,
        };

    const rootArtifactReports = removeKnownRootArtifacts(installRoot);
    const packageReport = existsSync(packageDir)
        ? removeTreeBestEffort(packageDir, { scheduleReboot: true })
        : {
            target: packageDir,
            removedPaths: [],
            pendingPaths: [],
            scheduledPaths: [],
            failedSchedulingPaths: [],
            warnings: [],
            existsAfter: false,
            removed: true,
            rebootNeeded: false,
        };

    const rootCheck = removeRootDirectoryIfEmpty(installRoot);

    const pendingPaths = unique([
        ...binaryLinkReport.pendingPaths,
        ...rootArtifactReports.flatMap(r => r.pendingPaths || []),
        ...packageReport.pendingPaths,
        ...rootCheck.pendingPaths,
    ]);

    const scheduledPaths = unique([
        ...binaryLinkReport.scheduledPaths,
        ...rootArtifactReports.flatMap(r => r.scheduledPaths || []),
        ...packageReport.scheduledPaths,
    ]);

    const warnings = unique([
        ...binaryLinkReport.warnings,
        ...rootArtifactReports.flatMap(r => r.warnings || []),
        ...packageReport.warnings,
        ...rootCheck.warnings,
    ]);

    const remainingEntries = rootCheck.remainingEntries || [];
    const rootRemoved = rootCheck.removed;
    const restartNeeded = IS_WINDOWS && pendingPaths.length > 0;
    let restarted = false;

    if (restartNeeded) {
        const shouldRestart = await maybeRestartWindowsAfterUninstall({ pendingPaths }, restartSpecified ? restart : null);
        restarted = shouldRestart;
    }

    return {
        installRoot,
        removed: true,
        rootRemoved,
        remainingEntries,
        data,
        binaryLinkReport,
        rootArtifactReports,
        packageReport,
        pendingPaths,
        scheduledPaths,
        warnings,
        restartNeeded,
        restarted,
        restartSpecified,
    };
}

function printInstallResult(result) {
    if (result.mode === 'install') {
        console.log(`Installed ${PRODUCT_NAME} ${result.version} to ${result.installRoot}`);
        console.log(`Version payload: ${result.versionDir}`);
        console.log(`Launcher: ${join(result.installRoot, IS_WINDOWS ? 'remote-process-server.cmd' : 'remote-process-server')}`);
    } else {
        if (result.previousVersion && result.previousVersion !== result.version) {
            console.log(`Updated ${PRODUCT_NAME} at ${result.installRoot}`);
            console.log(`Previous version: ${result.previousVersion}`);
            console.log(`Current version: ${result.version}`);
            console.log(`Version payload: ${result.versionDir}`);
        } else {
            console.log(`Refreshed existing ${PRODUCT_NAME} ${result.version} at ${result.installRoot}`);
            console.log(`Version payload: ${result.versionDir}`);
        }

        if (result.oldCleanup) {
            if (result.oldCleanup.skipped) {
                console.log('Old version cleanup was skipped because it would have touched the currently running source tree.');
            } else if (result.oldCleanup.removed) {
                console.log(`Previous version directory removed: ${result.oldCleanup.target}`);
            } else if (result.oldCleanup.pendingPaths?.length) {
                console.log(`Previous version cleanup is not fully finished yet: ${formatPathList(result.oldCleanup.pendingPaths)}`);
            }
        }

        if (result.oldCleanup?.scheduledPaths?.length) {
            console.log(`Some old files will be removed after restart: ${formatPathList(result.oldCleanup.scheduledPaths)}`);
        }

        if (result.oldCleanup?.warnings?.length) {
            for (const warning of result.oldCleanup.warnings) {
                console.warn(`Warning: ${warning}`);
            }
        }

        console.log(`Launcher: ${join(result.installRoot, IS_WINDOWS ? 'remote-process-server.cmd' : 'remote-process-server')}`);
    }

    if (result.binaryLinkCreated) {
        console.log(`Binary link created: ${result.binaryLinkPath}`);
    }

    if (result.binaryLinkWarning) {
        console.warn(result.binaryLinkWarning);
    }
}

function printUninstallResult(result) {
    if (result.notFound) {
        console.log(`No installation found at ${result.installRoot}`);
        return;
    }

    if (result.pendingPaths?.length) {
        console.log(
            `The installation directory couldn't be removed immediately because it couldn't delete ${formatPathList(result.pendingPaths)}. You need to restart your computer to completely uninstall this product.`
        );
    }

    if (result.scheduledPaths?.length) {
        console.log(`Some paths were scheduled for deletion after restart: ${formatPathList(result.scheduledPaths)}`);
    }

    if (result.restarted) {
        console.log('Restarting now...');
    }

    if (result.warnings?.length) {
        for (const warning of result.warnings) {
            console.warn(`Warning: ${warning}`);
        }
    }

    if (result.rootRemoved) {
        console.log(`Uninstalled ${PRODUCT_NAME} from ${result.installRoot}`);
        return;
    }

    if (result.remainingEntries?.length) {
        console.log(
            `Removed generated installation files from ${result.installRoot}, but the installation directory was not automatically removed because it still contains ${result.remainingEntries.length} entries. Please remove it manually if you need.`
        );
    } else {
        console.log(
            `Removed generated installation files from ${result.installRoot}, but the installation directory could not be removed immediately. Please remove it manually if you need.`
        );
    }
}

async function main(argv = process.argv.slice(2)) {
    const [command = '', ...rest] = argv;

    if (rest[0] === '-h' || rest[0] === '--help') {
        switch (command) {
            case 'install':
            case 'update':
                console.error(`[1;4mUsage:[0m maintainance.js ${command} [InstallationDestination] [--yes] [--create-link]

[1;4mDescription:[0m
  Install or update the application to the specified directory.
  If no InstallationDestination is provided, the default install root is used.
  --yes skips the confirmation prompt.
  --create-link creates a PATH wrapper in a system directory.`);
                break;
            case 'uninstall':
                console.error(`[1;4mUsage:[0m maintainance.js uninstall [InstallationDestination] [--yes] [--restart=(yes|no)]

[1;4mDescription:[0m
  Remove an installed copy of ${PRODUCT_NAME}.
  --yes skips the confirmation prompt.
  --restart controls whether Windows restarts to finish removing locked files.
  If no InstallationDestination is provided, the command will try to infer the install
  root from the current module location, or fall back to the default install root.`);
                break;
            case 'where':
                console.error(`[1;4mUsage:[0m maintainance.js where [InstallationDestination]

[1;4mDescription:[0m
  Print the normalized install root path.
  If a InstallationDestination is given, it is normalized and printed.
  Otherwise, the default install root is printed.`);
                break;
            default:
                console.error(`[1;4mUsage:[0m maintainance.js <command> [options]

[1;4mCommands:[0m
  install [path]   Install or update the application to the specified directory
  update [path]    Alias for install
  uninstall [path] Uninstall the application from the specified directory
  where [path]     Print the normalized install root path

[1;4mOptions:[0m
  -h, --help       Show this help message`);
        }
        return;
    }

    switch (command) {
        case 'install':
        case 'update': {
            const parsed = parseMaintenanceArgs(rest, { allowCreateLink: true });
            const result = await install(parsed.destination, {
                yes: parsed.yes,
                createLink: parsed.createLink,
            });
            printInstallResult(result);
            break;
        }

        case 'uninstall': {
            const parsed = parseMaintenanceArgs(rest, { allowRestart: true });
            if (!IS_WINDOWS && parsed.restartSpecified) {
                console.warn('Warning: --restart is only supported on Windows and will be ignored.');
            }
            const result = await uninstall(parsed.destination, {
                yes: parsed.yes,
                restart: parsed.restart,
                restartSpecified: parsed.restartSpecified,
            });
            printUninstallResult(result);
            break;
        }

        case 'where': {
            const parsed = parseMaintenanceArgs(rest, {});
            if (parsed.createLink || parsed.restartSpecified || parsed.yes) {
                throw new Error('The where command does not accept option flags');
            }
            console.log(normalizeInstallRoot(parsed.destination));
            break;
        }

        default:
            throw new Error(`Unknown maintenance command: ${command || '(empty)'}`);
    }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
    Promise.resolve(main()).catch((err) => {
        console.error(err?.stack || err);
        process.exit(1);
    });
}

export {
    CURRENT_VERSION,
    PRODUCT_NAME,
    getDefaultInstallRoot,
    inferInstallRootFromModuleDir,
    install,
    main,
    normalizeInstallRoot,
    removeTreeBestEffort,
    uninstall,
    writeLauncherFiles,
};

