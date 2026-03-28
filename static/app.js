/* ═══════════════════════════════════════════════
   JewelForge — Frontend Application (ES Module)
   ═══════════════════════════════════════════════ */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

// ─── State ──────────────────────────────────────
let currentImageB64 = null;
let currentSTLB64 = null;
let currentGLBB64 = null;
let currentAnalysis = null;

// Three.js
let scene, camera, renderer, controls, currentMesh;
let autoRotate = false;
let wireframeMode = false;
let animFrameId = null;

// ─── Tab Switching ──────────────────────────────
function switchTab(tab) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.querySelector(`[data-tab="${tab}"]`).classList.add('active');
    document.getElementById(`panel-${tab}`).classList.add('active');
}

function fillPrompt(text) {
    document.getElementById('promptInput').value = text;
}

// ─── File Upload ────────────────────────────────
const fileInput = document.getElementById('fileInput');
const dropZone = document.getElementById('dropZone');

dropZone.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', (e) => {
    if (e.target.files[0]) handleFile(e.target.files[0]);
});
dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('dragover'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', (e) => {
    e.preventDefault(); dropZone.classList.remove('dragover');
    if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
});

function handleFile(file) {
    if (!file.type.startsWith('image/')) return;
    const reader = new FileReader();
    reader.onload = (e) => {
        currentImageB64 = e.target.result.split(',')[1];
        document.getElementById('previewImg').src = e.target.result;
        document.getElementById('uploadPreview').style.display = 'block';
        dropZone.style.display = 'none';
    };
    reader.readAsDataURL(file);
}

function clearUpload() {
    currentImageB64 = null;
    document.getElementById('uploadPreview').style.display = 'none';
    dropZone.style.display = 'block';
    fileInput.value = '';
}

// ─── Pipeline Steps ─────────────────────────────
function setStep(stepId, state, detail) {
    const el = document.getElementById(`step-${stepId}`);
    el.className = `step ${state}`;
    const detailEl = document.getElementById(`step-${stepId}-detail`);
    if (detailEl && detail) detailEl.textContent = detail;
}

function dataURLtoBlob(base64) {
    const bytes = atob(base64);
    const buffer = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) buffer[i] = bytes.charCodeAt(i);
    return new Blob([buffer], { type: 'image/png' });
}

