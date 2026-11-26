// Renders every ```mermaid block in docs/architecture/01-hld.md to SVG and PNG in this folder (hld-NN-<slug>.*).
// Usage: node docs/architecture/diagrams/render-hld.js /path/to/node_modules/mermaid/dist/mermaid.min.js
// Needs Playwright with Chromium and a local mermaid 11 bundle (npm i mermaid@11.4.1). Nothing is fetched by the page.
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const mermaidJs = process.argv[2];
if (!mermaidJs) { console.error('pass the path to mermaid.min.js'); process.exit(2); }
const dir = __dirname;
const md = fs.readFileSync(path.join(dir, '..', '01-hld.md'), 'utf8');

const blocks = [];
let heading = '';
const re = /^(#{2,3} [^\n]+)$|```mermaid\n([\s\S]*?)```/gm;
let m;
while ((m = re.exec(md))) {
  if (m[1]) heading = m[1].replace(/^#+\s*/, '');
  else blocks.push({ heading, code: m[2] });
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1800, height: 1200 }, deviceScaleFactor: 2 });
  await page.setContent('<!doctype html><html><body style="margin:0;background:#F6F4EF"><div id="out" style="display:inline-block;padding:32px;background:#F6F4EF"></div></body></html>');
  await page.addScriptTag({ path: mermaidJs });
  await page.evaluate(() => window.mermaid.initialize({
    startOnLoad: false, theme: 'base', securityLevel: 'strict', fontFamily: 'Inter, Segoe UI, Arial, sans-serif',
    flowchart: { htmlLabels: true, curve: 'basis', nodeSpacing: 40, rankSpacing: 60 },
    sequence: { mirrorActors: false, wrap: true, width: 190, messageFontSize: 15, actorFontSize: 15 },
    themeVariables: {
      fontSize: '16px', primaryColor: '#FCFBF8', primaryBorderColor: '#8CC7B4', primaryTextColor: '#0F2233',
      lineColor: '#0B7A65', secondaryColor: '#DCEFE9', tertiaryColor: '#EAF4F0', clusterBkg: '#EAF4F0', clusterBorder: '#8CC7B4',
      actorBkg: '#DCEFE9', actorBorder: '#0B7A65', signalColor: '#0B7A65', noteBkgColor: '#F8EBD0', noteBorderColor: '#E6C98A',
      labelBoxBkgColor: '#F8EBD0', loopTextColor: '#0F2233', activationBkgColor: '#DCEFE9',
    },
  }));
  const out = [];
  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i];
    const slug = b.heading.toLowerCase().replace(/\(.*?\)/g, '').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 48);
    const base = `hld-${String(i + 1).padStart(2, '0')}-${slug}`;
    const svg = await page.evaluate(async ([id, code]) => (await window.mermaid.render(id, code)).svg, ['d' + i, b.code]);
    fs.writeFileSync(path.join(dir, base + '.svg'), svg);
    await page.evaluate(s => {
      const out = document.getElementById('out');
      out.innerHTML = s;
      const el = out.querySelector('svg');
      const [, , w, h] = el.getAttribute('viewBox').split(/\s+/).map(Number);
      el.removeAttribute('style'); el.setAttribute('width', w); el.setAttribute('height', h);
    }, svg);
    await page.locator('#out').screenshot({ path: path.join(dir, base + '.png') });
    out.push({ file: base, heading: b.heading });
    console.log('wrote', base);
  }
  fs.writeFileSync(path.join(dir, 'hld-index.json'), JSON.stringify(out, null, 2));
  await browser.close();
})();
