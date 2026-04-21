import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import JavaScriptObfuscator from 'javascript-obfuscator';
import obfuscatorOptions from '../config/obfuscator-config/config.js';

const files = [
  'entry.min.js',
  'maintainance.min.js',
];

for (const file of files) {
  const inputPath = resolve(file);
  const code = await readFile(inputPath, 'utf8');

  const result = JavaScriptObfuscator.obfuscate(code, obfuscatorOptions);
  const obfuscatedCode = result.getObfuscatedCode();

  await writeFile(inputPath, obfuscatedCode, 'utf8');
  console.log(`obfuscated: ${file}`);
}
