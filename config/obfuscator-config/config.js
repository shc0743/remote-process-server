/** @typedef {Parameters<import('javascript-obfuscator').obfuscate>[1]} ObfuscatorOptions */

/**
 * @param {ObfuscatorOptions} config
 * @returns {ObfuscatorOptions}
 */
export function defineConfig(config) {
  return config
}

const createObfuscator = (files) => defineConfig({
    // basic configuration
    include: files,
    exclude: ['node_modules/**'],
    target: 'node',
    seed: 981207,
    ignoreImports: true,

    // strings
    stringArray: true,
    stringArrayEncoding: ['rc4', 'base64'],
    stringArrayThreshold: 1,
    splitStrings: true,
    splitStringsChunkLength: 10,
    identifierNamesGenerator: 'mangled-shuffled',
    
    // more strings transform, see https://github.com/javascript-obfuscator/javascript-obfuscator/issues/1280
    stringArrayCallsTransform: true,
    stringArrayWrappersType: 'function',
    stringArrayEncodingEnabled: true,
    stringArrayThresholdEnabled: true,
    stringArrayRotate: true,
    stringArrayRotateEnabled: true,
    stringArrayShuffle: true,
    stringArrayShuffleEnabled: true,
    stringArrayWrappersCount: 5,

    // advanced options
    compact: true,
    transformObjectKeys: true,
    numbersToExpressions: true,
    controlFlowFlattening: true,
    controlFlowFlatteningThreshold: 1,
    deadCodeInjection: true,

    // debug options
    sourceMap: false,
    sourceMapMode: 'inline',
});

export default createObfuscator([
    "entry.min.js",
    "maintainance.min.js",
]);
