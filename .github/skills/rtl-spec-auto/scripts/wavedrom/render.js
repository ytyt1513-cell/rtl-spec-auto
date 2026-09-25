#!/usr/bin/env node
// WaveDrom JSON -> SVG (純 JS、ブラウザ不要)。使い方: node tools/wavedrom/render.js <in.json> [<out.svg>]  または  --all <dir>
// wavedrom-cli と同じ描画経路 (renderAny + onml) だが、PNG 用の native 依存 (svg2img/canvas) を持たない。
'use strict';
const fs = require('fs');
const path = require('path');
const wavedrom = require('wavedrom');
const onml = require('onml');

function render(inPath, outPath) {
  const src = JSON.parse(fs.readFileSync(inPath, 'utf8'));
  const tree = wavedrom.renderAny(0, src, wavedrom.waveSkin);
  let svg = onml.stringify(tree);
  if (!/xmlns=/.test(svg.slice(0, 200))) svg = svg.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"');
  fs.writeFileSync(outPath, svg + '\n');
  return svg.length;
}

const args = process.argv.slice(2);
if (args[0] === '--all') {
  const dir = args[1] || '.';
  const files = fs.readdirSync(dir).filter(f => /_exp_.*\.json$/.test(f)).sort();
  for (const f of files) {
    const out = path.join(dir, f.replace(/\.json$/, '.svg'));
    const n = render(path.join(dir, f), out);
    console.log(`${f} -> ${path.basename(out)} (${n} bytes)`);
  }
  console.log(`${files.length} charts rendered`);
} else if (args[0]) {
  const out = args[1] || args[0].replace(/\.json$/, '.svg');
  console.log(`${args[0]} -> ${out} (${render(args[0], out)} bytes)`);
} else {
  console.error('usage: node render.js <in.json> [out.svg] | --all <dir>'); process.exit(2);
}
