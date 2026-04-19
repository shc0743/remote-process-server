import { defineConfig } from 'rolldown';

export default defineConfig({
  input: 'entry.js',
  output: {
    format: 'es',
    file: 'entry.min.js',
    minify: true,
  },
  platform: 'node',
  external: ['./maintainance.js', './maintainance.min.js'],
});
