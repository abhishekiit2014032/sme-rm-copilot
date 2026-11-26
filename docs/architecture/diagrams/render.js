// Renders architecture-diagrams.html to PNGs next to it. Usage: node docs/architecture/diagrams/render.js
// Needs Playwright with Chromium (preinstalled in the build container). Offline: the page loads nothing external.
const path = require('path');
const { chromium } = require('playwright');

(async () => {
  const dir = __dirname;
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 2600, height: 1600 }, deviceScaleFactor: 2 });
  await page.goto('file://' + path.join(dir, 'architecture-diagrams.html'));
  for (const [id, file] of [['azure', 'azure-target-architecture.png'], ['local', 'current-application-architecture.png']]) {
    await page.locator('#' + id).screenshot({ path: path.join(dir, file) });
    console.log('wrote', file);
  }
  await browser.close();
})();
