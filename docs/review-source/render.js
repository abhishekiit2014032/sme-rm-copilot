// Renders the architecture review HTML to an A4 PDF with a footer on every page but the cover.
// Needs Playwright with Chromium.
//   node render.js                     -> ../architecture-review.pdf (the approved review)
//   node render.js <file>.html         -> <file>.pdf next to the HTML (for example the detailed variant)
const path = require('path'); const { chromium } = require('playwright');
const MAIN = 'sme-rm-copilot-architecture-review.html';
(async () => {
  const input = process.argv[2] || MAIN;
  const output = input === MAIN
    ? path.join(__dirname, '..', 'architecture-review.pdf')
    : path.join(__dirname, input.replace('.html', '.pdf'));
  const b = await chromium.launch(); const p = await b.newPage();
  await p.goto('file://' + path.join(__dirname, input), { waitUntil: 'networkidle' });
  await p.evaluate(() => document.fonts.ready);
  await p.pdf({ path: output, preferCSSPageSize: true, printBackground: true,
    displayHeaderFooter: true, headerTemplate: '<span></span>',
    footerTemplate: `<div style="width:100%;padding:0 17mm;font:8pt Inter,Arial,sans-serif;color:#7A8499;display:flex;justify-content:space-between">
      <span>SME RM Copilot · Architecture review · working draft</span><span class="pageNumber"></span></div>` });
  await b.close(); console.log('pdf written: ' + output);
})();
