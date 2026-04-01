/* ═══════════════════════════════════════════════
   JewelForge — Frontend Application
   Uses Google <model-viewer> for 3D display
   ═══════════════════════════════════════════════ */

// ─── State ──────────────────────────────────────
let currentImageB64 = null;
let currentSTLB64 = null;
let currentGLBB64 = null;
let currentGLBUrl = null; // URL for model-viewer (file URL or blob URL)
let currentSTLUrl = null; // URL for STL download
let currentAnalysis = null;

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
    ['analyze', 'grounding', '3d', 'refine', 'pave'].forEach(s => setStep(s, '', ''));

    try {
        let imageB64;

        // Step 1: Analyze / Generate image
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

        // Step 2: Grounding Pipeline — Sketch → Gold → Wax (visual chain + audit)
        // This is MANDATORY — never skip, never fall back to original image
        setStep('grounding', 'active', 'Step 1/3: Generating pencil sketch...');
        const groundRes = await fetch('/api/grounding-pipeline', {
            method: 'POST',
            body: new URLSearchParams({ image_base64: imageB64 }),
            signal: AbortSignal.timeout(300000), // 5 min timeout — pipeline generates 5+ images
        });
        if (!groundRes.ok) {
            const errText = await groundRes.text().catch(() => 'Unknown error');
            throw new Error(`Grounding pipeline failed: ${errText}`);
        }
        const gData = await groundRes.json();

        // Show wax views
        if (gData.wax_views_base64 && gData.wax_views_base64.length > 0) {
            showWaxViews(gData.wax_views_base64);
        }

        // Use audited best image for 3D — NEVER the original photo
        const meshInputB64 = gData.best_for_3d_base64;
        const auditMsg = gData.audit_passed
            ? `✓ Audit passed — wax ${gData.best_wax_idx + 1} is stone-free`
            : '⚠ Using gold render/sketch (no clean wax)';
        setStep('grounding', 'done', auditMsg);

        // Step 3: 3D mesh — submit audited image to Hitem3D
        setStep('3d', 'active', 'Submitting audited image to 3D engine...');
        const submitRes = await fetch('/api/generate-3d/submit', { method: 'POST', body: new URLSearchParams({ image_base64: meshInputB64, engine: 'hitem3d' }) });
        if (!submitRes.ok) throw new Error('3D submission failed');
        const submitData = await submitRes.json();
        const taskId = submitData.task_id;

        // Poll until done (up to 15 min)
        let meshData = null;
        for (let i = 0; i < 180; i++) {
            await new Promise(r => setTimeout(r, 5000)); // 5s interval
            const elapsed = ((i + 1) * 5);
            const mins = Math.floor(elapsed / 60);
            const secs = elapsed % 60;
            setStep('3d', 'active', `Building 3D mesh... ${mins}m ${secs}s`);

            try {
                const pollRes = await fetch(`/api/generate-3d/poll/${taskId}`);
                if (!pollRes.ok) continue;
                const pollData = await pollRes.json();

                if (pollData.state === 'success') {
                    meshData = pollData;
                    break;
                } else if (pollData.state === 'failed') {
                    throw new Error('3D generation failed on Hitem3D');
                }
                // queueing/processing — keep polling
            } catch (e) {
                if (e.message.includes('failed on Hitem3D')) throw e;
                console.warn('Poll error, retrying:', e);
            }
        }
        if (!meshData) throw new Error('3D generation timed out (15 min)');
        setStep('3d', 'done', `Engine: ${meshData.engine}`);

        // Step 4: Refine (scale + cleanup) — returns file URLs not base64
        setStep('refine', 'active', 'Scaling to real mm & cleaning mesh...');
        const refineRes = await fetch('/api/refine', { method: 'POST', body: new URLSearchParams({ glb_url: meshData.url }) });
        if (!refineRes.ok) throw new Error('Mesh refinement failed');
        const refineData = await refineRes.json();
        setStep('refine', 'done', refineData.refined ? 'Scaled & cleaned' : 'Raw AI mesh');

        // Step 5: Done
        setStep('pave', 'done', 'Complete');

        // Store URLs for download
        currentSTLUrl = refineData.stl_url || null;
        currentGLBUrl = refineData.glb_url || null;

        // Show viewer
        showViewer(refineData);

        // Show download links below viewer as fallback
        if (refineData.glb_download_url || refineData.stl_download_url) {
            const linksDiv = document.getElementById('downloadLinks');
            if (linksDiv) {
                let html = '<strong>Direct download links:</strong><br>';
                if (refineData.glb_download_url) html += `<a href="${refineData.glb_download_url}" download>GLB File</a> `;
                if (refineData.stl_download_url) html += `<a href="${refineData.stl_download_url}" download>STL File</a>`;
                linksDiv.innerHTML = html;
                linksDiv.style.display = 'block';
            }
        }

    } catch (error) {
        console.error('Pipeline error:', error);
        const steps = ['analyze', 'wax', '3d', 'refine', 'pave'];
        const activeStep = steps.find(s => document.getElementById(`step-${s}`) && document.getElementById(`step-${s}`).classList.contains('active'));
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

// ─── 3D Viewer (model-viewer) ──────────────────

function base64ToBlobUrl(base64, mimeType) {
    const binaryStr = atob(base64);
    const bytes = new Uint8Array(binaryStr.length);
    for (let i = 0; i < binaryStr.length; i++) bytes[i] = binaryStr.charCodeAt(i);
    const blob = new Blob([bytes], { type: mimeType });
    return URL.createObjectURL(blob);
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

    // Get GLB — prefer URL (lightweight), fall back to base64
    let viewerSrc = null;
    if (data.glb_url) {
        viewerSrc = data.glb_url;
        currentGLBUrl = data.glb_url;
    } else if (data.glb_base64) {
        if (currentGLBUrl && currentGLBUrl.startsWith('blob:')) {
            URL.revokeObjectURL(currentGLBUrl);
        }
        viewerSrc = base64ToBlobUrl(data.glb_base64, 'model/gltf-binary');
        currentGLBUrl = viewerSrc;
    } else {
        console.error('No GLB data for viewer');
        return;
    }

    // Get or create model-viewer element
    const wrap = document.getElementById('viewerWrap');
    let mv = wrap.querySelector('model-viewer');
    if (!mv) {
        mv = document.createElement('model-viewer');
        mv.id = 'modelViewer';
        mv.setAttribute('camera-controls', '');
        mv.setAttribute('touch-action', 'pan-y');
        mv.setAttribute('auto-rotate', '');
        mv.setAttribute('auto-rotate-delay', '0');
        mv.setAttribute('rotation-per-second', '20deg');
        mv.setAttribute('interaction-prompt', 'auto');
        mv.setAttribute('shadow-intensity', '0.6');
        mv.setAttribute('shadow-softness', '0.8');
        mv.setAttribute('exposure', '1.1');
        mv.setAttribute('environment-image', 'neutral');
        mv.setAttribute('tone-mapping', 'commerce');
        mv.setAttribute('interpolation-decay', '100');
        mv.style.cssText = 'width:100%;height:100%;display:block;outline:none;--poster-color:transparent;';
        // Remove any existing content (old canvas, overlay)
        wrap.innerHTML = '';
        wrap.appendChild(mv);
    }

    mv.setAttribute('src', viewerSrc);

    // Update download button
    const dlBtn = document.getElementById('btnDownload');
    if (dlBtn) {
        dlBtn.querySelector('.dl-text').textContent = (currentSTLUrl || currentSTLB64) ? 'Download STL' : 'Download GLB';
    }

    document.getElementById('viewerSection').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ─── Viewer Controls ────────────────────────────
function toggleAutoRotate() {
    const mv = document.getElementById('modelViewer');
    if (!mv) return;
    if (mv.hasAttribute('auto-rotate')) {
        mv.removeAttribute('auto-rotate');
        document.getElementById('btnRotate').classList.remove('active');
    } else {
        mv.setAttribute('auto-rotate', '');
        document.getElementById('btnRotate').classList.add('active');
    }
}

function toggleWireframe() {
    // model-viewer doesn't support wireframe natively — skip
}

function resetCamera() {
    const mv = document.getElementById('modelViewer');
    if (!mv) return;
    mv.cameraOrbit = 'auto auto auto';
    mv.cameraTarget = 'auto auto auto';
    mv.fieldOfView = 'auto';
    mv.jumpCameraToGoal();
}

function downloadModel() {
    // Prefer URL-based download (no base64 decoding needed)
    const url = currentSTLUrl || currentGLBUrl;
    const ext = currentSTLUrl ? 'stl' : 'glb';
    if (!url) {
        // Fallback to base64 if available
        const data = currentSTLB64 || currentGLBB64;
        if (!data) { alert('No model available yet.'); return; }
        const bytes = atob(data);
        const buffer = new Uint8Array(bytes.length);
        for (let i = 0; i < bytes.length; i++) buffer[i] = bytes.charCodeAt(i);
        const blob = new Blob([buffer], { type: 'application/octet-stream' });
        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = blobUrl;
        a.download = `jewelforge_${Date.now()}.${ext}`;
        a.click();
        URL.revokeObjectURL(blobUrl);
        return;
    }
    const a = document.createElement('a');
    a.href = url;
    a.download = `jewelforge_${Date.now()}.${ext}`;
    a.click();
}

// ─── Expose to HTML onclick handlers ────────────
window.JF = {
    switchTab, fillPrompt, clearUpload, startPipeline,
    toggleAutoRotate, toggleWireframe, resetCamera, downloadModel,
};
