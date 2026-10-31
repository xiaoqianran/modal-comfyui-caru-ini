import json,urllib.request
api='https://api.github.com/repos/stepfun-ai/Step-Video-TI2V/contents/benchmark/Step-Video-TI2V-Eval/'
items=[]
for group in ('ti2v_eval_ani','ti2v_eval_real'):
 for section in ('H','S','V'):
  entries=json.load(urllib.request.urlopen(api+group+'/'+section,timeout=20))
  texts=[x for x in entries if x['name'].endswith('.txt')]
  print('CATEGORY',group,section,'PROMPTS',len(texts),flush=True)
  for x in texts[:4]:
   raw=urllib.request.urlopen(x['download_url'],timeout=20).read().decode('utf-8-sig').strip()
   items.append({'id':group+'/'+section+'/'+x['name'][:-4],'original':raw,'source':x['html_url'],'image':x['html_url'][:-4]+'.png'})
print('RESULTS',json.dumps(items[:20],ensure_ascii=True,indent=2),flush=True)
