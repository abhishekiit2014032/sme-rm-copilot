// Renders complete-architecture.html to complete-architecture.png (2400x1500 at 2x) and the build-up frames
// step-00.png (v1 prototype) ... step-11.png (one per building block, at 1.5x). Runs the page's layout check
// (box overlap, wires through boxes, labels on wires, text overflow, fonts < 15 px) for every frame and exits
// with code 1 if anything fails. Needs Playwright with Chromium.  Usage: node render.js
const path = require('path');
const { chromium } = require('playwright');

const html = 'file://' + path.join(__dirname, 'complete-architecture.html');
const out = f => path.join(__dirname, f);

async function shoot(browser, step, file, scale) {
  const page = await browser.newPage({ viewport: { width: 2400, height: 1500 }, deviceScaleFactor: scale });
  await page.goto(html + (step === null ? '' : '?step=' + step));
  await page.evaluate(() => document.fonts.ready);
  const report = await page.evaluate(() => window.checkLayout());
  await page.locator('#canvas').screenshot({ path: out(file) });
  await page.close();
  const tag = step === null ? 'complete' : 'step ' + String(step).padStart(2, '0');
  console.log(`${file}: ${report.boxes} boxes, ${report.wires} wire segments, ${report.labels} labels, ` +
              (report.issues.length ? `${report.issues.length} ISSUES` : 'layout check passed') + ` (${tag})`);
  for (const i of report.issues) console.log('   - ' + i);
  return report.issues.length;
}

(async () => {
  const browser = await chromium.launch();
  let bad = await shoot(browser, null, 'complete-architecture.png', 2);
  for (let s = 0; s <= 11; s++) bad += await shoot(browser, s, `step-${String(s).padStart(2, '0')}.png`, 1.5);
  await browser.close();
  if (bad) { console.log(`${bad} layout issue(s)`); process.exit(1); }
})();
