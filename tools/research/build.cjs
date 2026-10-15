'use strict';
const path = require('node:path');
const fs = require('node:fs');
require('esbuild').buildSync({
  entryPoints: [path.resolve(__dirname, '../../app/research/terminal/adapter.ts')],
  outfile: path.resolve(__dirname, '../../app/research/terminal-runtime.cjs'),
  bundle: true, platform: 'node', target: 'node18', format: 'cjs', treeShaking: true,
  nodePaths: [path.join(__dirname, 'node_modules')], legalComments: 'linked',
});
fs.copyFileSync(path.join(__dirname, 'node_modules/zod/LICENSE'),
  path.resolve(__dirname, '../../app/research/terminal/ZOD-LICENSE'));
