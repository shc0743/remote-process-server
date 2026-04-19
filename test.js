#!/usr/bin/env node
import { spawn, execSync, spawnSync } from 'child_process';
import fs from 'fs';
import koffi from 'koffi';
//#region lib
const ISWINDOWS = process.platform === 'win32', echo = v => console.log(v), resultOf = cmdOrArgs => (typeof cmdOrArgs === 'string' ? execSync(cmdOrArgs, { encoding: 'utf-8', windowsVerbatimArguments: true }) : spawnSync(cmdOrArgs.shift(), cmdOrArgs, { shell: false, encoding: 'utf-8', windowsVerbatimArguments: true }).stdout).trim(), expect = (testId, cmdOrArgs, expected, timeout = 10000) => { const t = setTimeout(() => { console.error(new Error('Test #' + testId + ': Timed-out')); process.exit(1) }, timeout); try { const fact = resultOf(cmdOrArgs); clearTimeout(t); if (expected !== fact) { execSync('node entry.js kill'); throw new Error('In test #' + testId + '; Result is not expected; expected:\n' + expected + '\n\nBut in fact the result is:\n' + fact) } echo('√ ' + testId + ' passed'); return true } catch (e) { console.error(String(e)); process.exit(server.process.exitCode || 1) } }, waitFor = (command, args, waitForText, timeout = 10000) => new Promise((resolve, reject) => { const i = 'ignore', p = 'pipe', D = 'data', e = 'exit', E = 'error', S = 'std', c = spawn(command, args, { stdio: [i, p, p], shell: false }); const t = setTimeout(() => (c.kill(), reject(new Error('Timeout: ' + command + ' ' + args))), timeout); let o = ''; const d = (t, D) => (process[S + t].write(D.toString()), o += D.toString(), o.includes(waitForText)) && (clearTimeout(t), resolve({ process: c, get output() { return o } })); 'out,err'.split(',').forEach(f => c[S + f].on(D, d.bind(null, f))), c.on(E, e => (clearTimeout(t), reject(e))), c.on(e, code => (clearTimeout(t), reject(new Error(`Process exited without result, code: ${code}, output: ${o}`)))) }), ensureDir = d => !fs.existsSync(d) && fs.mkdirSync(d);
//#endregion lib

// 0. start server
console.log('Loading library, please wait...');
const lib = (() => {
    if (ISWINDOWS) return koffi.load('kernel32.dll');
    const lib = 'libc.so.6,libc.so,libSystem.B.dylib'.split(',');
    for (const i of lib) try { return koffi.load(i); } catch {}
    throw new Error('Cannot load libc');
})();
console.log('Killing old instances, please wait...');
try { execSync('node entry.js kill', { stdio: 'ignore' }) } catch {} // kill old processes to avoid conflict
console.log('Creating event, please wait...');
const hEvent = (() => {
    if (!ISWINDOWS) {
        const eventfd = lib.func('eventfd', 'int', ['uint32', 'int']);
        const fd = eventfd(0, 0);
        if (fd < 0) throw new Error(`eventfd failed`);
        return fd;
    }
    const SECURITY_ATTRIBUTES = koffi.struct('SECURITY_ATTRIBUTES', {
        nLength: 'uint32',
        lpSecurityDescriptor: 'void*',
        bInheritHandle: 'bool'
    });
    const sa = {
        nLength: koffi.sizeof(SECURITY_ATTRIBUTES),
        lpSecurityDescriptor: null,
        bInheritHandle: true
    };
    
    const CreateEventW = lib.func('CreateEventW', 'void*', ['SECURITY_ATTRIBUTES*', 'bool', 'bool', 'str16']);
    const h = CreateEventW(sa, false, false, null);
    if (!h) throw new Error(`CreateEventW failed`);
    return (h);
})();
console.log('Running server, please wait...');
const server = { process: spawn('node', ['entry.js', 'daemon', '--signal=' + String(ISWINDOWS ? koffi.address(hEvent) : hEvent)], { stdio: 'inherit' }), output: 'DEPRECATED' };
// Wait for the server to be ready
try { if (ISWINDOWS) {
    const WaitForSingleObject = lib.func('WaitForSingleObject', 'uint32', ['void*', 'uint32']);
    if (WaitForSingleObject(hEvent, 10000) !== 0) throw new Error('WaitForSingleObject timeout');
} else {
    const buffer = Buffer.alloc(8);
    await new Promise((resolve, reject) => {
        const buffer = Buffer.alloc(8);
        const timeout = setTimeout(() => {
            reject(new Error(`eventfd wait timed out`));
        }, 10000);
        fs.read(hEvent, buffer, 0, 8, null, (err, bytesRead) => {
            clearTimeout(timeout);
            if (err) {
                reject(err);
            } else if (bytesRead !== 8) {
                reject(new Error(`eventfd read incomplete: ${bytesRead} bytes`));
            } else {
                resolve();
            }
        });
    });
} } catch (e) { console.error(e); process.exit(1) }
console.log('√ Server is ready now!!');

