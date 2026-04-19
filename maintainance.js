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
        return require('./native/delayed_delete.windows_amd64.node');
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

function install(targetArg = null) {
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

    mkdirSync(getPackageRoot(installRoot), { recursive: true });

    const versionDir = prepareVersionDir(installRoot, CURRENT_VERSION);
    const versionDirAbs = normalize(resolve(versionDir));

    try {
        copyTree(sourceDir, versionDir);
        writeLauncherFiles(installRoot);

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
        };
    } catch (err) {
        // Best effort rollback for a failed install/update.
        try {
            removeTreeBestEffort(versionDir, { scheduleReboot: false });
        } catch {
            // ignore
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

function uninstall(targetArg = null) {
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

    const packageDir = getPackageRoot(installRoot);

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
        ...rootArtifactReports.flatMap(r => r.pendingPaths || []),
        ...packageReport.pendingPaths,
        ...rootCheck.pendingPaths,
    ]);

    const scheduledPaths = unique([
        ...rootArtifactReports.flatMap(r => r.scheduledPaths || []),
        ...packageReport.scheduledPaths,
    ]);

    const warnings = unique([
        ...rootArtifactReports.flatMap(r => r.warnings || []),
        ...packageReport.warnings,
        ...rootCheck.warnings,
    ]);

    const remainingEntries = rootCheck.remainingEntries || [];
    const rootRemoved = rootCheck.removed;

    return {
        installRoot,
        removed: true,
        rootRemoved,
        remainingEntries,
        data,
        rootArtifactReports,
        packageReport,
        pendingPaths,
        scheduledPaths,
        warnings,
    };
}

function printInstallResult(result) {
    if (result.mode === 'install') {
        console.log(`Installed ${PRODUCT_NAME} ${result.version} to ${result.installRoot}`);
        console.log(`Version payload: ${result.versionDir}`);
        console.log(`Launcher: ${join(result.installRoot, IS_WINDOWS ? 'remote-process-server.cmd' : 'remote-process-server')}`);
        return;
    }

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

function printUninstallResult(result) {
    if (result.notFound) {
        console.log(`No installation found at ${result.installRoot}`);
        return;
    }

    if (result.pendingPaths?.length) {
        console.log(`Some files could not be removed immediately and will need restart cleanup: ${formatPathList(result.pendingPaths)}`);
    }

    if (result.scheduledPaths?.length) {
        console.log(`Some paths were scheduled for deletion after restart: ${formatPathList(result.scheduledPaths)}`);
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
            `Removed generated installation files from ${result.installRoot}, but kept the directory because it still contains: ${result.remainingEntries.join(', ')}`
        );
    } else {
        console.log(
            `Removed generated installation files from ${result.installRoot}, but the directory could not be removed immediately`
        );
    }
}

function main(argv = process.argv.slice(2)) {
    const [command = '', ...rest] = argv;

    switch (command) {
        case 'install':
        case 'update': {
            const result = install(rest[0]);
            printInstallResult(result);
            break;
        }

        case 'uninstall': {
            const result = uninstall(rest[0]);
            printUninstallResult(result);
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
        console.error(err?.stack || err);
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
    removeTreeBestEffort,
    uninstall,
    writeLauncherFiles,
};
