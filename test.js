import { spawn, execSync, spawnSync } from 'child_process';
import fs from 'fs';
//#region lib
const ISWINDOWS = process.platform === 'win32', echo = v => console.log(v), resultOf = cmdOrArgs => (typeof cmdOrArgs === 'string' ? execSync(cmdOrArgs, { encoding: 'utf-8', windowsVerbatimArguments: true }) : spawnSync(cmdOrArgs.shift(), cmdOrArgs, { shell: false, encoding: 'utf-8', windowsVerbatimArguments: true }).stdout).trim(), expect = (testId, cmdOrArgs, expected) => (((fact) => { if (expected !== fact) { execSync('node entry.js kill'); throw new Error('In test #' + testId + '; Result is not expected; expected:\n' + expected + '\n\nBut in fact the result is:\n' + fact) } })(resultOf(cmdOrArgs)), echo('√ ' + testId + ' passed'), true), waitFor = (command, args, waitForText, timeout = 10000) => new Promise((resolve, reject) => { const i = 'ignore', p = 'pipe', D = 'data', e = 'exit', E = 'error', c = spawn(command, args, { stdio: [i, p, p], shell: false }); const t = setTimeout(() => (c.kill(), reject(new Error('Timeout: ' + command + ' ' + args))), timeout); let o = ''; const d = D => (o += D.toString(), o.includes(waitForText)) && (clearTimeout(t), resolve(c)); 'out,err'.split(',').forEach(f => c['std' + f].on(D, d)), c.on(E, e => (clearTimeout(t), reject(e))), c.on(e, code => (clearTimeout(t), reject(new Error(`Process exited without result, code: ${code}, output: ${o}`)))) }), ensureDir = d => !fs.existsSync(d) && fs.mkdirSync(d);
//#endregion lib

// 0. start server
try { execSync('node entry.js kill') } catch {} // kill old processes to avoid conflict
const server = await waitFor('node', ['entry.js', 'daemon'], 'Server has been started');
echo('√ Server started');


// 3. test commands with space
ensureDir('Dir space');
fs.writeFileSync('Dir space/test.txt', 'Content');
expect(3.1, ISWINDOWS ? 'node entry.js run cmd /d /c "echo ""Dir space\\test.txt"""' : 'cat "Dir space/test.txt"', 'Content');
fs.unlinkSync('Dir space/test.txt');
fs.rmdirSync('Dir space');

// 4. test commands with Unicode texts, such as Chinese
ensureDir('中文 space');
fs.writeFileSync('中文 space/test.txt', '中文Content');
expect(3.1, ISWINDOWS ? 'node entry.js run cmd /d /c "chcp 65001 & type ""中文 space\\test.txt"""' : 'cat "中文 space/test.txt"', '中文Content');
fs.unlinkSync('中文 space/test.txt');
fs.rmdirSync('中文 space');

// cleanup
execSync('node entry.js kill');

