// Renders request-walkthrough.html to request-walkthrough.png at 2x. Needs Playwright with Chromium.
const path = require('path');
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1680, height: 1200 }, deviceScaleFactor: 2 });
  await p.goto('file://' + path.join(__dirname, 'request-walkthrough.html'));
  await p.evaluate(() => document.fonts.ready);
  const over = await p.evaluate(() => [...document.querySelectorAll('pre,.node,.agent')].filter(e => e.scrollWidth > e.clientWidth + 1).length);
  await p.locator('#canvas').screenshot({ path: path.join(__dirname, 'request-walkthrough.png') });
  await b.close();
  console.log(over ? over + ' overflowing boxes' : 'no overflow');
})();
