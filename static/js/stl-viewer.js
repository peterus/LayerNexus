/* 3D Model Viewer using Three.js (ES module) — supports STL and 3MF */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { ThreeMFLoader } from 'three/addons/loaders/3MFLoader.js';

function setupScene(container) {
  container.innerHTML = '';
  var width = container.clientWidth;
  var height = container.clientHeight;

  var scene = new THREE.Scene();
  var isDark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
  scene.background = new THREE.Color(isDark ? 0x212529 : 0xf8f9fa);

  var camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 10000);
  var renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(width, height);
  renderer.setPixelRatio(window.devicePixelRatio);
  container.appendChild(renderer.domElement);

  var controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.1;

  var ambientLight = new THREE.AmbientLight(0xffffff, 0.6);
  scene.add(ambientLight);

  var dirLight1 = new THREE.DirectionalLight(0xffffff, 0.8);
  dirLight1.position.set(1, 1, 1);
  scene.add(dirLight1);

  var dirLight2 = new THREE.DirectionalLight(0xffffff, 0.3);
  dirLight2.position.set(-1, -1, -1);
  scene.add(dirLight2);

  function animate() {
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  }
  animate();

  window.addEventListener('resize', function () {
    var w = container.clientWidth;
    var h = container.clientHeight;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  });

  return { scene: scene, camera: camera, controls: controls };
}

var defaultMaterial = new THREE.MeshStandardMaterial({
  color: 0x4dabf7,
  metalness: 0.1,
  roughness: 0.6,
});

function fitCameraToObject(object, camera, controls) {
  var box = new THREE.Box3().setFromObject(object);
  var center = new THREE.Vector3();
  box.getCenter(center);
  object.position.sub(center);

  var size = new THREE.Vector3();
  box.getSize(size);
  var maxDim = Math.max(size.x, size.y, size.z);
  camera.position.set(maxDim * 1.2, maxDim * 0.8, maxDim * 1.5);
  camera.lookAt(0, 0, 0);
  controls.target.set(0, 0, 0);
  controls.update();
}

export function initSTLViewer(containerId, modelUrl) {
  var container = document.getElementById(containerId);
  if (!container || !modelUrl) return;

  var is3MF = modelUrl.toLowerCase().endsWith('.3mf');
  var ctx = setupScene(container);

  function onError() {
    container.innerHTML = '<span class="text-danger"><i class="bi bi-exclamation-triangle me-1"></i>Failed to load 3D model</span>';
  }

  if (is3MF) {
    var loader = new ThreeMFLoader();
    loader.load(modelUrl, function (group) {
      // Apply default material to meshes that don't have one
      group.traverse(function (child) {
        if (child.isMesh) {
          if (!child.material || (child.material.color && child.material.color.getHex() === 0xffffff)) {
            child.material = defaultMaterial;
          }
        }
      });
      ctx.scene.add(group);
      fitCameraToObject(group, ctx.camera, ctx.controls);
    }, undefined, onError);
  } else {
    var loader = new STLLoader();
    loader.load(modelUrl, function (geometry) {
      geometry.computeVertexNormals();
      var mesh = new THREE.Mesh(geometry, defaultMaterial);
      ctx.scene.add(mesh);
      fitCameraToObject(mesh, ctx.camera, ctx.controls);
    }, undefined, onError);
  }
}