// ─── Main Pipeline ──────────────────────────────
async function startPipeline() {
    const btn = document.getElementById('btnGenerate');
    const activeTab = document.querySelector('.tab.active').dataset.tab;
    const prompt = document.getElementById('promptInput').value.trim();

    if (activeTab === 'upload' && !currentImageB64) { alert('Please upload a jewelry photo first.'); return; }
    if (activeTab === 'prompt' && !prompt) { alert('Please describe your jewelry design.'); return; }

    btn.disabled = true;
    btn.classList.add('loading');
    btn.querySelector('.btn-text').textContent = 'Processing...';
    document.getElementById('pipelineSection').style.display = 'block';
    document.getElementById('viewerSection').style.display = 'none';
    document.getElementById('analysisCard').style.display = 'none';
    document.getElementById('waxPreview').style.display = 'none';
    ['analyze', 'wax', '3d', 'refine'].forEach(s => setStep(s, '', ''));

    try {
        let imageB64;

        // Step 1: Analyze / Generate
        if (activeTab === 'prompt') {
            setStep('analyze', 'active', 'Generating jewelry image from your description...');
            const res = await fetch('/api/generate-image', { method: 'POST', body: new URLSearchParams({ prompt }) });
            if (!res.ok) throw new Error('Image generation failed');
            const data = await res.json();
            imageB64 = data.image_base64;
            currentImageB64 = imageB64;
            currentAnalysis = data.analysis;
            showAnalysis(data.analysis);
            setStep('analyze', 'done', formatAnalysis(data.analysis));
        } else {
            imageB64 = currentImageB64;
            setStep('analyze', 'active', 'Analyzing your jewelry photo...');
            const formData = new FormData();
            formData.append('image', dataURLtoBlob(imageB64), 'jewelry.png');
            const res = await fetch('/api/analyze', { method: 'POST', body: formData });
            if (!res.ok) throw new Error('Analysis failed');
            const data = await res.json();
            currentAnalysis = data.analysis;
            showAnalysis(data.analysis);
            setStep('analyze', 'done', formatAnalysis(data.analysis));
        }

        // Step 2: Wax views
        setStep('wax', 'active', 'Creating multi-angle wax carving references...');
        let waxViews = [];
        try {
            const waxRes = await fetch('/api/generate-wax', { method: 'POST', body: new URLSearchParams({ image_base64: imageB64 }) });
            if (waxRes.ok) {
                const waxData = await waxRes.json();
                waxViews = waxData.wax_views || [];
                if (waxViews.length > 0) {
                    showWaxViews(waxViews);
                    // Keep original image for 3D — wax views are display-only
                }
            }
        } catch (e) { console.warn('Wax failed:', e); }
        setStep('wax', 'done', `${waxViews.length} views generated`);

        // Step 3: 3D mesh
        setStep('3d', 'active', 'Building 3D mesh with AI (1-3 min)...');
        const meshRes = await fetch('/api/generate-3d', { method: 'POST', body: new URLSearchParams({ image_base64: imageB64, engine: 'hitem3d' }) });
        if (!meshRes.ok) throw new Error('3D generation failed');
        const meshData = await meshRes.json();
        setStep('3d', 'done', `Engine: ${meshData.engine}`);

        // Step 4: Refine
        setStep('refine', 'active', 'Cleaning mesh topology & exporting STL...');
        const refineRes = await fetch('/api/refine', { method: 'POST', body: new URLSearchParams({ glb_url: meshData.url }) });
        if (!refineRes.ok) throw new Error('Mesh refinement failed');
        const refineData = await refineRes.json();
        setStep('refine', 'done', refineData.refined ? 'Blender-refined STL ready' : 'Raw AI mesh ready');

        currentSTLB64 = refineData.stl_base64 || null;
        currentGLBB64 = refineData.glb_base64 || null;

        console.log('Pipeline complete:', {
            hasSTL: !!currentSTLB64,
            stlLen: currentSTLB64 ? currentSTLB64.length : 0,
            hasGLB: !!currentGLBB64,
            glbLen: currentGLBB64 ? currentGLBB64.length : 0,
            stats: refineData.stats,
        });

        // Show viewer
        showViewer(refineData);

        const dlBtn = document.getElementById('btnDownload');
        dlBtn.textContent = currentSTLB64 ? '⬇ Download STL' : '⬇ Download GLB';

    } catch (error) {
        console.error('Pipeline error:', error);
        const steps = ['analyze', 'wax', '3d', 'refine'];
        const activeStep = steps.find(s => document.getElementById(`step-${s}`).classList.contains('active'));
        if (activeStep) setStep(activeStep, 'error', error.message);
    } finally {
        btn.disabled = false;
        btn.classList.remove('loading');
        btn.querySelector('.btn-text').textContent = 'Generate 3D Model';
    }
}

// ─── Analysis ───────────────────────────────────
function showAnalysis(analysis) {
    const container = document.getElementById('analysisContent');
    container.innerHTML = '';
    const fields = { type: 'Type', category: 'Category', metal_type: 'Metal', stone_shape: 'Stone', setting_style: 'Setting', complexity: 'Complexity', description: 'Description' };
    for (const [key, label] of Object.entries(fields)) {
        if (analysis[key]) {
            const tag = document.createElement('div');
            tag.className = 'analysis-tag';
            tag.innerHTML = `<span class="tag-label">${label}</span><span class="tag-value">${analysis[key]}</span>`;
            container.appendChild(tag);
        }
    }
    document.getElementById('analysisCard').style.display = 'block';
}
function formatAnalysis(a) { return `${a.type || 'jewelry'} — ${a.category || 'unknown'}`; }

