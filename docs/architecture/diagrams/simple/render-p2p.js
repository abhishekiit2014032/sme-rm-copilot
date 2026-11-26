// Renders prototype-to-production.html to prototype-to-production.png (1920x1080 at 2x). Needs Playwright with Chromium.
const path = require('path');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 2 });
  await page.goto('file://' + path.join(__dirname, 'prototype-to-production.html'));
  await page.locator('#slide').screenshot({ path: path.join(__dirname, 'prototype-to-production.png') });
  console.log('wrote prototype-to-production.png');
  await browser.close();
})();
