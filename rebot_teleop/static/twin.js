import * as THREE from './vendor/three.module.min.js';
import { OrbitControls } from './vendor/OrbitControls.js';
import { STLLoader } from './vendor/STLLoader.js';

const loader = new STLLoader();
const geometries = new Map();
function geometry(key) {
  if (!geometries.has(key)) {
    geometries.set(key, new Promise((resolve, reject) => loader.load('/api/mesh/' + key, g => { g.computeVertexNormals(); resolve(g); }, undefined, reject)));
  }
  return geometries.get(key);
}

function matrix4(rows) {
  const m = new THREE.Matrix4();
  m.set(...rows.flat());
  return m;
}

function matrix12(a) {
  const m = new THREE.Matrix4();
  m.set(a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7], a[8], a[9], a[10], a[11], 0, 0, 0, 1);
  return m;
}

const VIEWS = { front: [1.5, 0.0, 1.2], side: [0.3, 1.5, 1.15], top: [0.3, 0.01, 2.4] };

export class Twin {
  constructor(element, scene) {
    this.element = element;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    element.appendChild(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(40, 1, 0.02, 30);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(1.25, 0.75, 1.35);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.set(0.15, 0, 1.0);
    this.controls.enableDamping = true;
    this.scene.add(new THREE.HemisphereLight(0xdde6ff, 0x30281e, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.8);
    sun.position.set(2, 1.5, 3);
    this.scene.add(sun);
    const grid = new THREE.GridHelper(3, 30, 0x3a4250, 0x232830);
    grid.rotation.x = Math.PI / 2;
    this.scene.add(grid);
    this.links = {};
    this.build(scene);
    new ResizeObserver(() => this.resize()).observe(element);
    this.resize();
    const loop = () => {
      if (this.element.offsetParent !== null) {
        if (this.element.clientWidth !== this.width) this.resize();
        this.controls.update();
        this.renderer.render(this.scene, this.camera);
      }
      requestAnimationFrame(loop);
    };
    loop();
  }

  build(scene) {
    const post = scene.post;
    const postMesh = new THREE.Mesh(new THREE.BoxGeometry(...post.size), new THREE.MeshStandardMaterial({ color: 0xf2f2f0, roughness: 0.7 }));
    postMesh.position.set(...post.center);
    this.scene.add(postMesh);
    for (const part of scene.body) {
      geometry(part.mesh).then(g => {
        const mesh = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color: new THREE.Color(...part.rgba.slice(0, 3)), roughness: 0.6 }));
        mesh.matrixAutoUpdate = false;
        mesh.matrix.copy(matrix4(part.origin));
        this.scene.add(mesh);
      }).catch(() => {});
    }
    for (const arm of Object.keys(scene.mounts)) {
      this.links[arm] = {};
      for (const part of scene.arm_parts) {
        let group = this.links[arm][part.link];
        if (!group) {
          group = new THREE.Group();
          group.matrixAutoUpdate = false;
          group.visible = false;
          this.links[arm][part.link] = group;
          this.scene.add(group);
        }
        const material = new THREE.MeshStandardMaterial({ color: new THREE.Color(...part.rgba.slice(0, 3)), roughness: 0.5, metalness: 0.15 });
        geometry(part.mesh).then(g => {
          const mesh = new THREE.Mesh(g, material);
          mesh.matrixAutoUpdate = false;
          mesh.matrix.copy(matrix4(part.origin));
          group.add(mesh);
        }).catch(() => {});
      }
    }
  }

  update(twin, arms) {
    for (const [arm, links] of Object.entries(twin || {})) {
      const groups = this.links[arm];
      if (!groups) continue;
      const status = arms && arms[arm];
      const offline = status && !status.connected;
      for (const [name, m] of Object.entries(links)) {
        const group = groups[name];
        if (!group) continue;
        group.matrix.copy(matrix12(m));
        group.matrixWorldNeedsUpdate = true;
        group.visible = true;
        group.traverse(o => { if (o.material) { o.material.transparent = !!offline; o.material.opacity = offline ? 0.25 : 1; } });
      }
    }
  }

  view(name) {
    const p = VIEWS[name] || VIEWS.front;
    this.camera.position.set(...p);
    this.controls.target.set(0.15, 0, 1.0);
  }

  resize() {
    const w = this.element.clientWidth || 300, h = this.element.clientHeight || 200;
    this.width = this.element.clientWidth;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }
}
