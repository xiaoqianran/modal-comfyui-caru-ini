let sequence=0,scene=null,splatLibrary=null;
const image=document.getElementById('preview-img'),model=document.getElementById('model'),splat=document.getElementById('splat'),hint=document.getElementById('hint');
function clear(){image.hidden=true;image.removeAttribute('src');model.removeAttribute('src');model.style.display='none';splat.style.display='none';if(scene){try{scene.dispose()}catch(e){console.warn(e)}scene=null}splat.replaceChildren()}
export function deactivate(){sequence++;clear();hint.hidden=false;hint.textContent='支持 GLB / GLTF 及部分 Gaussian Splat 格式'}
export async function preview(item){let id=++sequence;clear();hint.hidden=false;hint.textContent='正在加载预览…';const url='/asset/'+encodeURIComponent(item.source)+'/'+item.key.split('/').map(encodeURIComponent).join('/');const ext=item.key.split('.').pop().toLowerCase();
if(['mp4','webm','mov','mkv','png','jpg','jpeg'].includes(ext)){
  let cover=url;
  if(['mp4','webm','mov','mkv'].includes(ext)){
    const original=item.key.replace(/^comfyui\//,'previews/');
    cover='/asset/r2/'+original.replace(/\.[^.]+$/,'_preview.png').split('/').map(encodeURIComponent).join('/');
  }
  image.onload=()=>{if(id===sequence){image.hidden=false;hint.hidden=true}};
  image.onerror=()=>{if(id===sequence)hint.textContent='预览尚未生成；原始文件可通过 CF 下载'};
  image.src=cover;
  return;
}
if(ext==='glb'||ext==='gltf'){try{if(!customElements.get('model-viewer'))await import('https://ajax.googleapis.com/ajax/libs/model-viewer/4.1.0/model-viewer.min.js');if(id!==sequence)return;model.src=url;model.style.display='block';hint.hidden=true}catch(e){if(id===sequence)hint.textContent='无法加载 3D Viewer：'+e.message}return}
if(['spz','ply','splat','ksplat'].includes(ext)){try{splatLibrary ||= import('https://esm.sh/@mkkellogg/gaussian-splats-3d@0.4.7?bundle');const G=await splatLibrary;if(id!==sequence)return;const v=new G.Viewer({rootElement:splat,selfDrivenMode:true,sharedMemoryForWorkers:false,dynamicScene:false});scene=v;splat.style.display='block';await v.addSplatScene(url,{splatAlphaRemovalThreshold:5,showLoadingUI:true});if(id!==sequence)return;v.start();hint.hidden=true}catch(e){if(id===sequence){clear();hint.textContent='预览失败：'+e.message+'，可下载文件'}}return}
hint.textContent='暂不支持该格式直接预览，请下载文件'}