// Renders hld-generic.html (cloud-agnostic HLD) to hld-generic.png at 2x and runs the page's layout check.
const path = require('path');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 2400, height: 1866 }, deviceScaleFactor: 2 });
  await page.goto('file://' + path.join(__dirname, 'hld-generic.html'));
  await page.evaluate(() => document.fonts.ready);
  const r = await page.evaluate(() => window.checkLayout());
  await page.locator('#canvas').screenshot({ path: path.join(__dirname, 'hld-generic.png') });
  await browser.close();
  console.log(r.issues.length ? r.issues.join('\n') : 'layout check passed');
  process.exit(r.issues.length ? 1 : 0);
})();
