import { spawn, execSync, spawnSync } from 'child_process';
import fs from 'fs';
//#region lib
const ISWINDOWS = process.platform === 'win32', echo = v => console.log(v), resultOf = cmdOrArgs => (typeof cmdOrArgs === 'string' ? execSync(cmdOrArgs, { encoding: 'utf-8', windowsVerbatimArguments: true }) : spawnSync(cmdOrArgs.shift(), cmdOrArgs, { shell: false, encoding: 'utf-8', windowsVerbatimArguments: true }).stdout).trim(), expect = (testId, cmdOrArgs, expected, timeout = 10000) => { const t = setTimeout(() => { console.error(new Error('Test #' + testId + ': Timed-out')); process.exit(1) }, timeout); try { const fact = resultOf(cmdOrArgs); clearTimeout(t); if (expected !== fact) { execSync('node entry.js kill'); throw new Error('In test #' + testId + '; Result is not expected; expected:\n' + expected + '\n\nBut in fact the result is:\n' + fact) } echo('√ ' + testId + ' passed'); return true } catch (e) { console.error(String(e)); console.error('--------\nServer output:\n' + server.output); process.exit(1) } }, waitFor = (command, args, waitForText, timeout = 10000) => new Promise((resolve, reject) => { const i = 'ignore', p = 'pipe', D = 'data', e = 'exit', E = 'error', S = 'std', c = spawn(command, args, { stdio: [i, p, p], shell: false }); const t = setTimeout(() => (c.kill(), reject(new Error('Timeout: ' + command + ' ' + args))), timeout); let o = ''; const d = (t, D) => (process[S + t].write(D.toString()), o += D.toString(), o.includes(waitForText)) && (clearTimeout(t), resolve({ process: c, get output() { return o } })); 'out,err'.split(',').forEach(f => c[S + f].on(D, d.bind(null, f))), c.on(E, e => (clearTimeout(t), reject(e))), c.on(e, code => (clearTimeout(t), reject(new Error(`Process exited without result, code: ${code}, output: ${o}`)))) }), ensureDir = d => !fs.existsSync(d) && fs.mkdirSync(d);
//#endregion lib

// 0. start server
try { execSync('node entry.js kill', { stdio: 'ignore' }) } catch {} // kill old processes to avoid conflict
const server = await waitFor('node', ['entry.js', 'daemon'], 'Server has been started');
echo('√ Server started');
server.process.on('exit', (code, signal) => {
    if (!code) return;
    throw new Error('Server exited unexpectedly!! Code: ' + code + ', Signal: ' + signal + ', output:\n' + server.output);
});
// Sleep for a while to check the server status
await new Promise(r => setTimeout(r, 3000));

// 1. test basic command
const simpleExpect = (id, cmd) => (expect(id, 'node entry.js run ' + cmd, resultOf(cmd)));
simpleExpect(1.1, ISWINDOWS ? 'cmd /D /c dir /b' : 'ls');
simpleExpect(1.2, ISWINDOWS ? 'cmd /D /c dir /b %SystemRoot%' : 'bash -ic "ls ~"');

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

// cleanup
execSync('node entry.js kill');
process.exit(0);