// ─── Wax Views ──────────────────────────────────
function showWaxViews(views) {
    const grid = document.getElementById('waxGrid');
    grid.innerHTML = '';
    ['Front', 'Side', 'Top'].forEach((label, i) => {
        if (views[i]) {
            const img = document.createElement('img');
            img.src = `data:image/png;base64,${views[i]}`;
            img.alt = label;
            grid.appendChild(img);
        }
    });
    document.getElementById('waxPreview').style.display = 'block';
}

// ─── 3D Viewer ──────────────────────────────────

// Wax material used for all meshes
function createWaxMaterial() {
    return new THREE.MeshPhysicalMaterial({
        color: 0x5B8DD9,
        metalness: 0.0,
        roughness: 0.28,
        clearcoat: 0.3,
        clearcoatRoughness: 0.25,
        reflectivity: 0.5,
        envMapIntensity: 0.8,
        sheen: 0.15,
        sheenRoughness: 0.3,
        sheenColor: new THREE.Color(0x88AADD),
    });
}

// Auto-fit camera to perfectly frame any model
function frameCameraToModel(object) {
    const box = new THREE.Box3().setFromObject(object);
    const size = new THREE.Vector3();
    const center = new THREE.Vector3();
    box.getSize(size);
    box.getCenter(center);

    const maxDim = Math.max(size.x, size.y, size.z);
    const fov = camera.fov * (Math.PI / 180);
    let dist = (maxDim / 2) / Math.tan(fov / 2);
    dist *= 1.8; // breathing room

    // Position camera at a nice 3/4 angle
    camera.position.set(
        center.x + dist * 0.6,
        center.y + dist * 0.4,
        center.z + dist * 0.7
    );
    controls.target.copy(center);
    controls.minDistance = dist * 0.2;
    controls.maxDistance = dist * 5;
    controls.update();
}

