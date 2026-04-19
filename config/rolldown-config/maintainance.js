import { defineConfig } from 'rolldown';

export default defineConfig({
  input: 'maintainance.js',
  output: {
    format: 'es',
    file: 'maintainance.min.js',
    minify: true,
  },
  platform: 'node',
  external: [/\.node$/],
});
