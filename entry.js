#!/usr/bin/env node
import { spawn } from 'child_process';

const action = process.argv[2]

switch (action) {
    case "serve":
        spawn('python', ['client.py', '--type=manager', ...process.argv.slice(3)], {
            stdio: 'inherit'
        }).on('exit', (code) => {
            process.exit(code)
        });
        break;

    default:
        console.error("Usage: npx remote-process-server ACTION\n\n" +
            "Available actions:\n" +
            "  serve: Start the server process\n"
        );
        process.exit(1);
}

