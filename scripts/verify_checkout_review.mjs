// Uses a separately installed @playwright/test for local, payment-disabled samples.
import assert from 'node:assert/strict';
import{mkdir}from'node:fs/promises';
const{chromium}=await import(process.env.PLAYWRIGHT_MODULE||'@playwright/test');
const browser=await chromium.launch({headless:true});let checks=0;
await mkdir('work/checkout-review',{recursive:true});
try{for(const width of[375,390,430,1365])for(const kind of['event','wedding'])for(const price of[93500,99000]){
 const page=await browser.newPage({viewport:{width,height:1000}});await page.route('**/checkout-sender',r=>r.abort());
 await page.goto(`http://127.0.0.1:4173/${kind}-${price}.html`);
 await page.selectOption('[name=choice0]','20');await page.selectOption('[name=choice1]','host');await page.selectOption('[name=choice2]','custom');
 await page.fill('[name=message]','<img src=x onerror=alert(1)> '+ '아주긴축하문구'.repeat(8));
 await page.fill('[name=sender]','가상협력기업 아주긴보내는분이름'.repeat(3));await page.check('[name=same_buyer]');
 assert.equal(await page.inputValue('[name=buyer]'),await page.inputValue('[name=sender]'));checks++;
 assert((await page.locator('#amount').innerText()).includes((price+10000).toLocaleString('ko-KR')));checks++;
 if(kind==='wedding'){
  assert((await page.locator('#scroll-weight').getAttribute('src')).endsWith('rice-20kg.webp'));checks++;
  assert.equal(await page.locator('#scroll-greeting img').count(),0);checks++;
 }
 const size=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));assert(size.scroll<=size.width+1,JSON.stringify({kind,price,size}));checks++;
 assert(await page.locator('button[type=submit]').isDisabled());checks++;
 await page.screenshot({path:`work/checkout-review/${kind}-${price}-${width}.png`,fullPage:true});await page.close();
}console.log(JSON.stringify({passed:true,checks,synthetic:true,payments:false,physicalMobile:false}));}finally{await browser.close()}
