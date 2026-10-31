import urllib.request,json
base='https://xiaoyueliangqqq--comfyui-1.us-east.modal.run'
a=json.load(urllib.request.urlopen(base+'/queue',timeout=15))
ids=[x[1] for x in a['queue_pending'] if '021_t2v_compbench_h3_' in x[2].get('92',{}).get('inputs',{}).get('filename_prefix','')]
print('FOUND',len(ids),flush=True)
for i in ids:
 try:
  req=urllib.request.Request(base+'/queue',data=json.dumps({'delete':[i]}).encode(),headers={'Content-Type':'application/json'},method='POST')
  print('DELETED',i,urllib.request.urlopen(req,timeout=15).status,flush=True)
 except Exception as e:print('ERROR',i,str(e),flush=True)
b=json.load(urllib.request.urlopen(base+'/queue',timeout=15))
print('REMAINING',sum('021_t2v_compbench_h3_' in x[2].get('92',{}).get('inputs',{}).get('filename_prefix','') for v in b.values() for x in v))
