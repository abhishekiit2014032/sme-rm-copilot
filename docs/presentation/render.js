// Renders sme-rm-copilot-architecture-review.html to PDF (A4) with a footer on every page but the cover.
// Needs Playwright with Chromium.  Usage: node render.js
const path = require('path'); const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch(); const p = await b.newPage();
  await p.goto('file://' + path.join(__dirname, process.argv[2] || 'sme-rm-copilot-architecture-review.html'), { waitUntil: 'networkidle' });
  await p.evaluate(() => document.fonts.ready);
  await p.pdf({ path: path.join(__dirname, (process.argv[2] || 'sme-rm-copilot-architecture-review.html').replace('.html', '.pdf')), preferCSSPageSize: true, printBackground: true,
    displayHeaderFooter: true, headerTemplate: '<span></span>',
    footerTemplate: `<div style="width:100%;padding:0 17mm;font:8pt Inter,Arial,sans-serif;color:#7A8499;display:flex;justify-content:space-between">
      <span>SME RM Copilot · Architecture review · working draft</span><span class="pageNumber"></span></div>` });
  await b.close(); console.log('pdf written');
})();
