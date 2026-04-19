import { readFile, writeFile } from 'fs/promises';

const version = JSON.parse(await readFile('package.json', { encoding: 'utf-8' })).version;
const expectContent = `#pragma once
#define SERVER_VERSION "${version}"

`;
const actualContent = await (async () => { try { return await readFile('server_version.h', { encoding: 'utf-8' }) } catch {} })();
if (expectContent !== actualContent) await writeFile('server_version.h', expectContent);

