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
let autoRotate = true;
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
                    imageB64 = waxViews[0];
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

    // Load model
    if (data.stl_base64) {
        console.log('Loading STL, length:', data.stl_base64.length);
        loadSTLFromBase64(data.stl_base64);
    } else if (data.glb_base64) {
        console.log('Loading GLB, length:', data.glb_base64.length);
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

    // Remove old canvas
    const oldCanvas = wrap.querySelector('canvas');
    if (oldCanvas) oldCanvas.remove();

    // Create new canvas
    const canvas = document.createElement('canvas');
    canvas.id = 'viewer3d';
    canvas.style.cssText = 'width:100%;height:100%;display:block;outline:none;';
    wrap.insertBefore(canvas, wrap.firstChild);

    // Scene — light studio background
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0xf0f0f2);

    // Generate a simple environment map for metallic reflections
    const envScene = new THREE.Scene();
    // Gradient environment: warm top, cool sides, neutral bottom
    const envGeo = new THREE.SphereGeometry(50, 32, 16);
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
                vec3 top = vec3(1.0, 0.98, 0.95);
                vec3 mid = vec3(0.92, 0.91, 0.90);
                vec3 bot = vec3(0.85, 0.84, 0.83);
                vec3 col = mix(bot, mid, smoothstep(0.0, 0.4, y));
                col = mix(col, top, smoothstep(0.4, 1.0, y));
                gl_FragColor = vec4(col, 1.0);
            }
        `
    });
    envScene.add(new THREE.Mesh(envGeo, envMat));

    // Camera
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    camera = new THREE.PerspectiveCamera(45, w / h, 0.001, 1000);
    camera.position.set(2, 1.5, 2);

    // Renderer — high quality for jewelry
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.4;
    renderer.outputColorSpace = THREE.SRGBColorSpace;

    // Generate cube render target for environment reflections
    const cubeRenderTarget = new THREE.WebGLCubeRenderTarget(256, {
        format: THREE.RGBAFormat,
        generateMipmaps: true,
        minFilter: THREE.LinearMipmapLinearFilter,
    });
    const cubeCamera = new THREE.CubeCamera(0.1, 100, cubeRenderTarget);
    envScene.add(cubeCamera);
    cubeCamera.update(renderer, envScene);
    scene.environment = cubeRenderTarget.texture;

    // Controls — full 360°
    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.autoRotate = autoRotate;
    controls.autoRotateSpeed = 2.0;
    controls.minDistance = 0.1;
    controls.maxDistance = 20;

    // Studio lighting — bright, jewelry-showcase style
    scene.add(new THREE.AmbientLight(0xffffff, 0.8));

    const key = new THREE.DirectionalLight(0xffffff, 1.8);
    key.position.set(3, 5, 3);
    scene.add(key);

    const fill = new THREE.DirectionalLight(0xfff8e7, 1.0);
    fill.position.set(-4, 3, -1);
    scene.add(fill);

    const rim = new THREE.DirectionalLight(0xffffff, 0.8);
    rim.position.set(0, 0, -4);
    scene.add(rim);

    const top = new THREE.DirectionalLight(0xffffff, 1.0);
    top.position.set(0, 6, 0);
    scene.add(top);

    const bottom = new THREE.DirectionalLight(0xf5f0e8, 0.4);
    bottom.position.set(0, -3, 0);
    scene.add(bottom);

    // Subtle ground shadow disc
    const groundGeo = new THREE.CircleGeometry(2, 64);
    const groundMat = new THREE.MeshBasicMaterial({
        color: 0xd8d8dc,
        transparent: true,
        opacity: 0.3,
    });
    const ground = new THREE.Mesh(groundGeo, groundMat);
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -0.5;
    scene.add(ground);

    // Animate loop
    function animate() {
        animFrameId = requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
    }
    animate();

    // Resize
    new ResizeObserver(() => {
        const nw = wrap.clientWidth;
        const nh = wrap.clientHeight;
        camera.aspect = nw / nh;
        camera.updateProjectionMatrix();
        renderer.setSize(nw, nh);
    }).observe(wrap);
}

function loadSTLFromBase64(base64) {
    try {
        const binaryStr = atob(base64);
        const bytes = new Uint8Array(binaryStr.length);
        for (let i = 0; i < binaryStr.length; i++) bytes[i] = binaryStr.charCodeAt(i);

        const loader = new STLLoader();
        const geometry = loader.parse(bytes.buffer);

        console.log('STL parsed:', geometry.attributes.position.count, 'vertices');

        // Center
        geometry.computeBoundingBox();
        const box = geometry.boundingBox;
        const center = new THREE.Vector3();
        box.getCenter(center);
        geometry.translate(-center.x, -center.y, -center.z);

        // Normalize size
        const size = new THREE.Vector3();
        box.getSize(size);
        const maxDim = Math.max(size.x, size.y, size.z);
        if (maxDim > 0) {
            const s = 1.5 / maxDim;
            geometry.scale(s, s, s);
        }

        geometry.computeVertexNormals();

        // Gold material — bright, reflective, jewelry showcase
        const material = new THREE.MeshPhysicalMaterial({
            color: 0xD4AF37,
            metalness: 1.0,
            roughness: 0.15,
            clearcoat: 0.4,
            clearcoatRoughness: 0.1,
            reflectivity: 1.0,
            envMapIntensity: 1.5,
        });

        if (currentMesh) scene.remove(currentMesh);
        currentMesh = new THREE.Mesh(geometry, material);
        scene.add(currentMesh);

        // Frame camera
        camera.position.set(1.5, 1, 1.5);
        controls.target.set(0, 0, 0);
        controls.update();

        console.log('STL loaded and displayed successfully');
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
            console.log('GLB parsed, scene children:', gltf.scene.children.length);

            if (currentMesh) scene.remove(currentMesh);

            const model = gltf.scene;

            // Center and scale
            const box = new THREE.Box3().setFromObject(model);
            const center = new THREE.Vector3();
            box.getCenter(center);
            model.position.sub(center);

            const size = new THREE.Vector3();
            box.getSize(size);
            const maxDim = Math.max(size.x, size.y, size.z);
            if (maxDim > 0) {
                const s = 1.5 / maxDim;
                model.scale.multiplyScalar(s);
            }

            // Apply gold material to all meshes — bright showcase
            model.traverse((child) => {
                if (child.isMesh) {
                    child.material = new THREE.MeshPhysicalMaterial({
                        color: 0xD4AF37,
                        metalness: 1.0,
                        roughness: 0.15,
                        clearcoat: 0.4,
                        clearcoatRoughness: 0.1,
                        reflectivity: 1.0,
                        envMapIntensity: 1.5,
                    });
                }
            });

            currentMesh = model;
            scene.add(model);

            camera.position.set(1.5, 1, 1.5);
            controls.target.set(0, 0, 0);
            controls.update();

            console.log('GLB loaded and displayed successfully');
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
    if (camera && controls) {
        camera.position.set(1.5, 1, 1.5);
        controls.target.set(0, 0, 0);
        controls.update();
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