function showViewer(data) {
    document.getElementById('viewerSection').style.display = 'block';

    // Stats
    if (data.stats && Object.keys(data.stats).length > 0) {
        const s = data.stats;
        document.getElementById('statVerts').textContent = (s.output_vertices || s.input_vertices || 0).toLocaleString();
        document.getElementById('statFaces').textContent = (s.output_faces || s.input_faces || 0).toLocaleString();
        document.getElementById('statWater').textContent = s.is_watertight ? '✓ Yes' : '✗ No';
        document.getElementById('statManifold').textContent = s.is_manifold ? '✓ Yes' : '✗ No';
        if (s.bounding_box_mm) {
            const bb = s.bounding_box_mm;
            document.getElementById('statSize').textContent = `${bb.x} × ${bb.y} × ${bb.z}`;
        }
        document.getElementById('meshStats').style.display = 'grid';
    }
    document.getElementById('statEngine').textContent = data.engine || '—';

    initViewer();

    if (data.stl_base64) {
        loadSTLFromBase64(data.stl_base64);
    } else if (data.glb_base64) {
        loadGLBFromBase64(data.glb_base64);
    } else {
        console.error('No model data received!');
    }

    document.getElementById('viewerSection').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function initViewer() {
    const wrap = document.getElementById('viewerWrap');

    // Clean up previous
    if (renderer) { renderer.dispose(); }
    if (controls) { controls.dispose(); }
    if (animFrameId) { cancelAnimationFrame(animFrameId); }
    const oldCanvas = wrap.querySelector('canvas');
    if (oldCanvas) oldCanvas.remove();

    const canvas = document.createElement('canvas');
    canvas.id = 'viewer3d';
    canvas.style.cssText = 'width:100%;height:100%;display:block;outline:none;touch-action:none;';
    wrap.insertBefore(canvas, wrap.firstChild);

    // Scene — soft gradient background
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0xf5f5f8);

    // Camera
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    camera = new THREE.PerspectiveCamera(40, w / h, 0.001, 2000);
    camera.position.set(3, 2, 3);

    // Renderer
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false });
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.2;
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;

    // Environment map for reflections
    const pmremGenerator = new THREE.PMREMGenerator(renderer);
    const envScene = new THREE.Scene();
    const envGeo = new THREE.SphereGeometry(50, 64, 32);
    const envMat = new THREE.ShaderMaterial({
        side: THREE.BackSide,
        uniforms: {},
        vertexShader: `
            varying vec3 vWorldPos;
            void main() {
                vWorldPos = normalize(position);
                gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
            }
        `,
        fragmentShader: `
            varying vec3 vWorldPos;
            void main() {
                float y = vWorldPos.y * 0.5 + 0.5;
                vec3 top = vec3(1.0, 0.99, 0.97);
                vec3 mid = vec3(0.88, 0.90, 0.94);
                vec3 bot = vec3(0.75, 0.78, 0.84);
                vec3 col = mix(bot, mid, smoothstep(0.0, 0.45, y));
                col = mix(col, top, smoothstep(0.45, 1.0, y));
                gl_FragColor = vec4(col, 1.0);
            }
        `
    });
    envScene.add(new THREE.Mesh(envGeo, envMat));
    const envRT = pmremGenerator.fromScene(envScene, 0.04);
    scene.environment = envRT.texture;
    pmremGenerator.dispose();

    // Controls — smooth, full 360°
    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.06;
    controls.rotateSpeed = 0.8;
    controls.zoomSpeed = 1.2;
    controls.panSpeed = 0.8;
    controls.autoRotate = autoRotate;
    controls.autoRotateSpeed = 1.5;
    controls.enablePan = true;
    controls.minPolarAngle = 0;
    controls.maxPolarAngle = Math.PI;
    controls.enableZoom = true;
    controls.touches = { ONE: THREE.TOUCH.ROTATE, TWO: THREE.TOUCH.DOLLY_PAN };

    // Hide overlay on first interaction
    const overlay = document.getElementById('viewerOverlay');
    const hideOverlay = () => {
        if (overlay) overlay.style.opacity = '0';
        setTimeout(() => { if (overlay) overlay.style.display = 'none'; }, 300);
        renderer.domElement.removeEventListener('pointerdown', hideOverlay);
    };
    renderer.domElement.addEventListener('pointerdown', hideOverlay);

    // Lighting — studio setup for jewelry
    scene.add(new THREE.AmbientLight(0xffffff, 0.5));

    const keyLight = new THREE.DirectionalLight(0xffffff, 1.8);
    keyLight.position.set(4, 6, 5);
    keyLight.castShadow = true;
    keyLight.shadow.mapSize.set(1024, 1024);
    keyLight.shadow.bias = -0.001;
    scene.add(keyLight);

    const fillLight = new THREE.DirectionalLight(0xdde4ff, 0.9);
    fillLight.position.set(-5, 3, 3);
    scene.add(fillLight);

    const rimLight = new THREE.DirectionalLight(0xffffff, 0.7);
    rimLight.position.set(0, 2, -5);
    scene.add(rimLight);

    const topLight = new THREE.DirectionalLight(0xffffff, 1.0);
    topLight.position.set(0, 10, 0);
    scene.add(topLight);

    const bottomFill = new THREE.DirectionalLight(0xe0e4f0, 0.3);
    bottomFill.position.set(0, -5, 0);
    scene.add(bottomFill);

    // Ground shadow catcher (subtle)
    const groundGeo = new THREE.PlaneGeometry(20, 20);
    const groundMat = new THREE.ShadowMaterial({ opacity: 0.08 });
    const ground = new THREE.Mesh(groundGeo, groundMat);
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -1;
    ground.receiveShadow = true;
    scene.add(ground);

    // Render loop
    function animate() {
        animFrameId = requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
    }
    animate();

    // Responsive resize
    const ro = new ResizeObserver(() => {
        const nw = wrap.clientWidth;
        const nh = wrap.clientHeight;
        if (nw === 0 || nh === 0) return;
        camera.aspect = nw / nh;
        camera.updateProjectionMatrix();
        renderer.setSize(nw, nh);
    });
    ro.observe(wrap);
}