// 1. test basic command
const simpleExpect = (id, cmd) => (expect(id, 'node entry.js run ' + cmd, resultOf(cmd)));
simpleExpect(1.1, ISWINDOWS ? 'cmd /D /c dir /b' : 'ls');
simpleExpect(1.2, ISWINDOWS ? 'cmd /D /c dir /b %SystemRoot%' : 'bash -c "ls $HOME"');

// 2. test complex command
ISWINDOWS ? console.log('√ 2.1 Skipped') : expect(2.1, `node|entry.js|run|bash|-c|cat << EOF
Hello World!
This is a text.
EOF`.split('|'), 'Hello World!\nThis is a text.');
simpleExpect(2.2, ISWINDOWS ? 'cmd /D /S /c cd' : 'bash -c pwd');

// 3. test commands with space
ensureDir('Dir space');
fs.writeFileSync('Dir space/test.txt', 'Content');
expect(3.1, ISWINDOWS ? 'node entry.js run --cmd-syntax -- cmd /d /S /c type "Dir space\\test.txt"' : 'cat "Dir space/test.txt"', 'Content');
expect(3.2, ISWINDOWS ? 'cmd.exe /d /c node entry.js run --cmd-syntax -- cmd /d /c chcp 65001 ^> NUL ^& type "Dir space\\test.txt"' : 'bash -c \'cat "Dir space/test.txt"\'', 'Content');
fs.unlinkSync('Dir space/test.txt');
fs.rmdirSync('Dir space');

// 4. test commands with Unicode texts, such as Chinese
ensureDir('中文 space');
fs.writeFileSync('中文 space/test.txt', '中文Content');
expect(4.1, ISWINDOWS ? 'node entry.js run --cmd-syntax -- cmd /d /S /c chcp 65001 >NUL 2>&1 & type "中文 space\\test.txt"' : 'cat "中文 space/test.txt"', '中文Content');
fs.unlinkSync('中文 space/test.txt');
fs.rmdirSync('中文 space');

// -- Windows specific tests
if (ISWINDOWS) {
    console.info('Windows detected, running Windows specific tests...');

    console.log('install test');
    execSync('node entry.js install');
    console.log('uninstall test');
    execSync('node entry.js uninstall');
    console.log('native module test');
    execSync('node entry.js install "C:\\Program Files\\test"');
    execSync('node entry.js kill');
    server.process = spawn('cmd', ['/D', '/S', '/C', '"C:\\Program Files\\test\\remote-process-server.cmd"', 'daemon']);
    await new Promise(r => setTimeout(r, 1000));
    execSync('node entry.js uninstall "C:\\Program Files\\test"');
    await new Promise(r => setTimeout(r, 1000));
    console.log(execSync('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager" /v PendingFileRenameOperations'));
}

// cleanup
execSync('node entry.js kill');

