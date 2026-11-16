// Prepared for a supported browser host. Not executed by the Docker API harness.
// Managed Sites use their supported cua_repl preview route; never auto-fallback.
export async function verifySeededScreen({page,baseURL,expected,sourceRevision,outputPath}){
 if(!page||!/^https?:\/\//.test(baseURL)||!/^[a-f0-9]{40}$/.test(sourceRevision)||expected?.sourceRevision!==sourceRevision)
  throw Error('approved browser/source context required');
 const origin=new URL(baseURL).origin,results=[];
 await page.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
 for(const width of [1280,390]){
  await page.setViewportSize({width,height:900});await page.goto(new URL('/review/app',baseURL).href);
  const actual={};for(const book of ['DP','BP','SP']){
   const text=await page.getByTestId('balance-'+book).innerText();actual[book]=Number(text.replace(/[^0-9-]/g,''));
   if(actual[book]!==expected.balances[book])throw Error('screen/API balance mismatch');
  }
  const fits=await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1);
  if(!fits)throw Error('horizontal overflow');
  await page.getByRole('link',{name:'이용 규칙',exact:true}).first().click();
  await page.screenshot({path:outputPath+'/policy-'+width+'.png',fullPage:true});
  results.push({width,actual,fits,sourceRevision,scope:'browser_viewport_not_real_device'});
 }
 return results;
}
