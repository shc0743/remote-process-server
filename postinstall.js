import { readFile, writeFile } from 'fs/promises';

const version = JSON.parse(await readFile('package.json', { encoding: 'utf-8' })).version;
await writeFile('server_version.h', `#pragma once
#define SERVER_VERSION "${version}"

`);
