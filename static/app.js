/* ═══════════════════════════════════════════════
   JewelForge — Frontend Application
   ═══════════════════════════════════════════════ */

// State
let currentImageB64 = null;
let currentSTLB64 = null;
let currentGLBB64 = null;
let currentAnalysis = null;

// Three.js globals
let scene, camera, renderer, controls, currentMesh;
let autoRotate = true;
let wireframeMode = false;

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

fileInput.addEventListener('change', (e) => {
    if (e.target.files[0]) handleFile(e.target.files[0]);
});

dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('dragover');
});
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
});

function handleFile(file) {
    if (!file.type.startsWith('image/')) return;
    const reader = new FileReader();
    reader.onload = (e) => {
        const b64 = e.target.result.split(',')[1];
        currentImageB64 = b64;
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

// ─── Pipeline ───────────────────────────────────
function setStep(stepId, state, detail) {
    const el = document.getElementById(`step-${stepId}`);
    el.className = `step ${state}`;
    const detailEl = document.getElementById(`step-${stepId}-detail`);
    if (detailEl && detail) detailEl.textContent = detail;
}

async function startPipeline() {
    const btn = document.getElementById('btnGenerate');
    const activeTab = document.querySelector('.tab.active').dataset.tab;
    const prompt = document.getElementById('promptInput').value.trim();

    // Validate input
    if (activeTab === 'upload' && !currentImageB64) {
        alert('Please upload a jewelry photo first.');
        return;
    }
    if (activeTab === 'prompt' && !prompt) {
        alert('Please describe your jewelry design.');
        return;
    }

    // UI: disable button, show pipeline
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

        // Step 1: Get image (generate if prompt, or use upload)
        if (activeTab === 'prompt') {
            setStep('analyze', 'active', 'Generating jewelry image from your description...');
            const res = await fetch('/api/generate-image', {
                method: 'POST',
                body: new URLSearchParams({ prompt }),
            });
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
            const res = await fetch('/api/analyze', {
                method: 'POST',
                body: formData,
            });
            if (!res.ok) throw new Error('Analysis failed');
            const data = await res.json();
            currentAnalysis = data.analysis;
            showAnalysis(data.analysis);
            setStep('analyze', 'done', formatAnalysis(data.analysis));
        }

        // Step 2: Generate wax views
        setStep('wax', 'active', 'Creating multi-angle wax carving references...');
        let waxViews = [];
        try {
            const waxRes = await fetch('/api/generate-wax', {
                method: 'POST',
                body: new URLSearchParams({ image_base64: imageB64 }),
            });
            if (waxRes.ok) {
                const waxData = await waxRes.json();
                waxViews = waxData.wax_views || [];
                if (waxViews.length > 0) {
                    showWaxViews(waxViews);
                    // Use front wax view for better 3D generation
                    imageB64 = waxViews[0];
                }
            }
        } catch (e) {
            console.warn('Wax generation failed, using original image:', e);
        }
        setStep('wax', 'done', `${waxViews.length} views generated`);

        // Step 3: Generate 3D mesh
        setStep('3d', 'active', 'Building 3D mesh with AI (this takes 1-3 minutes)...');
        const meshRes = await fetch('/api/generate-3d', {
            method: 'POST',
            body: new URLSearchParams({ image_base64: imageB64, engine: 'hitem3d' }),
        });
        if (!meshRes.ok) throw new Error('3D generation failed');
        const meshData = await meshRes.json();
        setStep('3d', 'done', `Engine: ${meshData.engine}`);

        // Step 4: Refine with Blender
        setStep('refine', 'active', 'Cleaning mesh topology & exporting STL...');
        const refineRes = await fetch('/api/refine', {
            method: 'POST',
            body: new URLSearchParams({ glb_url: meshData.url }),
        });
        if (!refineRes.ok) throw new Error('Mesh refinement failed');
        const refineData = await refineRes.json();
        setStep('refine', 'done', 'Production-ready STL generated');

        // Store results
        currentSTLB64 = refineData.stl_base64;
        currentGLBB64 = refineData.glb_base64;

        // Show 3D viewer
        showViewer(refineData);

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

// ─── Analysis Display ───────────────────────────
function showAnalysis(analysis) {
    const container = document.getElementById('analysisContent');
    container.innerHTML = '';
    const fields = {
        type: 'Type', category: 'Category', metal_type: 'Metal',
        stone_shape: 'Stone', setting_style: 'Setting', complexity: 'Complexity',
        description: 'Description'
    };
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

function formatAnalysis(a) {
    return `${a.type || 'jewelry'} — ${a.category || 'unknown'} — ${a.complexity || 'moderate'}`;
}

// ─── Wax Views ──────────────────────────────────
function showWaxViews(views) {
    const grid = document.getElementById('waxGrid');
    grid.innerHTML = '';
    const labels = ['Front', 'Side', 'Top'];
    views.forEach((b64, i) => {
        const img = document.createElement('img');
        img.src = `data:image/png;base64,${b64}`;
        img.alt = labels[i] || `View ${i + 1}`;
        img.title = labels[i] || `View ${i + 1}`;
        grid.appendChild(img);
    });
    document.getElementById('waxPreview').style.display = 'block';
}

// ─── 3D Viewer (Three.js) ──────────────────────
function showViewer(data) {
    document.getElementById('viewerSection').style.display = 'block';

    // Show stats
    if (data.stats) {
        const s = data.stats;
        document.getElementById('statVerts').textContent = (s.output_vertices || s.input_vertices || 0).toLocaleString();
        document.getElementById('statFaces').textContent = (s.output_faces || s.input_faces || 0).toLocaleString();
        document.getElementById('statWater').textContent = s.is_watertight ? '✓ Yes' : '✗ No';
        document.getElementById('statManifold').textContent = s.is_manifold ? '✓ Yes' : '✗ No';
        if (s.bounding_box_mm) {
            const bb = s.bounding_box_mm;
            document.getElementById('statSize').textContent = `${bb.x} × ${bb.y} × ${bb.z}`;
        }
        document.getElementById('statEngine').textContent = data.engine || '—';
        document.getElementById('meshStats').style.display = 'grid';
    }

    // Init Three.js
    initViewer();

    // Load the STL or GLB
    if (data.stl_base64) {
        loadSTL(data.stl_base64);
    } else if (data.glb_base64) {
        loadGLB(data.glb_base64);
    }

    // Scroll to viewer
    document.getElementById('viewerSection').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function initViewer() {
    const canvas = document.getElementById('viewer3d');
    const wrap = canvas.parentElement;

    if (renderer) {
        renderer.dispose();
        if (controls) controls.dispose();
    }

    // Scene
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x111113);

    // Camera
    const aspect = wrap.clientWidth / wrap.clientHeight;
    camera = new THREE.PerspectiveCamera(45, aspect, 0.01, 1000);
    camera.position.set(0, 0.5, 2);

    // Renderer
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    renderer.setSize(wrap.clientWidth, wrap.clientHeight);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.2;

    // Controls — full 360° rotation
    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.enableZoom = true;
    controls.enablePan = true;
    controls.autoRotate = autoRotate;
    controls.autoRotateSpeed = 2.0;
    controls.minDistance = 0.5;
    controls.maxDistance = 10;

    // Lighting — studio setup for jewelry
    const ambientLight = new THREE.AmbientLight(0xffffff, 0.4);
    scene.add(ambientLight);

    const keyLight = new THREE.DirectionalLight(0xffffff, 1.0);
    keyLight.position.set(2, 3, 2);
    scene.add(keyLight);

    const fillLight = new THREE.DirectionalLight(0xfff8e7, 0.6);
    fillLight.position.set(-2, 1, -1);
    scene.add(fillLight);

    const rimLight = new THREE.DirectionalLight(0xd4af37, 0.3);
    rimLight.position.set(0, -1, -2);
    scene.add(rimLight);

    const topLight = new THREE.DirectionalLight(0xffffff, 0.5);
    topLight.position.set(0, 5, 0);
    scene.add(topLight);

    // Ground grid
    const grid = new THREE.GridHelper(4, 20, 0x2a2a2e, 0x1c1c1f);
    grid.position.y = -0.5;
    scene.add(grid);

    // Animate
    function animate() {
        requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
    }
    animate();

    // Resize
    const resizeObserver = new ResizeObserver(() => {
        const w = wrap.clientWidth;
        const h = wrap.clientHeight;
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        renderer.setSize(w, h);
    });
    resizeObserver.observe(wrap);
}

function loadSTL(base64) {
    const loader = new THREE.STLLoader();
    const buffer = Uint8Array.from(atob(base64), c => c.charCodeAt(0)).buffer;
    const geometry = loader.parse(buffer);

    // Center and normalize
    geometry.computeBoundingBox();
    const box = geometry.boundingBox;
    const center = new THREE.Vector3();
    box.getCenter(center);
    geometry.translate(-center.x, -center.y, -center.z);

    const size = new THREE.Vector3();
    box.getSize(size);
    const maxDim = Math.max(size.x, size.y, size.z);
    const scale = 1.5 / maxDim;
    geometry.scale(scale, scale, scale);

    geometry.computeVertexNormals();

    // Gold material
    const material = new THREE.MeshPhysicalMaterial({
        color: 0xD4AF37,
        metalness: 0.95,
        roughness: 0.2,
        clearcoat: 0.3,
        clearcoatRoughness: 0.2,
        reflectivity: 0.9,
        envMapIntensity: 1.0,
    });

    // Remove old mesh
    if (currentMesh) scene.remove(currentMesh);

    currentMesh = new THREE.Mesh(geometry, material);
    scene.add(currentMesh);

    // Position camera
    camera.position.set(0, 0.5, 2.5);
    controls.target.set(0, 0, 0);
    controls.update();
}

function loadGLB(base64) {
    const loader = new THREE.GLTFLoader();
    const buffer = Uint8Array.from(atob(base64), c => c.charCodeAt(0)).buffer;
    loader.parse(buffer, '', (gltf) => {
        if (currentMesh) scene.remove(currentMesh);

        const model = gltf.scene;

        // Center and normalize
        const box = new THREE.Box3().setFromObject(model);
        const center = new THREE.Vector3();
        box.getCenter(center);
        model.position.sub(center);

        const size = new THREE.Vector3();
        box.getSize(size);
        const maxDim = Math.max(size.x, size.y, size.z);
        const scale = 1.5 / maxDim;
        model.scale.multiplyScalar(scale);

        currentMesh = model;
        scene.add(model);

        camera.position.set(0, 0.5, 2.5);
        controls.target.set(0, 0, 0);
        controls.update();
    });
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
            if (child.isMesh && child.material) {
                child.material.wireframe = wireframeMode;
            }
        });
    }
}

function resetCamera() {
    if (camera && controls) {
        camera.position.set(0, 0.5, 2.5);
        controls.target.set(0, 0, 0);
        controls.update();
    }
}

// ─── Download STL ───────────────────────────────
function downloadSTL() {
    if (!currentSTLB64) {
        alert('No STL available yet. Generate a 3D model first.');
        return;
    }
    const bytes = atob(currentSTLB64);
    const buffer = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) buffer[i] = bytes.charCodeAt(i);
    const blob = new Blob([buffer], { type: 'application/octet-stream' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `jewelforge_${Date.now()}.stl`;
    a.click();
    URL.revokeObjectURL(url);
}

// ─── Utility ────────────────────────────────────
function dataURLtoBlob(base64) {
    const bytes = atob(base64);
    const buffer = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) buffer[i] = bytes.charCodeAt(i);
    return new Blob([buffer], { type: 'image/png' });
}
