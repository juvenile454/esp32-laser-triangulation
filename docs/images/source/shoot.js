const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const jobs = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const browser = await chromium.launch();
  for (const j of jobs) {
    const ctx = await browser.newContext({ viewport: { width: j.w, height: j.h }, deviceScaleFactor: j.scale });
    const page = await ctx.newPage();
    await page.goto('file://' + j.html);
    await page.waitForTimeout(150);
    await page.screenshot({ path: j.png });
    await ctx.close();
    console.log('wrote', j.png);
  }
  await browser.close();
})();
