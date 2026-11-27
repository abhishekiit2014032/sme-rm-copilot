// Renders simple-hld.html to simple-hld.png at 2x; reports text overflowing its box. Needs Playwright with Chromium.
const path = require('path'); const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 2000, height: 1584 }, deviceScaleFactor: 2 });
  await p.goto('file://' + path.join(__dirname, 'simple-hld.html')); await p.evaluate(() => document.fonts.ready);
  const bad = await p.evaluate(() => [...document.querySelectorAll('.b')].filter(e => e.scrollHeight > e.clientHeight + 1 || e.scrollWidth > e.clientWidth + 1).map(e => e.querySelector('.t').textContent));
  await p.locator('#c').screenshot({ path: path.join(__dirname, 'simple-hld.png') }); await b.close();
  console.log(bad.length ? 'overflow: ' + bad.join(', ') : 'no overflow');
})();