function addModelToScene(object) {
    if (currentMesh) scene.remove(currentMesh);

    // Enable shadows on all meshes
    object.traverse((child) => {
        if (child.isMesh) {
            child.castShadow = true;
            child.receiveShadow = true;
        }
    });

    currentMesh = object;
    scene.add(object);

    // Auto-fit camera
    frameCameraToModel(object);

    // Start gentle auto-rotate
    autoRotate = true;
    controls.autoRotate = true;
    document.getElementById('btnRotate').classList.add('active');
}

function loadSTLFromBase64(base64) {
    try {
        const binaryStr = atob(base64);
        const bytes = new Uint8Array(binaryStr.length);
        for (let i = 0; i < binaryStr.length; i++) bytes[i] = binaryStr.charCodeAt(i);

        const loader = new STLLoader();
        const geometry = loader.parse(bytes.buffer);

        // Center at origin
        geometry.computeBoundingBox();
        const center = new THREE.Vector3();
        geometry.boundingBox.getCenter(center);
        geometry.translate(-center.x, -center.y, -center.z);
        geometry.computeVertexNormals();

        const mesh = new THREE.Mesh(geometry, createWaxMaterial());
        addModelToScene(mesh);
    } catch (err) {
        console.error('STL load error:', err);
    }
}

function loadGLBFromBase64(base64) {
    try {
        const binaryStr = atob(base64);
        const bytes = new Uint8Array(binaryStr.length);
        for (let i = 0; i < binaryStr.length; i++) bytes[i] = binaryStr.charCodeAt(i);

        const loader = new GLTFLoader();
        loader.parse(bytes.buffer, '', (gltf) => {
            const model = gltf.scene;

            // Center model
            const box = new THREE.Box3().setFromObject(model);
            const center = new THREE.Vector3();
            box.getCenter(center);
            model.position.sub(center);

            // Apply wax material to all meshes
            model.traverse((child) => {
                if (child.isMesh) {
                    child.material = createWaxMaterial();
                }
            });

            addModelToScene(model);
        }, (err) => {
            console.error('GLB parse error:', err);
        });
    } catch (err) {
        console.error('GLB load error:', err);
    }
}

// ─── Viewer Controls ────────────────────────────
function toggleAutoRotate() {
    autoRotate = !autoRotate;
    if (controls) controls.autoRotate = autoRotate;
    document.getElementById('btnRotate').classList.toggle('active', autoRotate);
}

function toggleWireframe() {
    wireframeMode = !wireframeMode;
    if (currentMesh) {
        currentMesh.traverse((child) => {
            if (child.isMesh && child.material) child.material.wireframe = wireframeMode;
        });
    }
}

function resetCamera() {
    if (camera && controls && currentMesh) {
        frameCameraToModel(currentMesh);
    }
}

function downloadModel() {
    const data = currentSTLB64 || currentGLBB64;
    const ext = currentSTLB64 ? 'stl' : 'glb';
    if (!data) { alert('No model available yet.'); return; }
    const bytes = atob(data);
    const buffer = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) buffer[i] = bytes.charCodeAt(i);
    const blob = new Blob([buffer], { type: 'application/octet-stream' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `jewelforge_${Date.now()}.${ext}`;
    a.click();
    URL.revokeObjectURL(url);
}

// ─── Expose to HTML onclick handlers ────────────
window.JF = {
    switchTab, fillPrompt, clearUpload, startPipeline,
    toggleAutoRotate, toggleWireframe, resetCamera, downloadModel,
};
