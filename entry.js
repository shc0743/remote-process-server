#!/usr/bin/env node
import { spawn, execSync } from 'child_process';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { readdirSync, copyFileSync, existsSync, readFileSync } from 'fs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const CLIENT_PY = join(join(__dirname, 'client'), 'client.py');
const PKG = JSON.parse(readFileSync(join(__dirname, 'package.json'), 'utf-8'));
const ISWINDOWS = process.platform === 'win32';

function getSupportedArchs() {
    const files = readdirSync(join(join(__dirname, 'native'), 'bin'));
    const archs = [];
    const prefix = 'rmpsm_server.';
    for (const file of files) {
        if (file.startsWith(prefix)) {
            archs.push(file.slice(prefix.length));
        }
    }
    return archs;
}

function getCurrentArch() {
    const script = join(__dirname, 'sys_name.py');
    if (!existsSync(script)) {
        console.error('Error: sys_name.py not found');
        process.exit(1);
    }
    try {
        return execSync(`python "${script}"`, { encoding: 'utf-8' }).trim();
    } catch (err) {
        console.error('Error executing sys_name.py:', err.message);
        process.exit(1);
    }
}

function getServerBinaryPath(arch) {
    const binaryName = `rmpsm_server.${arch}`;
    const binaryPath = join(join(join(__dirname, 'native'), 'bin'), binaryName);
    if (!existsSync(binaryPath)) {
        console.error(`Error: Server binary for architecture "${arch}" not found.`);
        process.exit(1);
    }
    return binaryPath;
}

function runServerBinary(arch, args) {
    const binaryPath = getServerBinaryPath(arch);
    const child = spawn(binaryPath, args, { stdio: 'inherit', executable: binaryPath });
    child.on('exit', (code) => process.exit(code));
}

function runPythonClient(args, headless = false) {
    const child = spawn((ISWINDOWS && headless) ? 'pythonw' : 'python', [CLIENT_PY, ...args], { stdio: 'inherit', detached: !!headless });
    child.on('exit', (code) => process.exit(code));
}

async function runMaintenance(command, restArgs) {
    const { main } = await import('./maintainance' + (__filename.endsWith('.min.js') ? '.min.js' : '.js'));
    try {
        main([command, ...restArgs]);
    } catch (err) {
        console.error(err?.message || err);
        process.exit(1);
    }
}

const action = process.argv[2];
const restArgs = process.argv.slice(3);

switch (action) {
    case '--':
        runPythonClient(restArgs);
        break;

    case 'serve':
        console.warn('Warning: action "serve" is deprecated and may be removed later; use "daemon" instead');
        // [[fallthrough]];
    case 'daemon':
        runPythonClient(['--type=manager', ...restArgs]);
        break;

    case 'run':
        runPythonClient(['--type=client', ...restArgs]);
        break;

    case 'run-headless':
        runPythonClient(['--type=client', ...restArgs], true);
        break;

    case 'kill':
    case 'stop':
        runPythonClient(['--kill', ...restArgs]);
        break;

    case 'helpclient':
        runPythonClient(['--help', ...restArgs]);
        break;

    case 'install':
    case 'uninstall':
    case 'update':
    case 'where':
        await runMaintenance(action, restArgs);
        break;

    case 'list-arch':
        console.log(getSupportedArchs().join('\n'));
        break;

    case 'arch':
        console.log(getCurrentArch());
        break;

    case 'is-supported': {
        const current = getCurrentArch();
        const supported = getSupportedArchs();
        const isSupported = supported.includes(current);
        console.log(isSupported ? 'true' : 'false');
        process.exit(isSupported ? 0 : 1);
    }

    case 'version':
        console.log(PKG.version);
        break;

    case 'run-server':
        process.chdir(__dirname);
        runServerBinary(getCurrentArch(), restArgs);
        break;

    case 'copy-server': {
        if (restArgs.length < 1) {
            console.error('Usage: copy-server <target_filename> [arch]');
            process.exit(1);
        }
        const targetPath = restArgs[0];
        const arch = restArgs[1] || getCurrentArch();
        const sourcePath = getServerBinaryPath(arch);
        try {
            copyFileSync(sourcePath, targetPath);
            console.log(`Copied ${arch} server binary to ${targetPath}`);
        } catch (err) {
            throw err;
        }
        break;
    }

    default:
        console.error(`\x1b[1;4mUsage:\x1b[0m npx remote-process-server ACTION args

\x1b[1;4mAvailable actions:\x1b[0m

\x1b[1mSpecial commands\x1b[0m
  --           Forward the remaining arguments to the Python client as is
\x1b[1mManager commands\x1b[0m
  daemon       Start the manager process
  serve        Alias for \`start\`; deprecated
\x1b[1mClient commands\x1b[0m
  run          Run something via the manager
  run-headless Run something via the manager without waiting
  stop         Send stop request to the manager
  kill         Alias for \`stop\`
\x1b[1mServer commands\x1b[0m
  run-server   Run the server; this is not the manager daemon
  copy-server  Copy the server binary to the specified path. Usage: copy-server <target_filename> [arch]; Default to current arch; fails if the specified arch is not found
  list-arch    List the currently supported architectures
\x1b[1mInstallation commands\x1b[0m
  install      Copy the package into a installation directory. Usage: install [InstallationDestination]
  update       Install to the target directory and replace the active version. Usage: update [InstallationDestination]
  uninstall    Remove an installed copy. Usage: uninstall [InstallationDestination]
  where        Show the default target installation directory (this does not show the existing installation)
\x1b[1mOther commands\x1b[0m
  arch         Show the current architecture
  is-supported Return whether the current architecture is in the supported architectures list
  helpclient   Show the help of the Python client
  version      Show the current version
`);
        process.exit(1);
}
