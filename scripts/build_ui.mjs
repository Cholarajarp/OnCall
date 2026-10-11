// Rebuilds app/vendor/astryx.{js,css} from the pinned packages in package-lock.json.
// app/ is otherwise hand-written and served directly by FastAPI; nothing else is generated.
import { build } from 'esbuild';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '..');

await build({
  entryPoints: [path.join(root, 'app/js/astryx-islands.jsx')],
  outfile: path.join(root, 'app/vendor/astryx.js'),
  bundle: true,
  format: 'esm',
  minify: true,
  target: 'es2022',
  jsx: 'automatic',
  define: { 'process.env.NODE_ENV': '"production"' },
  legalComments: 'eof', // keep third-party license headers in the redistributed bundle
  logLevel: 'info',
});
