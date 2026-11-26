// Renders simple-architecture.html to azure-simple.png and current-app-simple.png. Needs Playwright with Chromium.
const path = require('path');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1700, height: 1000 }, deviceScaleFactor: 2 });
  await page.goto('file://' + path.join(__dirname, 'simple-architecture.html'));
  for (const [id, file] of [['azure', 'azure-simple.png'], ['local', 'current-app-simple.png']]) {
    await page.locator('#' + id + ' svg').screenshot({ path: path.join(__dirname, file) });
    console.log('wrote', file);
  }
  await browser.close();
})();
