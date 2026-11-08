import * as THREE from 'three';
import {STLLoader} from 'three/examples/jsm/loaders/STLLoader.js';
import {ThreeMFLoader} from 'three/examples/jsm/loaders/3MFLoader.js';
import {OrbitControls} from 'three/examples/jsm/controls/OrbitControls.js';

export async function mount(container, url, kind, signal) {
  const response = await fetch(url,{signal});
  if (!response.ok) throw new Error('Preview kunne ikke hentes.');
  const data = kind !== 'sliced' ? await response.arrayBuffer() : await response.json();
  signal?.throwIfAborted();
  const scene = new THREE.Scene(); scene.background = new THREE.Color('#f5f7fc');
  const renderer = new THREE.WebGLRenderer({antialias:true});
  renderer.setPixelRatio(Math.min(devicePixelRatio,2));
  container.replaceChildren(renderer.domElement);
  const camera = new THREE.PerspectiveCamera(40,1,0.1,3000); camera.up.set(0,0,1);
  camera.position.set(355,-270,280);
  const controls = new OrbitControls(camera,renderer.domElement); controls.target.set(128,128,20);
  const grid = new THREE.GridHelper(256,16,'#afb9cd','#dce2ee'); grid.rotation.x=Math.PI/2; grid.position.set(128,128,0); scene.add(grid);
  scene.add(new THREE.HemisphereLight('#ffffff','#67728a',3));
  const light = new THREE.DirectionalLight('#ffffff',3); light.position.set(100,-100,300); scene.add(light);
  let object;
  const layerEnds=[];
  const draw = () => renderer.render(scene,camera);
  function removeObject() {
    if (!object) return;
    scene.remove(object);object.traverse(item=>{item.geometry?.dispose();if(Array.isArray(item.material))item.material.forEach(material=>material.dispose());else item.material?.dispose();});
  }
  function setLayer(index) {
    if (kind !== 'sliced') return;
    object.geometry.setDrawRange(0,layerEnds[index] || 0); draw();
  }
  if (kind === 'model') {
    const geometry=new STLLoader().parse(data); geometry.computeVertexNormals(); geometry.computeBoundingBox();
    const center=geometry.boundingBox.getCenter(new THREE.Vector3());
    object=new THREE.Mesh(geometry,new THREE.MeshStandardMaterial({color:'#7282dd',roughness:.6}));
    object.position.set(128-center.x,128-center.y,-geometry.boundingBox.min.z); scene.add(object);
    controls.target.set(128,128,(geometry.boundingBox.max.z-geometry.boundingBox.min.z)/2);
    const size=geometry.boundingBox.getSize(new THREE.Vector3()); const distance=Math.max(size.x,size.y,size.z,20)*2;
    camera.position.set(128+distance,128-distance,size.z/2+distance);
  } else if(kind === 'project') {
    object=new ThreeMFLoader().parse(data);scene.add(object);
    const box=new THREE.Box3().setFromObject(object);
    if(box.isEmpty())throw new Error('Projektets modelvisning kræver klargøring. Farver og dele bevares i 3MF-filen.');
    const size=box.getSize(new THREE.Vector3()),center=box.getCenter(new THREE.Vector3());const distance=Math.max(size.x,size.y,size.z,20)*2;
    controls.target.copy(center);camera.position.set(center.x+distance,center.y-distance,center.z+distance);
  } else {
    const positions=[],colors=[];
    const palette=new Map((data.filaments||[]).map(item=>[item.id,new THREE.Color(/^#[0-9a-f]{6}(?:[0-9a-f]{2})?$/i.test(item.color)?item.color.slice(0,7):'#5363cf')]));
    for(const layer of data.layers) {
      layer.segments.forEach((segment,index)=>{positions.push(segment[0],segment[1],layer.z,segment[2],segment[3],layer.z);const color=palette.get(layer.filament_ids?.[index]||1)||new THREE.Color('#5363cf');colors.push(color.r,color.g,color.b,color.r,color.g,color.b);});
      layerEnds.push(positions.length/3);
    }
    const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));
    geometry.setAttribute('color',new THREE.Float32BufferAttribute(colors,3));
    object=new THREE.LineSegments(geometry,new THREE.LineBasicMaterial({vertexColors:true}));scene.add(object);
    setLayer(data.layers.length-1);
    object.geometry.computeBoundingBox();
    const box=object.geometry.boundingBox;
    if(box && !box.isEmpty()) {const size=box.getSize(new THREE.Vector3());const center=box.getCenter(new THREE.Vector3());const distance=Math.max(size.x,size.y,size.z,20)*2;controls.target.copy(center);camera.position.set(center.x+distance,center.y-distance,center.z+distance);}
  }
  controls.addEventListener('change',draw);
  const resize=new ResizeObserver(()=>{
    const width=Math.max(1,container.clientWidth); const height=Math.max(1,container.clientHeight);
    renderer.setSize(width,height); camera.aspect=width/height; camera.updateProjectionMatrix(); draw();
  }); resize.observe(container); controls.update(); draw();
  return {data,setLayer,dispose(){resize.disconnect(); controls.dispose(); removeObject(); grid.geometry.dispose(); grid.material.dispose(); renderer.dispose(); renderer.domElement.remove();}};
}

