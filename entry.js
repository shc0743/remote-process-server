#!/usr/bin/env node
import { spawn } from 'child_process';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const CLIENT_PY = join(join(__dirname, 'client'), 'client.py');

function runPythonClient(args) {
    const child = spawn('python', [CLIENT_PY, ...args], { stdio: 'inherit' });
    child.on('exit', (code) => process.exit(code));
}

const action = process.argv[2];
const restArgs = process.argv.slice(3);

switch (action) {
    case '--':
        runPythonClient(restArgs);
        break;

    case 'serve':
        runPythonClient(['--type=manager', ...restArgs]);
        break;

    case 'run':
        runPythonClient(restArgs);
        break;

    case 'kill':
    case 'stop':
        runPythonClient(['--kill', ...restArgs]);
        break;

    case 'helpclient':
        runPythonClient(['--help', ...restArgs]);
        break;

    default:
        console.error(`Usage: npx remote-process-server ACTION

Available actions:
  --           Forward the remaining arguments to the Python client as is
  serve        Start the server process
  run          Run something
  stop         Stop the server
  kill         Alias for 'stop'
  helpclient   Show the help of the Python client
`);
        process.exit(1);
}
